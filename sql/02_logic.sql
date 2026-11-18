SET search_path = vytal;

-- ---------------------------------------------------------------- SE-04 who is really calling
-- The role the client acts as: the SET ROLE setting, or the login role when none is set, not current_user: inside the
-- SECURITY DEFINER procedures current_user is the owner role, while this stays the caller, and the application role cannot
-- SET ROLE to a superuser (or to the scheduler).
CREATE FUNCTION caller_role() RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN current_setting('role') = 'none' THEN session_user::text ELSE current_setting('role') END
$$;
-- True when that role is a superuser (setup, sample data, maintenance).
CREATE FUNCTION caller_is_superuser() RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT rolsuper FROM pg_roles WHERE rolname = caller_role()
$$;

-- ---------------------------------------------------------------- subclass row must match role (EER disjoint)
CREATE FUNCTION check_subclass_role() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r text;
BEGIN
    SELECT role INTO r FROM user_account WHERE user_id = NEW.user_id;
    IF r IS DISTINCT FROM TG_ARGV[0] THEN
        RAISE EXCEPTION 'user % has role %, cannot be added to %', NEW.user_id, r, TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER donor_role BEFORE INSERT OR UPDATE ON donor FOR EACH ROW EXECUTE FUNCTION check_subclass_role('Donor');
CREATE TRIGGER staff_role BEFORE INSERT OR UPDATE ON staff FOR EACH ROW EXECUTE FUNCTION check_subclass_role('Staff');
CREATE TRIGGER admin_role BEFORE INSERT OR UPDATE ON admin FOR EACH ROW EXECUTE FUNCTION check_subclass_role('Admin');

-- ---------------------------------------------------------------- FR-07 / FR-10 next eligible date
-- NULL = no restriction yet, 'infinity' = permanently deferred.
-- The interval is taken from the type of the donor's most recent donation, counted in clinic days (DI-06, clinic_date).
CREATE FUNCTION recompute_next_eligible(p_donor int) RETURNS void LANGUAGE plpgsql AS $$
DECLARE d_next date; def_next date; perm boolean;
BEGIN
    SELECT clinic_date(dn.donated_at) + di.interval_days INTO d_next
    FROM donation dn JOIN donor o ON o.user_id = dn.donor_id
    JOIN donation_interval di ON di.donation_type_code = dn.donation_type_code AND di.sex = o.sex
    WHERE dn.donor_id = p_donor ORDER BY dn.donated_at DESC LIMIT 1;

    SELECT bool_or(end_date IS NULL), max(end_date) INTO perm, def_next
    FROM deferral WHERE donor_id = p_donor AND (end_date IS NULL OR end_date > clinic_today());

    UPDATE donor SET next_eligible_date =
        CASE WHEN perm THEN 'infinity'::date
             ELSE NULLIF(greatest(coalesce(d_next, '-infinity'), coalesce(def_next, '-infinity')), '-infinity') END
    WHERE user_id = p_donor;
END $$;
CREATE FUNCTION trg_recompute_eligible() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM recompute_next_eligible(NEW.donor_id); RETURN NULL; END $$;
CREATE TRIGGER donation_eligible AFTER INSERT ON donation FOR EACH ROW EXECUTE FUNCTION trg_recompute_eligible();
CREATE TRIGGER deferral_eligible AFTER INSERT OR UPDATE ON deferral FOR EACH ROW EXECUTE FUNCTION trg_recompute_eligible();
-- FR-32: a changed interval applies to every donor whose last donation was of that type and sex, at once (not at their next
-- donation); a corrected sex re-reads the interval too
CREATE FUNCTION trg_interval_recompute() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM recompute_next_eligible(o.user_id) FROM donor o
    WHERE o.sex = NEW.sex AND EXISTS (SELECT 1 FROM donation d WHERE d.donor_id = o.user_id
                                      AND d.donation_type_code = NEW.donation_type_code);
    RETURN NULL;
END $$;
CREATE TRIGGER interval_eligible AFTER INSERT OR UPDATE OF interval_days ON donation_interval
    FOR EACH ROW EXECUTE FUNCTION trg_interval_recompute();
CREATE FUNCTION trg_sex_recompute() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM recompute_next_eligible(NEW.user_id); RETURN NULL; END $$;
CREATE TRIGGER donor_sex_eligible AFTER UPDATE OF sex ON donor FOR EACH ROW WHEN (OLD.sex IS DISTINCT FROM NEW.sex)
    EXECUTE FUNCTION trg_sex_recompute();

-- ---------------------------------------------------------------- estimated blood volume (Nadler formula)
-- Litres = k1 * height_m^3 + k2 * weight_kg + k3, returned in whole millilitres. Height and weight together say how much
-- blood a body holds, which a weight limit alone does not (a short, heavy donor and a tall, light one differ). Schema-qualified
-- like donor_eligibility so it works under a pinned search_path and the planner can inline it.
CREATE FUNCTION estimated_blood_volume_ml(p_sex char, p_height_cm numeric, p_weight_kg numeric) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$
    SELECT round(1000 * CASE p_sex
        WHEN 'M' THEN 0.3669 * (p_height_cm / 100) * (p_height_cm / 100) * (p_height_cm / 100) + 0.03219 * p_weight_kg + 0.6041
        WHEN 'F' THEN 0.3561 * (p_height_cm / 100) * (p_height_cm / 100) * (p_height_cm / 100) + 0.03308 * p_weight_kg + 0.1833
        END)
$$;

-- ---------------------------------------------------------------- FR-11 / DI-03 no donation for an ineligible donor
-- Age, weight, estimated blood volume and hemoglobin thresholds are the Canadian Blood Services values named in FR-07 / UC-07.
CREATE FUNCTION check_donation_eligible() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o donor%ROWTYPE; active boolean; er eligibility_rule%ROWTYPE; ebv numeric; on_day date := clinic_date(NEW.donated_at);
BEGIN
    SELECT * INTO o FROM donor WHERE user_id = NEW.donor_id;
    SELECT is_active INTO active FROM user_account WHERE user_id = NEW.donor_id;
    IF NOT active THEN RAISE EXCEPTION 'donor account is deactivated'; END IF;
    IF o.is_flagged THEN RAISE EXCEPTION 'donor is flagged after a failed test or for a lookback'; END IF;
    IF NEW.donated_at > now() + interval '5 minutes' THEN       -- DI-03: a donation is recorded when it happens, never ahead
        RAISE EXCEPTION 'a donation cannot be dated in the future'; END IF;
    SELECT * INTO er FROM eligibility_rule WHERE sex = o.sex;
    IF age(on_day, o.date_of_birth) < make_interval(years => er.min_age_years) THEN
        RAISE EXCEPTION 'donor is under %', er.min_age_years; END IF;
    IF o.weight_kg < er.min_weight_kg THEN RAISE EXCEPTION 'donor weighs under % kg', trim_scale(er.min_weight_kg); END IF;
    ebv := estimated_blood_volume_ml(o.sex, o.height_cm, o.weight_kg);
    IF ebv < er.min_blood_volume_ml THEN
        RAISE EXCEPTION 'donor''s estimated blood volume is under % ml: check height and weight', er.min_blood_volume_ml; END IF;
    -- the same cap record_donation checks, so direct SQL cannot draw more (DI-03)
    IF NEW.volume_ml > 0.15 * ebv THEN
        RAISE EXCEPTION 'volume % ml is more than 15%% of the donor''s estimated blood volume (% ml)', NEW.volume_ml, ebv; END IF;
    IF NEW.hemoglobin_gl < er.min_hemoglobin_gl THEN
        RAISE EXCEPTION 'hemoglobin % g/L is below the minimum for this donor', NEW.hemoglobin_gl; END IF;
    IF o.next_eligible_date IS NOT NULL AND o.next_eligible_date > on_day THEN
        RAISE EXCEPTION 'donor not eligible until %', to_char(o.next_eligible_date, 'FMMon FMDD, YYYY'); END IF;
    -- the group measured for this donation is kept on the donation, so later corrections to DONOR do not rewrite history
    IF NEW.blood_group IS NULL THEN
        IF o.verified_by IS NULL THEN RAISE EXCEPTION 'verify the donor''s blood group before recording the donation'; END IF;
        NEW.blood_group := o.blood_group;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM screening s WHERE s.donor_id = NEW.donor_id
                   AND s.completed_at BETWEEN NEW.donated_at - interval '24 hours' AND NEW.donated_at) THEN
        RAISE EXCEPTION 'donor has no screening in the 24 hours before this donation'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER donation_check BEFORE INSERT ON donation FOR EACH ROW EXECUTE FUNCTION check_donation_eligible();

