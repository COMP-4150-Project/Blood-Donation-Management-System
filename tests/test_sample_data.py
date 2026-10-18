"""sql/07_sample_data.sql is internally consistent: every invariant a real clinic's data must satisfy holds."""
from conftest import PREFIX, build, connect, drop


def test_row_volumes(sdb):
    counts = dict(sdb.q("SELECT 'donors', count(*) FROM donor UNION ALL SELECT 'donations', count(*) FROM donation "
                        "UNION ALL SELECT 'units', count(*) FROM blood_unit UNION ALL SELECT 'requests', count(*) FROM blood_request"))
    assert counts == {"donors": 70, "donations": 134, "units": 257, "requests": 90}


def test_every_table_has_rows(sdb):
    tables = [t for (t,) in sdb.q("SELECT tablename FROM pg_tables WHERE schemaname = 'vytal' ORDER BY 1")]
    empty = [t for t in tables if sdb.one(f"SELECT count(*) FROM vytal.{t}") == 0]
    assert len(tables) == 30 and empty == []


def test_every_deferral_reason_and_component_appears(sdb):
    assert sdb.one("SELECT count(*) FROM deferral_reason r WHERE NOT EXISTS "
                   "(SELECT 1 FROM deferral d WHERE d.reason_code = r.reason_code)") == 0
    assert sdb.one("SELECT count(*) FROM component_type c WHERE NOT EXISTS "
                   "(SELECT 1 FROM blood_unit u WHERE u.component_code = c.component_code)") == 0


def test_history_load_is_not_audited(sdb):
    """FR-33: the audit log starts with today's live actions; the migrated history adds no INSERT rows.
    History is anything older than the first audit entry (the load time), not older than CURRENT_DATE, so a build that
    straddles midnight cannot turn a live action into history."""
    assert sdb.one("SELECT count(*) FROM audit_log WHERE action = 'INSERT' AND table_name = 'facility'") == 0
    assert sdb.one("SELECT count(*) FROM audit_log a JOIN donation d ON d.donation_id::text = a.record_id "
                   "WHERE a.action = 'INSERT' AND a.table_name = 'donation' AND d.donated_at < (SELECT min(changed_at) FROM audit_log) - interval '1 hour'") == 0
    assert sdb.one("SELECT count(*) FROM audit_log a JOIN user_account u ON u.user_id::text = a.record_id "
                   "WHERE a.action = 'INSERT' AND a.table_name IN ('user_account','donor') AND u.created_at < (SELECT min(changed_at) FROM audit_log) - interval '1 hour'") == 0
    assert sdb.one("SELECT count(*) FROM audit_log a JOIN v_blood_unit u ON u.unit_id = a.record_id "
                   "WHERE a.action = 'INSERT' AND a.table_name = 'blood_unit' AND u.collected_at < (SELECT min(changed_at) FROM audit_log) - interval '1 hour'") == 0
    assert sdb.one("SELECT count(*) FROM audit_log WHERE action = 'INSERT' AND table_name = 'blood_request'") == 7   # today's open requests
    assert sdb.one("SELECT count(*) FROM audit_log WHERE action = 'INSERT' AND table_name = 'deferral' "
                   "AND user_id = 102") == 1                     # the donor's own screening today (UC-04)


def test_every_wasted_unit_has_exactly_one_wastage_record(sdb):
    assert sdb.one("SELECT count(*) FROM blood_unit u LEFT JOIN wastage_record w USING (unit_id) "
                   "WHERE (u.status IN ('Expired','Discarded')) <> (w.unit_id IS NOT NULL)") == 0


def test_issued_units_trace_to_one_issued_request(sdb):
    assert sdb.one("SELECT count(*) FROM blood_unit u WHERE status='Issued' AND "
                   "(SELECT count(*) FROM request_unit r WHERE r.unit_id=u.unit_id AND r.issued_at IS NOT NULL) <> 1") == 0


