"""Stored procedures: each test drives one procedure through its normal path and its refusals."""
import json
import re

import pytest

NEG = json.dumps({"HIV": "Negative", "HBV": "Negative", "HCV": "Negative", "SYPH": "Negative"})


# ---------------------------------------------------------------------- allocate() FR-23 / FR-24
def test_exact_match_first_then_fefo(db):
    old = db.stock("A+", days_ago=30)[0]
    new = db.stock("A+", days_ago=2)[0]
    db.stock("O+", days_ago=35)
    r = db.request("A+")
    assert db.one("SELECT allocate(%s)", (r,)) == 1
    assert db.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (r,)) == old
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (new,)) == "Available"


def test_o_negative_conserved_for_routine_requests(db):
    db.stock("O-", days_ago=30)
    db.stock("O+", days_ago=2)
    r = db.request("A+")
    db.q("SELECT allocate(%s)", (r,))
    assert db.groups_of(r) == ["O+"]


def test_o_negative_covers_only_what_other_stock_cannot(db):
    db.stock("O-", n=3)
    db.stock("A+")
    r = db.request("A+", n=3, urgency="Urgent")
    assert db.one("SELECT allocate(%s)", (r,)) == 3
    assert db.groups_of(r) == ["A+", "O-", "O-"]


def test_o_negative_used_when_nothing_else_fits(db):
    db.stock("O-")
    db.stock("A+")                                   # incompatible with an A- patient
    r = db.request("A-")
    db.q("SELECT allocate(%s)", (r,))
    assert db.groups_of(r) == ["O-"]


def test_unknown_group_emergency_gets_only_o_negative_and_records_shortfall(db):
    db.stock("O-", n=3)
    db.stock("O+", n=3)
    r = db.request(None, n=4, urgency="Emergency")
    assert db.one("SELECT allocate(%s)", (r,)) == 3
    assert db.groups_of(r) == ["O-"] * 3
    assert db.q("SELECT units_reserved, shortfall FROM v_request_status WHERE request_id=%s", (r,)) == [(3, 1)]


def test_plasma_compatibility_is_reversed(db):
    db.stock("O+", comps=("PLASMA",))
    r = db.request("AB+", comp="PLASMA")
    assert db.one("SELECT allocate(%s)", (r,)) == 0
    db.stock("AB+", comps=("PLASMA",))
    assert db.one("SELECT allocate(%s)", (r,)) == 1


def test_only_units_at_the_requesting_facility(db):
    dn = db.donate(db.donor("O+"), staff=db.phleb)          # collected at the blood bank, not the hospital
    db.q("SELECT separate_donation(%s, ARRAY['RBC'])", (dn,))
    db.q("SELECT record_tests(%s,%s)", (dn, NEG))
    assert db.one("SELECT allocate(%s)", (db.request("O+"),)) == 0


def test_expired_or_closed_cannot_be_allocated(db):
    db.stock("B+", days_ago=50)                               # RBC shelf life is 42 days
    r = db.request("B+")
    assert db.one("SELECT allocate(%s)", (r,)) == 0
    db.q("SELECT close_request(%s, TRUE)", (r,))
    assert "not open" in db.fails("SELECT allocate(%s)", (r,))


# ---------------------------------------------------------------------- close_request() FR-28
def test_unit_that_expired_while_reserved_is_never_issued(db):
    unit = db.stock("O+")[0]
    r = db.request("O+")
    db.q("SELECT allocate(%s)", (r,))
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (unit,))
    assert db.one("SELECT close_request(%s)", (r,)) == "Partially Fulfilled"
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Expired"
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (unit,)) == "Expired"


def test_close_issues_or_releases(db):
    db.stock("O+", n=2)
    full, cancel = db.request("O+"), db.request("O+")
    db.q("SELECT allocate(%s)", (full,))
    db.q("SELECT allocate(%s)", (cancel,))
    assert db.one("SELECT close_request(%s)", (full,)) == "Fulfilled"
    assert db.one("SELECT close_request(%s, TRUE)", (cancel,)) == "Cancelled"
    statuses = dict(db.q("SELECT request_id, b.status FROM request_unit JOIN blood_unit b USING (unit_id)"))
    assert statuses == {full: "Issued", cancel: "Available"}
    assert "not open" in db.fails("SELECT close_request(%s)", (full,))


