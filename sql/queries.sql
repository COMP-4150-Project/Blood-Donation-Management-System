-- Vytal key queries. Each block runs as-is in psql against a seeded database (no parameters to fill in).
-- Header lines (name, title, serves, why) are read by vytal/queries.py for the tests, benchmark and report.
SET search_path = vytal;

-- name: Q01
-- title: Open request queue
-- serves: FR-29, FR-24
-- why: Staff work the queue top-down: Emergency first, then the nearest required-by time, with the shortfall still to cover.
SELECT r.request_id, f.name AS facility, r.urgency, coalesce(r.recipient_group, 'Unknown') AS recipient,
       r.component_code, r.units_requested, s.units_reserved, s.shortfall,
       round(extract(epoch FROM r.required_by - now()) / 3600, 1) AS hours_left
FROM v_request r
JOIN v_request_status s USING (request_id)
JOIN facility f USING (facility_id)
WHERE r.status = 'Open'
ORDER BY array_position(ARRAY['Emergency','Urgent','Routine'], r.urgency), r.required_by;

-- name: Q02
-- title: Red cell stock against minimum levels
-- serves: FR-17, FR-18, FR-19
-- why: One screen shows what is on the shelf, what is about to expire, and which groups are below their minimum.
SELECT f.name AS facility, t.blood_group, t.min_units,
       coalesce(i.available, 0) AS available, coalesce(i.reserved, 0) AS reserved,
       coalesce(i.expiring_48h, 0) AS expiring_48h,
       CASE WHEN coalesce(i.available, 0) < t.min_units THEN 'LOW' ELSE 'OK' END AS stock_level
FROM stock_threshold t
JOIN facility f USING (facility_id)
LEFT JOIN v_inventory i USING (facility_id, blood_group, component_code)
WHERE t.component_code = 'RBC'
ORDER BY f.name, stock_level, t.blood_group;

-- name: Q03
-- title: Donors to call for the worst shortfall
-- serves: FR-25, FR-26, SE-07
-- why: For the open request with the biggest shortfall, list eligible compatible donors, longest-rested first, with no contact details.
WITH target AS (
    SELECT request_id FROM v_request_status JOIN blood_request USING (request_id)
    WHERE status = 'Open' AND shortfall > 0
    ORDER BY urgency = 'Emergency' DESC, shortfall DESC, request_id LIMIT 1
)
SELECT t.request_id, e.donor_code, e.blood_group, last.donated_at::date AS last_donation,
       CURRENT_DATE - last.donated_at::date AS days_since
FROM target t
CROSS JOIN LATERAL eligible_donors(t.request_id) e
JOIN donor o USING (donor_code)
LEFT JOIN LATERAL (SELECT max(donated_at) AS donated_at FROM donation WHERE donor_id = o.user_id) last ON TRUE
ORDER BY last.donated_at NULLS FIRST, e.donor_code
LIMIT 10;

-- name: Q04
-- title: A donor's own donation history
-- serves: FR-15, SE-07
-- why: What a donor sees: when and where they gave and how many units were made, never who received them.
WITH me AS (SELECT donor_id FROM donation GROUP BY donor_id ORDER BY count(*) DESC, donor_id LIMIT 1)
SELECT o.donor_code, d.donated_at::date AS donated_on, f.name AS facility, dt.name AS donation_type,
       d.status, count(u.unit_id) AS units_made, string_agg(u.component_code, ', ' ORDER BY u.component_code) AS components
FROM me
JOIN donation d USING (donor_id)
JOIN donor o ON o.user_id = d.donor_id
JOIN staff s ON s.user_id = d.recorded_by
JOIN facility f ON f.facility_id = s.facility_id
JOIN donation_type dt USING (donation_type_code)
LEFT JOIN blood_unit u USING (donation_id)
GROUP BY o.donor_code, d.donation_id, f.name, dt.name
ORDER BY d.donated_at;

-- name: Q05
-- title: Lookback for a flagged donor
-- serves: FR-30, FR-31
-- why: After a positive test, trace every unit the donor ever gave: where it is now and which facility's patient received it.
WITH flagged AS (
    SELECT user_id FROM donor WHERE is_flagged
    ORDER BY (SELECT count(*) FROM donation WHERE donor_id = user_id) DESC, user_id LIMIT 1
)
SELECT o.donor_code, l.donation_id, l.donated_at::date AS donated_on, l.unit_id, l.component_code, l.status,
       l.request_id, rf.name AS received_at, l.issued_at::date AS issued_on
FROM flagged g
JOIN v_lookback l ON l.donor_id = g.user_id
JOIN donor o ON o.user_id = l.donor_id
LEFT JOIN facility rf ON rf.facility_id = l.request_facility
ORDER BY l.donated_at, l.unit_id;

