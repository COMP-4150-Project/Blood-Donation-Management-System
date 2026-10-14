"""Stored procedures: each test drives one procedure through its normal path and its refusals."""
import datetime as dt
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
    assert db.one("CALL allocate(%s)", (r,)) == 1
    assert db.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (r,)) == old
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (new,)) == "Available"


def test_o_negative_conserved_for_routine_requests(db):
    db.stock("O-", days_ago=30)
    db.stock("O+", days_ago=2)
    r = db.request("A+")
    db.q("CALL allocate(%s)", (r,))
    assert db.groups_of(r) == ["O+"]


def test_o_negative_covers_only_what_other_stock_cannot(db):
    db.stock("O-", n=3)
    db.stock("A+")
    r = db.request("A+", n=3, urgency="Urgent")
    assert db.one("CALL allocate(%s)", (r,)) == 3
    assert db.groups_of(r) == ["A+", "O-", "O-"]


def test_o_negative_used_when_nothing_else_fits(db):
    db.stock("O-")
    db.stock("A+")                                   # incompatible with an A- patient
    r = db.request("A-")
    db.q("CALL allocate(%s)", (r,))
    assert db.groups_of(r) == ["O-"]


def test_unknown_group_emergency_gets_only_o_negative_and_records_shortfall(db):
    db.stock("O-", n=3)
    db.stock("O+", n=3)
    r = db.request(None, n=4, urgency="Emergency")
    assert db.one("CALL allocate(%s)", (r,)) == 3
    assert db.groups_of(r) == ["O-"] * 3
    assert db.q("SELECT units_reserved, shortfall FROM v_request_status WHERE request_id=%s", (r,)) == [(3, 1)]


def test_plasma_compatibility_is_reversed(db):
    db.stock("O+", comps=("PLASMA",))
    r = db.request("AB+", comp="PLASMA")
    assert db.one("CALL allocate(%s)", (r,)) == 0
    db.stock("AB+", comps=("PLASMA",))
    assert db.one("CALL allocate(%s)", (r,)) == 1


def test_only_units_at_the_requesting_facility(db):
    dn = db.donate(db.donor("O+"), staff=db.phleb)          # collected at the blood bank, not the hospital
    db.q("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))
    db.q("CALL record_tests(%s,%s)", (dn, NEG))
    assert db.one("CALL allocate(%s)", (db.request("O+"),)) == 0


def test_expired_or_closed_cannot_be_allocated(db):
    db.stock("B+", days_ago=50)                               # RBC shelf life is 42 days
    r = db.request("B+")
    assert db.one("CALL allocate(%s)", (r,)) == 0
    db.q("CALL close_request(%s, TRUE)", (r,))
    assert "not open" in db.fails("CALL allocate(%s)", (r,))


# ---------------------------------------------------------------------- close_request() FR-28
def test_unit_that_expired_while_reserved_is_never_issued(db):
    unit = db.stock("O+")[0]
    r = db.request("O+")
    db.q("CALL allocate(%s)", (r,))
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (unit,))
    assert db.one("CALL close_request(%s)", (r,)) == "Partially Fulfilled"
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Expired"
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (unit,)) == "Expired"


def test_close_issues_or_releases(db):
    db.stock("O+", n=2)
    full, cancel = db.request("O+"), db.request("O+")
    db.q("CALL allocate(%s)", (full,))
    db.q("CALL allocate(%s)", (cancel,))
    assert db.one("CALL close_request(%s)", (full,)) == "Fulfilled"
    assert db.one("CALL close_request(%s, TRUE)", (cancel,)) == "Cancelled"
    statuses = dict(db.q("SELECT request_id, b.status FROM request_unit JOIN blood_unit b USING (unit_id)"))
    assert statuses == {full: "Issued", cancel: "Available"}
    assert "not open" in db.fails("CALL close_request(%s)", (full,))


def test_partial_fulfilment(db):
    db.stock("O+")
    r = db.request("O+", n=3)
    db.q("CALL allocate(%s)", (r,))
    assert db.one("CALL close_request(%s)", (r,)) == "Partially Fulfilled"


# ---------------------------------------------------------------------- run_nightly_expiry() FR-20 / quarantine_donor() FR-31
def test_nightly_expiry_releases_reservations_and_logs_the_run(db):
    plt = db.stock("A+", comps=("PLT",), days_ago=6)[0]
    held = db.stock("O+")[0]
    r = db.request("O+")
    db.q("CALL allocate(%s)", (r,))
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (held,))
    assert db.one("CALL run_nightly_expiry()") == 2
    assert {u: s for u, s in db.q("SELECT unit_id, status FROM blood_unit WHERE unit_id = ANY(%s)", ([plt, held],))} == \
        {plt: "Expired", held: "Expired"}
    assert db.one("SELECT units_reserved FROM v_request_status WHERE request_id=%s", (r,)) == 0
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (plt,)) == "Expired"
    assert db.one("SELECT count(*) FROM audit_log WHERE action='JOB' AND record_id='run_nightly_expiry'") == 1