def test_partial_fulfilment(db):
    db.stock("O+")
    r = db.request("O+", n=3)
    db.q("SELECT allocate(%s)", (r,))
    assert db.one("SELECT close_request(%s)", (r,)) == "Partially Fulfilled"


# ---------------------------------------------------------------------- expire_units() FR-20 / quarantine_donor() FR-31
def test_nightly_expiry_releases_reservations_and_logs_the_run(db):
    plt = db.stock("A+", comps=("PLT",), days_ago=6)[0]
    held = db.stock("O+")[0]
    r = db.request("O+")
    db.q("SELECT allocate(%s)", (r,))
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (held,))
    assert db.one("SELECT expire_units()") == 2
    assert {u: s for u, s in db.q("SELECT unit_id, status FROM blood_unit WHERE unit_id = ANY(%s)", ([plt, held],))} == \
        {plt: "Expired", held: "Expired"}
    assert db.one("SELECT units_reserved FROM v_request_status WHERE request_id=%s", (r,)) == 0
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (plt,)) == "Expired"
    assert db.one("SELECT count(*) FROM audit_log WHERE action='JOB' AND record_id='expire_units'") == 1


def test_nightly_expiry_includes_units_still_awaiting_tests(db):
    """UC-14: a Collected unit past expiry is expired too, not left in limbo."""
    dn = db.donate(db.donor(), days_ago=6)
    unit = db.one("SELECT separate_donation(%s, ARRAY['PLT'])", (dn,))
    db.q("SELECT expire_units()")
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Expired"


def test_low_hemoglobin_defers_instead_of_donating(db):
    """UC-07 2a: a low reading records a temporary deferral and no donation."""
    d = db.donor(sex="F")
    db.screen(d)
    assert db.one("SELECT record_donation(%s,%s,'WB',450,120)", (d, db.nurse)) is None
    assert db.one("SELECT count(*) FROM donation WHERE donor_id=%s", (d,)) == 0
    assert db.q("SELECT reason_code, end_date - CURRENT_DATE FROM deferral WHERE donor_id=%s", (d,)) == [("LOWHB", 56)]
    ok = db.donor(sex="F")
    db.screen(ok)
    assert db.one("SELECT record_donation(%s,%s,'WB',450,130)", (ok, db.nurse)) is not None


def test_quarantine_pulls_available_and_reserved_units(db):
    d = db.donor("B+")
    dn = db.donate(d, days_ago=10)
    db.q("SELECT separate_donation(%s, ARRAY['RBC','PLASMA'])", (dn,))
    db.q("SELECT record_tests(%s,%s)", (dn, NEG))
    r = db.request("B+", comp="PLASMA")
    db.q("SELECT allocate(%s)", (r,))
    assert db.one("SELECT quarantine_donor(%s)", (d,)) == 2          # RBC Available + plasma Reserved
    assert db.one("SELECT units_reserved FROM v_request_status WHERE request_id=%s", (r,)) == 0
    assert db.one("SELECT is_flagged FROM donor WHERE user_id=%s", (d,))


# ---------------------------------------------------------------------- eligible_donors() FR-25 / SE-07
def test_donor_search_matches_and_hides_contact_details(db):
    ok, deferred, wrong_group, light = db.donor("O-"), db.donor("O-"), db.donor("A+"), db.donor("O-", kg=45)
    db.q("INSERT INTO deferral (donor_id,reason_code,end_date,recorded_by) VALUES (%s,'TRAVEL',CURRENT_DATE+30,%s)", (deferred, db.nurse))
    r = db.request("O-")
    db.cur.execute("SELECT * FROM eligible_donors(%s)", (r,))
    assert [c.name for c in db.cur.description] == ["donor_code", "blood_group", "next_eligible_date"]
    codes = {row[0] for row in db.cur.fetchall()}
    code = lambda d: db.one("SELECT donor_code FROM donor WHERE user_id=%s", (d,))
    assert code(ok) in codes
    assert not {code(deferred), code(wrong_group), code(light)} & codes


