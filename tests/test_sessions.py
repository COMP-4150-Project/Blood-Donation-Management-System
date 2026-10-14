"""SE-06 session test: idle timeouts (15 min staff and Admins, 30 min donors), refresh on activity, logout."""
import datetime as dt
import hashlib

import psycopg
import pytest

from conftest import PREFIX, build, drop
from vytal import services as svc


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def start(db, user, token):
    db.q("CALL start_session(%s, %s)", (user, token_hash(token)))
    return token_hash(token)


def idle(db, h, minutes):
    """Pretend the session was last used `minutes` ago (both stamps move, so the row stays consistent)."""
    db.q("UPDATE user_session SET created_at = created_at - make_interval(mins => %s), "
         "last_seen_at = last_seen_at - make_interval(mins => %s) WHERE token_hash = %s", (minutes, minutes, h))


def resume(db, h):
    return db.q("CALL resume_session(%s)", (h,))[0]


def test_donor_session_lasts_30_idle_minutes(db):
    d = db.donor()
    alive, dead = start(db, d, "donor-alive"), start(db, d, "donor-dead")
    idle(db, alive, 29)
    idle(db, dead, 31)
    assert resume(db, alive) == (d, "Donor")
    assert resume(db, dead) == (None, None)
    assert db.one("SELECT ended_at IS NOT NULL FROM user_session WHERE token_hash = %s", (dead,))


@pytest.mark.parametrize("who", ["nurse", "admin"])
def test_staff_and_admin_sessions_last_15_idle_minutes(db, who):
    user = getattr(db, who)
    role = "Staff" if who == "nurse" else "Admin"
    alive, dead = start(db, user, f"{who}-alive"), start(db, user, f"{who}-dead")
    idle(db, alive, 14)
    idle(db, dead, 16)
    assert resume(db, alive) == (user, role)
    assert resume(db, dead) == (None, None)


def test_activity_refreshes_the_idle_window(db):
    h = start(db, db.nurse, "busy-nurse")
    idle(db, h, 10)
    assert resume(db, h) == (db.nurse, "Staff")          # used at 10 minutes, so the clock restarts
    idle(db, h, 10)
    assert resume(db, h) == (db.nurse, "Staff")          # 20 minutes since sign-in, only 10 idle
    assert db.one("SELECT now() - last_seen_at < interval '1 minute' FROM user_session WHERE token_hash = %s", (h,))


def test_logout_ends_the_session(db):
    h = start(db, db.donor(), "leaving")
    db.q("CALL end_session(%s)", (h,))
    assert resume(db, h) == (None, None)


def test_deactivated_account_session_is_refused(db):
    d = db.donor()
    h = start(db, d, "soon-inactive")
    db.q("UPDATE user_account SET is_active = FALSE WHERE user_id = %s", (d,))
    assert resume(db, h) == (None, None)
    assert "not active" in db.fails("CALL start_session(%s, %s)", (d, token_hash("new-one")))


def test_unknown_token_is_refused(db):
    start(db, db.donor(), "real-token")
    assert resume(db, token_hash("guessed-token")) == (None, None)
    assert "check" in db.fails("CALL start_session(%s, 'not-a-hash')", (db.nurse,))


def test_report_role_cannot_read_token_hashes(db):
    start(db, db.donor(), "secret")
    db.q("SET LOCAL ROLE vytal_report")
    assert "permission denied" in db.fails("SELECT token_hash FROM user_session")
    assert db.one("SELECT count(*) FROM user_session") == 1     # who signed in and when is still reportable


def test_app_role_cannot_read_hashes_or_extend_a_session(db):
    h = start(db, db.donor(), "app-view")
    db.q("SET LOCAL ROLE vytal_app")
    assert "permission denied" in db.fails("SELECT token_hash FROM user_session")
    assert "permission denied" in db.fails("UPDATE user_session SET last_seen_at = now()")
    assert "permission denied" in db.fails("INSERT INTO user_session (user_id, token_hash) VALUES (1, %s)", (token_hash("forged"),))
    assert resume(db, h)[1] == "Donor"                           # the procedures still work for the app role


@pytest.fixture(scope="module")
def backend_db():
    name = build(PREFIX + "_sessions")
    yield name
    drop(name)


def test_backend_sign_in_stores_only_the_hash_and_signs_out(backend_db, monkeypatch):
    monkeypatch.setenv("PGDATABASE", backend_db)
    monkeypatch.setenv("PGOPTIONS", "-c role=vytal_app")
    uid = svc.register_donor("sess@v.ca", "correct-horse-42", "S", "E", None, dt.date(1990, 1, 1), "F", 60, "1 St")
    token, user_id, role = svc.sign_in("sess@v.ca", "correct-horse-42")
    assert (user_id, role) == (uid, "Donor")
    assert svc.session_user(token) == (uid, "Donor")
    with psycopg.connect(dbname=backend_db, options="") as owner:
        stored = owner.execute("SELECT token_hash FROM vytal.user_session WHERE user_id = %s", (uid,)).fetchone()[0]
        owner.execute("UPDATE vytal.user_session SET created_at = created_at - interval '31 minutes', "
                      "last_seen_at = last_seen_at - interval '31 minutes' WHERE user_id = %s", (uid,))
    assert stored != token and stored == token_hash(token)
    with pytest.raises(svc.AccessDenied, match="session expired"):
        svc.session_user(token)
    token2, _, _ = svc.sign_in("sess@v.ca", "correct-horse-42")
    svc.sign_out(token2)
    with pytest.raises(svc.AccessDenied, match="session expired"):
        svc.session_user(token2)