-- name: Q06
-- title: Admin overview
-- serves: FR-34, FR-21
-- why: The four numbers an Admin checks first, in one round trip.
SELECT
    (SELECT count(*) FROM v_donor_eligibility WHERE can_be_called AND group_verified) AS donors_eligible_now,
    (SELECT count(DISTINCT donor_id) FROM donation WHERE donated_at > now() - interval '12 months') AS donors_active_12m,
    (SELECT count(*) FROM blood_request WHERE status = 'Open' AND urgency = 'Emergency') AS open_emergency,
    (SELECT count(*) FROM blood_request WHERE status = 'Open' AND urgency = 'Urgent') AS open_urgent,
    (SELECT count(*) FROM blood_request WHERE status = 'Open' AND urgency = 'Routine') AS open_routine,
    (SELECT count(*) FROM stock_alert WHERE cleared_at IS NULL) AS open_alerts,
    (SELECT count(*) FROM wastage_record WHERE cause = 'Expired' AND wasted_at >= date_trunc('month', now())) AS expired_this_month;

-- name: Q07
-- title: Monthly wastage rate by component
-- serves: FR-20, FR-34
-- why: Wastage as a share of what was collected, with a 3-month rolling rate to smooth out small months.
WITH made AS (
    SELECT date_trunc('month', collected_at) AS month, component_code, count(*) AS units
    FROM v_blood_unit GROUP BY 1, 2
), lost AS (
    SELECT date_trunc('month', u.collected_at) AS month, u.component_code, count(*) AS units
    FROM wastage_record w JOIN v_blood_unit u USING (unit_id) GROUP BY 1, 2
)
SELECT m.month::date, m.component_code, m.units AS collected, coalesce(l.units, 0) AS wasted,
       round(100.0 * coalesce(l.units, 0) / m.units, 1) AS wastage_pct,
       round(100.0 * sum(coalesce(l.units, 0)) OVER w / sum(m.units) OVER w, 1) AS rolling_3m_pct