def test_nightly_expiry_includes_units_still_awaiting_tests(db):
    """UC-14: a Collected unit past expiry is expired too, not left in limbo."""
    dn = db.donate(db.donor(), days_ago=6)
    unit = db.one("CALL separate_donation(%s, ARRAY['PLT'])", (dn,))[0]
    db.q("CALL run_nightly_expiry()")
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Expired"


def test_low_hemoglobin_defers_instead_of_donating(db):
    """UC-07 2a: a low reading records a temporary deferral and no donation."""
    d = db.donor(sex="F")
    db.screen(d)
    assert db.one("CALL record_donation(%s,%s,'WB',450,120)", (d, db.nurse)) is None
    assert db.one("SELECT count(*) FROM donation WHERE donor_id=%s", (d,)) == 0
    assert db.q("SELECT reason_code, end_date - CURRENT_DATE FROM deferral WHERE donor_id=%s", (d,)) == [("LOWHB", 56)]
    ok = db.donor(sex="F")
    db.screen(ok)
    assert db.one("CALL record_donation(%s,%s,'WB',450,130)", (ok, db.nurse)) is not None


def test_quarantine_pulls_available_and_reserved_units(db):
    d = db.donor("B+")
    dn = db.donate(d, days_ago=10)
    db.q("CALL separate_donation(%s, ARRAY['RBC','PLASMA'])", (dn,))
    db.q("CALL record_tests(%s,%s)", (dn, NEG))
    r = db.request("B+", comp="PLASMA")
    db.q("CALL allocate(%s)", (r,))
    assert db.one("CALL quarantine_donor(%s)", (d,)) == 2          # RBC Available + plasma Reserved
    assert db.one("SELECT units_reserved FROM v_request_status WHERE request_id=%s", (r,)) == 0
    assert db.one("SELECT is_flagged FROM donor WHERE user_id=%s", (d,))


# ---------------------------------------------------------------------- eligible_donors() FR-25 / SE-07
def test_donor_search_matches_and_hides_contact_details(db):
    ok, deferred, wrong_group, light = (db.donor(g, kg=kg, screened=True) for g, kg in (("O-", 70), ("O-", 70), ("A+", 70), ("O-", 45)))
    unscreened = db.donor("O-")                                                           # UC-05 2a: no screening on record
    db.q("INSERT INTO deferral (donor_id,reason_code,end_date,recorded_by) VALUES (%s,'TRAVEL',CURRENT_DATE+30,%s)", (deferred, db.nurse))
    r = db.request("O-")
    db.cur.execute("SELECT * FROM eligible_donors(%s)", (r,))
    assert [c.name for c in db.cur.description] == ["donor_code", "blood_group", "next_eligible_date"]
    codes = {row[0] for row in db.cur.fetchall()}
    code = lambda d: db.one("SELECT donor_code FROM donor WHERE user_id=%s", (d,))
    assert code(ok) in codes
    assert not {code(deferred), code(wrong_group), code(light), code(unscreened)} & codes


# ---------------------------------------------------------------------- accounts FR-01 / FR-04 / SE-05
def test_register_donor_is_unverified_with_generated_code(db):
    d = db.donor("AB-", verified=False)
    code, group, verified = db.q("SELECT donor_code, blood_group, verified_by FROM donor WHERE user_id=%s", (d,))[0]
    assert re.fullmatch(r"D-\d{4,}", code) and (group, verified) == ("AB-", None)
    assert "unique" in db.fails("CALL register_donor('NURSE@v.ca','x','A','B',NULL,'1990-01-01','F',60,NULL,NULL)")


def test_create_staff_needs_an_active_facility(db):
    db.q("UPDATE facility SET is_active=FALSE WHERE facility_id=%s", (db.fac_b,))
    assert "inactive" in db.fails("CALL create_staff(%s,'s@v.ca','x','A','B',%s,'Nurse')", (db.admin, db.fac_b))


def test_lockout_after_five_failures_and_reset_on_success(db):
    d = db.donor()
    assert [db.one("CALL login_attempt(%s, FALSE)", (d,)) for _ in range(5)] == ["failed"] * 4 + ["locked"]
    assert db.one("CALL login_attempt(%s, TRUE)", (d,)) == "locked"          # right password still refused while locked
    db.q("UPDATE user_account SET locked_until = now() - interval '1 second' WHERE user_id=%s", (d,))
    assert db.one("CALL login_attempt(%s, FALSE)", (d,)) == "failed"
    assert db.one("CALL login_attempt(%s, TRUE)", (d,)) == "ok"
    assert db.one("SELECT failed_logins FROM user_account WHERE user_id=%s", (d,)) == 0
    db.q("UPDATE user_account SET is_active=FALSE WHERE user_id=%s", (d,))
    assert db.one("CALL login_attempt(%s, FALSE)", (d,)) == "failed"        # no hint without the password
    assert db.one("CALL login_attempt(%s, TRUE)", (d,)) == "inactive"


# ---------------------------------------------------------------------- submit_screening() FR-06 / FR-08 and the eligibility view FR-07
def answers(db, **by_reason):
    out = {}
    for qid, reason, on in db.q("SELECT question_id, reason_code, defer_on_answer FROM screening_question WHERE is_active"):
        out[str(qid)] = by_reason.get(reason, (not on) if reason else False)
    return json.dumps(out)


