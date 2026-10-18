"""Business rules enforced by triggers, independent of any screen (DI-03)."""
import pytest

from test_constraints import INSERT_DONATION

def next_in(db, donor):
    return db.one("SELECT next_eligible_date - CURRENT_DATE FROM donor WHERE user_id=%s", (donor,))


# ---------------------------------------------------------------------- FR-07 / FR-11 eligibility before a donation
@pytest.mark.parametrize("kwargs, hb, needle", [
    (dict(kg=45), 140, "50 kg"),
    (dict(sex="M"), 128, "hemoglobin"),
    (dict(sex="F"), 124, "hemoglobin"),
])
def test_threshold_rules(db, kwargs, hb, needle):
    assert needle in db.fails(INSERT_DONATION.replace(",140)", ",%s)"), (db.donor(**kwargs), db.nurse, 450, hb))


def test_donation_refused_below_the_minimum_age(db):
    """BR-01: a 17-year-old registered under the old rule cannot donate once an Admin raises the minimum to 18 (MA-01)."""
    d = db.donor(age=17)
    db.q("UPDATE eligibility_rule SET min_age_years = 18")
    assert "under 18" in db.fails(INSERT_DONATION, (d, db.nurse, 450))


def test_thresholds_are_data_not_code(db):
    """MA-01: lowering the male hemoglobin minimum lets 128 g/L donate with no code change."""
    d = db.donor(sex="M")
    db.q("UPDATE eligibility_rule SET min_hemoglobin_gl = 125 WHERE sex='M'")
    assert db.donate(d, hb=128) > 0


def test_interval_by_sex_and_second_donation_blocked(db):
    m, f = db.donor(sex="M"), db.donor(sex="F")
    db.donate(m)
    db.donate(f, hb=130)
    assert next_in(db, m) == 56 and next_in(db, f) == 84
    assert "not eligible until" in db.fails(INSERT_DONATION, (m, db.nurse, 450))


def test_unverified_flagged_and_inactive_donors_blocked(db):
    unverified = db.donor(group=None, verified=False)
    assert "verify" in db.fails(INSERT_DONATION, (unverified, db.nurse, 450))
    flagged = db.donor()
    db.q("UPDATE donor SET is_flagged = TRUE WHERE user_id=%s", (flagged,))
    assert "flagged" in db.fails(INSERT_DONATION, (flagged, db.nurse, 450))
    inactive = db.donor()
    db.q("UPDATE user_account SET is_active = FALSE WHERE user_id=%s", (inactive,))
    assert "deactivated" in db.fails(INSERT_DONATION, (inactive, db.nurse, 450))


def test_donation_needs_a_screening_in_the_last_24_hours(db):
    d = db.donor()
    assert "no screening" in db.fails(INSERT_DONATION, (d, db.nurse, 450))
    db.q("INSERT INTO screening (donor_id, completed_at) VALUES (%s, now() - interval '25 hours')", (d,))
    assert "no screening" in db.fails(INSERT_DONATION, (d, db.nurse, 450))


def test_donation_keeps_the_group_measured_at_the_time(db):
    d = db.donor("O+")
    dn = db.donate(d)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))        # staff correct a verified group (FR-03)
    db.q("UPDATE donor SET blood_group='A+' WHERE user_id=%s", (d,))
    assert db.one("SELECT blood_group FROM donation WHERE donation_id=%s", (dn,)) == "O+"


# ---------------------------------------------------------------------- FR-08 / FR-10 deferrals drive the next eligible date
def test_deferral_rules_and_eligibility_dates(db):
    d = db.donor()
    assert "end date" in db.fails("INSERT INTO deferral (donor_id,reason_code,recorded_by) VALUES (%s,'TATTOO',%s)", (d, db.nurse))
    assert "permanent" in db.fails("INSERT INTO deferral (donor_id,reason_code,end_date,recorded_by) VALUES (%s,'HBV',CURRENT_DATE+5,%s)", (d, db.nurse))
    assert "check" in db.fails("INSERT INTO deferral (donor_id,reason_code,end_date) VALUES (%s,'TATTOO',CURRENT_DATE+1)", (d,))
    db.q("INSERT INTO deferral (donor_id,reason_code,end_date,recorded_by) VALUES (%s,'TATTOO',CURRENT_DATE+90,%s)", (d, db.nurse))
    assert next_in(db, d) == 90
    db.q("UPDATE deferral SET start_date=CURRENT_DATE-100, end_date=CURRENT_DATE-1 WHERE donor_id=%s", (d,))
    assert db.one("SELECT next_eligible_date FROM donor WHERE user_id=%s", (d,)) is None
    db.q("INSERT INTO deferral (donor_id,reason_code,recorded_by) VALUES (%s,'HBV',%s)", (d, db.nurse))
    assert db.one("SELECT next_eligible_date::text FROM donor WHERE user_id=%s", (d,)) == "infinity"