FROM made m LEFT JOIN lost l USING (month, component_code)
WHERE m.month < date_trunc('month', now()) - interval '1 month'      -- recent units have not had time to expire
WINDOW w AS (PARTITION BY m.component_code ORDER BY m.month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
ORDER BY m.component_code, m.month;

-- name: Q08
-- title: Request fulfilment by hospital and urgency (last 6 months)
-- serves: FR-23, FR-24, FR-28
-- why: Fill rate and time to close show whether stock policy is meeting clinical demand where it matters most.
SELECT f.name AS facility, r.urgency, count(*) AS requests,
       sum(r.units_requested) AS units_requested,
       sum(iss.issued) AS units_issued,
       round(100.0 * sum(iss.issued) / sum(r.units_requested), 1) AS fill_rate_pct,
       round(100.0 * count(*) FILTER (WHERE r.status = 'Fulfilled') / count(*), 1) AS fully_met_pct,
       round((percentile_cont(.5) WITHIN GROUP (ORDER BY extract(epoch FROM r.closed_at - r.created_at) / 3600))::numeric, 1) AS median_hours
FROM v_request r
JOIN facility f USING (facility_id)
CROSS JOIN LATERAL (SELECT count(*) FILTER (WHERE issued_at IS NOT NULL) AS issued
                    FROM request_unit ru WHERE ru.request_id = r.request_id) iss
WHERE r.status IN ('Fulfilled', 'Partially Fulfilled') AND r.created_at > now() - interval '6 months'
GROUP BY f.name, r.urgency
ORDER BY f.name, array_position(ARRAY['Emergency','Urgent','Routine'], r.urgency);

-- name: Q09
-- title: Red cell supply and demand by blood group (last 90 days)
-- serves: FR-17, FR-23
-- why: Compares what each group gives with what patients of that group need, and how many days the shelf would last.
WITH supply AS (
    SELECT blood_group, count(*) AS collected FROM v_blood_unit
    WHERE component_code = 'RBC' AND collected_at > now() - interval '90 days' GROUP BY 1
), demand AS (
    SELECT recipient_group AS blood_group, sum(units_requested) AS requested FROM blood_request
    WHERE component_code = 'RBC' AND recipient_group IS NOT NULL AND created_at > now() - interval '90 days' GROUP BY 1
), issued AS (
    SELECT b.blood_group, count(*) AS issued FROM request_unit ru JOIN v_blood_unit b USING (unit_id)
    WHERE b.component_code = 'RBC' AND ru.issued_at > now() - interval '90 days' GROUP BY 1
), shelf AS (
    SELECT blood_group, count(*) AS available FROM v_blood_unit
    WHERE component_code = 'RBC' AND status = 'Available' GROUP BY 1
)
SELECT g.blood_group, coalesce(s.collected, 0) AS collected_90d, coalesce(d.requested, 0) AS requested_90d,
       coalesce(i.issued, 0) AS issued_90d, coalesce(sh.available, 0) AS on_shelf,
       round(coalesce(sh.available, 0) / nullif(i.issued / 90.0, 0), 1) AS days_of_supply
FROM blood_group g
LEFT JOIN supply s USING (blood_group) LEFT JOIN demand d USING (blood_group)
LEFT JOIN issued i USING (blood_group) LEFT JOIN shelf sh USING (blood_group)
ORDER BY coalesce(d.requested, 0) DESC;

-- name: Q10
-- title: How soon donors come back
-- serves: FR-07, FR-10
-- why: Gaps between whole-blood donations against the minimum interval show how much donor capacity goes unused.
WITH gaps AS (
    SELECT o.sex, d.donated_at::date - lag(d.donated_at::date) OVER (PARTITION BY d.donor_id ORDER BY d.donated_at) AS gap_days
    FROM donation d JOIN donor o ON o.user_id = d.donor_id
    WHERE d.donation_type_code = 'WB'
)
SELECT g.sex, i.interval_days AS minimum_days, count(*) AS repeat_donations,
       min(g.gap_days) AS shortest_gap,
       percentile_disc(.5) WITHIN GROUP (ORDER BY g.gap_days) AS median_gap,
       round(100.0 * count(*) FILTER (WHERE g.gap_days <= i.interval_days + 30) / count(*), 1) AS back_within_30d_pct
FROM gaps g JOIN donation_interval i ON i.donation_type_code = 'WB' AND i.sex = g.sex
WHERE g.gap_days IS NOT NULL
GROUP BY g.sex, i.interval_days
ORDER BY g.sex;

-- name: Q11
-- title: Lapsed regular donors to win back
-- serves: FR-26
-- why: Eligible donors who gave at least twice but not in the last 6 months, top 3 per group, the most widely transfusable groups first.
WITH history AS (
    SELECT donor_id, count(*) AS donations, max(donated_at)::date AS last_donation
    FROM donation GROUP BY donor_id
), ranked AS (
    SELECT e.donor_code, e.blood_group, h.donations, h.last_donation,
           row_number() OVER (PARTITION BY e.blood_group ORDER BY h.donations DESC, h.last_donation) AS rank_in_group
    FROM history h JOIN v_donor_eligibility e ON e.user_id = h.donor_id
    WHERE e.can_be_called AND h.donations >= 2 AND h.last_donation < CURRENT_DATE - 180
)
SELECT r.blood_group, r.rank_in_group, r.donor_code, r.donations, r.last_donation
FROM ranked r
LEFT JOIN (SELECT donor_group, count(*) AS takers FROM compatibility WHERE component_code = 'RBC' GROUP BY 1) c
       ON c.donor_group = r.blood_group
WHERE r.rank_in_group <= 3
ORDER BY c.takers DESC, r.blood_group, r.rank_in_group;

-- name: Q12
-- title: Where O-negative red cells went
-- serves: FR-23
-- why: O-negative is the universal supply; this checks it mostly reached O-negative, unknown-group or Emergency patients.
SELECT CASE WHEN r.recipient_group = 'O-' THEN 'O- patient'
            WHEN r.recipient_group IS NULL THEN 'Unknown group'
            WHEN r.urgency = 'Emergency' THEN 'Other group, Emergency'
            ELSE 'Other group, non-Emergency' END AS went_to,
       count(*) AS units,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS share_pct
FROM request_unit ru
JOIN v_blood_unit b USING (unit_id)
JOIN blood_request r USING (request_id)
WHERE b.blood_group = 'O-' AND b.component_code = 'RBC' AND ru.issued_at IS NOT NULL
GROUP BY 1
ORDER BY units DESC;

-- name: Q13
-- title: Why donors are deferred (last 12 months)
-- serves: FR-06, FR-08
-- why: Shows which deferral reasons cost the most donations, and whether they came from screening or from staff.
SELECT r.reason_code, r.description, r.is_permanent,
       count(*) AS deferrals,
       count(*) FILTER (WHERE d.screening_id IS NOT NULL) AS from_screening,
       count(*) FILTER (WHERE d.recorded_by IS NOT NULL) AS by_staff,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS share_pct
FROM deferral d JOIN deferral_reason r USING (reason_code)
WHERE d.start_date > CURRENT_DATE - 365
GROUP BY r.reason_code, r.description, r.is_permanent
ORDER BY deferrals DESC;