def test_reserved_units_belong_to_open_requests(sdb):
    assert sdb.one("SELECT count(*) FROM blood_unit u WHERE status='Reserved' AND NOT EXISTS (SELECT 1 FROM request_unit ru "
                   "JOIN blood_request r USING (request_id) WHERE ru.unit_id=u.unit_id AND ru.released_at IS NULL "
                   "AND ru.issued_at IS NULL AND r.status='Open')") == 0


def test_request_status_agrees_with_issued_units(sdb):
    bad = sdb.one("""SELECT count(*) FROM blood_request r
                     CROSS JOIN LATERAL (SELECT count(*) FILTER (WHERE issued_at IS NOT NULL) AS n FROM request_unit WHERE request_id=r.request_id) i
                     WHERE (r.status='Fulfilled' AND i.n <> r.units_requested)
                        OR (r.status='Partially Fulfilled' AND i.n >= r.units_requested)
                        OR (r.status='Open' AND i.n > 0)""")
    assert bad == 0


def test_issued_units_were_compatible_and_unexpired(sdb):
    assert sdb.one("""SELECT count(*) FROM request_unit ru JOIN v_blood_unit b USING (unit_id) JOIN blood_request r USING (request_id)
                      WHERE ru.issued_at IS NOT NULL AND (b.expires_at <= ru.reserved_at OR NOT EXISTS (
                          SELECT 1 FROM compatibility c WHERE c.component_code=r.component_code AND c.donor_group=b.blood_group
                          AND (c.recipient_group=r.recipient_group OR (r.recipient_group IS NULL AND
                               (SELECT count(*) FROM compatibility c2 WHERE c2.component_code=r.component_code AND c2.donor_group=b.blood_group)=8))))""") == 0


def test_issued_units_came_from_the_requesting_facility(sdb):
    assert sdb.one("SELECT count(*) FROM request_unit ru JOIN blood_unit b USING (unit_id) JOIN v_request r USING (request_id) "
                   "WHERE b.facility_id <> r.facility_id") == 0


def test_stored_eligibility_dates_match_a_fresh_recompute(sdb):
    snapshot = "SELECT user_id, next_eligible_date::text FROM donor ORDER BY 1"      # text keeps 'infinity' comparable
    before = sdb.q(snapshot)
    sdb.q("SELECT recompute_next_eligible(user_id) FROM donor")
    assert sdb.q(snapshot) == before


def test_history_timestamps_are_in_the_past_and_ordered(sdb):
    assert sdb.one("SELECT count(*) FROM donation WHERE donated_at > now()") == 0
    assert sdb.one("SELECT count(*) FROM test_result t JOIN donation d USING (donation_id) WHERE t.tested_at < d.donated_at") == 0
    assert sdb.one("SELECT count(*) FROM wastage_record w JOIN v_blood_unit u USING (unit_id) WHERE w.wasted_at < u.collected_at") == 0
    assert sdb.one("SELECT count(*) FROM blood_request WHERE closed_at < created_at") == 0
    for table, col in (("test_result", "tested_at"), ("blood_request", "closed_at"), ("request_unit", "issued_at"),
                       ("blood_unit", "status_changed_at"), ("wastage_record", "wasted_at")):
        assert sdb.one(f"SELECT count(*) FROM {table} WHERE {col} > now()") == 0, table


def test_sample_data_is_reproducible(seeded_db):
    """MA-03: loading the sample data twice gives identical data (dates are relative to the load time)."""
    other = build(PREFIX + "_seeded_again", with_data=True)
    sig = ("SELECT md5(string_agg(concat_ws('|', o.donor_code, o.blood_group, CASE WHEN o.next_eligible_date = 'infinity' THEN -1 ELSE o.next_eligible_date - CURRENT_DATE END, d.donation_type_code, "
           "u.unit_id, u.status, ru.request_id), ',' ORDER BY o.donor_code, d.donation_id, u.unit_id, ru.request_id)) "
           "FROM donor o LEFT JOIN donation d ON d.donor_id = o.user_id LEFT JOIN blood_unit u USING (donation_id) "
           "LEFT JOIN request_unit ru USING (unit_id)")
    a, b = connect(seeded_db), connect(other)
    try:
        assert a.execute(sig).fetchone() == b.execute(sig).fetchone()
    finally:
        a.close()
        b.close()
        drop(other)