# ---------------------------------------------------------------------- FR-12 / FR-13 / FR-14 unit lifecycle
def test_units_get_expiry_location_and_donation_status(db):
    dn = db.donate(db.donor(), staff=db.phleb)
    assert db.one("SELECT status FROM donation WHERE donation_id=%s", (dn,)) == "Awaiting separation"
    db.q("CALL separate_donation(%s, ARRAY['RBC','PLASMA','PLT'])", (dn,))
    assert db.one("SELECT status FROM donation WHERE donation_id=%s", (dn,)) == "Separated"
    days = dict(db.q("SELECT component_code, round(extract(epoch FROM expires_at - collected_at) / 86400)::int "
                     "FROM v_blood_unit WHERE donation_id=%s", (dn,)))
    assert days == {"RBC": 42, "PLASMA": 365, "PLT": 5}
    assert {f for (f,) in db.q("SELECT facility_id FROM blood_unit WHERE donation_id=%s", (dn,))} == {db.fac_b}


def test_expiry_before_collection_rejected(db):
    dn = db.donate(db.donor())
    assert "after collection" in db.fails("INSERT INTO blood_unit (unit_id,donation_id,component_code,expires_at) "
                                          "VALUES ('E',%s,'RBC','2000-01-01')", (dn,))


def test_collected_unit_cannot_be_quarantined(db):
    """BR-12: lookback quarantine applies to Available and Reserved units only (Table 1)."""
    unit = db.one("CALL separate_donation(%s, ARRAY['RBC'])", (db.donate(db.donor()),))[0]
    assert "invalid unit status" in db.fails("UPDATE blood_unit SET status='Quarantined' WHERE unit_id=%s", (unit,))


def test_release_needs_every_test_negative(db):
    dn = db.donate(db.donor())
    unit = db.one("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))[0]
    assert "tests missing" in db.fails("UPDATE blood_unit SET status='Available' WHERE unit_id=%s", (unit,))
    db.q("INSERT INTO test_result (donation_id,test_code,result) VALUES (%s,'HIV','Negative')", (dn,))
    assert "tests missing" in db.fails("UPDATE blood_unit SET status='Available' WHERE unit_id=%s", (unit,))


@pytest.mark.parametrize("path, bad", [
    ([], "Collected"),
    (["Expired"], "Available"),
    (["Discarded"], "Available"),
    (["Reserved", "Issued"], "Available"),
    (["Reserved"], "Expired"),                   # not in Table 1 (BR-12): a reservation is released first
])
def test_invalid_status_changes(db, path, bad):
    unit = db.stock("O+")[0]
    for s in path:
        db.q("UPDATE blood_unit SET status=%s WHERE unit_id=%s", (s, unit))
    assert "invalid unit status" in db.fails("UPDATE blood_unit SET status=%s WHERE unit_id=%s", (bad, unit))


def test_positive_test_discards_flags_and_notifies(db):
    d = db.donor()
    dn = db.donate(d)
    db.q("CALL separate_donation(%s, ARRAY['RBC','PLASMA'])", (dn,))
    db.q("INSERT INTO test_result (donation_id,test_code,result) VALUES (%s,'HBV','Positive')", (dn,))
    assert db.q("SELECT DISTINCT status FROM blood_unit WHERE donation_id=%s", (dn,)) == [("Discarded",)]
    assert db.one("SELECT count(*) FROM wastage_record w JOIN blood_unit u USING (unit_id) WHERE donation_id=%s AND cause='Discarded'", (dn,)) == 2
    assert db.one("SELECT status FROM donation WHERE donation_id=%s", (dn,)) == "Discarded"
    assert db.one("SELECT is_flagged FROM donor WHERE user_id=%s", (d,))
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s AND message LIKE %s", (db.admin, f"Donation {dn} failed%")) == 1


def test_wastage_recorded_once_per_unit(db):
    unit = db.stock("O+")[0]
    db.q("UPDATE blood_unit SET status='Discarded', status_reason='Bag damaged' WHERE unit_id=%s", (unit,))
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (unit,)) == "Discarded"
    assert "unique" in db.fails("INSERT INTO wastage_record (unit_id,cause) VALUES (%s,'Expired')", (unit,))


