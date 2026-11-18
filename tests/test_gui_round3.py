"""Round-3 GUI regressions: one test (or a few) per issue fixed in the third review. Same fixtures as test_gui.py, on its own
database so the order of the modules does not matter."""
import contextlib
import datetime as _dt
import pathlib
import re

import psycopg
import pytest
from psycopg import errors as pg

from tests.conftest import PREFIX, build, drop
from tests.test_gui import Browser, db, healthy, rendered

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = (ROOT / "gui" / "static" / "app.css").read_text()
JS = (ROOT / "gui" / "static" / "app.js").read_text()


@pytest.fixture(scope="module")
def gui_db():
    name = build(PREFIX + "_gui3", with_data=True)
    yield name
    drop(name)


@pytest.fixture(scope="module")
def app(gui_db):
    import os
    mp = pytest.MonkeyPatch()
    mp.setenv("PGDATABASE", gui_db)
    mp.setenv("PGUSER", "vytal_app")
    mp.setenv("PGPASSWORD", os.environ.get("VYTAL_APP_PW", "vytal_app_dev"))
    from gui.app import app as flask_app
    yield flask_app
    mp.undo()


@pytest.fixture(autouse=True)
def _fresh_throttle():
    import gui.app as ga
    ga._login_fails.clear()
    yield
    ga._login_fails.clear()


@pytest.fixture
def staff(app):
    return Browser(app).login("m.ross@riverside.example.ca")      # Riverside General, facility 1


@pytest.fixture
def admin(app):
    return Browser(app).login("admin@vytal.example.ca")


def ctx_of(seen, name):
    return [c for n, c in seen if n == name][-1]


def later(days):
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


REG = {"first_name": "Tia", "last_name": "Third", "phone": "", "date_of_birth": "1990-03-04", "sex": "F", "weight_kg": "64",
       "height_cm": "166", "address": "3 Main St, Windsor", "blood_group": "", "password": "a-long-password-3", "password2": "a-long-password-3"}


def ferrs(b):
    with b.c.session_transaction() as s:
        return s.get("ferrs") or {}


# ------------------------------------------------------------------ item 2: wastage chart uses clinic months
def test_wastage_chart_counts_the_seeded_wastage(app, admin, gui_db):
    with rendered(app) as seen:
        admin.get("/admin")
    w = ctx_of(seen, "admin_home.html")["wastage"]
    with db(gui_db) as con:
        want = con.execute("SELECT coalesce(sum(units_wasted), 0) FROM v_wastage WHERE month >= date_trunc('month', now() AT TIME ZONE 'America/Toronto') "
                           "AT TIME ZONE 'America/Toronto' - make_interval(months => %s)", (len(w) - 1,)).fetchone()[0]
    assert want > 0, "the sample data records wastage"
    assert sum(x["wasted"] for x in w) == want


# ------------------------------------------------------------------ item 5: lock conflicts read as conflicts, retried once
@pytest.mark.parametrize("cls", [pg.DeadlockDetected, pg.SerializationFailure])
def test_conflict_is_not_reported_as_database_down(cls):
    from gui.app import CONFLICT_MESSAGE, friendly
    assert issubclass(cls, psycopg.OperationalError)
    assert friendly(cls("x")) == (None, CONFLICT_MESSAGE)
    assert "not reachable" in friendly(psycopg.OperationalError("down"))[1]


def test_service_call_retries_a_deadlock_once(app, monkeypatch):
    from vytal import services as svc
    real, calls = svc.session, []

    @contextlib.contextmanager
    def flaky(user_id):
        calls.append(user_id)
        if len(calls) == 1:
            raise pg.DeadlockDetected("deadlock detected")
        with real(user_id) as con:
            yield con
    monkeypatch.setattr(svc, "session", flaky)
    assert svc._call(None, "SELECT 41 + 1") == 42 and len(calls) == 2

    calls.clear()

    @contextlib.contextmanager
    def always(user_id):
        calls.append(user_id)
        raise pg.SerializationFailure("could not serialize")
        yield
    monkeypatch.setattr(svc, "session", always)
    with pytest.raises(pg.SerializationFailure):
        svc._call(None, "SELECT 1")
    assert len(calls) == 2                                                                    # once more, then it gives up