def test_todays_live_actions_are_audited_with_the_actor(sdb):
    """FR-33: today's donation and profile change carry the staff member or donor who made them."""
    assert sdb.q("SELECT user_id FROM audit_log WHERE table_name = 'donation' AND action = 'INSERT'") == [(4,)]
    assert sdb.q("SELECT DISTINCT user_id FROM audit_log WHERE table_name = 'user_account' AND action = 'UPDATE'") == [(131,)]
    assert sdb.one("SELECT count(*) FROM audit_log WHERE table_name = 'user_account' AND action = 'INSERT'") == 1   # new sign-up


def test_donation_calls_have_realistic_responses(sdb):
    """FR-26 / FR-27: several calls, each answered by some donors and still pending for others."""
    assert sdb.one("SELECT count(*) FROM donation_call") >= 6
    assert sdb.one("SELECT count(DISTINCT s.facility_id) FROM donation_call c JOIN staff s ON s.user_id = c.sent_by") >= 3
    assert sdb.one("SELECT count(*) FROM call_recipient") >= 10
    assert {r for (r,) in sdb.q("SELECT DISTINCT response FROM call_recipient")} == {"Accepted", "Declined", "Pending"}


def test_lookback_quarantined_several_units(sdb):
    """FR-31: two lookbacks (D-1042 after a positive test, D-1043 after reporting malaria) pulled their units."""
    assert sdb.one("SELECT count(*) FROM blood_unit WHERE status = 'Quarantined'") >= 3
    assert sdb.q("SELECT DISTINCT donor_id FROM v_blood_unit WHERE status = 'Quarantined' ORDER BY 1") == [(127,), (128,)]


def test_fourteen_o_negative_donors_can_be_called_for_tr_2291(sdb):
    """Scenario 1 step 5: 14 O-negative donors are eligible today for the unknown-group trauma patient."""
    found = sdb.q("SELECT blood_group FROM eligible_donors(23)")
    assert len(found) == 14 and {g for (g,) in found} == {"O-"}


def test_no_donor_is_under_the_minimum_age(sdb):
    """UC-01 2b / BR-01: register_donor refuses an under-17 sign-up, so no donor row is under the age limit."""
    assert sdb.one("SELECT count(*) FROM donor o JOIN eligibility_rule r USING (sex) "
                   "WHERE age(o.date_of_birth) < make_interval(years => r.min_age_years)") == 0


def test_scenario_1_emergency_trauma_request(sdb):
    """TR-2291: 3 O- units reserved earliest-expiry first, shortfall 1, O- stock 0 below 6 with an open alert,
    14 eligible O- donors all called, two accept. The lab's A+ confirmation (step 6) has not happened yet."""
    assert sdb.q("SELECT units_reserved, shortfall FROM v_request_status WHERE request_id = 23") == [(3, 1)]
    held = sdb.q("SELECT b.blood_group, b.expires_at FROM request_unit r JOIN v_blood_unit b USING (unit_id) "
                 "WHERE r.request_id = 23 AND r.released_at IS NULL ORDER BY b.expires_at")
    assert [g for g, _ in held] == ["O-", "O-", "O-"]
    assert sdb.one("SELECT count(*) FROM blood_unit u JOIN donation d USING (donation_id) WHERE u.facility_id = 1 "
                   "AND d.blood_group = 'O-' AND u.component_code = 'RBC' AND u.status = 'Available'") == 0
    assert sdb.one("SELECT min_units FROM stock_threshold WHERE facility_id = 1 AND blood_group = 'O-' AND component_code = 'RBC'") == 6
    assert sdb.one("SELECT count(*) FROM stock_alert WHERE facility_id = 1 AND blood_group = 'O-' AND component_code = 'RBC' "
                   "AND cleared_at IS NULL") == 1
    called = sdb.q("SELECT r.response FROM call_recipient r JOIN donation_call c USING (call_id) WHERE c.request_id = 23")
    assert len(called) == 14 and sum(r == "Accepted" for (r,) in called) == 2