def test_clean_screening_creates_no_deferral(db):
    d = db.donor()
    db.one("CALL submit_screening(%s,%s)", (d, answers(db)))
    assert db.one("SELECT count(*) FROM deferral WHERE donor_id=%s", (d,)) == 0


def test_deferring_answer_creates_a_system_deferral(db):
    d = db.donor()
    sid = db.one("CALL submit_screening(%s,%s)", (d, answers(db, TATTOO=True)))
    assert db.q("SELECT reason_code, end_date - CURRENT_DATE, screening_id FROM deferral WHERE donor_id=%s", (d,)) == [("TATTOO", 90, sid)]
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)).startswith("Not eligible until")


@pytest.mark.parametrize("reason, days", [("MEDS", 30), ("PREG", 180)])
def test_medication_and_pregnancy_questions_defer(db, reason, days):
    d = db.donor(sex="F")
    db.one("CALL submit_screening(%s,%s)", (d, answers(db, **{reason: True})))
    assert db.q("SELECT reason_code, end_date - CURRENT_DATE FROM deferral WHERE donor_id=%s", (d,)) == [(reason, days)]


def test_unanswered_questions_rejected(db):
    assert "unanswered" in db.fails("CALL submit_screening(%s,'{\"1\": false}')", (db.donor(),))


@pytest.mark.parametrize("setup, expected", [
    ("", "Eligible"),
    ("UPDATE donor SET is_flagged=TRUE WHERE user_id=%(d)s", "Not eligible (flagged after a failed test)"),
    ("UPDATE donor SET weight_kg=45 WHERE user_id=%(d)s", "Not eligible (under 50 kg)"),
    ("UPDATE user_account SET is_active=FALSE WHERE user_id=%(d)s", "Account inactive"),
    ("INSERT INTO deferral (donor_id,reason_code,recorded_by) VALUES (%(d)s,'HIV',%(s)s)", "Permanently deferred"),
])
def test_eligibility_status_labels(db, setup, expected):
    d = db.donor(screened=True)
    if setup:
        db.q(setup, {"d": d, "s": db.nurse})
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == expected


def test_under_the_minimum_age_shows_the_birthday(db):
    """A 17-year-old who registered before an Admin raised the minimum to 18 sees the date they become eligible."""
    d = db.one("CALL register_donor('kid@v.ca','x','A','B',NULL,(CURRENT_DATE - interval '17 years')::date,'F',60,NULL,'O+')")
    db.q("UPDATE eligibility_rule SET min_age_years = 18")
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == \
        "Not eligible until " + str(db.one("SELECT (CURRENT_DATE + interval '1 year')::date"))


@pytest.mark.parametrize("years, refused", [(15, True), (16, True), (17, False)])
def test_registration_refuses_donors_under_the_minimum_age(db, years, refused):
    """UC-01 2b / BR-01: the minimum age comes from eligibility_rule; exactly 17 today is accepted."""
    sql = ("CALL register_donor(%s,'x','A','B',NULL,(CURRENT_DATE - make_interval(years => %s))::date,'M',70,NULL,'O+')")
    msg = db.fails(sql, (f"age{years}@v.ca", years))
    assert ("at least 17 years old" in msg) if refused else msg == ""


# ---------------------------------------------------------------------- record_tests() FR-13
def test_units_released_only_when_all_tests_are_in(db):
    dn = db.donate(db.donor())
    db.q("CALL separate_donation(%s, ARRAY['RBC','PLASMA'])", (dn,))
    assert db.one("CALL record_tests(%s,'{\"HIV\":\"Negative\",\"HBV\":\"Negative\"}')", (dn,)) == "Awaiting 2 test(s)"
    assert db.one("CALL record_tests(%s,'{\"HCV\":\"Negative\",\"SYPH\":\"Negative\"}')", (dn,)) == "Released"
    assert db.q("SELECT DISTINCT status FROM blood_unit WHERE donation_id=%s", (dn,)) == [("Available",)]


