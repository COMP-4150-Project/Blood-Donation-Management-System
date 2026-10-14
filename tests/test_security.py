"""SE-04 least privilege, SE-08 / DI-04 history that cannot be rewritten."""
import json
import pytest


@pytest.fixture
def app(db):
    db.donate(db.donor())
    db.q("SET LOCAL ROLE vytal_app")
    return db


@pytest.mark.parametrize("sql", [
    "DELETE FROM donation", "DELETE FROM blood_unit", "DELETE FROM blood_request", "DELETE FROM user_account",
    "TRUNCATE notification", "UPDATE audit_log SET new_value = 'x'", "DELETE FROM audit_log",
    "INSERT INTO audit_log (action, table_name, record_id) VALUES ('UPDATE','donor','1')",
    "CREATE TABLE x (a int)",
])
def test_app_role_cannot_rewrite_history(app, sql):
    assert "permission denied" in app.fails(sql)


def test_app_role_cannot_drop_tables(app):
    assert "must be owner" in app.fails("DROP TABLE donor")


def test_app_role_still_produces_audit_rows_through_procedures(db):
    """SE-08: a change made through a procedure is audited under the acting user, though the app cannot write audit_log."""
    d = db.donor()
    email = db.one("SELECT email FROM user_account WHERE user_id=%s", (d,))
    db.q("SET LOCAL ROLE vytal_app")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    db.q("CALL update_donor_profile(%s,'519-555-0100',%s,'2 Main St',81)", (d, email))
    db.q("RESET ROLE")
    assert db.q("SELECT user_id FROM audit_log WHERE table_name='donor' AND field_name='weight_kg' AND record_id=%s",
                (str(d),)) == [(d,)]


def test_report_role_is_read_only_and_cannot_see_password_hashes(db):
    db.q("SET LOCAL ROLE vytal_report")
    db.one("SELECT count(*) FROM v_inventory")                     # allowed: raises if it were not
    assert "permission denied" in db.fails("SELECT password_hash FROM user_account")
    assert "permission denied" in db.fails("UPDATE facility SET name = 'x'")


APP_FUNCTIONS = {"donor_eligibility(integer,date)", "eligible_donors(integer)", "donors_for_group(text,text)"}


def test_app_role_runs_every_procedure_and_only_the_read_only_functions(db):
    """SE-04: the app may CALL every procedure and SELECT three read-only functions; helpers and trigger functions are closed."""
    can = {(sig, kind) for sig, kind in db.q(
        "SELECT p.oid::regprocedure::text, p.prokind FROM pg_proc p WHERE p.pronamespace = 'vytal'::regnamespace "
        "AND has_function_privilege('vytal_app', p.oid, 'EXECUTE')")}
    assert {s for s, k in can if k == "f"} == APP_FUNCTIONS
    assert db.q("SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'vytal'::regnamespace "
                "AND p.prokind = 'p' AND NOT has_function_privilege('vytal_app', p.oid, 'EXECUTE')") == []
    assert db.q("SELECT p.oid::regprocedure::text FROM pg_proc p, aclexplode(p.proacl) a "
                "WHERE p.pronamespace = 'vytal'::regnamespace AND a.grantee = 0") == []


def test_application_roles_have_passwords(db):
    assert db.q("SELECT rolname FROM pg_authid WHERE rolname IN ('vytal_app','vytal_report') AND rolpassword IS NULL") == []


def test_routines_refuse_a_user_id_that_is_not_the_session_user(db):
    """SE-02: as vytal_app, a donor's session cannot act as the Admin or as staff by passing their ids."""
    d, u = db.donor(), db.stock("O+")[0]
    db.q("SET LOCAL ROLE vytal_app")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    for sql, args in (("CALL create_admin(%s,'evil@v.ca','x','E','V')", (db.admin,)),
                      ("CALL create_staff(%s,'evil2@v.ca','x','E','V',%s,'Nurse')", (db.admin, db.fac_h)),
                      ("CALL set_threshold(%s,%s,'O-','RBC',9)", (db.nurse, db.fac_h)),
                      ("CALL discard_unit(%s,%s,'x')", (db.nurse, u)),
                      ("CALL record_deferral(%s,%s,'HIV')", (db.nurse, d)),
                      ("CALL verify_blood_group(%s,%s,'O-')", (db.nurse, d)),
                      ("CALL submit_request(%s,'P-1','O+','RBC',1,'Routine',now())", (db.nurse,)),
                      ("CALL confirm_request_group(%s,1,'O+')", (db.nurse,))):
        assert "acting user does not match the session" in db.fails(sql, args)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.admin),))
    assert db.one("CALL create_admin(%s,'ok@v.ca','x','O','K')", (db.admin,)) > 0


def test_report_role_cannot_read_audit_values(db):
    """Audit values can hold emails and phones the report role is otherwise denied."""
    d = db.donor()
    db.q("UPDATE user_account SET phone='519-555-0199' WHERE user_id=%s", (d,))
    db.q("SET LOCAL ROLE vytal_report")
    assert "permission denied" in db.fails("SELECT new_value FROM audit_log")
    assert "permission denied" in db.fails("SELECT * FROM audit_log")
    assert db.q("SELECT table_name, field_name FROM audit_log WHERE table_name='user_account' AND action='UPDATE'") == \
        [("user_account", "phone")]


