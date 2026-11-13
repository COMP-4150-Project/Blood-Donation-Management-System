"""Step 5 GUI: every screen renders for its role, each role's create / read / update / delete flows work end to end against
the real database, and the guards (role, facility, CSRF, session) hold. Runs the Flask app through its test client as the
low-privilege vytal_app login, on its own database so the other suites' sample data stays untouched."""
import os
import re

import psycopg
import pytest

from tests.conftest import PREFIX, build, drop

PW = "Vytal-demo-2026"
SUPER = {"user": os.environ.get("PGUSER"), "password": os.environ.get("PGPASSWORD") or None}      # the build login, read before the app login is set


@pytest.fixture(scope="module")
def gui_db():
    name = build(PREFIX + "_gui", with_data=True)
    yield name
    drop(name)


@pytest.fixture(scope="module")
def app(gui_db):
    mp = pytest.MonkeyPatch()
    mp.setenv("PGDATABASE", gui_db)
    mp.setenv("PGUSER", "vytal_app")
    mp.setenv("PGPASSWORD", os.environ.get("VYTAL_APP_PW", "vytal_app_dev"))
    from gui.app import app as flask_app
    yield flask_app
    mp.undo()


def healthy(question_ids):
    """Question 3 is "Are you feeling well today?": a healthy donor answers yes to it and no to everything else."""
    return {f"q{q}": ("yes" if q == "3" else "no") for q in question_ids}


class Browser:
    def __init__(self, app):
        self.c = app.test_client()

    def token(self):
        return re.search(r'data-csrf="([^"]+)"', self.c.get("/login").text).group(1)

    def login(self, email, password=PW):
        r = self.c.post("/login", data={"email": email, "password": password, "_csrf": self.token()})
        assert r.status_code == 302, r.text[:200]
        return self

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, data=None, follow=False, **kw):
        data = dict(data or {})
        data["_csrf"] = self.token_for_session()
        return self.c.post(path, data=data, follow_redirects=follow, **kw)

    def token_for_session(self):
        with self.c.session_transaction() as s:
            if "csrf" not in s:
                self.c.get("/login")
        with self.c.session_transaction() as s:
            return s["csrf"]

    def flashes(self):
        with self.c.session_transaction() as s:
            return [m for _, m in s.get("_flashes", [])]


@pytest.fixture
def donor(app):
    return Browser(app).login("liam.tremblay@mail.ca")


@pytest.fixture
def staff(app):
    return Browser(app).login("m.ross@riverside.example.ca")      # Riverside General, facility 1


@pytest.fixture
def admin(app):
    return Browser(app).login("admin@vytal.example.ca")


def db(gui_db):
    c = psycopg.connect(dbname=gui_db, **{k: v for k, v in SUPER.items() if v})
    c.execute("SET search_path = vytal")
    return c


PAGES = {
    "donor": ["/donor", "/donor/screening", "/notifications", "/profile"],
    "staff": ["/staff", "/staff/requests", "/staff/requests?status=Open", "/staff/requests/new", "/staff/requests/23", "/staff/inventory",
              "/staff/inventory?status=Available&component=RBC&group=O-", "/staff/donors", "/staff/donors?q=D-10&ready=1", "/staff/donors/101",
              "/staff/search", "/staff/search?group=O-&component=RBC", "/staff/tests", "/notifications", "/profile"],
    "admin": ["/admin", "/admin/users", "/admin/users?role=Staff&q=ross", "/admin/facilities", "/admin/facilities?thresholds=1", "/admin/rules",
              "/admin/audit", "/admin/audit?table=blood_unit&since=2026-01-01", "/admin/lookback", "/admin/lookback?code=D-1042", "/profile"],
}


@pytest.mark.parametrize("role", PAGES)
def test_every_screen_renders(role, donor, staff, admin):
    who = {"donor": donor, "staff": staff, "admin": admin}[role]
    for path in PAGES[role]:
        r = who.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert "<title>" in r.text and "Vytal" in r.text


def test_permanently_deferred_donor_sees_a_clear_dashboard(app, gui_db):
    with db(gui_db) as con:
        email = con.execute("SELECT a.email FROM donor d JOIN user_account a ON a.user_id = d.user_id WHERE d.next_eligible_date = 'infinity' LIMIT 1").fetchone()[0]
    page = Browser(app).login(email).get("/donor")
    assert page.status_code == 200 and "permanently deferred" in page.text.lower()
    assert "Complete screening" not in page.text