-- ---------------------------------------------------------------- FR-12 expiry from shelf life, DI-02 expiry after collection
CREATE FUNCTION set_unit_expiry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE collected timestamptz; shelf int; dfac int; dtype text;
BEGIN
    -- DI-03 / Table 1: every unit starts as (new) -> Collected, and only from a donation type that yields that component
    IF NEW.status IS DISTINCT FROM 'Collected' THEN
        RAISE EXCEPTION 'a new unit starts as Collected (Table 1), not %', NEW.status; END IF;
    SELECT dn.donated_at, st.facility_id, dn.donation_type_code INTO collected, dfac, dtype
    FROM donation dn JOIN staff st ON st.user_id = dn.recorded_by WHERE dn.donation_id = NEW.donation_id;
    IF dtype IS NOT NULL AND NOT donation_type_allows(dtype, NEW.component_code) THEN
        RAISE EXCEPTION 'a % donation cannot yield %', dtype, NEW.component_code; END IF;
    NEW.facility_id := coalesce(NEW.facility_id, dfac);   -- a new bag starts where it was collected
    SELECT shelf_life_days INTO shelf FROM component_type WHERE component_code = NEW.component_code;
    IF NEW.expires_at IS NULL THEN NEW.expires_at := collected + make_interval(days => shelf); END IF;
    IF NEW.expires_at <= collected THEN RAISE EXCEPTION 'expiry must be after collection'; END IF;
    RETURN NEW;
END $$;
-- NOT NULL on donation.blood_group and blood_unit.expires_at holds as designed: PostgreSQL checks it after BEFORE triggers run
CREATE TRIGGER unit_expiry BEFORE INSERT ON blood_unit FOR EACH ROW EXECUTE FUNCTION set_unit_expiry();

-- ---------------------------------------------------------------- FR-13 / FR-14 valid status changes
CREATE FUNCTION check_unit_status() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ok boolean; bad_or_missing int;
BEGIN
    IF NEW.status = OLD.status THEN RETURN NEW; END IF;
    -- exactly the lifecycle in Step 2 Table 1 (BR-12); every other change is refused
    ok := (OLD.status, NEW.status) IN (
        ('Collected','Available'), ('Collected','Discarded'), ('Collected','Expired'),
        ('Available','Reserved'), ('Available','Discarded'), ('Available','Expired'), ('Available','Quarantined'),
        ('Reserved','Available'), ('Reserved','Issued'), ('Reserved','Quarantined'),
        ('Quarantined','Discarded'), ('Quarantined','Available'));
    IF NOT ok THEN RAISE EXCEPTION 'invalid unit status change % -> %', OLD.status, NEW.status; END IF;
    NEW.status_changed_at := now();
    IF OLD.status = 'Collected' AND NEW.status = 'Available' THEN
        SELECT count(*) INTO bad_or_missing FROM test_type tt
        LEFT JOIN test_result tr ON tr.test_code = tt.test_code AND tr.donation_id = NEW.donation_id
        WHERE tt.is_active AND (tr.result IS NULL OR tr.result <> 'Negative');
        IF bad_or_missing > 0 THEN RAISE EXCEPTION 'unit % cannot be released: tests missing or positive', NEW.unit_id; END IF;
        IF (SELECT o.is_flagged FROM donation d JOIN donor o ON o.user_id = d.donor_id WHERE d.donation_id = NEW.donation_id) THEN
            RAISE EXCEPTION 'unit % cannot be released: donor is flagged', NEW.unit_id; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER unit_status BEFORE UPDATE OF status ON blood_unit FOR EACH ROW EXECUTE FUNCTION check_unit_status();

-- FR-13: a positive test discards every unit of that donation and flags the donor. A unit already Reserved (a test type
-- added or reactivated after release, BR-07) is released from its request first, along Table 1, and the request is topped up.
-- An existing result is final once positive; a negative one turned positive is handled like a new positive (DI-03).
CREATE FUNCTION on_positive_test() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE reqs int[]; r int;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.result = 'Positive' AND NEW.result IS DISTINCT FROM 'Positive' THEN
        RAISE EXCEPTION 'a positive test result cannot be changed'; END IF;
    IF NEW.result = 'Positive' AND (TG_OP = 'INSERT' OR OLD.result IS DISTINCT FROM 'Positive') THEN
        SELECT array_agg(DISTINCT ru.request_id ORDER BY ru.request_id) INTO reqs
        FROM request_unit ru JOIN blood_unit b USING (unit_id)
        WHERE b.donation_id = NEW.donation_id AND b.status = 'Reserved' AND ru.released_at IS NULL AND ru.issued_at IS NULL;
        IF reqs IS NOT NULL THEN
            PERFORM 1 FROM blood_request WHERE request_id = ANY (reqs) ORDER BY request_id FOR UPDATE;   -- same lock order as close_request
            UPDATE request_unit SET released_at = now() WHERE released_at IS NULL AND issued_at IS NULL
              AND unit_id IN (SELECT unit_id FROM blood_unit WHERE donation_id = NEW.donation_id AND status = 'Reserved');
            UPDATE blood_unit SET status = 'Available', status_reason = 'Released: positive ' || NEW.test_code
            WHERE donation_id = NEW.donation_id AND status = 'Reserved';
        END IF;
        UPDATE blood_unit SET status = 'Discarded', status_reason = 'Positive ' || NEW.test_code
        WHERE donation_id = NEW.donation_id AND status IN ('Collected','Available','Quarantined');
        UPDATE donor SET is_flagged = TRUE WHERE user_id = (SELECT donor_id FROM donation WHERE donation_id = NEW.donation_id);
        UPDATE donation SET status = 'Discarded' WHERE donation_id = NEW.donation_id;
        INSERT INTO notification (user_id, message)
        SELECT a.user_id, format('Donation %s from donor %s failed test %s. Units discarded and donor flagged; run a lookback.',
                                 NEW.donation_id, (SELECT o.donor_code FROM donation d JOIN donor o ON o.user_id = d.donor_id
                                                   WHERE d.donation_id = NEW.donation_id), NEW.test_code)
        FROM admin a JOIN user_account ua USING (user_id) WHERE ua.is_active;
        FOREACH r IN ARRAY coalesce(reqs, '{}') LOOP PERFORM top_up_request(r); END LOOP;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER test_positive AFTER INSERT OR UPDATE OF result ON test_result FOR EACH ROW EXECUTE FUNCTION on_positive_test();