# ---------------------------------------------------------------------- accounts FR-01 / FR-04 / SE-05
def test_register_donor_is_unverified_with_generated_code(db):
    d = db.donor("AB-", verified=False)
    code, group, verified = db.q("SELECT donor_code, blood_group, verified_by FROM donor WHERE user_id=%s", (d,))[0]
    assert re.fullmatch(r"D-\d{4,}", code) and (group, verified) == ("AB-", None)
    assert "unique" in db.fails("SELECT register_donor('NURSE@v.ca','x','A','B',NULL,'1990-01-01','F',60,NULL,NULL)")


def test_create_staff_needs_an_active_facility(db):
    db.q("UPDATE facility SET is_active=FALSE WHERE facility_id=%s", (db.fac_b,))
    assert "inactive" in db.fails("SELECT create_staff(%s,'s@v.ca','x','A','B',%s,'Nurse')", (db.admin, db.fac_b))


def test_lockout_after_five_failures_and_reset_on_success(db):
    d = db.donor()
    assert [db.one("SELECT login_attempt(%s, FALSE)", (d,)) for _ in range(5)] == ["failed"] * 4 + ["locked"]
    assert db.one("SELECT login_attempt(%s, TRUE)", (d,)) == "locked"          # right password still refused while locked
    db.q("UPDATE user_account SET locked_until = now() - interval '1 second' WHERE user_id=%s", (d,))
    assert db.one("SELECT login_attempt(%s, FALSE)", (d,)) == "failed"
    assert db.one("SELECT login_attempt(%s, TRUE)", (d,)) == "ok"
    assert db.one("SELECT failed_logins FROM user_account WHERE user_id=%s", (d,)) == 0
    db.q("UPDATE user_account SET is_active=FALSE WHERE user_id=%s", (d,))
    assert db.one("SELECT login_attempt(%s, FALSE)", (d,)) == "failed"        # no hint without the password
    assert db.one("SELECT login_attempt(%s, TRUE)", (d,)) == "inactive"


# ---------------------------------------------------------------------- submit_screening() FR-06 / FR-08 and the eligibility view FR-07
def answers(db, **by_reason):
    out = {}
    for qid, reason, on in db.q("SELECT question_id, reason_code, defer_on_answer FROM screening_question WHERE is_active"):
        out[str(qid)] = by_reason.get(reason, (not on) if reason else False)
    return json.dumps(out)


def test_clean_screening_creates_no_deferral(db):
    d = db.donor()
    db.one("SELECT submit_screening(%s,%s)", (d, answers(db)))
    assert db.one("SELECT count(*) FROM deferral WHERE donor_id=%s", (d,)) == 0


def test_deferring_answer_creates_a_system_deferral(db):
    d = db.donor()
    sid = db.one("SELECT submit_screening(%s,%s)", (d, answers(db, TATTOO=True)))
    assert db.q("SELECT reason_code, end_date - CURRENT_DATE, screening_id FROM deferral WHERE donor_id=%s", (d,)) == [("TATTOO", 90, sid)]
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)).startswith("Not eligible until")


@pytest.mark.parametrize("reason, days", [("MEDS", 30), ("PREG", 180)])
def test_medication_and_pregnancy_questions_defer(db, reason, days):
    d = db.donor(sex="F")
    db.one("SELECT submit_screening(%s,%s)", (d, answers(db, **{reason: True})))
    assert db.q("SELECT reason_code, end_date - CURRENT_DATE FROM deferral WHERE donor_id=%s", (d,)) == [(reason, days)]


def test_unanswered_questions_rejected(db):
    assert "unanswered" in db.fails("SELECT submit_screening(%s,'{\"1\": false}')", (db.donor(),))