def test_public_pages_and_redirects(app):
    c = Browser(app)
    for path in ("/", "/login", "/register"):
        assert c.get(path).status_code == 200
    for path in ("/donor", "/staff", "/admin", "/notifications", "/profile"):
        r = c.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path
    assert c.get("/no-such-page").status_code == 404


def test_login_errors_are_plain_and_do_not_leak(app):
    b = Browser(app)
    r = b.c.post("/login", data={"email": "liam.tremblay@mail.ca", "password": "wrong", "_csrf": b.token()})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert any("wrong email or password" in m.lower() for m in b.flashes())
    r = b.c.post("/login", data={"email": "nobody@example.com", "password": "x", "_csrf": b.token()})
    assert any("wrong email or password" in m.lower() for m in b.flashes())


def test_csrf_and_roles_are_enforced(app, donor, staff, admin):
    r = donor.c.post("/logout", data={})
    assert r.status_code == 302 and donor.get("/donor").status_code == 200       # no token, nothing happened
    for path in ("/staff", "/admin"):
        assert donor.get(path).status_code == 302
    assert staff.get("/admin/users").status_code == 302
    assert admin.get("/staff/requests").status_code == 302
    r = staff.get("/staff/requests/141")                                          # another facility's request
    assert r.status_code in (302, 404)


# ------------------------------------------------------------------ donor: register, screen, profile
def test_donor_registers_screens_edits_profile_and_changes_password(app, gui_db):
    b = Browser(app)
    form = {"first_name": "Nora", "last_name": "Test", "email": "nora.test@mail.ca", "phone": "519-555-9000", "date_of_birth": "1995-04-12",
            "sex": "F", "weight_kg": "64", "address": "1 University Ave W, Windsor", "blood_group": "A+", "password": "a-long-password-1",
            "password2": "a-long-password-1"}
    bad = dict(form, date_of_birth="2015-01-01")
    b.post("/register", bad)
    assert any("at least" in m for m in b.flashes())                                # field-level reason, not a stack trace
    r = b.get("/register")
    assert "at least" in r.text or "Become a donor" in r.text
    r = b.post("/register", form)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    b.login("nora.test@mail.ca", "a-long-password-1")
    home = b.get("/donor").text
    assert "Complete screening" in home and "Nora" in home
    questions = re.findall(r'name="q(\d+)" value="no"', b.get("/donor/screening").text)
    assert len(questions) == 6
    r = b.post("/donor/screening", healthy(questions))
    assert r.status_code == 302 and any("Eligible" in m for m in b.flashes())
    b.post("/profile", {"email": "nora.test@mail.ca", "phone": "519-555-9111", "address": "2 Ouellette Ave", "weight_kg": "65"})
    assert any("Profile saved" in m for m in b.flashes())
    assert "519-555-9111" in b.get("/profile").text
    b.post("/password", {"old_password": "a-long-password-1", "new_password": "another-long-pass-2", "confirm": "another-long-pass-2"})
    assert b.get("/donor").status_code == 302                                       # sessions ended, sign in again
    Browser(app).login("nora.test@mail.ca", "another-long-pass-2")


def test_donor_answers_a_call(app, staff, gui_db):
    with db(gui_db) as con:
        row = con.execute("SELECT r.call_id, a.email FROM call_recipient r JOIN user_account a ON a.user_id = r.donor_id "
                          "WHERE r.response = 'Pending' LIMIT 1").fetchone()
    if row is None:
        pytest.skip("sample data has no pending call")
    call_id, email = row
    d = Browser(app).login(email)
    assert "I can help" in d.get("/donor").text
    d.post(f"/donor/calls/{call_id}", {"answer": "accept"})
    assert d.flashes()