# ------------------------------------------------------------------ item 6: DON-08 on the server
def test_screening_resubmitted_from_a_fresh_form_is_stored_once(app, gui_db):
    b = Browser(app)
    b.post("/register", dict(REG, email="tia.once@mail.ca"))
    forms = []
    for _ in range(2):                                                                        # two tabs, or Back and a reload
        page = b.get("/donor/screening").text
        once = re.search(r'name="_once" value="([^"]+)"', page).group(1)
        forms.append(dict(healthy(re.findall(r'name="q(\d+)" value="no"', page)), _once=once))
    b.post("/donor/screening", forms[0])
    assert b.flashes()[-1].startswith("Screening saved")
    b.post("/donor/screening", forms[1])
    assert b.flashes()[-1].startswith("Screening already saved")
    with db(gui_db) as con:
        n = con.execute("SELECT count(*) FROM screening s JOIN user_account a ON a.user_id = s.donor_id WHERE a.email = 'tia.once@mail.ca'").fetchone()[0]
    assert n == 1


# ------------------------------------------------------------------ items 7 and 8: account names
def test_rename_lands_on_the_new_name(app, admin, gui_db):
    with db(gui_db) as con:
        uid = con.execute("SELECT user_id FROM user_account WHERE email = 'aisha.rahman@mail.ca'").fetchone()[0]
    r = admin.post(f"/admin/users/{uid}/edit", {"first_name": "Aysha", "last_name": "Rahmani", "email": "aisha.rahman@mail.ca", "phone": ""})
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "Aysha" in loc and "Aisha" not in loc
    assert any("Aysha Rahmani" in m for m in admin.flashes())
    with rendered(app) as seen:
        admin.get(loc)
    assert [i["user_id"] for i in ctx_of(seen, "admin_users.html")["items"]] == [uid]


def test_names_are_at_most_50_everywhere(app, admin, gui_db):
    from vytal import services as svc
    assert svc.NAME_MAX == 50
    b = Browser(app)
    page = b.get("/register").text
    assert page.count('maxlength="50"') >= 2
    b.post("/register", dict(REG, email="long.name@mail.ca", first_name="A" * 51))
    assert "50" in ferrs(b).get("first_name", "")
    with db(gui_db) as con:
        uid = con.execute("SELECT user_id FROM user_account WHERE email = 'm.ross@riverside.example.ca'").fetchone()[0]
    assert admin.get(f"/admin/users/{uid}/edit").text.count('maxlength="50"') >= 2
    admin.post(f"/admin/users/{uid}/edit", {"first_name": "Maya", "last_name": "R" * 51, "email": "m.ross@riverside.example.ca", "phone": ""})
    assert "50" in ferrs(admin).get("last_name", "")
    assert admin.get("/admin/users").text.count('maxlength="50"') >= 2


# ------------------------------------------------------------------ item 9: a call's date is at most the day it is needed
def test_preferred_date_after_the_needed_day_is_refused(app, staff, gui_db):
    staff.post("/staff/requests/new", {"urgency": "Routine", "patient_ref": "P-CALL-R3", "recipient_group": "O+", "component": "RBC",
                                       "units": "50", "required_by": later(3)})
    with db(gui_db) as con:
        rid, need = con.execute("SELECT request_id, clinic_date(required_by) FROM blood_request WHERE patient_ref = 'P-CALL-R3'").fetchone()
    page = staff.get(f"/staff/requests/{rid}").text
    assert f'max="{need.isoformat()}"' in page
    code = re.search(r'name="donor" value="(D-\d+)"', page).group(1)
    staff.post(f"/staff/requests/{rid}/call", {"donor": code, "preferred": (need + _dt.timedelta(days=2)).isoformat()})
    assert "on or before" in ferrs(staff).get("preferred", "")
    with db(gui_db) as con:
        assert con.execute("SELECT count(*) FROM donation_call WHERE request_id = %s", (rid,)).fetchone()[0] == 0
    staff.post(f"/staff/requests/{rid}/call", {"donor": code, "preferred": need.isoformat()})
    with db(gui_db) as con:
        assert con.execute("SELECT count(*) FROM donation_call WHERE request_id = %s", (rid,)).fetchone()[0] == 1


# ------------------------------------------------------------------ item 10: STF-07 one toast
def test_closing_another_facilitys_request_shows_one_toast(app, staff):
    staff.post("/staff/requests/1/close", {"action": "cancel"})                               # request 1 is Essex County's
    staff.get("/staff/requests/1")
    msgs = [m for m in staff.flashes() if "another facility" in m]
    assert len(msgs) == 1, staff.flashes()