def test_tests_recorded_before_separation_still_release(db):
    dn = db.donate(db.donor())
    db.q("CALL record_tests(%s,%s)", (dn, NEG))
    unit = db.one("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))[0]
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Available"


def test_flagged_donor_units_are_never_released(db):
    d = db.donor()
    dn = db.donate(d)
    db.q("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))
    db.q("CALL quarantine_donor(%s)", (d,))
    assert "donor is flagged" in db.fails("CALL record_tests(%s,%s)", (dn, NEG))


def test_positive_result_reports_discarded(db):
    dn = db.donate(db.donor())
    db.q("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))
    assert db.one("CALL record_tests(%s,'{\"HIV\":\"Positive\"}')", (dn,)) == "Discarded"
    assert "discarded" in db.fails("CALL separate_donation(%s, ARRAY['PLASMA'])", (dn,))


# ---------------------------------------------------------------------- send_donation_call() / respond_to_call() FR-26 / FR-27
def eligibility(db, d, on=None):
    return db.q("SELECT status, eligible_from, reason FROM donor_eligibility(%s, coalesce(%s::date, CURRENT_DATE))", (d, on))[0]


def test_eligibility_function_answers_for_any_date(db):
    """FR-07: the eligibility function takes the date to check; a temporary deferral blocks today but not after it ends."""
    d = db.donor(screened=True)
    db.q("CALL record_deferral(%s,%s,'TATTOO')", (db.nurse, d))
    end = db.one("SELECT end_date FROM deferral WHERE donor_id=%s", (d,))
    assert eligibility(db, d) == (f"Not eligible until {end}", end, "Tattoo or piercing in the last 3 months")
    assert eligibility(db, d, end) == ("Eligible", None, None)
    assert eligibility(db, d, end - dt.timedelta(days=1))[0] == f"Not eligible until {end}"


def test_no_screening_on_record_asks_for_one(db):
    """UC-05 2a: an otherwise eligible donor with no screening is asked to complete one; after screening they are Eligible."""
    d = db.donor()
    assert eligibility(db, d) == ("Complete screening to confirm eligibility", None, "No pre-donation screening on record")
    db.one("CALL submit_screening(%s,%s)", (d, answers(db)))
    assert eligibility(db, d) == ("Eligible", None, None)


def test_temporary_deferral_shows_its_end_date_and_reason(db):
    """FR-07: 'Not eligible until a date' with the reason; eligible again on that date (Scenario 2)."""
    d = db.donor()
    db.one("CALL submit_screening(%s,%s)", (d, answers(db, TATTOO=True)))
    until = db.one("SELECT CURRENT_DATE + 90")
    assert eligibility(db, d) == (f"Not eligible until {until}", until, "Tattoo or piercing in the last 3 months")
    assert eligibility(db, d, until)[0] == "Eligible"


def test_permanent_deferral_and_interval_reasons(db):
    perm = db.donor(screened=True)
    db.q("INSERT INTO deferral (donor_id,reason_code,recorded_by) VALUES (%s,'HIV',%s)", (perm, db.nurse))
    assert eligibility(db, perm) == ("Permanently deferred", None, "HIV positive")
    gave = db.donor(screened=True)
    db.donate(gave)
    assert eligibility(db, gave)[2] == "Minimum interval since the last donation"
    kid = db.one("CALL register_donor('kid2@v.ca','x','A','B',NULL,(CURRENT_DATE - interval '17 years')::date,'F',60,NULL,'O+')")
    db.q("UPDATE eligibility_rule SET min_age_years = 18")          # an Admin raises the minimum (MA-01)
    assert eligibility(db, kid)[2] == "Donors must be at least 18"


COMMANDS = ["allocate", "close_request", "quarantine_donor", "run_nightly_expiry", "register_donor", "create_staff",
            "create_admin", "login_attempt", "submit_screening", "record_donation", "separate_donation", "release_tested",
            "record_tests", "send_donation_call", "respond_to_call", "record_deferral", "discard_unit", "set_threshold",
            "set_account_active", "set_facility_active", "save_facility", "update_donor_profile",
            "verify_blood_group", "submit_request", "confirm_request_group", "set_compatibility", "set_shelf_life",
            "set_donation_interval", "save_deferral_reason", "set_eligibility_rule", "mark_notification_read"]


def test_commands_are_procedures_and_calculations_are_functions(db):
    """Every command that changes data is a stored procedure (CALL); functions are only calculations and helpers."""
    kinds = dict(db.q("SELECT p.proname, p.prokind FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                      "WHERE n.nspname = 'vytal' AND p.prorettype IS DISTINCT FROM 'trigger'::regtype"))
    assert {c: kinds.get(c) for c in COMMANDS} == {c: "p" for c in COMMANDS}
    assert {f: kinds.get(f) for f in ("donor_eligibility", "eligible_donors", "require_role", "require_session_user")} == \
        {f: "f" for f in ("donor_eligibility", "eligible_donors", "require_role", "require_session_user")}


def test_call_reaches_only_eligible_compatible_donors(db):
    good, bad, unscreened = db.donor("O-", screened=True), db.donor("B+", screened=True), db.donor("O-")
    r = db.request("O-")
    call = db.one("CALL send_donation_call(%s,%s,'O-negative donors needed')", (r, db.nurse))
    assert [d for (d,) in db.q("SELECT donor_id FROM call_recipient WHERE call_id=%s", (call,))] == [good]
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s", (bad,)) == 0
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s", (unscreened,)) == 0
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s", (good,)) == 1


def test_call_with_no_match_is_refused_and_leaves_nothing_behind(db):
    db.donor("A+")
    r = db.request("O-")
    assert "no eligible" in db.fails("CALL send_donation_call(%s,%s,'x')", (r, db.nurse))
    assert db.one("SELECT count(*) FROM donation_call") == 0


def test_no_call_for_a_closed_request(db):
    db.donor("O-")
    r = db.request("O-")
    db.q("CALL close_request(%s, TRUE)", (r,))
    assert "not open" in db.fails("CALL send_donation_call(%s,%s,'x')", (r, db.nurse))


def test_donor_answers_once(db):
    d = db.donor("O-", screened=True)
    call = db.one("CALL send_donation_call(%s,%s,'x')", (db.request("O-"), db.nurse))
    db.q("CALL respond_to_call(%s,%s,TRUE)", (call, d))
    assert db.one("SELECT response FROM call_recipient WHERE call_id=%s", (call,)) == "Accepted"
    assert "no pending call" in db.fails("CALL respond_to_call(%s,%s,FALSE)", (call, d))


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
    pulled = db.one("CALL separate_donation(%s, ARRAY['PLASMA'])", (dn,))[0]
    db.q("CALL record_tests(%s,%s)", (dn, NEG))
    r = db.request("B+", comp="PLASMA")
    db.q("CALL allocate(%s)", (r,))
    assert db.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (r,)) == pulled
    spare = db.stock("B+", comps=("PLASMA",), days_ago=2)[0]                # arrives after the reservation was made
    db.q("CALL quarantine_donor(%s)", (d,))
    assert db.q("SELECT unit_id FROM request_unit WHERE request_id=%s AND released_at IS NULL", (r,)) == [(spare,)]
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (pulled,)) == "Quarantined"


# ---------------------------------------------------------------------- record_deferral() UC-06 / FR-08
def test_staff_record_temporary_and_permanent_deferrals(db):
    d = db.donor()
    db.one("CALL record_deferral(%s,%s,'TRAVEL')", (db.nurse, d))
    assert db.one("SELECT end_date - CURRENT_DATE FROM deferral WHERE donor_id=%s", (d,)) == 90   # reason's default length
    db.one("CALL record_deferral(%s,%s,'TATTOO',CURRENT_DATE + 120)", (db.nurse, d))
    assert db.one("SELECT next_eligible_date - CURRENT_DATE FROM donor WHERE user_id=%s", (d,)) == 120
    db.one("CALL record_deferral(%s,%s,'HIV')", (db.nurse, d))
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
    assert needle in db.fails("CALL record_deferral(%s,%s,%s,%s)", (who, d, reason, end))


# ---------------------------------------------------------------------- discard_unit() UC-15 / FR-16
def test_staff_discard_an_available_unit_with_a_reason(db):
    u = db.stock("O+")[0]
    db.q("CALL discard_unit(%s,%s,'Damaged bag')", (db.nurse, u))
    assert db.q("SELECT status, status_reason FROM blood_unit WHERE unit_id=%s", (u,)) == [("Discarded", "Damaged bag")]
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (u,)) == "Discarded"


def test_staff_cannot_discard_a_collected_unit(db):
    """FR-16 / UC-15: staff discard Available units; Collected -> Discarded happens only on a failed test (UC-09)."""
    u = db.one("CALL separate_donation(%s, ARRAY['RBC'])", (db.donate(db.donor()),))[0]
    assert "only available units" in db.fails("CALL discard_unit(%s,%s,'Temperature breach')", (db.nurse, u))
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (u,)) == "Collected"


def test_discard_refusals(db):
    units = db.stock("O+", n=2)
    r = db.request("O+")
    db.q("CALL allocate(%s)", (r,))
    reserved = db.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (r,))
    other = next(u for u in units if u != reserved)
    assert "release unit" in db.fails("CALL discard_unit(%s,%s,'x')", (db.nurse, reserved))
    assert "another facility" in db.fails("CALL discard_unit(%s,%s,'x')", (db.phleb, other))
    assert "staff or admin access only" in db.fails("CALL discard_unit(%s,%s,'x')", (db.donor(), other))
    assert "quarantined units only" in db.fails("CALL discard_unit(%s,%s,'x')", (db.admin, other))
    assert "not found" in db.fails("CALL discard_unit(%s,'U0-RBC','x')", (db.nurse,))