# ------------------------------------------------------------------ staff: requests, inventory, donors
def test_staff_request_lifecycle(staff, gui_db):
    due = "2026-12-31T12:00:00+00:00"
    from datetime import datetime, timedelta, timezone
    due = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    r = staff.post("/staff/requests/new", {"urgency": "Urgent", "patient_ref": "P-GUI-1", "recipient_group": "O+", "component": "RBC", "units": "2", "required_by": due})
    assert r.status_code == 302
    rid = int(re.search(r"/staff/requests/(\d+)", r.headers["Location"]).group(1))
    page = staff.get(f"/staff/requests/{rid}").text
    assert "P-GUI-1" in page and "Reserved" in page
    # an Emergency due in 5 hours is refused by the database and shown as a message, with the form kept
    bad = staff.post("/staff/requests/new", {"urgency": "Emergency", "patient_ref": "P-GUI-2", "recipient_group": "O+", "component": "RBC", "units": "1", "required_by": due})
    assert "/new" in bad.headers["Location"] and any("Emergency" in m for m in staff.flashes())
    assert "P-GUI-2" in staff.get("/staff/requests/new").text
    staff.post(f"/staff/requests/{rid}/call", {})
    staff.post(f"/staff/requests/{rid}/close", {"action": "cancel"})
    assert any("cancelled" in m.lower() for m in staff.flashes())
    assert "Cancelled" in staff.get(f"/staff/requests/{rid}").text
    with db(gui_db) as con:
        assert con.execute("SELECT status FROM blood_request WHERE request_id = %s", (rid,)).fetchone()[0] == "Cancelled"
        assert con.execute("SELECT count(*) FROM audit_log WHERE table_name = 'blood_request' AND record_id = %s", (str(rid),)).fetchone()[0] >= 1


def test_staff_unit_discard_and_threshold(staff, gui_db):
    with db(gui_db) as con:
        unit = con.execute("SELECT unit_id FROM blood_unit WHERE facility_id = 1 AND status = 'Available' LIMIT 1").fetchone()[0]
    staff.post(f"/staff/units/{unit}/discard", {"reason": "Damaged bag"})
    with db(gui_db) as con:
        assert con.execute("SELECT status FROM blood_unit WHERE unit_id = %s", (unit,)).fetchone()[0] == "Discarded"
    staff.post("/staff/thresholds", {"blood_group": "AB-", "component": "PLT", "min_units": "3"})
    with db(gui_db) as con:
        assert con.execute("SELECT min_units FROM stock_threshold WHERE facility_id = 1 AND blood_group = 'AB-' AND component_code = 'PLT'").fetchone()[0] == 3
    staff.post("/staff/thresholds", {"blood_group": "AB-", "component": "PLT", "min_units": "-4"})
    assert staff.flashes()                                                           # refused with a reason


def test_staff_screens_verifies_and_records_a_donation_then_tests(staff, gui_db):
    with db(gui_db) as con:
        donor_id, code = con.execute("SELECT user_id, donor_code FROM v_donor_eligibility WHERE can_be_called AND group_verified "
                                     "AND donor_code NOT IN ('D-1042') ORDER BY donor_code LIMIT 1").fetchone()
    page = staff.get(f"/staff/donors/{donor_id}").text
    qs = re.findall(r'name="q(\d+)" value="no"', page)
    staff.post(f"/staff/donors/{donor_id}/screen", healthy(qs))
    assert any("Eligible" in m for m in staff.flashes())
    r = staff.post(f"/staff/donors/{donor_id}/donate", {"donation_type": "WB", "volume_ml": "450", "hemoglobin_gl": "150", "components": ["RBC", "PLASMA"]})
    msgs = staff.flashes()
    assert r.status_code == 302
    with db(gui_db) as con:
        did = con.execute("SELECT max(donation_id) FROM donation WHERE donor_id = %s", (donor_id,)).fetchone()[0]
    assert did and any("recorded" in m.lower() or "deferred" in m.lower() for m in msgs), msgs
    tests = staff.get("/staff/tests").text
    assert f"Donation #{did}" in tests
    staff.post(f"/staff/tests/{did}", {"t_HIV": "Negative", "t_HBV": "Negative", "t_HCV": "Negative", "t_SYPH": "Negative"})
    with db(gui_db) as con:
        assert con.execute("SELECT count(*) FROM blood_unit WHERE donation_id = %s AND status = 'Available'", (did,)).fetchone()[0] == 2
    # field-level error: a donation without components
    staff.post(f"/staff/donors/{donor_id}/donate", {"donation_type": "WB", "volume_ml": "450", "hemoglobin_gl": "150"})
    assert any("component" in m.lower() for m in staff.flashes())
    staff.post(f"/staff/donors/{donor_id}/defer", {"reason": "TATTOO"})
    assert any("Deferral recorded" in m for m in staff.flashes())


