"""SE-04 least privilege, SE-08 / DI-04 history that cannot be rewritten."""
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
