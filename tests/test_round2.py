"""Round 2 regressions: clinic calendar days, direct-SQL guards, automatic top-ups, calls, messages, sign-in and privacy."""
import datetime as dt
import random
import threading
from zoneinfo import ZoneInfo

import psycopg
import pytest

from conftest import DB, PREFIX, build
from vytal import db as vdb
from vytal import services as svc

TORONTO = ZoneInfo("America/Toronto")
PASSWORD = "correct-horse-42"
NEG = '{"HIV":"Negative","HBV":"Negative","HCV":"Negative","SYPH":"Negative"}'


def as_user(db, uid):
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(uid),))


# ====================================================================== SQL, one rolled-back transaction per test
# ---------------------------------------------------------------------- DON-01 / INT-05 / REV-13 clinic calendar day (C1)
def test_clinic_day_is_toronto_not_utc(db):
    assert db.one("SELECT clinic_date('2026-11-15 23:30-05')") == dt.date(2026, 11, 15)     # 04:30 UTC on the 16th
    assert db.one("SELECT current_setting('TimeZone')") in ("UTC", "Etc/UTC")             # DI-06 the session stays UTC
    assert db.one("SELECT clinic_today()") == dt.datetime.now(TORONTO).date()


def test_an_evening_donation_counts_on_its_clinic_day(db):
    d = db.donor(sex="M")
    at = db.one("SELECT ((clinic_today() - 10) + time '23:30') AT TIME ZONE 'America/Toronto'")
    db.q("INSERT INTO screening (donor_id, completed_at) VALUES (%s, %s - interval '10 minutes')", (d, at))
    db.q("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
         "VALUES (%s,%s,'WB',%s,450,140)", (d, db.nurse, at))
    assert db.one("SELECT next_eligible_date - clinic_today() FROM donor WHERE user_id=%s", (d,)) == 56 - 10


def test_a_one_day_deferral_ends_tomorrow_on_the_clinic_calendar(db):
    d = db.donor()
    db.q("CALL record_deferral(%s,%s,'UNWELL')", (db.nurse, d))
    assert db.one("SELECT end_date FROM deferral WHERE donor_id=%s", (d,)) == db.one("SELECT clinic_today() + 1")


# ---------------------------------------------------------------------- INT-06 a new interval updates stored dates
def test_changing_the_interval_moves_next_eligible_dates(db):
    m, f = db.donor(sex="M"), db.donor(sex="F")
    db.donate(m)
    db.donate(f, hb=130)
    db.q("CALL set_donation_interval(%s,'WB','M',70)", (db.admin,))
    assert db.q("SELECT next_eligible_date - clinic_today() FROM donor WHERE user_id IN (%s,%s) ORDER BY sex DESC",
                (m, f)) == [(70,), (84,)]
    db.q("UPDATE donor SET sex='F' WHERE user_id=%s", (m,))                          # the recorded sex changes the interval too
    assert db.one("SELECT next_eligible_date - clinic_today() FROM donor WHERE user_id=%s", (m,)) == 84


# ---------------------------------------------------------------------- INT-07 29 February birthdays
def test_leap_day_birthday_view_and_trigger_agree(db):
    db.q("CALL register_donor('leap@v.ca','x','L','P',NULL,'2008-02-29','M',70,178,'1 St','O+')")
    d = db.one("SELECT user_id FROM user_account WHERE email='leap@v.ca'")
    as_user(db, db.nurse)
    db.q("UPDATE donor SET verified_by=%s, verified_at=now() WHERE user_id=%s", (db.nurse, d))
    status = db.one("SELECT status FROM donor_eligibility(%s, '2025-02-28')", (d,))
    assert status == "Not eligible until 2025-03-01"
    feb28 = dt.datetime(2025, 2, 28, 12, tzinfo=TORONTO)
    db.q("INSERT INTO screening (donor_id, completed_at) VALUES (%s, %s)", (d, feb28 - dt.timedelta(minutes=10)))
    assert "under 17" in db.fails("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
                                  "VALUES (%s,%s,'WB',%s,450,140)", (d, db.nurse, feb28))
    mar1 = dt.datetime(2025, 3, 1, 12, tzinfo=TORONTO)
    db.q("INSERT INTO screening (donor_id, completed_at) VALUES (%s, %s)", (d, mar1 - dt.timedelta(minutes=10)))
    db.q("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
         "VALUES (%s,%s,'WB',%s,450,140)", (d, db.nurse, mar1))