def test_quarantined_units_are_discarded_by_an_admin_only(db):
    u = db.stock("O+")[0]
    db.q("CALL quarantine_donor(%s)", (db.one("SELECT donor_id FROM v_blood_unit WHERE unit_id=%s", (u,)),))
    assert "only available units" in db.fails("CALL discard_unit(%s,%s,'x')", (db.nurse, u))
    db.q("CALL discard_unit(%s,%s,'Lookback confirmed')", (db.admin, u))
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (u,)) == "Discarded"


# ---------------------------------------------------------------------- set_threshold() UC-12 / FR-18
def test_staff_set_their_own_thresholds_and_stock_is_rechecked(db):
    db.q("CALL set_threshold(%s,%s,'AB-','RBC',2)", (db.nurse, db.fac_h))
    assert db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group='AB-' AND cleared_at IS NULL", (db.fac_h,)) == 1
    db.q("CALL set_threshold(%s,%s,'AB-','RBC',0)", (db.nurse, db.fac_h))             # same key updates, alert clears
    assert db.q("SELECT min_units FROM stock_threshold WHERE facility_id=%s AND blood_group='AB-'", (db.fac_h,)) == [(0,)]
    assert db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group='AB-' AND cleared_at IS NULL", (db.fac_h,)) == 0
    db.q("CALL set_threshold(%s,%s,'O-','PLT',1)", (db.admin, db.fac_b))               # an Admin may set any facility
    assert db.one("SELECT min_units FROM stock_threshold WHERE facility_id=%s AND blood_group='O-'", (db.fac_b,)) == 1