@pytest.mark.parametrize("setup, expected", [
    ("", "Eligible"),
    ("UPDATE donor SET is_flagged=TRUE WHERE user_id=%(d)s", "Not eligible (flagged after a failed test)"),
    ("UPDATE donor SET weight_kg=45 WHERE user_id=%(d)s", "Not eligible (under 50 kg)"),
    ("UPDATE user_account SET is_active=FALSE WHERE user_id=%(d)s", "Account inactive"),
    ("INSERT INTO deferral (donor_id,reason_code,recorded_by) VALUES (%(d)s,'HIV',%(s)s)", "Permanently deferred"),
])
def test_eligibility_status_labels(db, setup, expected):
    d = db.donor()
    if setup:
        db.q(setup, {"d": d, "s": db.nurse})
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == expected


def test_under_17_shows_the_birthday(db):
    d = db.one("SELECT register_donor('kid@v.ca','x','A','B',NULL,(CURRENT_DATE - interval '16 years')::date,'F',60,NULL,'O+')")
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == \
        "Not eligible until " + str(db.one("SELECT (CURRENT_DATE + interval '1 year')::date"))


# ---------------------------------------------------------------------- record_tests() FR-13
def test_units_released_only_when_all_tests_are_in(db):
    dn = db.donate(db.donor())
    db.q("SELECT separate_donation(%s, ARRAY['RBC','PLASMA'])", (dn,))
    assert db.one("SELECT record_tests(%s,'{\"HIV\":\"Negative\",\"HBV\":\"Negative\"}')", (dn,)) == "Awaiting 2 test(s)"
    assert db.one("SELECT record_tests(%s,'{\"HCV\":\"Negative\",\"SYPH\":\"Negative\"}')", (dn,)) == "Released"
    assert db.q("SELECT DISTINCT status FROM blood_unit WHERE donation_id=%s", (dn,)) == [("Available",)]


def test_tests_recorded_before_separation_still_release(db):
    dn = db.donate(db.donor())
    db.q("SELECT record_tests(%s,%s)", (dn, NEG))
    unit = db.one("SELECT separate_donation(%s, ARRAY['RBC'])", (dn,))
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Available"


def test_flagged_donor_units_are_never_released(db):
    d = db.donor()
    dn = db.donate(d)
    db.q("SELECT separate_donation(%s, ARRAY['RBC'])", (dn,))
    db.q("SELECT quarantine_donor(%s)", (d,))
    assert "donor is flagged" in db.fails("SELECT record_tests(%s,%s)", (dn, NEG))


def test_positive_result_reports_discarded(db):
    dn = db.donate(db.donor())
    db.q("SELECT separate_donation(%s, ARRAY['RBC'])", (dn,))
    assert db.one("SELECT record_tests(%s,'{\"HIV\":\"Positive\"}')", (dn,)) == "Discarded"
    assert "discarded" in db.fails("SELECT separate_donation(%s, ARRAY['PLASMA'])", (dn,))


# ---------------------------------------------------------------------- send_donation_call() / respond_to_call() FR-26 / FR-27
def test_call_reaches_only_eligible_compatible_donors(db):
    good, bad = db.donor("O-"), db.donor("B+")
    r = db.request("O-")
    call = db.one("SELECT send_donation_call(%s,%s,'O-negative donors needed')", (r, db.nurse))
    assert [d for (d,) in db.q("SELECT donor_id FROM call_recipient WHERE call_id=%s", (call,))] == [good]
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s", (bad,)) == 0
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s", (good,)) == 1


def test_call_with_no_match_is_refused_and_leaves_nothing_behind(db):
    db.donor("A+")
    r = db.request("O-")
    assert "no eligible" in db.fails("SELECT send_donation_call(%s,%s,'x')", (r, db.nurse))
    assert db.one("SELECT count(*) FROM donation_call") == 0


def test_no_call_for_a_closed_request(db):
    db.donor("O-")
    r = db.request("O-")
    db.q("SELECT close_request(%s, TRUE)", (r,))
    assert "not open" in db.fails("SELECT send_donation_call(%s,%s,'x')", (r, db.nurse))


def test_donor_answers_once(db):
    d = db.donor("O-")
    call = db.one("SELECT send_donation_call(%s,%s,'x')", (db.request("O-"), db.nurse))
    db.q("SELECT respond_to_call(%s,%s,TRUE)", (call, d))
    assert db.one("SELECT response FROM call_recipient WHERE call_id=%s", (call,)) == "Accepted"
    assert "no pending call" in db.fails("SELECT respond_to_call(%s,%s,FALSE)", (call, d))