# ---------------------------------------------------------------------- INT-08 DI-03 rules hold for direct SQL
def test_direct_sql_cannot_skip_the_rules(db):
    d = db.donor(sex="F", kg=55, cm=160)
    dn = db.donate(d, hb=130)
    assert "starts as collected" in db.fails("INSERT INTO blood_unit (unit_id,donation_id,component_code,status) "
                                             "VALUES ('X1-RBC',%s,'RBC','Available')", (dn,))
    e = db.donor(sex="F", kg=55, cm=160)
    db.screen(e)
    sql = ("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
           "VALUES (%s,%s,'WB',now() + %s::interval,%s,130)")
    assert "more than 15%" in db.fails(sql, (e, db.nurse, "0 s", 1000))
    assert "in the future" in db.fails(sql, (e, db.nurse, "30 days", 450))
    unit = db.stock("A+")[0]
    dn2 = db.one("SELECT donation_id FROM blood_unit WHERE unit_id=%s", (unit,))
    db.q("UPDATE test_result SET result='Positive' WHERE donation_id=%s AND test_code='HCV'", (dn2,))
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Discarded"
    assert "cannot be changed" in db.fails("UPDATE test_result SET result='Negative' WHERE donation_id=%s AND test_code='HCV'", (dn2,))


# ---------------------------------------------------------------------- INT-09 a positive result on a Reserved unit
def test_positive_result_on_a_reserved_unit_releases_and_tops_up(db):
    first, spare = db.stock("B+", n=2)
    r = db.request("B+")
    as_user(db, db.nurse)
    db.q("CALL allocate(%s)", (r,))
    held = db.one("SELECT unit_id FROM request_unit WHERE request_id=%s AND released_at IS NULL", (r,))
    dn = db.one("SELECT donation_id FROM blood_unit WHERE unit_id=%s", (held,))
    db.q("UPDATE test_result SET result='Positive' WHERE donation_id=%s AND test_code='HIV'", (dn,))
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (held,)) == "Discarded"
    other = ({first, spare} - {held}).pop()
    assert db.q("SELECT unit_id FROM request_unit WHERE request_id=%s AND released_at IS NULL", (r,)) == [(other,)]
    assert db.one("CALL close_request(%s)", (r,)) == "Fulfilled"
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (held,)) == "Discarded"


# ---------------------------------------------------------------------- INT-10 a NULL in the issue list
def test_close_request_refuses_an_empty_entry(db):
    db.stock("O+", n=2)
    r = db.request("O+", n=2)
    as_user(db, db.nurse)
    db.q("CALL allocate(%s)", (r,))
    unit = db.one("SELECT min(unit_id) FROM request_unit WHERE request_id=%s", (r,))
    assert "empty entry" in db.fails("CALL close_request(%s, FALSE, ARRAY[%s, NULL])", (r, unit))
    assert db.one("SELECT count(*) FROM blood_unit WHERE status='Issued'") == 0


# ---------------------------------------------------------------------- INT-11 / ADM-04 expiry: alert and top-up (C10, C15)
def test_expiry_opens_the_alert_and_tops_up_the_request(db):
    db.q("INSERT INTO stock_threshold VALUES (%s,'A-','RBC',1)", (db.fac_h,))
    old, fresh = db.stock("A-", n=2)
    r = db.request("A-")
    as_user(db, db.nurse)
    db.q("CALL allocate(%s)", (r,))
    held = db.one("SELECT unit_id FROM request_unit WHERE request_id=%s AND released_at IS NULL", (r,))
    spare = ({old, fresh} - {held}).pop()
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (held,))
    as_user(db, db.admin)
    db.q("CALL run_nightly_expiry()")
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (held,)) == "Expired"
    assert db.q("SELECT unit_id FROM request_unit WHERE request_id=%s AND released_at IS NULL", (r,)) == [(spare,)]
    assert db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group='A-' AND cleared_at IS NULL",
                  (db.fac_h,)) == 1