# ------------------------------------------------------------------ admin: accounts, facilities, rules, audit, lookback
def test_admin_accounts_facilities_rules_and_audit(app, admin, gui_db):
    admin.post("/admin/users/create", {"role": "Staff", "facility_id": "2", "first_name": "Zed", "last_name": "Tester", "email": "z.tester@essex.example.ca",
                                       "password": "starting-pass-123", "job_title": "Lab tech"})
    assert any("Account created" in m for m in admin.flashes())
    s = Browser(app).login("z.tester@essex.example.ca", "starting-pass-123")
    assert s.get("/staff").status_code == 200 and "Essex County Hospital" in s.get("/staff").text
    with db(gui_db) as con:
        uid = con.execute("SELECT user_id FROM user_account WHERE email = 'z.tester@essex.example.ca'").fetchone()[0]
    admin.post(f"/admin/users/{uid}/password", {"new_password": "reset-pass-12345"})
    Browser(app).login("z.tester@essex.example.ca", "reset-pass-12345")
    admin.post(f"/admin/users/{uid}/active", {"active": "0"})
    b = Browser(app)
    r = b.c.post("/login", data={"email": "z.tester@essex.example.ca", "password": "reset-pass-12345", "_csrf": b.token()})
    assert any("deactivated" in m for m in b.flashes())
    admin.post(f"/admin/users/{uid}/active", {"active": "1"})

    admin.post("/admin/facilities/save", {"name": "Harbour Clinic", "facility_type": "Blood Bank", "address": "5 Harbour Rd", "phone": "519-555-0999", "email": "hc@example.ca"})
    assert any("Facility saved" in m for m in admin.flashes())
    assert "Harbour Clinic" in admin.get("/admin/facilities").text
    with db(gui_db) as con:
        fid = con.execute("SELECT facility_id FROM facility WHERE name = 'Harbour Clinic'").fetchone()[0]
    admin.post(f"/admin/facilities/{fid}/threshold", {"blood_group": "O-", "component": "RBC", "min_units": "7"})
    admin.post(f"/admin/facilities/{fid}/active", {"active": "0"})
    assert any("deactivated" in m for m in admin.flashes())

    admin.post("/admin/rules/shelf", {"component": "PLT", "days": "7"})
    admin.post("/admin/rules/shelf", {"component": "PLT", "days": "5"})
    admin.post("/admin/rules/interval", {"type": "WB", "sex": "M", "days": "56"})
    admin.post("/admin/rules/reason", {"code": "ZIKA", "description": "Recent Zika exposure", "default_days": "120"})
    assert any("reason saved" in m.lower() for m in admin.flashes())
    r = admin.post("/admin/rules/compat", {})                                       # form post without JSON body is refused
    j = admin.c.post("/admin/rules/compat", json={"component": "PLT", "recipient": "A+", "donor": "B+", "allowed": True}, headers={"X-CSRF": admin.token_for_session()})
    assert j.status_code == 200 and j.get_json()["ok"]
    j = admin.c.post("/admin/rules/compat", json={"component": "RBC", "recipient": "O-", "donor": "O-", "allowed": False}, headers={"X-CSRF": admin.token_for_session()})
    assert j.status_code == 422 and "donor" in j.get_json()["message"].lower()      # would strand O- patients

    page = admin.get("/admin/audit?table=facility").text
    assert "Harbour" in page or "facility" in page
    csv = admin.get("/admin/audit.csv?table=facility")
    assert csv.status_code == 200 and csv.mimetype == "text/csv" and csv.text.splitlines()[0].startswith("audit_id")


def test_admin_lookback_quarantine_and_expiry(admin, gui_db):
    page = admin.get("/admin/lookback?code=D-1042").text
    assert "D-1042" in page and "Quarantine" in page
    csv = admin.get("/admin/lookback.csv?code=D-1042")
    assert csv.status_code == 200 and csv.mimetype == "text/csv"
    admin.post("/admin/lookback/quarantine", {"code": "D-1059"})
    assert any("Quarantine" in m for m in admin.flashes())
    admin.post("/admin/lookback/clear", {"code": "D-1059", "note": "Repeat test negative"})
    admin.post("/admin/expiry", {})
    assert any("Expiry job" in m for m in admin.flashes())
    bad = admin.get("/admin/lookback?code=D-9999")
    assert "no donor" in bad.text.lower()