# ---------------------------------------------------------------------- FR-19 low-stock alerts
def open_alerts(db, group):
    return db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group=%s AND cleared_at IS NULL", (db.fac_h, group))


def test_alert_opens_and_clears_with_stock(db):
    db.q("INSERT INTO stock_threshold VALUES (%s,'O+','RBC',2)", (db.fac_h,))
    assert open_alerts(db, "O+") == 1
    units = db.stock("O+", n=2)
    assert open_alerts(db, "O+") == 0
    r = db.request("O+")
    db.q("CALL allocate(%s)", (r,))
    assert open_alerts(db, "O+") == 1
    db.q("CALL close_request(%s, TRUE)", (r,))
    assert open_alerts(db, "O+") == 0
    assert db.one("SELECT count(*) FROM blood_unit WHERE unit_id = ANY(%s) AND status='Available'", (units,)) == 2


def test_changing_a_minimum_rechecks_stock_at_once(db):
    db.stock("A+", n=2)
    db.q("INSERT INTO stock_threshold VALUES (%s,'A+','RBC',2)", (db.fac_h,))
    assert open_alerts(db, "A+") == 0
    db.q("UPDATE stock_threshold SET min_units = 5 WHERE facility_id=%s AND blood_group='A+'", (db.fac_h,))
    assert open_alerts(db, "A+") == 1