def test_threshold_refusals(db):
    assert "own facility" in db.fails("CALL set_threshold(%s,%s,'O-','RBC',1)", (db.nurse, db.fac_b))
    assert "staff or admin access only" in db.fails("CALL set_threshold(%s,%s,'O-','RBC',1)", (db.donor(), db.fac_h))
    assert "check" in db.fails("CALL set_threshold(%s,%s,'O-','RBC',-1)", (db.nurse, db.fac_h))


# ---------------------------------------------------------------------- accounts and facilities UC-23
def test_admin_creates_admins_and_staff_others_cannot(db):
    a = db.one("CALL create_admin(%s,'New.Admin@v.ca','x','N','A')", (db.admin,))
    assert db.q("SELECT email, role, created_by FROM user_account WHERE user_id=%s", (a,)) == [("new.admin@v.ca", "Admin", db.admin)]
    assert db.one("SELECT count(*) FROM admin WHERE user_id=%s", (a,)) == 1
    assert "admin access only" in db.fails("CALL create_admin(%s,'x@v.ca','x','N','A')", (db.nurse,))
    assert "admin access only" in db.fails("CALL create_staff(%s,'s2@v.ca','x','A','B',%s,'Nurse')", (db.nurse, db.fac_h))


def test_deactivate_and_reactivate_an_account(db):
    d = db.donor()
    for _ in range(5):
        db.q("CALL login_attempt(%s, FALSE)", (d,))
    db.q("CALL set_account_active(%s,%s,FALSE)", (db.admin, d))
    assert db.one("CALL login_attempt(%s, TRUE)", (d,)) == "inactive"
    db.q("CALL set_account_active(%s,%s,TRUE)", (db.admin, d))                       # reactivating clears the lockout
    assert db.one("CALL login_attempt(%s, TRUE)", (d,)) == "ok"
    assert "admin access only" in db.fails("CALL set_account_active(%s,%s,FALSE)", (db.nurse, d))
    assert "not found" in db.fails("CALL set_account_active(%s,0,FALSE)", (db.admin,))


def test_facility_with_open_requests_cannot_be_deactivated(db):
    r = db.request("O+")
    assert "open requests" in db.fails("CALL set_facility_active(%s,%s,FALSE)", (db.admin, db.fac_h))
    db.q("CALL close_request(%s, TRUE)", (r,))
    db.q("CALL set_facility_active(%s,%s,FALSE)", (db.admin, db.fac_h))
    assert db.one("SELECT is_active FROM facility WHERE facility_id=%s", (db.fac_h,)) is False
    assert "admin access only" in db.fails("CALL set_facility_active(%s,%s,TRUE)", (db.nurse, db.fac_h))


def test_save_facility_creates_and_edits(db):
    f = db.one("CALL save_facility(%s,NULL,'Essex County Hospital','Hospital','9 Essex Rd',NULL,NULL)", (db.admin,))
    assert db.one("CALL save_facility(%s,%s,'Essex County Hospital','Hospital','10 Essex Rd','519-555-0199',NULL)",
                  (db.admin, f)) == f
    assert db.one("SELECT address FROM facility WHERE facility_id=%s", (f,)) == "10 Essex Rd"
    assert "not found" in db.fails("CALL save_facility(%s,0,'X','Hospital','1 St',NULL,NULL)", (db.admin,))
    assert "admin access only" in db.fails("CALL save_facility(%s,NULL,'Y','Hospital','1 St',NULL,NULL)", (db.nurse,))


# ---------------------------------------------------------------------- update_donor_profile() UC-03 / FR-03
def test_donor_updates_their_profile_and_it_is_audited(db):
    d = db.donor(kg=70)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    db.q("CALL update_donor_profile(%s,'519-555-0101','New@Mail.ca','2 Main St',48)", (d,))
    assert db.q("SELECT a.phone, a.email, o.address FROM user_account a JOIN donor o USING (user_id) WHERE user_id=%s", (d,)) == \
        [("519-555-0101", "new@mail.ca", "2 Main St")]
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == "Not eligible (under 50 kg)"
    assert {f for (f,) in db.q("SELECT field_name FROM audit_log WHERE user_id=%s", (d,))} >= {"phone", "email", "address", "weight_kg"}
    assert "donor access only" in db.fails("CALL update_donor_profile(%s,NULL,'n@v.ca',NULL,70)", (db.nurse,))


# ---------------------------------------------------------------------- UC-04 precondition / UC-20 responses
def test_permanently_deferred_donor_cannot_screen(db):
    d = db.donor()
    db.q("CALL record_deferral(%s,%s,'HBV')", (db.nurse, d))
    assert "permanently deferred" in db.fails("CALL submit_screening(%s,%s)", (d, answers(db)))