ANSWERS = "SELECT jsonb_object_agg(question_id::text, coalesce(NOT defer_on_answer, FALSE)) FROM screening_question WHERE is_active"


def test_screening_is_entered_by_the_donor_or_staff(db):
    """UC-04: as vytal_app, another donor or an Admin cannot submit a donor's screening; the donor and staff can."""
    d, other = db.donor(), db.donor()
    answers = json.dumps(db.one(ANSWERS))
    db.q("SET LOCAL ROLE vytal_app")
    for who in (other, db.admin):
        db.q("SELECT set_config('vytal.user_id', %s, true)", (str(who),))
        assert "entered by the donor or by staff" in db.fails("CALL submit_screening(%s,%s)", (d, answers))
    for who in (d, db.nurse):
        db.q("SELECT set_config('vytal.user_id', %s, true)", (str(who),))
        assert db.one("CALL submit_screening(%s,%s)", (d, answers)) > 0


def test_only_the_called_donor_answers_a_call(db):
    """UC-20: as vytal_app, a session cannot answer a donation call on another donor's behalf."""
    d, other = db.donor(), db.donor()
    db.q("SET LOCAL ROLE vytal_app")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(other),))
    assert "acting user does not match the session" in db.fails("CALL respond_to_call(1,%s,TRUE)", (d,))


def test_a_donor_cannot_be_inserted_already_verified(db):
    """FR-09: the app cannot insert donor rows at all; inside the procedures (owner role) only the acting staff can verify."""
    uid = db.user("Donor")
    row = "INSERT INTO donor (user_id,donor_code,date_of_birth,sex,weight_kg,blood_group,verified_by,verified_at) " \
          "VALUES (%s,'D-9001','1990-01-01','F',60,'A+',%s,now())"
    db.q("SET LOCAL ROLE vytal_app")
    assert "permission denied" in db.fails(row, (uid, db.nurse))
    db.q("SET LOCAL ROLE vytal_owner")                                  # what a SECURITY DEFINER procedure runs as
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(uid),))
    assert "only staff can verify" in db.fails(row, (uid, db.nurse))
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    db.q(row, (uid, db.nurse))
    assert db.one("SELECT verified_by FROM donor WHERE user_id=%s", (uid,)) == db.nurse


def test_app_role_cannot_write_any_table(db):
    """SE-04: no INSERT, UPDATE or DELETE privilege on any table or view for the application role."""
    assert db.q("SELECT c.relname, p.priv FROM pg_class c CROSS JOIN (VALUES ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE')) p(priv) "
                "WHERE c.relnamespace = 'vytal'::regnamespace AND c.relkind IN ('r','v') "
                "AND has_table_privilege('vytal_app', c.oid, p.priv)") == []
    assert db.q("WITH s AS MATERIALIZED (SELECT oid, relname FROM pg_class WHERE relnamespace = 'vytal'::regnamespace AND relkind = 'S') "
                "SELECT relname FROM s WHERE has_sequence_privilege('vytal_app', s.oid, 'USAGE')") == []


@pytest.mark.parametrize("sql", ["UPDATE user_account SET role = 'Admin' WHERE user_id = (SELECT min(user_id) FROM donor)",
                                 "UPDATE donor SET is_flagged = NOT is_flagged",
                                 "UPDATE compatibility SET donor_group = 'O-'",
                                 "INSERT INTO deferral (donor_id, reason_code, end_date, recorded_by) VALUES (1, 'TATTOO', CURRENT_DATE, 1)"])
def test_app_role_cannot_bypass_the_procedures(app, sql):
    assert "permission denied" in app.fails(sql)


@pytest.mark.parametrize("sql", ["SELECT recompute_next_eligible(1)", "SELECT refresh_alert(1, 'O-', 'RBC')",
                                 "SELECT require_role(1, 'Admin')", "SELECT require_session_user(1)",
                                 "SELECT caller_is_superuser()", "SELECT session_timeout('Admin')"])
def test_app_role_cannot_call_internal_helpers(app, sql):
    assert "permission denied" in app.fails(sql)


def test_app_role_cannot_borrow_a_superuser_exemption(db):
    """The exemption reads the role the client acts as (SET ROLE, else the login role). A vytal_app login is no superuser and
    belongs to no role, so it can never SET ROLE to a superuser or to the owner; the owner role cannot log in."""
    assert db.q("SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname IN ('vytal_app','vytal_owner') ORDER BY 1") == \
        [("vytal_app", False, True), ("vytal_owner", False, False)]
    assert db.one("SELECT count(*) FROM pg_auth_members WHERE member = 'vytal_app'::regrole") == 0
    assert db.one("SELECT caller_is_superuser()") is True                 # this superuser test session
    db.q("SET LOCAL ROLE vytal_owner")                                      # acting as another role turns the exemption off
    assert db.one("SELECT caller_is_superuser()") is False