def test_an_expired_unit_is_not_counted_as_stock_before_the_job_runs(db):
    db.q("INSERT INTO stock_threshold VALUES (%s,'AB+','RBC',1)", (db.fac_h,))
    unit = db.stock("AB+")[0]
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (unit,))
    assert db.one("SELECT count(*) FROM stock_alert WHERE blood_group='AB+' AND cleared_at IS NULL") == 1


# ---------------------------------------------------------------------- C15 / ADM-04 automatic top-up
def test_release_tops_up_a_short_request(db):
    r = db.request("O-")
    as_user(db, db.nurse)
    assert db.one("CALL allocate(%s)", (r,)) == 0
    unit = db.stock("O-")[0]
    assert db.groups_of(r) == ["O-"] and db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Reserved"


def test_clearing_a_lookback_tops_up(db):
    unit = db.stock("B-")[0]
    donor = db.one("SELECT donor_id FROM v_blood_unit WHERE unit_id=%s", (unit,))
    as_user(db, db.admin)
    db.q("CALL quarantine_donor(%s)", (donor,))
    # ADM-14 a lookback hold is not described as a failed test
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (donor,)) == \
        "Not eligible (flagged for a lookback investigation)"
    r = db.request("B-")
    assert db.groups_of(r) == []
    db.q("CALL clear_lookback(%s,%s,'Recipient traced, no link')", (db.admin, donor))
    assert db.groups_of(r) == ["B-"]


def test_adding_a_pairing_tops_up(db):
    db.q("DELETE FROM compatibility WHERE component_code='RBC' AND recipient_group='A+' AND donor_group='O+'")
    db.stock("O+")
    r = db.request("A+")
    assert db.groups_of(r) == []
    db.q("CALL set_compatibility(%s,'RBC','A+','O+',TRUE)", (db.admin,))
    assert db.groups_of(r) == ["O+"]


# ---------------------------------------------------------------------- INT-12 sample data
def test_sample_request_22_is_a_state_the_procedures_produce(sdb):
    assert sdb.one("SELECT status FROM blood_request WHERE request_id = 22") == "Cancelled"
    assert sdb.one("SELECT count(*) FROM blood_request r WHERE status = 'Partially Fulfilled' AND NOT EXISTS "
                   "(SELECT 1 FROM request_unit u WHERE u.request_id = r.request_id AND u.issued_at IS NOT NULL)") == 0