def test_call_responses_accept_decline_and_closed(db):
    db.q("UPDATE facility SET phone = '519-555-0111' WHERE facility_id=%s", (db.fac_h,))
    yes, no, late = (db.donor("O-", screened=True) for _ in range(3))
    r = db.request("O-")
    call = db.one("CALL send_donation_call(%s,%s,'x')", (r, db.nurse))
    assert db.one("CALL respond_to_call(%s,%s,TRUE)", (call, yes)) == "Riverside General, 1 River Rd, 519-555-0111"
    assert db.one("CALL respond_to_call(%s,%s,FALSE)", (call, no)) == "declined"
    db.q("CALL close_request(%s, TRUE)", (r,))
    assert db.one("CALL respond_to_call(%s,%s,TRUE)", (call, late)) == "closed"
    assert db.one("SELECT response FROM call_recipient WHERE call_id=%s AND donor_id=%s", (call, late)) == "Pending"
    assert "no pending call" in db.fails("CALL respond_to_call(0,%s,TRUE)", (yes,))


# ---------------------------------------------------------------------- verify_blood_group / submit_request / confirm_request_group
def test_verify_blood_group_is_a_staff_command(db):
    """FR-09: staff verify a donor's group and are recorded as the verifier; a donor cannot verify."""
    d = db.donor("A+", verified=False)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    assert "staff access only" in db.fails("CALL verify_blood_group(%s,%s,'A+')", (d, d))
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    db.q("CALL verify_blood_group(%s,%s,'A-')", (db.nurse, d))
    assert db.q("SELECT blood_group, verified_by FROM donor WHERE user_id=%s", (d,)) == [("A-", db.nurse)]


def test_submit_request_reserves_and_reports_the_shortfall(db):
    """UC-16 / FR-22 / FR-24: one call creates the request, reserves what stock covers and returns the shortfall."""
    db.stock("O-")
    rid, reserved, short = db.q("CALL submit_request(%s,'P-77','O-','RBC',3,'Urgent',now() + interval '1 day')", (db.nurse,))[0]
    assert (reserved, short) == (1, 2)
    assert db.q("SELECT requested_by, status FROM blood_request WHERE request_id=%s", (rid,)) == [(db.nurse, "Open")]
    assert "staff access only" in db.fails("CALL submit_request(%s,'P-78','O-','RBC',1,'Urgent',now())", (db.admin,))


def test_confirm_request_group_only_at_the_requesting_facility_and_once(db):
    """UC-16 step 6: the requesting facility confirms an Unknown group once; compatible stock is then reserved."""
    db.stock("A+")
    rid = db.q("CALL submit_request(%s,'TR-9',NULL,'RBC',1,'Emergency',now() + interval '1 hour')", (db.nurse,))[0][0]
    assert "another facility" in db.fails("CALL confirm_request_group(%s,%s,'A+')", (db.phleb, rid))
    assert db.q("CALL confirm_request_group(%s,%s,'A+')", (db.nurse, rid)) == [(1, 0)]
    assert "already has a confirmed group" in db.fails("CALL confirm_request_group(%s,%s,'B+')", (db.nurse, rid))


# ---------------------------------------------------------------------- FR-32 / UC-24 clinical rules (Admin)
def test_admin_edits_every_kind_of_clinical_rule_and_each_change_is_audited(db):
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.admin),))
    db.q("CALL set_shelf_life(%s,'PLT',7)", (db.admin,))
    db.q("CALL set_donation_interval(%s,'WB','M',60)", (db.admin,))
    db.q("CALL set_eligibility_rule(%s,'M',18,55,135)", (db.admin,))
    db.q("CALL save_deferral_reason(%s,'surg','Surgery in the last 6 months',FALSE,180)", (db.admin,))
    db.q("CALL save_deferral_reason(%s,'SURG','Major surgery in the last 6 months',FALSE,120)", (db.admin,))
    db.q("CALL set_compatibility(%s,'RBC','AB+','O+',FALSE)", (db.admin,))
    assert db.one("SELECT shelf_life_days FROM component_type WHERE component_code='PLT'") == 7
    assert db.one("SELECT interval_days FROM donation_interval WHERE donation_type_code='WB' AND sex='M'") == 60
    assert db.q("SELECT min_age_years, min_weight_kg, min_hemoglobin_gl FROM eligibility_rule WHERE sex='M'") == [(18, 55, 135)]
    assert db.q("SELECT description, default_days FROM deferral_reason WHERE reason_code='SURG'") == \
        [("Major surgery in the last 6 months", 120)]
    assert db.one("SELECT count(*) FROM compatibility WHERE component_code='RBC' AND recipient_group='AB+' AND donor_group='O+'") == 0
    db.q("CALL set_compatibility(%s,'RBC','AB+','O+',TRUE)", (db.admin,))
    audited = {t for (t,) in db.q("SELECT DISTINCT table_name FROM audit_log WHERE user_id = %s", (db.admin,))}
    assert {"component_type", "donation_interval", "eligibility_rule", "deferral_reason", "compatibility"} <= audited