-- ---------------------------------------------------------------- UC-07/UC-08 donation status follows its units
CREATE FUNCTION mark_separated() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE donation SET status = 'Separated' WHERE donation_id = NEW.donation_id AND status = 'Awaiting separation';
    RETURN NULL;
END $$;
CREATE TRIGGER unit_separated AFTER INSERT ON blood_unit FOR EACH ROW EXECUTE FUNCTION mark_separated();

-- FR-16 / FR-20 every expired or discarded unit gets one wastage record
CREATE FUNCTION record_wastage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('Expired','Discarded') AND OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO wastage_record (unit_id, cause) VALUES (NEW.unit_id, NEW.status);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER unit_wastage AFTER UPDATE OF status ON blood_unit FOR EACH ROW EXECUTE FUNCTION record_wastage();

-- a deferral must agree with its reason: permanent reason = no end date, temporary reason = end date
CREATE FUNCTION check_deferral_reason() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE perm boolean;
BEGIN
    SELECT is_permanent INTO perm FROM deferral_reason WHERE reason_code = NEW.reason_code;
    IF perm AND NEW.end_date IS NOT NULL THEN RAISE EXCEPTION 'permanent deferral reason cannot have an end date'; END IF;
    IF NOT perm AND NEW.end_date IS NULL THEN RAISE EXCEPTION 'temporary deferral reason needs an end date'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER deferral_reason_match BEFORE INSERT OR UPDATE ON deferral FOR EACH ROW EXECUTE FUNCTION check_deferral_reason();

-- ---------------------------------------------------------------- views (derived values removed during normalization)
CREATE VIEW v_blood_unit AS
SELECT u.*, dn.blood_group, dn.donated_at AS collected_at, dn.donor_id
FROM blood_unit u JOIN donation dn USING (donation_id);

CREATE VIEW v_wastage AS                 -- FR-20 / FR-34 / Scenario 4 monthly wastage
SELECT u.facility_id, u.component_code, w.cause, date_trunc('month', w.wasted_at, clinic_tz()) AS month, count(*) AS units_wasted
FROM wastage_record w JOIN blood_unit u USING (unit_id) GROUP BY 1, 2, 3, 4;

CREATE VIEW v_request AS                 -- a request's facility is its requester's facility (FR-04)
SELECT r.*, s.facility_id FROM blood_request r JOIN staff s ON s.user_id = r.requested_by;

CREATE VIEW v_request_status AS          -- FR-24 reserved count and shortfall
SELECT r.request_id, r.units_requested,
       count(ru.unit_id) FILTER (WHERE ru.released_at IS NULL) AS units_reserved,
       greatest(r.units_requested - count(ru.unit_id) FILTER (WHERE ru.released_at IS NULL), 0) AS shortfall
FROM blood_request r LEFT JOIN request_unit ru USING (request_id) GROUP BY r.request_id;

CREATE VIEW v_inventory AS               -- FR-17
SELECT facility_id, blood_group, component_code,
       count(*) FILTER (WHERE status = 'Available' AND expires_at > now()) AS available,      -- a unit past its date is not stock
       count(*) FILTER (WHERE status = 'Reserved') AS reserved,
       count(*) FILTER (WHERE status = 'Available' AND expires_at > now() AND expires_at < now() + interval '48 hours') AS expiring_48h
FROM v_blood_unit GROUP BY 1, 2, 3;