def test_one_statement_opens_and_clears_alerts_for_different_groups(db):
    """The statement-level alert check sees every group a multi-row UPDATE touched (PR-05 bulk path)."""
    (falling,) = db.stock("O+")
    dn = db.donate(db.donor("A+"))
    (rising,) = db.one("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))
    for t in ("HIV", "HBV", "HCV", "SYPH"):
        db.q("INSERT INTO test_result (donation_id, test_code, result) VALUES (%s,%s,'Negative')", (dn, t))
    db.q("INSERT INTO stock_threshold VALUES (%s,'O+','RBC',1), (%s,'A+','RBC',1)", (db.fac_h, db.fac_h))
    assert (open_alerts(db, "O+"), open_alerts(db, "A+")) == (0, 1)
    db.q("UPDATE blood_unit SET status = CASE WHEN unit_id = %s THEN 'Discarded' ELSE 'Available' END WHERE unit_id IN (%s,%s)",
         (falling, falling, rising))
    assert (open_alerts(db, "O+"), open_alerts(db, "A+")) == (1, 0)


# ---------------------------------------------------------------------- FR-33 / EER
def test_audit_log_records_who_field_old_new(db):
    d = db.donor(kg=70)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    db.q("UPDATE donor SET weight_kg = 72 WHERE user_id=%s", (d,))
    assert db.q("SELECT user_id, old_value, new_value FROM audit_log WHERE table_name='donor' AND record_id=%s "
                "AND field_name='weight_kg'", (str(d),)) == [(db.nurse, "70.0", "72.0")]


@pytest.mark.parametrize("sql, table", [
    ("UPDATE eligibility_rule SET min_hemoglobin_gl = 126 WHERE sex = 'F'", "eligibility_rule"),
    ("UPDATE component_type SET shelf_life_days = 7 WHERE component_code = 'PLT'", "component_type"),
    ("UPDATE donation_interval SET interval_days = 90 WHERE donation_type_code = 'WB' AND sex = 'F'", "donation_interval"),
    ("UPDATE deferral_reason SET default_days = 120 WHERE reason_code = 'TATTOO'", "deferral_reason"),
])
def test_clinical_rule_changes_are_audited(db, sql, table):
    """SE-08 / FR-32: an Admin's change to any rule table leaves an audit row."""
    db.q(sql)
    assert db.one("SELECT count(*) FROM audit_log WHERE table_name=%s AND action='UPDATE'", (table,)) == 1


def test_temp_table_cannot_shadow_the_audit_log(db):
    """SE-08: SECURITY DEFINER functions search pg_temp last."""
    d = db.donor()
    email = db.one("SELECT email FROM user_account WHERE user_id=%s", (d,))
    db.q("SET LOCAL ROLE vytal_app")
    db.q("CREATE TEMP TABLE audit_log (audit_id bigint, user_id int, action text, table_name text, record_id text, "
         "field_name text, old_value text, new_value text, changed_at timestamptz)")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    db.q("CALL update_donor_profile(%s, NULL, %s, NULL, 80)", (d, email))
    assert db.one("SELECT count(*) FROM pg_temp.audit_log") == 0
    db.q("RESET ROLE")
    assert db.one("SELECT count(*) FROM vytal.audit_log WHERE record_id=%s AND field_name='weight_kg'", (str(d),)) == 1


def test_disjoint_specialization(db):
    assert "role" in db.fails("INSERT INTO donor (user_id,donor_code,date_of_birth,sex,weight_kg) VALUES (%s,'X','1990-01-01','M',70)", (db.nurse,))
    assert "role" in db.fails("INSERT INTO admin VALUES (%s)", (db.nurse,))


# ---------------------------------------------------------------------- FR-33 accounts, facilities, thresholds, donations
def test_login_counters_and_password_hashes_never_reach_the_audit_log(db):
    d = db.donor()
    for _ in range(5):
        db.q("CALL login_attempt(%s, FALSE)", (d,))
    db.q("UPDATE user_account SET password_hash = 'new-hash' WHERE user_id=%s", (d,))
    assert db.one("SELECT locked_until IS NOT NULL FROM user_account WHERE user_id=%s", (d,))
    assert db.one("SELECT count(*) FROM audit_log WHERE table_name='user_account' AND action='UPDATE'") == 0
    db.q("UPDATE user_account SET phone = '519-555-0102' WHERE user_id=%s", (d,))
    assert db.q("SELECT field_name, new_value FROM audit_log WHERE table_name='user_account' AND action='UPDATE'") == \
        [("phone", "519-555-0102")]


def test_facility_threshold_and_donation_changes_are_audited(db):
    db.q("INSERT INTO stock_threshold (facility_id, blood_group, component_code, min_units) VALUES (%s,'O-','RBC',3)", (db.fac_h,))
    db.q("UPDATE facility SET phone = '519-555-0103' WHERE facility_id=%s", (db.fac_b,))
    dn = db.donate(db.donor())
    assert {(t, a, r) for t, a, r in db.q("SELECT table_name, action, record_id FROM audit_log "
                                          "WHERE table_name IN ('facility','stock_threshold','donation')")} >= {
        ("stock_threshold", "INSERT", f"{db.fac_h}/O-/RBC"), ("facility", "UPDATE", str(db.fac_b)), ("donation", "INSERT", str(dn))}


def audit_rows(db, table, action=None):
    return db.q("SELECT action, record_id, field_name, old_value, new_value FROM audit_log WHERE table_name=%s "
                "AND (%s::text IS NULL OR action=%s) ORDER BY audit_id", (table, action, action))


def test_staff_changes_are_audited(db):
    uid = db.one("CALL create_staff(%s,'aud@v.ca','x','Al','Ud',%s,'Nurse')", (db.admin, db.fac_h))
    assert [a for a, r, *_ in audit_rows(db, "staff") if r == str(uid)] == ["INSERT"]
    db.q("UPDATE staff SET facility_id=%s, job_title='Lead' WHERE user_id=%s", (db.fac_b, uid))
    assert {(f, o, n) for a, r, f, o, n in audit_rows(db, "staff", "UPDATE") if r == str(uid)} == {
        ("facility_id", str(db.fac_h), str(db.fac_b)), ("job_title", "Nurse", "Lead")}
    db.q("DELETE FROM staff WHERE user_id=%s", (uid,))
    assert [a for a, r, *_ in audit_rows(db, "staff") if r == str(uid)] == ["INSERT", "UPDATE", "UPDATE", "DELETE"]
    assert "password" not in "".join(str(x) for row in audit_rows(db, "staff") for x in row)


def test_test_result_changes_are_audited(db):
    dn = db.donate(db.donor())
    db.q("INSERT INTO test_result (donation_id, test_code, result) VALUES (%s,'HIV','Negative')", (dn,))
    rec = f"{dn}/HIV"
    assert [(a, r) for a, r, *_ in audit_rows(db, "test_result")] == [("INSERT", rec)]
    db.q("UPDATE test_result SET result='Positive' WHERE donation_id=%s AND test_code='HIV'", (dn,))
    assert ("UPDATE", rec, "result", "Negative", "Positive") in audit_rows(db, "test_result")
    db.q("DELETE FROM test_result WHERE donation_id=%s", (dn,))
    assert audit_rows(db, "test_result")[-1][:2] == ("DELETE", rec)


def test_request_unit_changes_are_audited(db):
    unit = db.stock("O+")[0]
    r = db.request("O+")
    db.q("CALL allocate(%s)", (r,))
    rec = f"{r}/{unit}"
    assert [(a, rid) for a, rid, *_ in audit_rows(db, "request_unit")] == [("INSERT", rec)]
    db.q("UPDATE request_unit SET released_at = now() WHERE request_id=%s", (r,))
    assert [(a, rid, f) for a, rid, f, *_ in audit_rows(db, "request_unit", "UPDATE")] == [("UPDATE", rec, "released_at")]
    db.q("DELETE FROM request_unit WHERE request_id=%s", (r,))
    assert audit_rows(db, "request_unit")[-1][:2] == ("DELETE", rec)


def test_donation_call_and_recipient_changes_are_audited(db):
    d = db.donor("O-", screened=True)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    r = db.request("O-", comp="RBC", urgency="Emergency")
    cid = db.one("INSERT INTO donation_call (request_id, sent_by, message) VALUES (%s,%s,'m') RETURNING call_id", (r, db.nurse))
    db.q("INSERT INTO call_recipient (call_id, donor_id) VALUES (%s,%s)", (cid, d))
    assert [(a, rid) for a, rid, *_ in audit_rows(db, "donation_call")] == [("INSERT", str(cid))]
    assert [(a, rid) for a, rid, *_ in audit_rows(db, "call_recipient")] == [("INSERT", f"{cid}/{d}")]
    db.q("UPDATE donation_call SET message='n' WHERE call_id=%s", (cid,))
    db.q("UPDATE call_recipient SET response='Accepted', responded_at=now() WHERE call_id=%s", (cid,))
    assert ("UPDATE", str(cid), "message", "m", "n") in audit_rows(db, "donation_call")
    assert {f for a, rid, f, *_ in audit_rows(db, "call_recipient", "UPDATE")} == {"response", "responded_at"}
    db.q("DELETE FROM call_recipient WHERE call_id=%s", (cid,))
    db.q("DELETE FROM donation_call WHERE call_id=%s", (cid,))
    assert audit_rows(db, "call_recipient")[-1][:2] == ("DELETE", f"{cid}/{d}")
    assert audit_rows(db, "donation_call")[-1][:2] == ("DELETE", str(cid))


def test_every_audited_table_has_its_audit_trigger(db):
    """FR-33: the audited tables are exactly these; a table added to or dropped from the list must be a decision."""
    audited = {t for (t,) in db.q("SELECT c.relname FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
                                   "JOIN pg_proc p ON p.oid = g.tgfoid WHERE p.proname = 'audit_row' AND NOT g.tgisinternal")}
    assert audited == {"donor", "deferral", "blood_unit", "blood_request", "compatibility", "component_type",
                       "donation_interval", "eligibility_rule", "deferral_reason", "user_account", "facility",
                       "stock_threshold", "donation", "staff", "test_result", "request_unit", "donation_call",
                       "call_recipient"}


# ---------------------------------------------------------------------- UC-24 2a compatibility coverage
def test_compatibility_edit_that_keeps_coverage_is_accepted(db):
    db.q("DELETE FROM compatibility WHERE component_code='RBC' AND recipient_group='AB+' AND donor_group='A+'")
    db.q("UPDATE compatibility SET donor_group='A+' WHERE component_code='RBC' AND recipient_group='AB+' AND donor_group='B+'")
    assert db.one("SELECT count(*) FROM compatibility WHERE component_code='RBC' AND recipient_group='AB+'") == 7


@pytest.mark.parametrize("prep, sql", [
    ("", "DELETE FROM compatibility WHERE component_code='RBC' AND recipient_group='O-'"),
    ("", "DELETE FROM compatibility WHERE component_code='PLT'"),
    # moves O-'s only pair to O+ (the O+ <- O- pair is removed first so only the coverage rule can refuse it)
    ("DELETE FROM compatibility WHERE component_code='RBC' AND recipient_group='O+' AND donor_group='O-'",
     "UPDATE compatibility SET recipient_group='O+' WHERE component_code='RBC' AND recipient_group='O-'"),
])
def test_compatibility_edit_that_strands_a_group_is_blocked(db, prep, sql):
    if prep:
        db.q(prep)
    assert "no compatible donor group" in db.fails(sql)


# ---------------------------------------------------------------------- FR-03 / UC-03 2a verified blood group
def test_only_staff_change_a_verified_blood_group(db):
    d = db.donor("O+")
    assert "only staff" in db.fails("UPDATE donor SET blood_group='A+' WHERE user_id=%s", (d,))      # no acting user
    for who in (d, db.admin):
        db.q("SELECT set_config('vytal.user_id', %s, true)", (str(who),))
        assert "only staff" in db.fails("UPDATE donor SET blood_group='A+' WHERE user_id=%s", (d,))
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    db.q("UPDATE donor SET blood_group='A+' WHERE user_id=%s", (d,))
    assert db.one("SELECT blood_group FROM donor WHERE user_id=%s", (d,)) == "A+"


def test_donor_may_change_an_unverified_group(db):
    d = db.donor("O+", verified=False)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    db.q("UPDATE donor SET blood_group='B+' WHERE user_id=%s", (d,))
    assert db.one("SELECT blood_group FROM donor WHERE user_id=%s", (d,)) == "B+"


def test_verification_cannot_be_cleared_or_rewritten_by_non_staff(db):
    """FR-03: clearing verified_by first must not open the group to a donor's edit."""
    d = db.donor("O+")
    for who in (d, db.admin):
        db.q("SELECT set_config('vytal.user_id', %s, true)", (str(who),))
        assert "only staff" in db.fails("UPDATE donor SET verified_by=NULL, verified_at=NULL WHERE user_id=%s", (d,))
        assert "only staff" in db.fails("UPDATE donor SET verified_at=now() - interval '1 day' WHERE user_id=%s", (d,))
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.phleb),))
    db.q("UPDATE donor SET verified_by=%s, verified_at=now() WHERE user_id=%s", (db.phleb, d))      # active staff may re-verify
    assert db.one("SELECT verified_by FROM donor WHERE user_id=%s", (d,)) == db.phleb