# ---------------------------------------------------------------------- C8 / STF-06 tests in any subset
def test_tests_can_be_recorded_in_parts(db):
    dn = db.donate(db.donor())
    db.q("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))
    assert db.one("CALL record_tests(%s, '{\"HIV\":\"Negative\",\"HBV\":\"Negative\"}')", (dn,)) == "Awaiting 2 test(s)"
    assert "already recorded" in db.fails("CALL record_tests(%s, '{\"HIV\":\"Positive\"}')", (dn,))
    assert "at least one" in db.fails("CALL record_tests(%s, '{}')", (dn,))
    assert "not a known test" in db.fails("CALL record_tests(%s, '{\"XYZ\":\"Negative\"}')", (dn,))
    assert db.one("CALL record_tests(%s, '{\"HCV\":\"Negative\",\"SYPH\":\"Negative\"}')", (dn,)) == "Released"


# ---------------------------------------------------------------------- C9 / DON-03 / DON-16 / STF-12 calls
def test_call_answer_marks_read_and_tells_staff(db):
    d = db.donor("O-", screened=True)
    r = db.request("O-")
    call = db.one("CALL send_donation_call(%s,%s)", (r, db.nurse))
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s AND call_id=%s AND read_at IS NULL", (d, call)) == 1
    as_user(db, d)
    reply = db.one("CALL respond_to_call(%s,%s,TRUE)", (call, d))
    assert reply == "Thank you. Please come to Riverside General, 1 River Rd."
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s AND call_id=%s AND read_at IS NULL", (d, call)) == 0
    code = db.one("SELECT donor_code FROM donor WHERE user_id=%s", (d,))
    assert db.q("SELECT message FROM notification WHERE user_id=%s AND call_id=%s", (db.nurse, call)) == \
        [(f"Donor {code} accepted the donation call for request #{r}",)]


def test_a_call_past_its_required_by_time_cannot_be_accepted(db):
    d = db.donor("O-", screened=True)
    r = db.request("O-")
    call = db.one("CALL send_donation_call(%s,%s)", (r, db.nurse))
    db.q("ALTER TABLE blood_request DISABLE TRIGGER USER")             # move the deadline into the past, as time would
    db.q("UPDATE blood_request SET required_by = now() - interval '1 minute' WHERE request_id=%s", (r,))
    db.q("ALTER TABLE blood_request ENABLE TRIGGER USER")
    as_user(db, d)
    assert "no longer open" in db.fails("CALL respond_to_call(%s,%s,TRUE)", (call, d))
    assert db.one("SELECT response FROM call_recipient WHERE call_id=%s", (call,)) == "Pending"


# ---------------------------------------------------------------------- C6 / STF-07 / REV-15 not found vs another facility
def test_missing_records_are_not_found_not_another_facilitys(db):
    as_user(db, db.nurse)
    assert db.fails("CALL close_request(999999)") == "request: request #999999 was not found"
    assert db.fails("CALL confirm_request_group(%s, 999999, 'O+')", (db.nurse,)) == "request: request #999999 was not found"
    assert db.fails("CALL discard_unit(%s, 'X999-RBC', 'Bag damaged')", (db.nurse,)) == "unit: unit x999-rbc was not found"
    r = db.request("O+")
    as_user(db, db.phleb)
    assert "belongs to another facility" in db.fails("CALL confirm_request_group(%s,%s,'O+')", (db.phleb, r))


# ---------------------------------------------------------------------- STF-04 / STF-05 / DON-11 / DON-12 messages (C5, C7)
def test_request_messages_name_the_field(db):
    as_user(db, db.nurse)
    sql = "CALL submit_request(%s,%s,'O+','RBC',%s,'Routine',now()+interval '1 day')"
    assert db.fails(sql, (db.nurse, "P" * 21, 1)).startswith("patient_ref: use at most 20 characters")
    assert db.fails(sql, (db.nurse, "P-1", 51)) == db.fails(sql, (db.nurse, "P-1", 101)) == \
        "units: enter a whole number of units from 1 to 50"


def test_registration_refuses_implausible_phone_and_birth_date(db):
    sql = "CALL register_donor(%s,'x','A','B',%s,%s,'M',70,178,'1 St','O+')"
    assert db.fails(sql, ("p1@v.ca", "call me maybe", "1990-01-01")).startswith("phone: enter a phone number")
    assert db.fails(sql, ("p2@v.ca", None, "1890-01-01")) == "date_of_birth: enter a real date of birth"
    assert db.fails(sql, ("NURSE@v.ca", None, "1990-01-01")).startswith("email: that email is already")


def test_refusals_use_names_not_ids(db):
    db.request("O+")
    msg = db.fails("CALL set_facility_active(%s,%s,FALSE)", (db.admin, db.fac_h))
    assert msg.startswith("riverside general has open requests")


# ---------------------------------------------------------------------- C14 / ADM-09 facility names ignore letter case
def test_facility_names_are_unique_whatever_the_case(db):
    assert db.fails("CALL save_facility(%s,NULL,'RIVERSIDE general','Hospital','9 St',NULL,NULL)", (db.admin,)) \
        .startswith("name: a facility named")
    assert "unique" in db.fails("INSERT INTO facility (name,facility_type,address) VALUES ('riverside GENERAL','Hospital','x')")


# ---------------------------------------------------------------------- C12 retire a deferral reason, edit an account
def test_a_retired_reason_is_refused_for_new_deferrals(db):
    db.q("CALL save_deferral_reason(%s,'COLD','Recent cold',FALSE,7)", (db.admin,))
    db.q("CALL set_deferral_reason_active(%s,'cold',FALSE)", (db.admin,))
    d = db.donor()
    assert "is retired" in db.fails("CALL record_deferral(%s,%s,'COLD')", (db.nurse, d))
    assert "cannot be retired" in db.fails("CALL set_deferral_reason_active(%s,'LOWHB',FALSE)", (db.admin,))
    assert "active screening question" in db.fails("CALL set_deferral_reason_active(%s,'TATTOO',FALSE)", (db.admin,))
    db.q("CALL set_deferral_reason_active(%s,'COLD',TRUE)", (db.admin,))
    db.q("CALL record_deferral(%s,%s,'COLD')", (db.nurse, d))


def test_admin_edits_an_account_and_finds_it_by_part_of_the_email(db):
    as_user(db, db.admin)
    db.q("CALL update_account(%s,%s,'Maya','Rossi','Maya.Rossi@V.ca','519-555-0101')", (db.admin, db.nurse))
    assert db.q("SELECT email, phone, last_name FROM account_contact(%s)", (db.nurse,)) == \
        [("maya.rossi@v.ca", "519-555-0101", "Rossi")]
    assert db.q("SELECT user_id FROM match_email('ROSSI@')") == [(db.nurse,)]
    assert db.q("SELECT user_id FROM match_email('ro')") == []
    assert db.q("SELECT user_id FROM match_email('%%%%%%')") == []           # wildcards are literal
    assert "already used" in db.fails("CALL update_account(%s,%s,'Lee','Park','MAYA.ROSSI@v.ca',NULL)", (db.admin, db.phleb))


# ---------------------------------------------------------------------- SEC-ISS-10 the reporting login's least privilege
def test_report_role_cannot_read_private_donor_facts(db):
    d = db.donor(screened=True)
    db.q("SET LOCAL ROLE vytal_report")
    for sql in ("SELECT address FROM donor", "SELECT date_of_birth FROM donor", "SELECT * FROM screening_answer",
                "SELECT * FROM test_result"):
        assert "permission denied" in db.fails(sql)
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == "Eligible"
    assert db.one("SELECT count(*) FROM donor") >= 1                                  # the granted columns still count
    db.q("RESET ROLE")


# ====================================================================== backend, as the application role, committed rows
@pytest.fixture(scope="module")
def site():
    name = build(PREFIX + "_round2")
    with psycopg.connect(dbname=name) as con:
        con.execute("SET search_path = vytal")
        fac = [con.execute("INSERT INTO facility (name, facility_type, address, phone) VALUES (%s,'Hospital','1 St','519-555-0199') "
                           "RETURNING facility_id", (n,)).fetchone()[0] for n in ("Windsor Regional", "Essex Clinic")]
        admin = con.execute("INSERT INTO user_account (email,password_hash,first_name,last_name,role) "
                            "VALUES ('root@v.ca','x','A','B','Admin') RETURNING user_id").fetchone()[0]
        con.execute("INSERT INTO admin VALUES (%s)", (admin,))
        staff = [con.execute("CALL create_staff(%s,%s,'x','S','T',%s,'Nurse')", (admin, f"n{i}@v.ca", f)).fetchone()[0]
                 for i, f in enumerate(fac)]
    return {"db": name, "admin": admin, "fac": fac, "staff": staff}


@pytest.fixture
def app(site, monkeypatch):
    monkeypatch.setenv("PGDATABASE", site["db"])
    monkeypatch.setenv("PGOPTIONS", "-c role=vytal_app")
    return site


def donor(email):
    return svc.register_donor(email, PASSWORD, "Ana", "Roy", "519-555-0100", dt.date(1990, 5, 1), "M", 72, 175, "1 Main St", "O+")


# ---------------------------------------------------------------------- C3 / DON-15 / SEC-ISS-05 / SEC-ISS-02 sign-in
def test_one_message_for_every_failed_sign_in(app):
    d = donor("lock@v.ca")
    with pytest.raises(svc.AccessDenied) as unknown:
        svc.login("nobody@v.ca", PASSWORD)
    for _ in range(5):
        with pytest.raises(svc.AccessDenied):
            svc.login("lock@v.ca", "wrong-pass-1")
    with pytest.raises(svc.AccessDenied) as locked:                 # locked now, even with the right password
        svc.login("lock@v.ca", PASSWORD)
    assert str(unknown.value) == str(locked.value) == svc.LOGIN_FAILED
    assert d


def test_exempt_demo_accounts_are_never_locked(app):
    d = donor("demo@v.ca")
    for _ in range(8):
        with pytest.raises(svc.AccessDenied, match="Wrong email or password"):
            svc.login("demo@v.ca", "wrong-pass-1", exempt_ids={d})
    assert svc.login("DEMO@v.ca ", PASSWORD, exempt_ids={d}) == (d, "Donor")


# ---------------------------------------------------------------------- C4 / ADM-06 / SEC-ISS-07 password rules
@pytest.mark.parametrize("pw", [" " * 12, "aaaaaaaaaa", "aaaaaaaaa1", "password12", "Password123", "qwerty2026!", "short1"])
def test_weak_passwords_are_refused(pw):
    with pytest.raises(svc.InvalidInput, match="^password: "):
        svc.check_password(pw)


def test_a_password_holding_the_email_name_is_refused():
    with pytest.raises(svc.InvalidInput, match="^password: "):
        svc.check_password("anaroy-river-7", "anaroy@v.ca")
    svc.check_password(PASSWORD, "anaroy@v.ca")


# ---------------------------------------------------------------------- SEC-ISS-09 / DON-10 current-password guesses
def test_wrong_current_passwords_lock_the_change(app):
    d = donor("change@v.ca")
    for _ in range(4):
        with pytest.raises(svc.InvalidInput, match="^current_password: The current password is wrong"):
            svc.change_password(d, "not-it-123x", "brand-new-pass-9")
    with pytest.raises(svc.InvalidInput, match="^current_password: Too many wrong attempts"):     # the fifth locks
        svc.change_password(d, "not-it-123x", "brand-new-pass-9")
    with pytest.raises(svc.InvalidInput, match="^current_password: Too many wrong attempts"):
        svc.change_password(d, PASSWORD, "brand-new-pass-9")


# ---------------------------------------------------------------------- C13 one connection per web request
def test_request_scope_reuses_one_connection_and_restores_the_user(app):
    assert svc.current_connection() is None
    with svc.request_scope():
        with vdb.session(7) as a:
            pid = a.info.backend_pid
            with vdb.session(9) as inner:
                assert inner is a and inner.execute("SELECT current_setting('vytal.user_id')").fetchone() == ("9",)
            assert a.execute("SELECT current_setting('vytal.user_id')").fetchone() == ("7",)
        with vdb.session() as b:
            assert b.info.backend_pid == pid
            assert b.execute("SELECT current_setting('vytal.user_id', true)").fetchone()[0] in ("", None)
        con = svc.current_connection()
        with pytest.raises(svc.InvalidInput):                   # a failed unit of work leaves the connection usable
            svc.update_account(app["admin"], 999999, "Al", "Bo", "nobody.here@v.ca")
        assert svc.current_connection() is con
        assert svc.accounts_matching_email(app["admin"], "ROOT@") == [app["admin"]]
    assert con.closed and svc.current_connection() is None


# ---------------------------------------------------------------------- C2 / ADM-01 / ADM-02 / INT-04 / REV-01 audit bounds
def test_audit_dates_are_whole_clinic_days():
    since, until = svc._audit_bounds(dt.date(2026, 7, 14), dt.date(2026, 7, 14))
    assert since == dt.datetime(2026, 7, 14, tzinfo=TORONTO) and until == dt.datetime(2026, 7, 15, tzinfo=TORONTO)
    exact = dt.datetime(2026, 7, 14, 12, 30, tzinfo=dt.timezone.utc)
    assert svc._audit_bounds(None, exact)[1] == exact
    with pytest.raises(svc.InvalidInput, match="^until: "):
        svc._audit_bounds(dt.date(2026, 7, 15), dt.date(2026, 7, 14))


# ---------------------------------------------------------------------- C12 Admin account edit through the backend
def test_admin_account_edit_through_the_backend(app):
    d = donor("edit.me@v.ca")
    svc.update_account(app["admin"], d, "Ana", "Roy-Lee", "Edit.Me@V.ca", "519-555-0111")
    got = svc.account_details(app["admin"], d)
    assert (got["email"], got["phone"], got["last_name"], got["role"]) == ("edit.me@v.ca", "519-555-0111", "Roy-Lee", "Donor")
    assert svc.accounts_matching_email(app["admin"], "EDIT.ME") == [d]
    with pytest.raises(svc.InvalidInput, match="^phone: "):
        svc.update_account(app["admin"], d, "Ana", "Roy", "edit.me@v.ca", "not a phone")
    with pytest.raises(svc.AccessDenied):
        svc.account_details(d, d)


# ---------------------------------------------------------------------- C6 / STF-07 through the backend
def test_backend_not_found_vs_another_facility(app):
    windsor, essex = app["staff"]
    with pytest.raises(svc.InvalidInput, match=r"^request: Request #999999 was not found"):
        svc.close_request(windsor, 999999)
    with pytest.raises(svc.InvalidInput, match="^donation: Donation 999999 was not found"):
        svc.record_tests(windsor, 999999, {"HIV": "Negative"})
    r = svc.submit_request(essex, "P-9", "O+", "RBC", 1, "Routine", dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))["request_id"]
    with pytest.raises(svc.AccessDenied, match="belongs to another facility"):
        svc.close_request(windsor, r)
    svc.close_request(essex, r, cancel=True)


