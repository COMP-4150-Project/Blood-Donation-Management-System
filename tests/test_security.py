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


def test_app_role_still_produces_audit_rows_through_triggers(app):
    d = app.one("SELECT user_id FROM donor LIMIT 1")
    app.q("UPDATE donor SET weight_kg = weight_kg + 1 WHERE user_id = %s", (d,))
    app.q("RESET ROLE")
    assert app.one("SELECT count(*) FROM audit_log WHERE table_name='donor' AND field_name='weight_kg' AND record_id=%s", (str(d),)) == 1


def test_report_role_is_read_only_and_cannot_see_password_hashes(db):
    db.q("SET LOCAL ROLE vytal_report")
    db.one("SELECT count(*) FROM v_inventory")                     # allowed: raises if it were not
    assert "permission denied" in db.fails("SELECT password_hash FROM user_account")
    assert "permission denied" in db.fails("UPDATE facility SET name = 'x'")


def test_app_role_can_run_every_routine_and_public_none(db):
    """GRANT/REVOKE ... ON ALL ROUTINES covers procedures as well as functions."""
    assert db.q("SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'vytal'::regnamespace "
                "AND NOT has_function_privilege('vytal_app', p.oid, 'EXECUTE')") == []
    assert db.q("SELECT p.oid::regprocedure::text FROM pg_proc p, aclexplode(p.proacl) a "
                "WHERE p.pronamespace = 'vytal'::regnamespace AND a.grantee = 0") == []


def test_application_roles_have_passwords(db):
    assert db.q("SELECT rolname FROM pg_authid WHERE rolname IN ('vytal_app','vytal_report') AND rolpassword IS NULL") == []


def test_routines_refuse_a_user_id_that_is_not_the_session_user(db):
    """SE-02: as vytal_app, a donor's session cannot act as the Admin or as staff by passing their ids."""
    d, u = db.donor(), db.stock("O+")[0]
    db.q("SET LOCAL ROLE vytal_app")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    for sql, args in (("SELECT create_admin(%s,'evil@v.ca','x','E','V')", (db.admin,)),
                      ("SELECT create_staff(%s,'evil2@v.ca','x','E','V',%s,'Nurse')", (db.admin, db.fac_h)),
                      ("SELECT set_threshold(%s,%s,'O-','RBC',9)", (db.nurse, db.fac_h)),
                      ("SELECT discard_unit(%s,%s,'x')", (db.nurse, u)),
                      ("SELECT record_deferral(%s,%s,'HIV')", (db.nurse, d))):
        assert "acting user does not match the session" in db.fails(sql, args)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.admin),))
    assert db.one("SELECT create_admin(%s,'ok@v.ca','x','O','K')", (db.admin,)) > 0


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
        assert "entered by the donor or by staff" in db.fails("SELECT submit_screening(%s,%s)", (d, answers))
    for who in (d, db.nurse):
        db.q("SELECT set_config('vytal.user_id', %s, true)", (str(who),))
        assert db.one("SELECT submit_screening(%s,%s)", (d, answers)) > 0


def test_only_the_called_donor_answers_a_call(db):
    """UC-20: as vytal_app, a session cannot answer a donation call on another donor's behalf."""
    d, other = db.donor(), db.donor()
    db.q("SET LOCAL ROLE vytal_app")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(other),))
    assert "acting user does not match the session" in db.fails("SELECT respond_to_call(1,%s,TRUE)", (d,))


def test_a_donor_cannot_be_inserted_already_verified(db):
    """FR-09: as vytal_app, only the acting staff member can be recorded as verifier on a new donor row."""
    uid = db.user("Donor")
    row = "INSERT INTO donor (user_id,donor_code,date_of_birth,sex,weight_kg,blood_group,verified_by,verified_at) " \
          "VALUES (%s,'D-9001','1990-01-01','F',60,'A+',%s,now())"
    db.q("SET LOCAL ROLE vytal_app")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(uid),))
    assert "only staff can verify" in db.fails(row, (uid, db.nurse))
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    db.q(row, (uid, db.nurse))
    assert db.one("SELECT verified_by FROM donor WHERE user_id=%s", (uid,)) == db.nurse