# ------------------------------------------------------------------ item 12: demo keeps clinical rules as seeded
def test_demo_locks_clinical_rules_but_not_thresholds(app, admin, gui_db, monkeypatch):
    import gui.app as ga
    monkeypatch.setattr(ga, "DEMO_MODE", True)
    with db(gui_db) as con:
        before = con.execute("SELECT interval_days FROM donation_interval WHERE donation_type_code = 'WB' AND sex = 'M'").fetchone()[0]
    admin.post("/admin/rules/interval", {"type": "WB", "sex": "M", "days": str(before + 7)})
    assert "read-only on the demo" in " ".join(admin.flashes())
    j = admin.c.post("/admin/rules/compat", json={"component": "PLT", "recipient": "A+", "donor": "B+", "allowed": False},
                     headers={"X-CSRF": admin.token_for_session()})
    assert j.status_code == 403 and "read-only" in j.get_json()["message"]
    assert "read-only on the demo" in admin.get("/admin/rules").text
    admin.post("/admin/facilities/1/threshold", {"blood_group": "AB-", "component": "PLASMA", "min_units": "2"})
    assert "Minimum saved." in admin.flashes()
    with db(gui_db) as con:
        assert con.execute("SELECT interval_days FROM donation_interval WHERE donation_type_code = 'WB' AND sex = 'M'").fetchone()[0] == before
        assert con.execute("SELECT min_units FROM stock_threshold WHERE facility_id = 1 AND blood_group = 'AB-' AND component_code = 'PLASMA'").fetchone()[0] == 2


# ------------------------------------------------------------------ item 17: labels and numbers
def test_partially_fulfilled_label_and_whole_hemoglobin(app, staff, gui_db):
    assert "Partially fulfilled" in staff.get("/staff/requests/9").text
    assert "Partially Fulfilled</span>" not in staff.get("/staff/requests").text
    with db(gui_db) as con:
        did, don = con.execute("SELECT donor_id, donation_id FROM donation ORDER BY donation_id LIMIT 1").fetchone()
        con.execute("UPDATE donation SET hemoglobin_gl = 151 WHERE donation_id = %s", (don,))
        con.commit()
    page = staff.get(f"/staff/donors/{did}").text
    assert '<td class="mono">151</td>' in page and "151.0" not in page


# ------------------------------------------------------------------ item 19: hint, proxy address, leetspeak
def test_emergency_hint_names_the_universal_components(app, staff):
    page = staff.get("/staff/requests/new").text
    assert "O-negative red cells" in page and "AB plasma and platelets" in page


def test_forwarded_address_is_read_only_behind_the_proxy(app, monkeypatch):
    import gui.app as ga
    env = {"REMOTE_ADDR": "127.0.0.1"}
    hdr = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
    monkeypatch.setattr(ga, "TRUST_PROXY", False)
    with app.test_request_context("/login", headers=hdr, environ_base=env):
        assert ga.client_key() == "127.0.0.1"
    monkeypatch.setattr(ga, "TRUST_PROXY", True)
    with app.test_request_context("/login", headers=hdr, environ_base=env):
        assert ga.client_key() == "203.0.113.9"
    with app.test_request_context("/login", headers=hdr, environ_base={"REMOTE_ADDR": "198.51.100.4"}):
        assert ga.client_key() == "198.51.100.4"                                             # not from the proxy: header ignored


@pytest.mark.parametrize("pw", ["P@ssw0rd123", "Pa55word123", "Qw3rty12345"])
def test_common_password_in_leetspeak_is_refused(pw):
    from vytal import services as svc
    from vytal.services import InvalidInput
    with pytest.raises(InvalidInput, match="^password:"):
        svc.check_password(pw)


# ------------------------------------------------------------------ items 13 to 18: layout and focus
def test_layout_rules_for_tables_hidden_text_and_bars(app, staff):
    assert "@media(max-width:1279.98px){.col-opt{display:none!important}}" in CSS                 # 13
    assert "table.audit td.who{display:table-cell" in CSS                                           # 14: .who is a flex row elsewhere
    sr = re.search(r"\n\.sr\{([^}]*)\}", CSS).group(1)                                               # 15
    assert "clip-path:inset(50%)" in sr and "margin:-1px" in sr and "padding:0" in sr
    assert '<table class="sr">' not in (ROOT / "gui" / "templates" / "admin_home.html").read_text()
    for path in ("/staff", "/staff/requests/23"):                                                    # 16: bars without JS
        bars = re.findall(r'<i data-w="(\d+)"([^>]*)>', staff.get(path).text)
        assert bars and all(f'style="--w:{w}%"' in rest for w, rest in bars), path


def test_confirm_dialog_traps_focus_and_skip_link_shows_on_focus():
    assert "e.key !== 'Tab'" in JS and "last.focus()" in JS and "first.focus()" in JS                # 18
    assert "dlg.addEventListener('close'" in JS and "o.focus()" in JS
    skip = re.search(r"\n\.skip\{([^}]*)\}", CSS).group(1)
    focus = re.search(r"\n\.skip:focus\{([^}]*)\}", CSS).group(1)
    assert "clip-path:inset(50%)" in skip and "position:static" in focus                             # in flow: never over the menu