def test_backend_facility_name_is_case_insensitive(app):
    with pytest.raises(svc.InvalidInput, match="^name: "):
        svc.save_facility(app["admin"], None, "windsor REGIONAL", "Hospital", "5 St")


# ====================================================================== concurrency (INT-01 / INT-02 / INT-03)
@pytest.fixture
def owner_world(site):
    """A facility of its own per test, so stock and top-ups of other tests stay out of the way."""
    con = psycopg.connect(dbname=site["db"])
    con.execute("SET search_path = vytal")
    w = DB(con)
    w.seq = random.randrange(10 ** 6, 10 ** 7)
    w.admin = site["admin"]
    w.fac_h = w.one("INSERT INTO facility (name,facility_type,address) VALUES (%s,'Hospital','1 St') RETURNING facility_id",
                    (f"Race {w.seq}",))
    w.nurse = w.one("CALL create_staff(%s,%s,'x','R','S',%s,'Nurse')", (w.admin, f"race{w.seq}@v.ca", w.fac_h))
    con.commit()
    yield w
    con.close()


def race(site, statements):
    """Runs each (sql, args) in its own connection at the same moment, commits, and returns the errors."""
    barrier, errors = threading.Barrier(len(statements)), []

    def run(sql, args):
        with psycopg.connect(dbname=site["db"]) as c:
            c.execute("SET search_path = vytal")
            barrier.wait()
            try:
                c.execute(sql, args)
                c.commit()
            except psycopg.Error as e:
                errors.append(e)

    threads = [threading.Thread(target=run, args=s) for s in statements]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    return errors