-- FR-19 / C10 the shelves below their minimum right now. A unit past its expiry time is not stock even before the hourly
-- job marks it Expired, so a shelf can be short here a little before its stock_alert row opens; screens read this view
-- (with the alert's opened time when there is one) so they never wait for the job (INT-11).
CREATE VIEW v_low_stock AS
SELECT t.facility_id, t.blood_group, t.component_code, coalesce(i.available, 0) AS available, t.min_units,
       coalesce(a.opened_at, (SELECT max(u.expires_at) FROM v_blood_unit u WHERE u.facility_id = t.facility_id
                               AND u.blood_group = t.blood_group AND u.component_code = t.component_code
                               AND u.status = 'Available' AND u.expires_at <= now()), now()) AS opened_at
FROM stock_threshold t
LEFT JOIN v_inventory i USING (facility_id, blood_group, component_code)
LEFT JOIN stock_alert a ON a.facility_id = t.facility_id AND a.blood_group = t.blood_group
     AND a.component_code = t.component_code AND a.cleared_at IS NULL
WHERE coalesce(i.available, 0) < t.min_units;

-- ---------------------------------------------------------------- FR-19 low-stock alerts (one open alert per threshold)
-- Two transactions changing the same shelf at once would each count the other's units with the old status (READ COMMITTED),
-- so both could miss the alert (or both leave it open). A transaction lock per facility and component makes the second
-- one wait for the first to commit; its count then sees both changes. Most transactions touch one facility and component,
-- and statements take the keys in sorted order (trg_unit_alert), so the lock does not add deadlocks between them.
-- The shelf lock (one per facility and component). Every procedure that moves units on several shelves, or that holds
-- request rows or unit rows while it waits, takes its shelf locks first, sorted by facility then component, right after
-- any request rows and before any unit row: request rows -> shelf locks -> unit rows is the one global lock order, so
-- the hourly expiry job, a quarantine and an issue wait for each other instead of deadlocking (NEW-03).
CREATE FUNCTION lock_shelf(p_fac int, p_comp text, p_wait boolean DEFAULT TRUE) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    IF p_wait THEN PERFORM pg_advisory_xact_lock(p_fac, hashtext('stock:' || p_comp)); RETURN TRUE; END IF;
    RETURN pg_try_advisory_xact_lock(p_fac, hashtext('stock:' || p_comp));
END $$;

CREATE FUNCTION refresh_alert(p_fac int, p_group text, p_comp text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE minu int; avail int;
BEGIN
    SELECT min_units INTO minu FROM stock_threshold
    WHERE facility_id = p_fac AND blood_group = p_group AND component_code = p_comp;
    IF minu IS NULL THEN RETURN; END IF;
    PERFORM lock_shelf(p_fac, p_comp);
    -- PL/pgSQL takes a new snapshot for this count, after the lock is held
    SELECT count(*) INTO avail FROM v_blood_unit
    WHERE facility_id = p_fac AND blood_group = p_group AND component_code = p_comp AND status = 'Available'
      AND expires_at > now();
    IF avail < minu THEN
        INSERT INTO stock_alert (facility_id, blood_group, component_code) VALUES (p_fac, p_group, p_comp)
        ON CONFLICT (facility_id, blood_group, component_code) WHERE cleared_at IS NULL DO NOTHING;
    ELSE
        UPDATE stock_alert SET cleared_at = now()
        WHERE facility_id = p_fac AND blood_group = p_group AND component_code = p_comp AND cleared_at IS NULL;
    END IF;
END $$;
-- statement level (PR-05): one stock check per facility/group/component a statement touched, not one per unit.
-- No join between the transition tables: they have no statistics, so the planner nested-loops them. refresh_alert is
-- idempotent, so checking every key in either table gives the same alerts as checking only changed rows.
CREATE FUNCTION trg_unit_alert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE k record;
BEGIN
    IF TG_OP = 'INSERT' THEN
        FOR k IN SELECT DISTINCT n.facility_id, d.blood_group, n.component_code
                 FROM (SELECT DISTINCT facility_id, donation_id, component_code FROM new_units) n
                 JOIN donation d USING (donation_id) ORDER BY 1, 3, 2 LOOP
            PERFORM refresh_alert(k.facility_id, k.blood_group, k.component_code);
        END LOOP;
    ELSE
        FOR k IN SELECT DISTINCT u.facility_id, d.blood_group, u.component_code
                 FROM (SELECT facility_id, donation_id, component_code FROM new_units
                       UNION SELECT facility_id, donation_id, component_code FROM old_units) u
                 JOIN donation d USING (donation_id) ORDER BY 1, 3, 2 LOOP
            PERFORM refresh_alert(k.facility_id, k.blood_group, k.component_code);
        END LOOP;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER unit_alert_insert AFTER INSERT ON blood_unit REFERENCING NEW TABLE AS new_units
    FOR EACH STATEMENT EXECUTE FUNCTION trg_unit_alert();
CREATE TRIGGER unit_alert_update AFTER UPDATE ON blood_unit REFERENCING OLD TABLE AS old_units NEW TABLE AS new_units
    FOR EACH STATEMENT EXECUTE FUNCTION trg_unit_alert();

-- FR-18 / FR-19: changing a minimum re-checks stock straight away, not at the next unit movement
CREATE FUNCTION trg_threshold_alert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM refresh_alert(NEW.facility_id, NEW.blood_group, NEW.component_code); RETURN NULL; END $$;
CREATE TRIGGER threshold_alert AFTER INSERT OR UPDATE OF min_units ON stock_threshold
    FOR EACH ROW EXECUTE FUNCTION trg_threshold_alert();

-- DI-03: a corrected group on a donation moves its units between stock counts, so both groups' alerts are checked again
CREATE FUNCTION trg_donation_group_alert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE k record;
BEGIN
    FOR k IN SELECT DISTINCT b.facility_id, g.grp, b.component_code FROM blood_unit b
             CROSS JOIN (VALUES (OLD.blood_group), (NEW.blood_group)) g(grp)
             WHERE b.donation_id = NEW.donation_id ORDER BY 1, 3, 2 LOOP
        PERFORM refresh_alert(k.facility_id, k.grp, k.component_code);
    END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER donation_group_alert AFTER UPDATE OF blood_group ON donation
    FOR EACH ROW WHEN (OLD.blood_group IS DISTINCT FROM NEW.blood_group) EXECUTE FUNCTION trg_donation_group_alert();

-- ---------------------------------------------------------------- FR-23 FEFO allocation with O-negative conservation, DI-01
-- allocate_units is the rule itself, for one request: it reserves what compatible stock covers of the shortfall and hands back
-- how many units it reserved. Internal (no caller checks): allocate() is the checked entry point for staff, and the procedures
-- that put units back on the shelf top up short requests through top_up_requests() (C15). With p_wait false a request that
-- another transaction is working on (its row is locked) is skipped instead of waited for, so a top-up never deadlocks.
CREATE FUNCTION allocate_units(p_request int, p_wait boolean DEFAULT TRUE) RETURNS int LANGUAGE plpgsql AS $$
DECLARE r blood_request%ROWTYPE; need int; fac int; picked text[];
BEGIN
    IF p_wait THEN SELECT * INTO r FROM blood_request WHERE request_id = p_request FOR UPDATE;
    ELSE SELECT * INTO r FROM blood_request WHERE request_id = p_request FOR UPDATE SKIP LOCKED;
    END IF;
    IF r.status IS DISTINCT FROM 'Open' THEN RETURN 0; END IF;
    SELECT facility_id INTO fac FROM staff WHERE user_id = r.requested_by;
    -- the shelf lock before any unit row (lock_shelf); a top-up skips a shelf another transaction is working on
    IF NOT lock_shelf(fac, r.component_code, p_wait) THEN RETURN 0; END IF;
    SELECT greatest(r.units_requested - count(*), 0) INTO need FROM request_unit WHERE request_id = p_request AND released_at IS NULL;
    IF need = 0 THEN RETURN 0; END IF;
    SELECT array_agg(x.unit_id) INTO picked FROM (
        SELECT b.unit_id FROM blood_unit b JOIN donation dn USING (donation_id)
        WHERE b.facility_id = fac AND b.component_code = r.component_code
          AND b.status = 'Available' AND b.expires_at > now()
          AND (   (r.recipient_group IS NOT NULL AND EXISTS (SELECT 1 FROM compatibility c
                     WHERE c.component_code = r.component_code AND c.recipient_group = r.recipient_group
                       AND c.donor_group = dn.blood_group))
               -- BR-14: an unknown group is treated as O- red cells / AB plasma only for an Emergency; any other unknown-group
               -- request reserves nothing until the lab confirms the group (confirm_request_group), so O- is kept (BR-09)
               OR (r.recipient_group IS NULL AND r.urgency = 'Emergency' AND (SELECT count(*) FROM compatibility c
                     WHERE c.component_code = r.component_code AND c.donor_group = dn.blood_group) = 8))
          -- O- red cells go to other groups in an Emergency, or only for what other compatible stock cannot cover
          AND (dn.blood_group <> 'O-' OR r.recipient_group = 'O-' OR r.recipient_group IS NULL
               OR r.urgency = 'Emergency' OR r.component_code <> 'RBC'
               OR (SELECT count(*) FROM v_blood_unit x JOIN compatibility c
                     ON c.component_code = r.component_code AND c.recipient_group = r.recipient_group AND c.donor_group = x.blood_group
                     WHERE x.facility_id = fac AND x.component_code = r.component_code
                       AND x.status = 'Available' AND x.expires_at > now() AND x.blood_group <> 'O-') < need)
        ORDER BY (dn.blood_group = r.recipient_group) DESC, (dn.blood_group = 'O-') ASC, b.expires_at ASC
        LIMIT need
        FOR UPDATE OF b SKIP LOCKED) x;
    IF picked IS NULL THEN RETURN 0; END IF;
    -- one statement each, so the stock trigger checks every touched shelf once, in key order
    -- a unit this request held before (released by a quarantine, a pairing change or a test) gets its row back:
    -- the key stays (request_id, unit_id), the reservation restarts now and the audit log keeps the earlier release
    INSERT INTO request_unit (request_id, unit_id) SELECT p_request, u FROM unnest(picked) u
    ON CONFLICT (request_id, unit_id) DO UPDATE SET reserved_at = now(), released_at = NULL, issued_at = NULL;
    UPDATE blood_unit SET status = 'Reserved' WHERE unit_id = ANY (picked);
    RETURN cardinality(picked);
END $$;

-- Staff allocate for their own facility's requests; an Admin's quarantine re-allocates the requests it took units from (UC-22 3a).
CREATE PROCEDURE allocate(p_request int, INOUT reserved_count int DEFAULT NULL) LANGUAGE plpgsql AS $$
DECLARE st text; fac int; actor int := require_actor('{Staff,Admin}');
BEGIN
    SELECT r.status, s.facility_id INTO st, fac FROM blood_request r JOIN staff s ON s.user_id = r.requested_by
    WHERE r.request_id = p_request;
    IF NOT FOUND THEN RAISE EXCEPTION 'request: Request #% was not found', p_request; END IF;
    IF st <> 'Open' THEN RAISE EXCEPTION 'Request #% is not open', p_request; END IF;
    PERFORM require_own_facility(actor, fac, format('request #%s', p_request));
    reserved_count := allocate_units(p_request);
END $$;

-- C15: open requests that are short get a share of units that come (back) onto a shelf: after tests release a donation, a
-- lookback is cleared, a pairing is (re)allowed, an expired reservation or an unissued unit is released. Most urgent and
-- soonest needed first, under the same FEFO, compatibility and O-negative rules. NULL facility or component = any.
-- Requests that another transaction holds are skipped (allocate_units with p_wait false). Hands back the units reserved.
CREATE FUNCTION top_up_requests(p_fac int, p_comp text, p_except int DEFAULT NULL) RETURNS int LANGUAGE plpgsql AS $$
DECLARE r int; n int := 0;
BEGIN
    FOR r IN SELECT br.request_id FROM blood_request br JOIN staff s ON s.user_id = br.requested_by
             WHERE br.status = 'Open' AND (p_fac IS NULL OR s.facility_id = p_fac)
               AND (p_comp IS NULL OR br.component_code = p_comp) AND br.request_id IS DISTINCT FROM p_except
               AND br.units_requested > (SELECT count(*) FROM request_unit ru WHERE ru.request_id = br.request_id
                                         AND ru.released_at IS NULL)
             ORDER BY CASE br.urgency WHEN 'Emergency' THEN 0 WHEN 'Urgent' THEN 1 ELSE 2 END, br.required_by, br.request_id
    LOOP
        n := n + allocate_units(r, FALSE);
    END LOOP;
    RETURN n;
END $$;
-- one request, when it is open and short (used after it lost a reservation)
CREATE FUNCTION top_up_request(p_request int) RETURNS int LANGUAGE sql AS $$
    SELECT vytal.allocate_units(p_request, FALSE)
$$;

-- FR-28 issue reserved units and close; unused units go back to Available
-- UC-21: p_units lists the reserved units actually issued; the rest go back to Available. NULL issues every reservation.
CREATE PROCEDURE close_request(p_request int, p_cancel boolean DEFAULT FALSE, p_units text[] DEFAULT NULL,
                               INOUT final_status text DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE issued int; req int; st text; stray text; active int; actor int := require_actor('{Staff}'); fac int; comp text;
        unsafe text;
BEGIN
    IF NOT p_cancel AND p_units IS NOT NULL AND cardinality(p_units) = 0 THEN        -- nothing ticked: never close with 0 issued
        RAISE EXCEPTION 'issued_units: Select at least one reserved unit to issue, or cancel the request'; END IF;
    IF p_units IS NOT NULL AND array_position(p_units, NULL) IS NOT NULL THEN        -- NULL would make every <> ALL test unknown
        RAISE EXCEPTION 'issued_units: The list of units to issue has an empty entry'; END IF;
    SELECT r.units_requested, r.status, s.facility_id, r.component_code INTO req, st, fac, comp
    FROM blood_request r JOIN staff s ON s.user_id = r.requested_by WHERE r.request_id = p_request FOR UPDATE OF r;
    IF NOT FOUND THEN RAISE EXCEPTION 'request: Request #% was not found', p_request; END IF;
    IF st <> 'Open' THEN RAISE EXCEPTION 'Request #% is not open', p_request; END IF;
    PERFORM require_own_facility(actor, fac, format('request #%s', p_request));
    PERFORM lock_shelf(fac, comp);              -- request row -> shelf lock -> unit rows (lock_shelf)
    -- lock the request's units after the request row (quarantine_donor and on_positive_test take the same order)
    PERFORM 1 FROM blood_unit WHERE unit_id IN (SELECT unit_id FROM request_unit WHERE request_id = p_request
                                                AND released_at IS NULL AND issued_at IS NULL) ORDER BY unit_id FOR UPDATE;
    IF NOT p_cancel THEN
        -- a reservation that expired before issue is released (Reserved -> Available) and the unit expires (Available -> Expired)
        UPDATE request_unit SET released_at = now() WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL
          AND unit_id IN (SELECT unit_id FROM blood_unit WHERE status = 'Reserved' AND expires_at <= now());
        UPDATE blood_unit SET status = 'Available' WHERE status = 'Reserved' AND expires_at <= now()
          AND unit_id IN (SELECT unit_id FROM request_unit WHERE request_id = p_request);
        UPDATE blood_unit SET status = 'Expired', status_reason = 'Wastage: expired while reserved'
        WHERE status = 'Available' AND expires_at <= now()
          AND unit_id IN (SELECT unit_id FROM request_unit WHERE request_id = p_request);
        IF p_units IS NOT NULL THEN
            SELECT u INTO stray FROM unnest(p_units) u WHERE u NOT IN
                (SELECT unit_id FROM request_unit WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL) LIMIT 1;
            IF stray IS NOT NULL THEN RAISE EXCEPTION 'issued_units: Unit % is not reserved for request #%', stray, p_request; END IF;
            UPDATE blood_unit SET status = 'Available' WHERE unit_id IN
                (SELECT unit_id FROM request_unit WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL)
              AND unit_id <> ALL (p_units);
            UPDATE request_unit SET released_at = now() WHERE request_id = p_request AND released_at IS NULL
              AND issued_at IS NULL AND unit_id <> ALL (p_units);
        END IF;
        SELECT count(*) INTO active FROM request_unit WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL;
        IF active = 0 THEN
            RAISE EXCEPTION 'request #% has no unexpired reserved units to issue; cancel the request instead', p_request; END IF;
        -- BR-07: never issue a unit whose donor is flagged or whose donation has a positive test
        SELECT b.unit_id INTO unsafe FROM request_unit ru JOIN blood_unit b USING (unit_id) JOIN donation d USING (donation_id)
        JOIN donor o ON o.user_id = d.donor_id
        WHERE ru.request_id = p_request AND ru.released_at IS NULL AND ru.issued_at IS NULL
          AND (o.is_flagged OR EXISTS (SELECT 1 FROM test_result t WHERE t.donation_id = d.donation_id AND t.result = 'Positive'))
        ORDER BY b.unit_id LIMIT 1;
        IF unsafe IS NOT NULL THEN
            RAISE EXCEPTION 'issued_units: Unit % cannot be issued: its donor is flagged or a test was positive', unsafe; END IF;
        UPDATE blood_unit SET status = 'Issued' WHERE unit_id IN
            (SELECT unit_id FROM request_unit WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL);
        UPDATE request_unit SET issued_at = now() WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL;
    ELSE
        UPDATE blood_unit SET status = 'Available' WHERE unit_id IN
            (SELECT unit_id FROM request_unit WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL);
        UPDATE request_unit SET released_at = now() WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL;
    END IF;
    SELECT count(*) FILTER (WHERE issued_at IS NOT NULL) INTO issued FROM request_unit WHERE request_id = p_request;
    st := CASE WHEN p_cancel THEN 'Cancelled' WHEN issued >= req THEN 'Fulfilled' ELSE 'Partially Fulfilled' END;
    UPDATE blood_request SET status = st, closed_at = now() WHERE request_id = p_request;
    final_status := st;
    PERFORM top_up_requests(fac, comp);         -- C15: units it did not issue go to other short requests at the facility
END $$;

-- FR-31 quarantine every Available / Reserved unit from a donor; UC-22 3a open requests that lose a unit are re-allocated
CREATE PROCEDURE quarantine_donor(p_donor int, INOUT quarantined int DEFAULT NULL) LANGUAGE plpgsql AS $$
DECLARE r int; got int; affected int[];
BEGIN
    PERFORM require_actor('{Admin}');                -- UC-22: the Admin runs the lookback
    IF NOT EXISTS (SELECT 1 FROM donor WHERE user_id = p_donor) THEN RAISE EXCEPTION 'donor: The donor was not found'; END IF;
    -- lock order request rows, then units, as close_request does, so a quarantine and an issue at the same moment wait for
    -- each other instead of deadlocking: the open requests holding this donor's units first
    PERFORM 1 FROM blood_request WHERE status = 'Open' AND request_id IN
        (SELECT ru.request_id FROM request_unit ru JOIN blood_unit b USING (unit_id) JOIN donation d USING (donation_id)
         WHERE d.donor_id = p_donor AND ru.released_at IS NULL AND ru.issued_at IS NULL)
    ORDER BY request_id FOR UPDATE;
    -- then the shelf locks of every shelf the donor's units are on, sorted (lock_shelf), so the hourly expiry job cannot
    -- hold one of them while it waits for a unit row this quarantine has locked
    PERFORM lock_shelf(k.facility_id, k.component_code) FROM (
        SELECT DISTINCT b.facility_id, b.component_code FROM blood_unit b JOIN donation d USING (donation_id)
        WHERE d.donor_id = p_donor AND b.status IN ('Available','Reserved') ORDER BY 1, 2) k;
    -- then the donor's shelf units: a concurrent allocate() either finishes (and its reservation is seen below)
    -- or skips them (SKIP LOCKED), so no reservation can survive on a Quarantined unit
    PERFORM 1 FROM blood_unit b JOIN donation d USING (donation_id)
    WHERE d.donor_id = p_donor AND b.status IN ('Available','Reserved') ORDER BY b.unit_id FOR UPDATE OF b;
    SELECT array_agg(DISTINCT ru.request_id) INTO affected
    FROM request_unit ru JOIN v_blood_unit u ON u.unit_id = ru.unit_id JOIN blood_request br ON br.request_id = ru.request_id
    WHERE u.donor_id = p_donor AND u.status = 'Reserved' AND ru.released_at IS NULL AND ru.issued_at IS NULL
      AND br.status = 'Open';
    UPDATE request_unit SET released_at = now() WHERE released_at IS NULL AND issued_at IS NULL
      AND unit_id IN (SELECT unit_id FROM v_blood_unit WHERE donor_id = p_donor AND status = 'Reserved');
    UPDATE blood_unit SET status = 'Quarantined', status_reason = 'Lookback'
    WHERE status IN ('Available','Reserved') AND donation_id IN (SELECT donation_id FROM donation WHERE donor_id = p_donor);
    GET DIAGNOSTICS quarantined = ROW_COUNT;
    UPDATE donor SET is_flagged = TRUE WHERE user_id = p_donor;
    FOREACH r IN ARRAY coalesce(affected, '{}') LOOP got := allocate_units(r); END LOOP;
END $$;

-- FR-07 / BR-05 / UC-05 the eligibility function: status, the date the donor can give again, and the reason.
-- next_eligible_date already folds in the BR-02 interval and active BR-04 deferrals (recompute_next_eligible); a failed
-- screening shows up as the deferral it created, so the donor is Eligible again when it ends (Scenario 2). UC-05 2a: an
-- otherwise eligible donor with no screening on record is asked to complete one, and BR-05 / FR-06: the screening must
-- be in the 24 hours before the donation, the rule check_donation_eligible() enforces, so an older one is stale. That
-- window ends now (or at the end of p_on when p_on is in the past), so a donor who says Eligible passes every
-- eligibility rule here. record_donation also needs a verified blood group, which this status does not look at
-- (can_be_called does). LANGUAGE sql so the planner inlines it.
-- SE-04: the donor's private facts (date of birth, whether a test was positive) come in as arguments, so the reporting
-- login reads eligibility through v_donor_eligibility (whose own columns are checked as the view's owner) without being
-- granted the date of birth or the test results. donor_eligibility() is the same rule for one donor.
CREATE FUNCTION eligibility_of(p_donor int, p_dob date, p_sex char, p_weight numeric, p_height numeric, p_flagged boolean,
                               p_next date, p_positive boolean, p_on date)
RETURNS TABLE (status text, eligible_from date, reason text) LANGUAGE sql STABLE AS $$
    -- tables are schema-qualified instead of SET search_path, which would stop the planner inlining this function.
    -- A flagged donor reads differently when no test was positive: a lookback hold, not a failed test (ADM-14).
    -- Blood volume is given in whole ml, so the figure and the minimum never round to the same number.
    SELECT CASE WHEN NOT a.is_active THEN 'Account inactive'
                WHEN o.next_eligible_date = 'infinity' THEN 'Permanently deferred'    -- UC-06 2a: shown before the flag
                WHEN o.is_flagged AND w.positive THEN 'Not eligible (flagged after a failed test)'
                WHEN o.is_flagged THEN 'Not eligible (flagged for a lookback investigation)'
                WHEN o.weight_kg < r.min_weight_kg THEN format('Not eligible (under %s kg)', trim_scale(r.min_weight_kg))
                WHEN w.ebv < r.min_blood_volume_ml
                    THEN format('Not eligible (estimated blood volume under %s ml: check height and weight)',
                                to_char(r.min_blood_volume_ml, 'FM999,999'))
                WHEN w.from_date > p_on THEN 'Not eligible until ' || w.from_date
                WHEN NOT EXISTS (SELECT 1 FROM vytal.screening s WHERE s.donor_id = o.user_id)
                    THEN 'Complete screening to confirm eligibility'
                WHEN NOT EXISTS (SELECT 1 FROM vytal.screening s WHERE s.donor_id = o.user_id
                                 AND s.completed_at BETWEEN w.ref - interval '24 hours' AND w.ref)
                    THEN 'Screening older than 24 hours, repeat before donating'
                ELSE 'Eligible' END,
           CASE WHEN a.is_active AND NOT o.is_flagged AND o.next_eligible_date IS DISTINCT FROM 'infinity'
                     AND o.weight_kg >= r.min_weight_kg
                     AND w.ebv >= r.min_blood_volume_ml AND w.from_date > p_on THEN w.from_date END,
           CASE WHEN NOT a.is_active THEN 'The account is deactivated'
                WHEN o.next_eligible_date = 'infinity' THEN
                    (SELECT dr.description FROM vytal.deferral d JOIN vytal.deferral_reason dr USING (reason_code)
                     WHERE d.donor_id = o.user_id AND d.end_date IS NULL ORDER BY d.deferral_id DESC LIMIT 1)
                WHEN o.is_flagged AND w.positive THEN 'A test on an earlier donation was positive'
                WHEN o.is_flagged THEN 'Units from this donor are on hold while a lookback investigation is under way'
                WHEN o.weight_kg < r.min_weight_kg THEN format('Donors must weigh at least %s kg', trim_scale(r.min_weight_kg))
                WHEN w.ebv < r.min_blood_volume_ml
                    THEN format('Height and weight give an estimated blood volume of %s ml, under the %s ml minimum',
                                to_char(w.ebv, 'FM999,999'), to_char(r.min_blood_volume_ml, 'FM999,999'))
                WHEN w.from_date > p_on THEN coalesce(
                    (SELECT dr.description FROM vytal.deferral d JOIN vytal.deferral_reason dr USING (reason_code)
                     WHERE d.donor_id = o.user_id AND d.end_date = w.from_date ORDER BY d.deferral_id DESC LIMIT 1),
                    CASE WHEN w.age_date = w.from_date THEN format('Donors must be at least %s', r.min_age_years)
                         ELSE 'Minimum interval since the last donation' END)
                WHEN NOT EXISTS (SELECT 1 FROM vytal.screening s WHERE s.donor_id = o.user_id)
                    THEN 'No pre-donation screening on record'
                WHEN NOT EXISTS (SELECT 1 FROM vytal.screening s WHERE s.donor_id = o.user_id
                                 AND s.completed_at BETWEEN w.ref - interval '24 hours' AND w.ref)
                    THEN 'The last screening was more than 24 hours ago'
           END
    FROM (SELECT p_donor AS user_id, p_dob AS date_of_birth, p_sex AS sex, p_weight AS weight_kg, p_height AS height_cm,
                 p_flagged AS is_flagged, p_next AS next_eligible_date) o
    JOIN vytal.user_account a USING (user_id) JOIN vytal.eligibility_rule r USING (sex)
    -- age_date: the first day age() counts the minimum age, as check_donation_eligible does (a 29 February birthday turns
    -- 17 on 1 March in a common year, not on 28 February)
    CROSS JOIN LATERAL (SELECT (o.date_of_birth + make_interval(years => r.min_age_years))::date
                               + CASE WHEN extract(day FROM o.date_of_birth + make_interval(years => r.min_age_years))
                                           <> extract(day FROM o.date_of_birth) THEN 1 ELSE 0 END AS age_date) ad
    CROSS JOIN LATERAL (SELECT ad.age_date, greatest(o.next_eligible_date, ad.age_date) AS from_date,
                               vytal.estimated_blood_volume_ml(o.sex, o.height_cm, o.weight_kg) AS ebv,
                               o.is_flagged AND p_positive AS positive,
                               -- the 24-hour screening window ends now, or at the end of p_on (clinic day) when p_on is past
                               CASE WHEN p_on < vytal.clinic_today() THEN ((p_on + 1)::timestamp AT TIME ZONE vytal.clinic_tz())
                                    ELSE now() END AS ref) w
$$;

-- a flagged donor's positive test, if any (the lateral join keeps eligibility_of() inlinable: its arguments stay columns)
CREATE FUNCTION donor_eligibility(p_donor int, p_on date DEFAULT vytal.clinic_today())
RETURNS TABLE (status text, eligible_from date, reason text) LANGUAGE sql STABLE AS $$
    SELECT e.status, e.eligible_from, e.reason
    FROM vytal.donor o
    LEFT JOIN LATERAL (SELECT TRUE AS positive FROM vytal.donation dn JOIN vytal.test_result t USING (donation_id)
                       WHERE o.is_flagged AND dn.donor_id = o.user_id AND t.result = 'Positive' LIMIT 1) p ON TRUE
    CROSS JOIN LATERAL vytal.eligibility_of(o.user_id, o.date_of_birth, o.sex, o.weight_kg, o.height_cm, o.is_flagged,
                                            o.next_eligible_date, coalesce(p.positive, FALSE), p_on) e
    WHERE o.user_id = p_donor
$$;

-- FR-07 / US-02 eligibility shown to donors and staff: one row per donor, read from donor_eligibility().
-- can_be_called: eligible apart from a screening older than 24 hours. A call is an invitation and the screening is done
-- at the visit right before the donation (UC-07, BR-05), so a stale one must not keep a donor off the call lists and
-- counts; a donor with no screening on record at all (UC-05 2a) is still left out. status = 'Eligible' stays strict.
CREATE VIEW v_donor_eligibility AS
SELECT o.user_id, o.donor_code, o.blood_group, o.next_eligible_date, o.verified_by IS NOT NULL AS group_verified,
       e.status, e.eligible_from, e.reason,
       e.status IN ('Eligible', 'Screening older than 24 hours, repeat before donating') AS can_be_called
FROM donor o
LEFT JOIN LATERAL (SELECT TRUE AS positive FROM donation dn JOIN test_result t USING (donation_id)
                   WHERE o.is_flagged AND dn.donor_id = o.user_id AND t.result = 'Positive' LIMIT 1) p ON TRUE
CROSS JOIN LATERAL eligibility_of(o.user_id, o.date_of_birth, o.sex, o.weight_kg, o.height_cm, o.is_flagged,
                                  o.next_eligible_date, coalesce(p.positive, FALSE), clinic_today()) e;

-- FR-25 eligible, compatible, group-verified donors for a request (code, group, eligibility only; no contact details)
CREATE FUNCTION eligible_donors(p_request int)
RETURNS TABLE (donor_code varchar, blood_group varchar, next_eligible_date date) LANGUAGE sql STABLE AS $$
    SELECT e.donor_code, e.blood_group, e.next_eligible_date
    FROM blood_request r JOIN compatibility c ON c.component_code = r.component_code
         AND (c.recipient_group = r.recipient_group OR (r.recipient_group IS NULL AND c.recipient_group = 'AB+' AND
              (SELECT count(*) FROM compatibility c2 WHERE c2.component_code = r.component_code AND c2.donor_group = c.donor_group) = 8))
    JOIN v_donor_eligibility e ON e.blood_group = c.donor_group
    WHERE r.request_id = p_request AND e.can_be_called AND e.group_verified
$$;

-- UC-18 manual search: eligible, verified donors whose group can give this component to a recipient group (no request needed)
CREATE FUNCTION donors_for_group(p_recipient text, p_component text DEFAULT 'RBC')
RETURNS TABLE (donor_code varchar, blood_group varchar, next_eligible_date date) LANGUAGE sql STABLE AS $$
    SELECT e.donor_code, e.blood_group, e.next_eligible_date
    FROM compatibility c JOIN v_donor_eligibility e ON e.blood_group = c.donor_group
    WHERE c.component_code = p_component AND c.recipient_group = p_recipient AND e.can_be_called AND e.group_verified
$$;

-- FR-30 lookback
CREATE VIEW v_lookback AS
SELECT dn.donor_id, dn.donation_id, dn.donated_at, u.unit_id, u.component_code, u.status,
       ru.request_id, vr.facility_id AS request_facility, ru.issued_at, u.facility_id AS held_at
FROM donation dn JOIN blood_unit u USING (donation_id)
LEFT JOIN request_unit ru ON ru.unit_id = u.unit_id AND ru.released_at IS NULL LEFT JOIN v_request vr ON vr.request_id = ru.request_id;

-- ---------------------------------------------------------------- FR-33 / SE-08 audit log (one row per changed field)
CREATE FUNCTION audit_row() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
DECLARE o jsonb := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
        n jsonb := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
        k text; uid int := nullif(current_setting('vytal.user_id', true), '')::int;
        rid text := (SELECT string_agg(coalesce(n, o) ->> a, '/') FROM unnest(TG_ARGV) a);
BEGIN
    IF TG_OP = 'UPDATE' THEN
        FOR k IN SELECT jsonb_object_keys(n) LOOP
            -- secrets and login counters are never copied into the log
            IF n -> k IS DISTINCT FROM o -> k AND k NOT IN ('password_hash', 'failed_logins', 'locked_until') THEN
                INSERT INTO audit_log (user_id, action, table_name, record_id, field_name, old_value, new_value)
                VALUES (uid, TG_OP, TG_TABLE_NAME, rid, k, o ->> k, n ->> k);
            END IF;
        END LOOP;
    ELSE                                  -- FR-33: the whole row as created (INSERT) or as it was (DELETE)
        INSERT INTO audit_log (user_id, action, table_name, record_id, old_value, new_value)
        VALUES (uid, TG_OP, TG_TABLE_NAME, rid, (o - 'password_hash' - 'failed_logins' - 'locked_until')::text,
                (n - 'password_hash' - 'failed_logins' - 'locked_until')::text);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER audit_donor AFTER INSERT OR UPDATE OR DELETE ON donor FOR EACH ROW EXECUTE FUNCTION audit_row('user_id');
CREATE TRIGGER audit_deferral AFTER INSERT OR UPDATE OR DELETE ON deferral FOR EACH ROW EXECUTE FUNCTION audit_row('deferral_id');
CREATE TRIGGER audit_unit AFTER INSERT OR UPDATE OR DELETE ON blood_unit FOR EACH ROW EXECUTE FUNCTION audit_row('unit_id');
CREATE TRIGGER audit_request AFTER INSERT OR UPDATE OR DELETE ON blood_request FOR EACH ROW EXECUTE FUNCTION audit_row('request_id');
CREATE TRIGGER audit_compat AFTER INSERT OR UPDATE OR DELETE ON compatibility FOR EACH ROW EXECUTE FUNCTION audit_row('component_code', 'recipient_group', 'donor_group');
-- SE-08: every clinical rule table an Admin can change (FR-32) is audited too
CREATE TRIGGER audit_component AFTER INSERT OR UPDATE OR DELETE ON component_type FOR EACH ROW EXECUTE FUNCTION audit_row('component_code');
CREATE TRIGGER audit_interval AFTER INSERT OR UPDATE OR DELETE ON donation_interval FOR EACH ROW EXECUTE FUNCTION audit_row('donation_type_code', 'sex');
CREATE TRIGGER audit_rule AFTER INSERT OR UPDATE OR DELETE ON eligibility_rule FOR EACH ROW EXECUTE FUNCTION audit_row('sex');
CREATE TRIGGER audit_reason AFTER INSERT OR UPDATE OR DELETE ON deferral_reason FOR EACH ROW EXECUTE FUNCTION audit_row('reason_code');
-- UC-03 / UC-23 / FR-33: accounts, facilities, thresholds and donations
CREATE TRIGGER audit_user AFTER INSERT OR UPDATE OR DELETE ON user_account FOR EACH ROW EXECUTE FUNCTION audit_row('user_id');
CREATE TRIGGER audit_facility AFTER INSERT OR UPDATE OR DELETE ON facility FOR EACH ROW EXECUTE FUNCTION audit_row('facility_id');
CREATE TRIGGER audit_threshold AFTER INSERT OR UPDATE OR DELETE ON stock_threshold FOR EACH ROW EXECUTE FUNCTION audit_row('facility_id', 'blood_group', 'component_code');
CREATE TRIGGER audit_donation AFTER INSERT OR UPDATE OR DELETE ON donation FOR EACH ROW EXECUTE FUNCTION audit_row('donation_id');
-- FR-33: staff, lab results, reservations and donation calls too, so every change to a clinical record or to who may act on it
-- is traceable. None of these tables holds a secret (the login hash lives on user_account and audit_row skips it).
CREATE TRIGGER audit_staff AFTER INSERT OR UPDATE OR DELETE ON staff FOR EACH ROW EXECUTE FUNCTION audit_row('user_id');
CREATE TRIGGER audit_test_result AFTER INSERT OR UPDATE OR DELETE ON test_result FOR EACH ROW EXECUTE FUNCTION audit_row('donation_id', 'test_code');
CREATE TRIGGER audit_request_unit AFTER INSERT OR UPDATE OR DELETE ON request_unit FOR EACH ROW EXECUTE FUNCTION audit_row('request_id', 'unit_id');
CREATE TRIGGER audit_call AFTER INSERT OR UPDATE OR DELETE ON donation_call FOR EACH ROW EXECUTE FUNCTION audit_row('call_id');
CREATE TRIGGER audit_call_recipient AFTER INSERT OR UPDATE OR DELETE ON call_recipient FOR EACH ROW EXECUTE FUNCTION audit_row('call_id', 'donor_id');

-- ---------------------------------------------------------------- UC-24 2a every recipient group keeps a compatible donor group
-- Checked per statement for the components the statement touched (a new component with no rules yet does not block
-- other edits), so edit a rule with UPDATE (or insert the new pair before deleting the old one).
CREATE FUNCTION check_compat_coverage() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE comp text; grp text;
BEGIN
    SELECT c.component_code, g.blood_group INTO comp, grp
    FROM (SELECT DISTINCT component_code FROM old_rows) c CROSS JOIN blood_group g
    WHERE NOT EXISTS (SELECT 1 FROM compatibility x WHERE x.component_code = c.component_code AND x.recipient_group = g.blood_group)
    ORDER BY 1, 2 LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'change would leave % recipients with no compatible donor group for %', grp, comp;
    END IF;
    RETURN NULL;
END $$;
-- a trigger with transition tables can serve one event only, hence two
CREATE TRIGGER compat_coverage_update AFTER UPDATE ON compatibility REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION check_compat_coverage();
CREATE TRIGGER compat_coverage_delete AFTER DELETE ON compatibility REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION check_compat_coverage();

-- ---------------------------------------------------------------- FR-03 / UC-03 2a only active staff change a verified blood group
-- Covers the verification fields too, so clearing verified_by first cannot open the group to edits.
CREATE FUNCTION guard_verified_group() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actor int := nullif(current_setting('vytal.user_id', true), '')::int;
        is_staff boolean := EXISTS (SELECT 1 FROM staff s JOIN user_account a USING (user_id) WHERE s.user_id = actor AND a.is_active);
BEGIN
    IF OLD.verified_by IS NOT NULL AND NOT is_staff
       AND (NEW.blood_group, NEW.verified_by, NEW.verified_at) IS DISTINCT FROM (OLD.blood_group, OLD.verified_by, OLD.verified_at) THEN
        RAISE EXCEPTION 'only staff can change a verified blood group';
    END IF;
    -- FR-09: verifying is a staff action, recorded under the staff member who did it
    IF NEW.verified_by IS DISTINCT FROM OLD.verified_by AND NEW.verified_by IS NOT NULL
       AND (NOT is_staff OR NEW.verified_by <> actor) THEN
        RAISE EXCEPTION 'only staff can verify a blood group, as themselves';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER donor_group_guard BEFORE UPDATE OF blood_group, verified_by, verified_at ON donor
    FOR EACH ROW EXECUTE FUNCTION guard_verified_group();

-- FR-09 at registration: a new donor starts unverified unless staff (or a superuser loading data) record the verification
CREATE FUNCTION guard_verified_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actor int := nullif(current_setting('vytal.user_id', true), '')::int;
BEGIN
    IF NEW.verified_by IS NOT NULL AND NOT caller_is_superuser()
       AND NOT EXISTS (SELECT 1 FROM staff s JOIN user_account a USING (user_id)
                       WHERE s.user_id = actor AND a.is_active AND s.user_id = NEW.verified_by) THEN
        RAISE EXCEPTION 'only staff can verify a blood group, as themselves';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER donor_verified_insert BEFORE INSERT ON donor FOR EACH ROW EXECUTE FUNCTION guard_verified_insert();