# ---------------------------------------------------------------------- run_nightly_expiry() procedure FR-20
def test_nightly_job_is_a_procedure_that_reports_its_count(db):
    db.stock("A+", comps=("PLT",), days_ago=6)
    assert db.one("SELECT prokind FROM pg_proc WHERE proname = 'run_nightly_expiry'") == "p"
    assert db.one("CALL run_nightly_expiry()") == 1
    db.q("SET LOCAL ROLE vytal_app")                                      # the scheduler's least-privilege login can run it
    db.q("SET LOCAL search_path = public")                                # cron connects without the vytal search_path
    assert db.one("CALL vytal.run_nightly_expiry()") == 0


# ---------------------------------------------------------------------- quarantine re-allocation UC-22 3a
def test_quarantine_reallocates_the_open_request_it_robbed(db):
    d = db.donor("B+")
    dn = db.donate(d, days_ago=10)
    pulled = db.one("SELECT separate_donation(%s, ARRAY['PLASMA'])", (dn,))
    db.q("SELECT record_tests(%s,%s)", (dn, NEG))
    r = db.request("B+", comp="PLASMA")
    db.q("SELECT allocate(%s)", (r,))
    assert db.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (r,)) == pulled
    spare = db.stock("B+", comps=("PLASMA",), days_ago=2)[0]                # arrives after the reservation was made
    db.q("SELECT quarantine_donor(%s)", (d,))
    assert db.q("SELECT unit_id FROM request_unit WHERE request_id=%s AND released_at IS NULL", (r,)) == [(spare,)]
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (pulled,)) == "Quarantined"


# ---------------------------------------------------------------------- record_deferral() UC-06 / FR-08
def test_staff_record_temporary_and_permanent_deferrals(db):
    d = db.donor()
    db.one("SELECT record_deferral(%s,%s,'TRAVEL')", (db.nurse, d))
    assert db.one("SELECT end_date - CURRENT_DATE FROM deferral WHERE donor_id=%s", (d,)) == 90   # reason's default length
    db.one("SELECT record_deferral(%s,%s,'TATTOO',CURRENT_DATE + 120)", (db.nurse, d))
    assert db.one("SELECT next_eligible_date - CURRENT_DATE FROM donor WHERE user_id=%s", (d,)) == 120
    db.one("SELECT record_deferral(%s,%s,'HIV')", (db.nurse, d))
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == "Permanently deferred"


@pytest.mark.parametrize("actor, reason, end, needle", [
    ("donor", "TRAVEL", None, "staff access only"),
    ("admin", "TRAVEL", None, "staff access only"),
    ("nurse", "NOPE", None, "unknown deferral reason"),
    ("nurse", "HIV", "2030-01-01", "permanent deferral reason cannot have an end date"),
])
def test_record_deferral_refusals(db, actor, reason, end, needle):
    d = db.donor()
    who = {"donor": d, "admin": db.admin, "nurse": db.nurse}[actor]
    assert needle in db.fails("SELECT record_deferral(%s,%s,%s,%s)", (who, d, reason, end))


# ---------------------------------------------------------------------- discard_unit() UC-15 / FR-16
def test_staff_discard_an_available_unit_with_a_reason(db):
    u = db.stock("O+")[0]
    db.q("SELECT discard_unit(%s,%s,'Damaged bag')", (db.nurse, u))
    assert db.q("SELECT status, status_reason FROM blood_unit WHERE unit_id=%s", (u,)) == [("Discarded", "Damaged bag")]
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (u,)) == "Discarded"


def test_staff_cannot_discard_a_collected_unit(db):
    """FR-16 / UC-15: staff discard Available units; Collected -> Discarded happens only on a failed test (UC-09)."""
    u = db.one("SELECT separate_donation(%s, ARRAY['RBC'])", (db.donate(db.donor()),))
    assert "only available units" in db.fails("SELECT discard_unit(%s,%s,'Temperature breach')", (db.nurse, u))
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (u,)) == "Collected"