@pytest.mark.parametrize("sql", [
    "CALL set_shelf_life(%s,'PLT',7)", "CALL set_donation_interval(%s,'WB','M',60)",
    "CALL set_eligibility_rule(%s,'M',18,55,135)", "CALL save_deferral_reason(%s,'SURG','Surgery',FALSE,180)",
    "CALL set_compatibility(%s,'RBC','AB+','O+',FALSE)"])
def test_clinical_rules_are_admin_only(db, sql):
    assert "admin access only" in db.fails(sql, (db.nurse,))


def test_rule_edit_that_strands_a_group_is_refused(db):
    """UC-24 2a: removing O- recipients' only red cell donor group is blocked."""
    assert "no compatible donor group" in db.fails("CALL set_compatibility(%s,'RBC','O-','O-',FALSE)", (db.admin,))


def test_a_new_shelf_life_applies_to_future_units(db):
    db.q("CALL set_shelf_life(%s,'PLT',7)", (db.admin,))
    unit = db.stock("A+", ("PLT",))[0]
    assert db.one("SELECT expires_at - collected_at FROM v_blood_unit WHERE unit_id=%s", (unit,)) == dt.timedelta(days=7)


# ---------------------------------------------------------------------- UC-09 / UC-20 notifications
def test_a_notification_is_marked_read_once_and_only_by_its_owner(db):
    d, other = db.donor(), db.donor()
    nid = db.one("INSERT INTO notification (user_id, message) VALUES (%s,'Hello') RETURNING notification_id", (d,))
    db.q("CALL mark_notification_read(%s,%s)", (d, nid))
    first = db.one("SELECT read_at FROM notification WHERE notification_id=%s", (nid,))
    db.q("CALL mark_notification_read(%s,%s)", (d, nid))
    assert first is not None and db.one("SELECT read_at FROM notification WHERE notification_id=%s", (nid,)) == first
    assert "not found" in db.fails("CALL mark_notification_read(%s,%s)", (other, nid))


# ---------------------------------------------------------------------- UC-21 issue only the units staff confirm
def test_close_request_issues_only_the_confirmed_units(db):
    a, b = db.stock("O+", n=2)
    r = db.request("O+", n=2)
    db.q("CALL allocate(%s)", (r,))
    assert "not reserved for request" in db.fails("CALL close_request(%s, FALSE, ARRAY['U-NOPE'])", (r,))
    assert db.one("CALL close_request(%s, FALSE, ARRAY[%s])", (r, a)) == "Partially Fulfilled"
    assert dict(db.q("SELECT unit_id, status FROM blood_unit WHERE unit_id IN (%s,%s)", (a, b))) == {a: "Issued", b: "Available"}
    assert db.one("SELECT released_at IS NOT NULL FROM request_unit WHERE request_id=%s AND unit_id=%s", (r, b))


# ---------------------------------------------------------------------- FR-33 whole rows on INSERT and DELETE, never secrets
def test_audit_keeps_the_row_on_insert_and_delete_without_secrets(db):
    d = db.donor()
    created = json.loads(db.one("SELECT new_value FROM audit_log WHERE table_name='user_account' AND action='INSERT' "
                                "AND record_id=%s", (str(d),)))
    assert created["email"].startswith("d") and not {"password_hash", "failed_logins", "locked_until"} & created.keys()
    db.q("INSERT INTO stock_threshold VALUES (%s,'B-','RBC',0)", (db.fac_h,))          # 0: no alert holds the row
    db.q("DELETE FROM stock_threshold WHERE facility_id=%s AND blood_group='B-' AND component_code='RBC'", (db.fac_h,))
    gone = json.loads(db.one("SELECT old_value FROM audit_log WHERE table_name='stock_threshold' AND action='DELETE'"))
    assert gone["min_units"] == 0 and gone["blood_group"] == "B-"


# ---------------------------------------------------------------------- BR-10 urgency windows
@pytest.mark.parametrize("urgency, needed, error", [
    ("Emergency", "30 minutes", ""), ("Emergency", "2 hours", "within 1 hour"),
    ("Urgent", "23 hours", ""), ("Urgent", "25 hours", "within 24 hours"),
    ("Routine", "10 days", ""), ("Routine", "-1 hour", "in the future")])
def test_request_urgency_windows(db, urgency, needed, error):
    msg = db.fails("CALL submit_request(%s,'P-9','O+','RBC',1,%s,now() + %s::interval)", (db.nurse, urgency, needed))
    assert (error in msg) if error else msg == ""


# ---------------------------------------------------------------------- UC-18 manual search by blood group
def test_manual_search_finds_eligible_compatible_verified_donors(db):
    o_neg, a_pos, b_pos = db.donor("O-", screened=True), db.donor("A+", screened=True), db.donor("B+", screened=True)
    unscreened = db.donor("A+")
    codes = {c for c, _, _ in db.q("SELECT * FROM donors_for_group('A+','RBC')")}
    code = lambda u: db.one("SELECT donor_code FROM donor WHERE user_id=%s", (u,))
    assert {code(o_neg), code(a_pos)} <= codes and not {code(b_pos), code(unscreened)} & codes
    assert code(b_pos) not in {c for c, _, _ in db.q("SELECT * FROM donors_for_group('A+','PLASMA')")}
