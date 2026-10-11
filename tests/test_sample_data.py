"""sql/07_sample_data.sql is internally consistent: every invariant a real clinic's data must satisfy holds."""


def test_row_volumes(sdb):
    counts = dict(sdb.q("SELECT 'donors', count(*) FROM donor UNION ALL SELECT 'donations', count(*) FROM donation "
                        "UNION ALL SELECT 'units', count(*) FROM blood_unit UNION ALL SELECT 'requests', count(*) FROM blood_request"))
    assert counts == {"donors": 36, "donations": 77, "units": 153, "requests": 26}


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