def test_discard_refusals(db):
    units = db.stock("O+", n=2)
    r = db.request("O+")
    db.q("SELECT allocate(%s)", (r,))
    reserved = db.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (r,))
    other = next(u for u in units if u != reserved)
    assert "release unit" in db.fails("SELECT discard_unit(%s,%s,'x')", (db.nurse, reserved))
    assert "another facility" in db.fails("SELECT discard_unit(%s,%s,'x')", (db.phleb, other))
    assert "staff or admin access only" in db.fails("SELECT discard_unit(%s,%s,'x')", (db.donor(), other))
    assert "quarantined units only" in db.fails("SELECT discard_unit(%s,%s,'x')", (db.admin, other))
    assert "not found" in db.fails("SELECT discard_unit(%s,'U0-RBC','x')", (db.nurse,))


def test_quarantined_units_are_discarded_by_an_admin_only(db):
    u = db.stock("O+")[0]
    db.q("SELECT quarantine_donor((SELECT donor_id FROM v_blood_unit WHERE unit_id=%s))", (u,))
    assert "only available units" in db.fails("SELECT discard_unit(%s,%s,'x')", (db.nurse, u))
    db.q("SELECT discard_unit(%s,%s,'Lookback confirmed')", (db.admin, u))
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (u,)) == "Discarded"


# ---------------------------------------------------------------------- set_threshold() UC-12 / FR-18
def test_staff_set_their_own_thresholds_and_stock_is_rechecked(db):
    db.q("SELECT set_threshold(%s,%s,'AB-','RBC',2)", (db.nurse, db.fac_h))
    assert db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group='AB-' AND cleared_at IS NULL", (db.fac_h,)) == 1
    db.q("SELECT set_threshold(%s,%s,'AB-','RBC',0)", (db.nurse, db.fac_h))             # same key updates, alert clears
    assert db.q("SELECT min_units FROM stock_threshold WHERE facility_id=%s AND blood_group='AB-'", (db.fac_h,)) == [(0,)]
    assert db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group='AB-' AND cleared_at IS NULL", (db.fac_h,)) == 0
    db.q("SELECT set_threshold(%s,%s,'O-','PLT',1)", (db.admin, db.fac_b))               # an Admin may set any facility
    assert db.one("SELECT min_units FROM stock_threshold WHERE facility_id=%s AND blood_group='O-'", (db.fac_b,)) == 1


def test_threshold_refusals(db):
    assert "own facility" in db.fails("SELECT set_threshold(%s,%s,'O-','RBC',1)", (db.nurse, db.fac_b))
    assert "staff or admin access only" in db.fails("SELECT set_threshold(%s,%s,'O-','RBC',1)", (db.donor(), db.fac_h))
    assert "check" in db.fails("SELECT set_threshold(%s,%s,'O-','RBC',-1)", (db.nurse, db.fac_h))


# ---------------------------------------------------------------------- accounts and facilities UC-23
def test_admin_creates_admins_and_staff_others_cannot(db):
    a = db.one("SELECT create_admin(%s,'New.Admin@v.ca','x','N','A')", (db.admin,))
    assert db.q("SELECT email, role, created_by FROM user_account WHERE user_id=%s", (a,)) == [("new.admin@v.ca", "Admin", db.admin)]
    assert db.one("SELECT count(*) FROM admin WHERE user_id=%s", (a,)) == 1
    assert "admin access only" in db.fails("SELECT create_admin(%s,'x@v.ca','x','N','A')", (db.nurse,))
    assert "admin access only" in db.fails("SELECT create_staff(%s,'s2@v.ca','x','A','B',%s,'Nurse')", (db.nurse, db.fac_h))


def test_deactivate_and_reactivate_an_account(db):
    d = db.donor()
    for _ in range(5):
        db.q("SELECT login_attempt(%s, FALSE)", (d,))
    db.q("SELECT set_account_active(%s,%s,FALSE)", (db.admin, d))
    assert db.one("SELECT login_attempt(%s, TRUE)", (d,)) == "inactive"
    db.q("SELECT set_account_active(%s,%s,TRUE)", (db.admin, d))                       # reactivating clears the lockout
    assert db.one("SELECT login_attempt(%s, TRUE)", (d,)) == "ok"
    assert "admin access only" in db.fails("SELECT set_account_active(%s,%s,FALSE)", (db.nurse, d))
    assert "not found" in db.fails("SELECT set_account_active(%s,0,FALSE)", (db.admin,))


