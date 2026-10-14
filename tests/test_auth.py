"""SE-01: passwords are stored only as salted bcrypt hashes."""
import datetime as dt

import psycopg
import pytest

from vytal import services as svc
from vytal.auth import MAX_BYTES, ROUNDS, hash_password, verify_password

DEMO = "Vytal-demo-2026"


def test_hash_is_bcrypt_at_the_configured_cost_and_verifies():
    h = hash_password("correct-horse-42")
    assert h.startswith(f"$2b${ROUNDS:02d}$") and len(h) == 60
    assert verify_password("correct-horse-42", h)


def test_wrong_password_is_refused():
    assert not verify_password("correct-horse-43", hash_password("correct-horse-42"))


@pytest.mark.parametrize("stored", ["", "x", "not-a-hash", "$2b$12$short",
                                    "scrypt$16384$8$1$c440a8023f149b633cdaf20a8027a3c7$7378"])
def test_malformed_or_legacy_hash_returns_false_without_raising(stored):
    assert verify_password("anything", stored) is False


def test_every_hash_has_its_own_salt():
    a, b = hash_password("same-password-1"), hash_password("same-password-1")
    assert a != b and verify_password("same-password-1", a) and verify_password("same-password-1", b)


@pytest.mark.parametrize("password, ok", [
    ("a" * MAX_BYTES, True),
    ("a" * (MAX_BYTES + 1), False),
    ("é" * 36, True),              # 36 characters, 72 bytes
    ("é" * 37, False),             # 37 characters but 74 bytes: the limit is bytes, not characters
])
def test_registration_refuses_passwords_bcrypt_would_truncate(password, ok, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("validation should run before any database work")
    monkeypatch.setattr(svc, "session", refuse)
    args = ("p@v.ca", password, "A", "B", None, dt.date(1990, 1, 1), "M", 70, "1 Main St")
    if ok:
        with pytest.raises(AssertionError, match="validation should run"):
            svc.register_donor(*args)
    else:
        with pytest.raises(svc.InvalidInput, match=r"^password: .*72 bytes"):
            svc.register_donor(*args)


def test_seeded_accounts_log_in_with_the_demo_password(seeded_db, monkeypatch):
    monkeypatch.setenv("PGDATABASE", seeded_db)
    with psycopg.connect(dbname=seeded_db) as con:
        hashes = {h for (h,) in con.execute("SELECT DISTINCT password_hash FROM vytal.user_account")}
        uid = con.execute("SELECT user_id FROM vytal.user_account WHERE email = 'm.ross@riverside.example.ca'").fetchone()[0]
    assert len(hashes) == 1 and verify_password(DEMO, hashes.pop())
    assert svc.login("m.ross@riverside.example.ca", DEMO) == (uid, "Staff")