def open_alerts(w, group):
    return w.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group=%s AND cleared_at IS NULL", (w.fac_h, group))


def test_concurrent_reservations_open_the_low_stock_alert(site, owner_world):
    w = owner_world
    w.q("INSERT INTO stock_threshold VALUES (%s,'AB+','RBC',2)", (w.fac_h,))
    w.stock("AB+", n=3)
    reqs = [w.request("AB+") for _ in range(2)]
    w.con.commit()
    assert open_alerts(w, "AB+") == 0
    w.con.commit()
    for _ in range(3):
        assert race(site, [("CALL allocate(%s)", (r,)) for r in reqs]) == []
        if w.one("SELECT count(*) FROM request_unit WHERE request_id = ANY(%s) AND released_at IS NULL", (reqs,)) == 2:
            break
    assert open_alerts(w, "AB+") == 1                       # one Available unit left, under the minimum of 2


def test_concurrent_releases_clear_the_alert(site, owner_world):
    w = owner_world
    w.q("INSERT INTO stock_threshold VALUES (%s,'B-','PLASMA',2)", (w.fac_h,))
    dns = []
    for _ in range(2):
        dn = w.donate(w.donor("B-"))
        w.one("CALL separate_donation(%s, ARRAY['PLASMA'])", (dn,))
        dns.append(dn)
    w.con.commit()
    assert open_alerts(w, "B-") == 1
    w.con.commit()
    assert race(site, [("CALL record_tests(%s, %s)", (dn, NEG)) for dn in dns]) == []
    assert open_alerts(w, "B-") == 0


def test_issue_and_quarantine_at_once_do_not_deadlock(site, owner_world):
    w = owner_world
    for _ in range(4):
        unit = w.stock("A-")[0]
        d = w.one("SELECT donor_id FROM v_blood_unit WHERE unit_id=%s", (unit,))
        r = w.request("A-")
        w.q("CALL allocate(%s)", (r,))
        w.con.commit()
        errors = race(site, [("CALL close_request(%s)", (r,)), ("CALL quarantine_donor(%s)", (d,))])
        assert not [e for e in errors if isinstance(e, psycopg.errors.DeadlockDetected)]
        status = w.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,))
        assert status in ("Issued", "Quarantined")
        w.con.commit()