def test_facility_with_open_requests_cannot_be_deactivated(db):
    r = db.request("O+")
    assert "open requests" in db.fails("SELECT set_facility_active(%s,%s,FALSE)", (db.admin, db.fac_h))
    db.q("SELECT close_request(%s, TRUE)", (r,))
    db.q("SELECT set_facility_active(%s,%s,FALSE)", (db.admin, db.fac_h))
    assert db.one("SELECT is_active FROM facility WHERE facility_id=%s", (db.fac_h,)) is False
    assert "admin access only" in db.fails("SELECT set_facility_active(%s,%s,TRUE)", (db.nurse, db.fac_h))


def test_save_facility_creates_and_edits(db):
    f = db.one("SELECT save_facility(%s,NULL,'Essex County Hospital','Hospital','9 Essex Rd',NULL,NULL)", (db.admin,))
    assert db.one("SELECT save_facility(%s,%s,'Essex County Hospital','Hospital','10 Essex Rd','519-555-0199',NULL)",
                  (db.admin, f)) == f
    assert db.one("SELECT address FROM facility WHERE facility_id=%s", (f,)) == "10 Essex Rd"
    assert "not found" in db.fails("SELECT save_facility(%s,0,'X','Hospital','1 St',NULL,NULL)", (db.admin,))
    assert "admin access only" in db.fails("SELECT save_facility(%s,NULL,'Y','Hospital','1 St',NULL,NULL)", (db.nurse,))


# ---------------------------------------------------------------------- update_donor_profile() UC-03 / FR-03
def test_donor_updates_their_profile_and_it_is_audited(db):
    d = db.donor(kg=70)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    db.q("SELECT update_donor_profile(%s,'519-555-0101','New@Mail.ca','2 Main St',48)", (d,))
    assert db.q("SELECT a.phone, a.email, o.address FROM user_account a JOIN donor o USING (user_id) WHERE user_id=%s", (d,)) == \
        [("519-555-0101", "new@mail.ca", "2 Main St")]
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == "Not eligible (under 50 kg)"
    assert {f for (f,) in db.q("SELECT field_name FROM audit_log WHERE user_id=%s", (d,))} >= {"phone", "email", "address", "weight_kg"}
    assert "donor access only" in db.fails("SELECT update_donor_profile(%s,NULL,'n@v.ca',NULL,70)", (db.nurse,))


# ---------------------------------------------------------------------- UC-04 precondition / UC-20 responses
def test_permanently_deferred_donor_cannot_screen(db):
    d = db.donor()
    db.q("SELECT record_deferral(%s,%s,'HBV')", (db.nurse, d))
    assert "permanently deferred" in db.fails("SELECT submit_screening(%s,%s)", (d, answers(db)))


def test_call_responses_accept_decline_and_closed(db):
    db.q("UPDATE facility SET phone = '519-555-0111' WHERE facility_id=%s", (db.fac_h,))
    yes, no, late = db.donor("O-"), db.donor("O-"), db.donor("O-")
    r = db.request("O-")
    call = db.one("SELECT send_donation_call(%s,%s,'x')", (r, db.nurse))
    assert db.one("SELECT respond_to_call(%s,%s,TRUE)", (call, yes)) == "Riverside General, 1 River Rd, 519-555-0111"
    assert db.one("SELECT respond_to_call(%s,%s,FALSE)", (call, no)) == "declined"
    db.q("SELECT close_request(%s, TRUE)", (r,))
    assert db.one("SELECT respond_to_call(%s,%s,TRUE)", (call, late)) == "closed"
    assert db.one("SELECT response FROM call_recipient WHERE call_id=%s AND donor_id=%s", (call, late)) == "Pending"
    assert "no pending call" in db.fails("SELECT respond_to_call(0,%s,TRUE)", (yes,))