def test_scenario_2_first_time_donor_is_temporarily_deferred(sdb):
    """Priya (19, self-reported B+) said she had a tattoo six weeks ago: deferred until three months after it, not yet donated."""
    row = sdb.q("SELECT date_part('year', age(o.date_of_birth))::int, o.blood_group, o.verified_by, e.status, e.next_eligible_date "
                "FROM donor o JOIN v_donor_eligibility e USING (user_id) JOIN user_account a USING (user_id) WHERE a.first_name = 'Priya'")
    assert len(row) == 1
    age, group, verified_by, status, until = row[0]
    assert (age, group, verified_by) == (19, "B+", None)
    assert status == f"Not eligible until {until}"
    assert sdb.one("SELECT d.end_date - s.completed_at::date FROM deferral d JOIN screening s USING (screening_id) "
                   "WHERE d.donor_id = 157 AND d.reason_code = 'TATTOO'") == 90
    assert sdb.one("SELECT count(*) FROM donation WHERE donor_id = 157") == 0


def test_scenario_3_from_donation_to_shelf(sdb):
    """Marcus, a regular O+ donor at Lakeshore: yesterday's 142 g/L whole blood became three Available units with the right
    expiry, his next eligible date is 56 days on, and Lakeshore's O+ red cell alert cleared when they went on the shelf."""
    donor, = sdb.q("SELECT user_id FROM user_account WHERE first_name = 'Marcus'")
    assert sdb.one("SELECT count(*) FROM donation d JOIN staff s ON s.user_id = d.recorded_by WHERE d.donor_id = %s AND s.facility_id = 3",
                   donor) >= 4
    dn, hb, day = sdb.q("SELECT donation_id, hemoglobin_gl, donated_at::date FROM donation WHERE donor_id = %s "
                        "ORDER BY donated_at DESC LIMIT 1", donor)[0]
    assert hb == 142
    assert sdb.one("SELECT next_eligible_date - %s FROM donor WHERE user_id = %s", (day, donor[0])) == 56
    units = dict(sdb.q("SELECT u.component_code, extract(day FROM u.expires_at - d.donated_at)::int FROM blood_unit u "
                       "JOIN donation d USING (donation_id) WHERE u.donation_id = %s AND u.status = 'Available'", (dn,)))
    assert units == {"RBC": 42, "PLASMA": 365, "PLT": 5}
    alerts = sdb.q("SELECT cleared_at IS NOT NULL FROM stock_alert WHERE facility_id = 3 AND blood_group = 'O+' AND component_code = 'RBC'")
    assert alerts == [(True,)]


def test_scenario_4_platelet_wastage(sdb):
    """Four A+ platelet units at Riverside: the Routine request took the two that expired first, the other two expired as
    wastage, and staff then lowered the A+ platelet minimum (audited)."""
    assert sdb.q("SELECT urgency, units_requested, status FROM blood_request WHERE request_id = 32") == [("Routine", 2, "Fulfilled")]
    issued = sdb.q("SELECT max(b.expires_at) FROM request_unit r JOIN blood_unit b USING (unit_id) WHERE r.request_id = 32")[0][0]
    left = sdb.q("SELECT u.status, u.expires_at, w.cause FROM blood_unit u JOIN donation d USING (donation_id) "
                 "LEFT JOIN wastage_record w USING (unit_id) WHERE u.component_code = 'PLT' AND d.blood_group = 'A+' "
                 "AND u.facility_id = 1 AND d.donated_at > now() - interval '14 days' AND u.status <> 'Issued'")
    assert len(left) == 2 and all(st == "Expired" and exp > issued and cause == "Expired" for st, exp, cause in left)
    assert sdb.one("SELECT min_units FROM stock_threshold WHERE facility_id = 1 AND blood_group = 'A+' AND component_code = 'PLT'") == 1
    assert sdb.q("SELECT user_id, old_value, new_value FROM audit_log WHERE table_name = 'stock_threshold' AND action = 'UPDATE' "
                 "AND field_name = 'min_units'") == [(4, "2", "1")]