def test_only_staff_verify_a_group_and_only_as_themselves(db):
    """FR-09: a donor or Admin cannot mark a group verified, and staff cannot record someone else as the verifier."""
    d = db.donor("A+", verified=False)
    for who in (d, db.admin):
        db.q("SELECT set_config('vytal.user_id', %s, true)", (str(who),))
        assert "only staff can verify" in db.fails("UPDATE donor SET verified_by=%s, verified_at=now() WHERE user_id=%s", (db.nurse, d))
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    assert "only staff can verify" in db.fails("UPDATE donor SET verified_by=%s, verified_at=now() WHERE user_id=%s", (db.phleb, d))
    db.q("UPDATE donor SET verified_by=%s, verified_at=now() WHERE user_id=%s", (db.nurse, d))
    assert db.one("SELECT verified_by FROM donor WHERE user_id=%s", (d,)) == db.nurse


def test_inactive_staff_cannot_change_a_verified_group(db):
    d = db.donor("O+")
    db.q("UPDATE user_account SET is_active=FALSE WHERE user_id=%s", (db.nurse,))
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    assert "only staff" in db.fails("UPDATE donor SET blood_group='A+' WHERE user_id=%s", (d,))


def test_coverage_check_ignores_components_the_edit_did_not_touch(db):
    """UC-24: a new component without rules yet must not block an unrelated red-cell edit."""
    db.q("INSERT INTO component_type (component_code, name, shelf_life_days) VALUES ('CRYO','Cryoprecipitate',365)")
    db.q("DELETE FROM compatibility WHERE component_code='RBC' AND recipient_group='AB+' AND donor_group='A+'")
    assert db.one("SELECT count(*) FROM compatibility WHERE component_code='RBC' AND recipient_group='AB+'") == 7


def test_coverage_error_names_the_stranded_group_and_component(db):
    msg = db.fails("DELETE FROM compatibility WHERE component_code='RBC' AND recipient_group='O-'")
    assert "o- recipients" in msg and "for rbc" in msg
