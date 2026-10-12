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