def test_scenario_5_lookback_after_a_positive_test(sdb):
    """D-1042: the positive donation's units are Discarded and he is permanently deferred; his 5 earlier donations made 11 units
    (2 Available + 1 Reserved now Quarantined, 6 Issued, 2 Expired) and the request that held one was re-allocated."""
    latest = sdb.one("SELECT max(donation_id) FILTER (WHERE status = 'Discarded') FROM donation WHERE donor_id = 127")
    assert {s for (s,) in sdb.q("SELECT status FROM blood_unit WHERE donation_id = %s", (latest,))} == {"Discarded"}
    assert sdb.one("SELECT count(*) FROM deferral WHERE donor_id = 127 AND reason_code = 'HBV' AND end_date IS NULL "
                   "AND recorded_by IS NOT NULL") == 1
    assert sdb.one("SELECT count(*) FROM donation WHERE donor_id = 127 AND donation_id <> %s", (latest,)) == 5
    counts = dict(sdb.q("SELECT u.status, count(*) FROM blood_unit u JOIN donation d USING (donation_id) "
                        "WHERE d.donor_id = 127 AND d.donation_id <> %s GROUP BY 1", (latest,)))
    assert counts == {"Quarantined": 3, "Issued": 6, "Expired": 2}
    moved = sdb.q("SELECT ru.released_at IS NULL, b.donor_id = 127 FROM request_unit ru JOIN v_blood_unit b USING (unit_id) "
                  "WHERE ru.request_id = 31")
    assert sorted(moved) == [(False, True), (True, False), (True, False)]       # one of his units released, two replacements held


def test_sample_data_breadth(sdb):
    """Every site has staff and plasma minimums, unknown groups are never callable, and notifications get read."""
    assert sdb.one("SELECT count(*) FROM facility") >= 4
    assert sdb.one("SELECT count(*) FROM staff") >= 8 and sdb.one("SELECT count(*) FROM admin") >= 3
    assert sdb.one("SELECT count(*) FROM facility f WHERE NOT EXISTS (SELECT 1 FROM staff s WHERE s.facility_id = f.facility_id)") == 0
    assert sdb.one("SELECT count(DISTINCT u.facility_id) FROM blood_unit u WHERE u.component_code = 'PLASMA' AND NOT EXISTS "
                   "(SELECT 1 FROM stock_threshold t WHERE t.facility_id = u.facility_id AND t.component_code = 'PLASMA')") == 0
    unknown = [u for (u,) in sdb.q("SELECT user_id FROM donor WHERE blood_group IS NULL")]
    assert len(unknown) >= 4
    assert sdb.one("SELECT count(*) FROM call_recipient WHERE donor_id = ANY(%s)", (unknown,)) == 0
    assert sdb.one("SELECT count(*) FROM notification WHERE read_at IS NOT NULL") >= 5
    assert sdb.one("SELECT count(*) FROM notification WHERE read_at IS NULL") >= 5


def test_todays_partial_issue_and_rule_edit(sdb):
    """UC-21: request 24 issued one of two reserved units; UC-24: an Admin's deferral-reason edit is audited."""
    assert sdb.q("SELECT status FROM blood_request WHERE request_id = 24") == [("Partially Fulfilled",)]
    assert sdb.q("SELECT count(*) FILTER (WHERE issued_at IS NOT NULL), count(*) FILTER (WHERE released_at IS NOT NULL) "
                 "FROM request_unit WHERE request_id = 24") == [(1, 1)]
    assert sdb.q("SELECT user_id FROM audit_log WHERE table_name = 'deferral_reason' AND action = 'UPDATE' "
                 "AND record_id = 'TATTOO'") == [(11,)]

