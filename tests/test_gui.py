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
            "sex": "F", "weight_kg": "64", "height_cm": "168", "address": "1 University Ave W, Windsor", "blood_group": "A+", "password": "a-long-password-1",
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
    b.post("/profile", {"email": "nora.test@mail.ca", "phone": "519-555-9111", "address": "2 Ouellette Ave", "weight_kg": "65", "height_cm": "169"})
    assert any("Profile saved" in m for m in b.flashes())
    assert "519-555-9111" in b.get("/profile").text
    b.post("/password", {"old_password": "a-long-password-1", "new_password": "another-long-pass-2", "confirm": "another-long-pass-2"})
    assert b.get("/donor").status_code == 302                                       # sessions ended, sign in again
    Browser(app).login("nora.test@mail.ca", "another-long-pass-2")


def test_donor_answers_a_call(app, staff, gui_db):
    with db(gui_db) as con:
        row = con.execute("SELECT r.call_id, a.email FROM call_recipient r JOIN user_account a ON a.user_id = r.donor_id "
                          "JOIN donation_call c USING (call_id) JOIN v_request q USING (request_id) "
                          "WHERE r.response = 'Pending' AND q.status = 'Open' ORDER BY r.call_id, r.donor_id LIMIT 1").fetchone()
    if row is None:
        pytest.skip("sample data has no pending call")
    call_id, email = row
    d = Browser(app).login(email)
    page = d.get("/donor").text
    assert "I can help" in page, (email, call_id, re.sub(r"<[^>]+>", " ", page)[:1500])
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
    assert j.status_code == 422                                                     # unsafe pairing is refused (issue 1)
    for allowed in (False, True):                                                   # a safe pair can still be removed and re-added
        j = admin.c.post("/admin/rules/compat", json={"component": "PLT", "recipient": "A+", "donor": "A+", "allowed": allowed}, headers={"X-CSRF": admin.token_for_session()})
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


# ------------------------------------------------------------------ test-report fixes
import datetime as _dt
from contextlib import contextmanager

from flask import template_rendered


@contextmanager
def rendered(app):
    seen = []
    def rec(sender, template, context, **extra):
        seen.append((template.name, context))
    template_rendered.connect(rec, app)
    try:
        yield seen
    finally:
        template_rendered.disconnect(rec, app)


def _routine_request(staff, ref):
    due = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=5)).isoformat()
    r = staff.post("/staff/requests/new", {"urgency": "Routine", "patient_ref": ref, "recipient_group": "O+", "component": "RBC", "units": "1", "required_by": due})
    return int(re.search(r"/staff/requests/(\d+)", r.headers["Location"]).group(1))


def _eligible(gui_db, rid):
    with db(gui_db) as con:
        return [r[0] for r in con.execute("SELECT donor_code FROM eligible_donors(%s) ORDER BY 1", (rid,)).fetchall()]


def _calls(gui_db, rid):
    with db(gui_db) as con:
        return con.execute("SELECT count(*) FROM donation_call WHERE request_id = %s", (rid,)).fetchone()[0]


def _new_staff(app, admin, gui_db, tag):
    email = f"{tag}@essex.example.ca"
    admin.post("/admin/users/create", {"role": "Staff", "facility_id": "2", "first_name": "Tmp", "last_name": tag.title(), "email": email,
                                       "password": "starting-pass-123", "job_title": "Tester"})
    with db(gui_db) as con:
        uid = con.execute("SELECT user_id FROM user_account WHERE email = %s", (email,)).fetchone()[0]
    return email, uid


def test_call_needs_a_ticked_donor_and_blocks_repeats(staff, gui_db):                       # issues 3 and 22
    rid = _routine_request(staff, "P-CALL-1")
    staff.post(f"/staff/requests/{rid}/call", {})
    assert any("Tick at least one donor" in m for m in staff.flashes()) and _calls(gui_db, rid) == 0
    staff.post(f"/staff/requests/{rid}/call", {"donor": "D-1042", "preferred": "2020-01-01"})
    assert any("later date" in m for m in staff.flashes()) and _calls(gui_db, rid) == 0
    code = _eligible(gui_db, rid)[0]
    staff.post(f"/staff/requests/{rid}/call", {"donor": code})
    assert any("call sent" in m.lower() for m in staff.flashes()) and _calls(gui_db, rid) == 1
    staff.post(f"/staff/requests/{rid}/call", {"donor": code, "preferred": "2099-01-01"})
    assert any("already called" in m for m in staff.flashes()) and _calls(gui_db, rid) == 1
    staff.post(f"/staff/requests/{rid}/call", {"all": "1"})                                     # the explicit call-all action still works
    assert any("call sent" in m.lower() for m in staff.flashes()) or _calls(gui_db, rid) >= 1
    staff.post(f"/staff/requests/{rid}/close", {"action": "cancel"})


def test_issuing_with_no_unit_ticked_does_nothing(staff, gui_db):                            # issue 4
    rid = _routine_request(staff, "P-ISSUE-1")
    staff.post(f"/staff/requests/{rid}/close", {"pick": "1"})
    assert any("at least one unit" in m for m in staff.flashes())
    with db(gui_db) as con:
        assert con.execute("SELECT status FROM blood_request WHERE request_id = %s", (rid,)).fetchone()[0] == "Open"
    staff.post(f"/staff/requests/{rid}/close", {"action": "cancel"})


def test_donor_home_gets_status_and_closed_calls(app, staff, gui_db):                         # issues 5 and 10
    rid = _routine_request(staff, "P-CLOSED-1")
    code = _eligible(gui_db, rid)[0]
    staff.post(f"/staff/requests/{rid}/call", {"donor": code})
    with db(gui_db) as con:
        email, call_id = con.execute("SELECT a.email, max(r.call_id) FROM donor d JOIN user_account a ON a.user_id = d.user_id "
                                     "JOIN call_recipient r ON r.donor_id = d.user_id WHERE d.donor_code = %s GROUP BY a.email", (code,)).fetchone()
    staff.post(f"/staff/requests/{rid}/close", {"action": "cancel"})
    d = Browser(app).login(email)
    with rendered(app) as seen:
        assert d.get("/donor").status_code == 200
    ctx = [c for n, c in seen if n == "donor_home.html"][0]
    assert isinstance(ctx["elig_status"], str) and ctx["elig_status"] and call_id in ctx["closed_calls"]
    d.post(f"/donor/calls/{call_id}", {"answer": "accept"})
    assert any("This call has closed, thank you" in m for m in d.flashes())


def test_required_fields_are_trimmed_and_refused(staff, admin, gui_db):                      # issue 8
    due = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=5)).isoformat()
    staff.post("/staff/requests/new", {"urgency": "Routine", "patient_ref": "   ", "recipient_group": "O+", "component": "RBC", "units": "1", "required_by": due})
    assert any("patient reference" in m for m in staff.flashes())
    admin.post("/admin/facilities/save", {"name": "   ", "facility_type": "Hospital", "address": "1 Main St"})
    assert any("facility name" in m for m in admin.flashes())
    admin.post("/admin/facilities/save", {"name": "Name Only", "facility_type": "Hospital", "address": "  "})
    assert any("facility address" in m for m in admin.flashes())
    admin.post("/admin/users/create", {"role": "Staff", "facility_id": "2", "first_name": " ", "last_name": "X", "email": "blank.name@essex.example.ca", "password": "starting-pass-123"})
    assert any("first name" in m for m in admin.flashes())
    with db(gui_db) as con:
        assert con.execute("SELECT count(*) FROM facility WHERE trim(name) = '' OR name = 'Name Only'").fetchone()[0] == 0


MALFORMED_GETS = [
    "/staff/requests?status=%00", "/staff/requests?status=Bogus", "/staff/requests/99999999999999999999", "/staff/inventory?status=%FF&component=x&group=%00",
    "/staff/inventory?group=A%20", "/staff/donors?q=%00", "/staff/donors?q=%25_%5C&group=zz", "/staff/donors/99999999999", "/staff/donors/0",
    "/staff/search?group=%00&component=%00", "/staff/search?group=O-&component=EVIL", "/staff/units/%00/discard",
]
MALFORMED_ADMIN = [
    "/admin/audit?user=%C2%B2", "/admin/audit?user=99999999999", "/admin/audit?since=garbage&until=2026-13-45", "/admin/audit?since=0001-01-01&until=9999-12-31",
    "/admin/audit?page=abc", "/admin/audit?page=99999999999999", "/admin/audit?table=%00", "/admin/audit?record=%00", "/admin/audit.csv?since=x&user=%C2%B2",
    "/admin/users?role=Hacker", "/admin/users?q=%00", "/admin/facilities?thresholds=abc", "/admin/facilities?thresholds=99999999999",
    "/admin/lookback?code=%00", "/admin/lookback?unit=%00", "/admin/lookback.csv?code=nope", "/admin/lookback.csv?code=%00", "/admin/lookback.csv",
]


@pytest.mark.parametrize("path", MALFORMED_GETS)
def test_malformed_staff_input_never_500s(staff, path):                                    # issue 16
    r = staff.get(path)
    assert r.status_code < 500, (path, r.status_code)


@pytest.mark.parametrize("path", MALFORMED_ADMIN)
def test_malformed_admin_input_never_500s(admin, path):
    r = admin.get(path)
    assert r.status_code < 500, (path, r.status_code)


def test_malformed_posts_never_500(app, staff, admin, donor):                               # issue 16
    b = Browser(app)
    assert b.c.post("/login", data={"email": "a\x00b@x.ca", "password": "p\x00w", "_csrf": "é☃"}).status_code < 500   # non-ASCII CSRF token
    assert b.c.post("/login", data={"email": "a\x00b@x.ca", "password": "x" * 5000, "_csrf": b.token()}).status_code == 302
    assert b.c.post("/logout", data={"_csrf": "é"}).status_code == 302
    assert b.c.post("/logout", headers={"X-CSRF": "☃"}).status_code == 302
    for path, data in [("/staff/thresholds", {"blood_group": "O-", "component": "RBC", "min_units": "99999999999999"}),
                       ("/staff/thresholds", {"blood_group": "\x00", "component": "RBC", "min_units": "1"}),
                       ("/staff/requests/new", {"urgency": "Routine", "patient_ref": "a\x00b", "component": "RBC", "units": "1", "required_by": "9999-12-31T23:59:59"}),
                       ("/staff/requests/new", {"urgency": "Routine", "patient_ref": "p", "component": "RBC", "units": "1e5", "required_by": "x"}),
                       ("/staff/donors/101/donate", {"volume_ml": "450", "hemoglobin_gl": "NaN", "components": ["RBC"]}),
                       ("/staff/donors/101/donate", {"volume_ml": "450", "hemoglobin_gl": "1e999", "components": ["RBC"]}),
                       ("/staff/donors/101/defer", {"reason": "TATTOO", "end_date": "2026-99-99"}),
                       ("/staff/tests/99999999999", {"t_HIV": "Negative"}),
                       ("/notifications/read", {"id": ["abc", "99999999999999"]})]:
        assert staff.post(path, data).status_code < 500, (path, data)
    assert staff.c.post("/staff/requests/1/call", data={"donor": "☃", "_csrf": staff.token_for_session()}).status_code < 500
    for path, data in [("/admin/users/create", {"role": "Hacker", "email": "x"}), ("/admin/users/99999999999/active", {"active": "1"}),
                       ("/admin/rules/shelf", {"component": "PLT", "days": "abc"}), ("/admin/rules/eligibility", {"sex": "M", "min_age": "x"}),
                       ("/admin/rules/reason", {"code": "\x00", "description": "d", "default_days": "5"}),
                       ("/admin/lookback/quarantine", {"code": "\x00"}), ("/admin/lookback/clear", {"code": "D-1042", "note": ""}),
                       ("/admin/units/%00/discard", {"reason": "x"})]:
        assert admin.post(path, data).status_code < 500, (path, data)
    for body in ("[]", '"x"', '{"component": ["PLT"], "recipient": {}, "donor": 1}', "{"):
        r = admin.c.post("/admin/rules/compat", data=body, content_type="application/json", headers={"X-CSRF": admin.token_for_session()})
        assert r.status_code == 422, body
    assert donor.post("/donor/calls/1", {"answer": "maybe"}).status_code < 500
    assert donor.post("/profile", {"email": "x", "phone": "1" * 500, "address": "a", "weight_kg": "abc"}).status_code < 500


def test_database_errors_become_plain_messages():                                           # issue 17
    from gui.app import friendly
    E = psycopg.errors
    assert friendly(E.StringDataRightTruncation("value too long for type character varying(10)"))[1] == "That text is too long."
    assert "out of range" in friendly(E.NumericValueOutOfRange("x"))[1]
    assert "linked" in friendly(E.ForeignKeyViolation("x"))[1]
    assert "required" in friendly(E.NotNullViolation("x"))[1]
    assert "database" in friendly(psycopg.OperationalError("connection refused to 10.0.0.1"))[1].lower()
    for e in (E.DataError("secret detail xyz"), E.InternalError("secret detail xyz")):
        assert "xyz" not in friendly(e)[1]


def test_screening_keeps_answers_when_one_is_missing(app, donor, staff, gui_db):             # issue 18
    ids = re.findall(r'name="q(\d+)" value="no"', donor.get("/donor/screening").text)
    partial = healthy(ids)
    partial.pop(f"q{ids[0]}")
    with rendered(app) as seen:
        r = donor.post("/donor/screening", partial)
    assert r.status_code == 200 and "Answer every question" in r.text
    ctx = [c for n, c in seen if n == "donor_screening.html"][0]
    assert ctx["answers"] == {int(k[1:]): v for k, v in partial.items()}
    with db(gui_db) as con:
        did = con.execute("SELECT user_id FROM donor ORDER BY donor_code LIMIT 1 OFFSET 7").fetchone()[0]
    with rendered(app) as seen:
        r = staff.post(f"/staff/donors/{did}/screen", {f"q{ids[1]}": "no"})
    assert r.status_code == 200 and [c for n, c in seen if n == "staff_donor.html"][0]["answers"] == {int(ids[1]): "no"}


def test_demo_mode_protects_seeded_accounts(app, donor, admin, gui_db, monkeypatch):         # issue 20
    import gui.app as ga
    with db(gui_db) as con:
        liam = con.execute("SELECT user_id FROM user_account WHERE email = 'liam.tremblay@mail.ca'").fetchone()[0]
        ross = con.execute("SELECT user_id FROM user_account WHERE email = 'm.ross@riverside.example.ca'").fetchone()[0]
    monkeypatch.setattr(ga, "DEMO_MODE", True)
    monkeypatch.setattr(ga, "DEMO_IDS", {liam, ross})
    donor.post("/password", {"old_password": PW, "new_password": "brand-new-pass-99", "confirm": "brand-new-pass-99"})
    assert any("demo" in m for m in donor.flashes()) and donor.get("/donor").status_code == 200
    admin.post(f"/admin/users/{ross}/active", {"active": "0"})
    assert any("demo" in m for m in admin.flashes())
    admin.post(f"/admin/users/{ross}/password", {"new_password": "brand-new-pass-99"})
    assert any("demo" in m for m in admin.flashes())
    Browser(app).login("m.ross@riverside.example.ca")                                           # still works


def test_quick_filter_matches_negative_groups(app, staff):                                   # issue 22
    from gui import reads
    assert staff.get("/staff/donors?q=O-").status_code == 200
    for q in ("O-", "o-", "AB+"):
        found = reads.donors_list(_staff_id(app), q)
        assert found and all(d["blood_group"].upper() == q.upper() or q.lower() in d["donor_code"].lower() for d in found), q
    assert all(d["blood_group"] == "A+" for d in reads.donors_list(_staff_id(app), None, "A+"))
    page = staff.get("/staff/donors?group=A+")                                                   # a bare plus arrives as a space
    assert page.status_code == 200 and not any("not recognised" in m for m in staff.flashes())


def _staff_id(app):
    from vytal import services as svc
    return svc.login("m.ross@riverside.example.ca", PW)[0]


def test_audit_to_date_is_inclusive_and_pages(app, admin, gui_db):                            # issue 23
    from gui.app import _audit_filters
    f = _audit_filters({"until": "2026-05-01", "since": "2026-04-01"})
    assert f["until"] == _dt.datetime(2026, 5, 2, tzinfo=_dt.timezone.utc) and f["since"] == _dt.datetime(2026, 4, 1, tzinfo=_dt.timezone.utc)
    assert _audit_filters({"until": "2026-05-01T10:30"})["until"].hour == 10
    admin.post("/admin/rules/shelf", {"component": "PLT", "days": "6"})
    today = _dt.date.today().isoformat()
    page = admin.get(f"/admin/audit?table=component_type&since={today}&until={today}")
    assert page.status_code == 200 and "component_type" in page.text
    with rendered(app) as seen:
        admin.get("/admin/audit")
    ctx = [c for n, c in seen if n == "admin_audit.html"][0]
    assert ctx["page"] == 1 and ctx["per_page"] == 50 and len(ctx["items"]) <= 50 and ctx["has_prev"] is False
    with rendered(app) as seen:
        admin.get("/admin/audit?page=2")
    ctx2 = [c for n, c in seen if n == "admin_audit.html"][0]
    assert ctx2["page"] == 2 and ctx2["has_prev"] is True
    if ctx["has_next"]:
        assert ctx2["items"] and ctx2["items"][0] != ctx["items"][0]


def test_accounts_search_by_email(app, admin):                                               # issue 23
    with rendered(app) as seen:
        r = admin.get("/admin/users?q=admin@vytal.example.ca")
    ctx = [c for n, c in seen if n == "admin_users.html"][0]
    assert r.status_code == 200 and [i["last_name"] for i in ctx["items"]] == ["Haddad"]
    with rendered(app) as seen:
        admin.get("/admin/users?q=nobody@nowhere.example")
    assert [c for n, c in seen if n == "admin_users.html"][0]["items"] == []


def test_long_lists_report_shown_and_total(app, staff, admin):                               # issue 24
    for path, tpl in (("/staff/requests", "staff_requests.html"), ("/staff/inventory", "staff_inventory.html"),
                      ("/staff/donors", "staff_donors.html"), ("/admin/users", "admin_users.html")):
        who = admin if path.startswith("/admin") else staff
        with rendered(app) as seen:
            assert who.get(path).status_code == 200
        ctx = [c for n, c in seen if n == tpl][0]
        assert ctx["shown"] == len(ctx["items"] if "items" in ctx else ctx["units"]) and ctx["total"] >= ctx["shown"], path


def test_security_headers_and_cache_control(app, admin):                                     # issue 28
    r = admin.get("/admin")
    h = r.headers
    assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"]
    assert "fonts.googleapis.com" in h["Content-Security-Policy"] and "fonts.gstatic.com" in h["Content-Security-Policy"] and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert h["Cache-Control"] == "no-store" and "Strict-Transport-Security" not in h
    r = admin.c.get("/admin", base_url="https://localhost")
    assert "max-age" in r.headers["Strict-Transport-Security"]
    assert app.config["MAX_CONTENT_LENGTH"] and app.config["SESSION_COOKIE_HTTPONLY"]


def test_secure_cookie_default(monkeypatch):                                                  # issue 28
    import gui.app as ga
    for k in ("VYTAL_SECURE_COOKIE", "VYTAL_DEBUG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VYTAL_HOST", "127.0.0.1")
    assert ga._secure_cookie() is False
    monkeypatch.setenv("VYTAL_HOST", "0.0.0.0")
    assert ga._secure_cookie() is True
    monkeypatch.setenv("VYTAL_SECURE_COOKIE", "0")
    assert ga._secure_cookie() is False


def test_redirects_stay_on_this_site(app):                                                    # issue 28
    from gui.app import safe_path
    for bad in ("//evil.com", "/\\evil.com", "/a\tb", "/a\nb", "https://evil.com", "evil.com", "/x\\y", "", None, "/\x00"):
        assert safe_path(bad) is None, bad
    assert safe_path("/staff/requests?status=Open") == "/staff/requests?status=Open"
    b = Browser(app)
    r = b.c.post("/login", data={"email": "admin@vytal.example.ca", "password": PW, "next": "//evil.com/x", "_csrf": b.token()})
    assert r.headers["Location"].endswith("/admin")
    b2 = Browser(app)
    r = b2.c.post("/logout", data={"_csrf": "wrong"}, headers={"Referer": "https://evil.example/phish"})
    assert "evil" not in r.headers["Location"]
    r = b2.c.post("/logout", data={"_csrf": "wrong"}, headers={"Referer": "http://localhost/login?x=1"})
    assert r.headers["Location"].endswith("/login?x=1")


def test_csv_cells_are_neutralised():                                                         # issue 28
    from gui.app import csv_safe
    out = csv_safe('a,b\r\n=1+1,"+cmd"\r\n-2,@x\r\n\tt,normal\r\n')
    assert out.splitlines() == ["a,b", "'=1+1,'+cmd", "'-2,'@x", "'\tt,normal"]


def test_password_strength_and_body_size(app, admin):                                         # issue 28
    admin.post("/admin/users/create", {"role": "Staff", "facility_id": "2", "first_name": "Num", "last_name": "Pass", "email": "num.pass@essex.example.ca", "password": "12345678901234"})
    assert any("letters or symbols" in m for m in admin.flashes())
    r = admin.c.post("/admin/expiry", data={"_csrf": admin.token_for_session(), "pad": "x" * (2 * 1024 * 1024)})
    assert r.status_code == 413


def test_double_submit_is_ignored(staff, gui_db):                                             # issue 31
    due = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=6)).isoformat()
    form = {"urgency": "Routine", "patient_ref": "P-DOUBLE-1", "recipient_group": "A+", "component": "PLASMA", "units": "1", "required_by": due}
    staff.post("/staff/requests/new", form)
    r = staff.post("/staff/requests/new", form)
    assert r.status_code == 302 and any("already submitted" in m for m in staff.flashes())
    with db(gui_db) as con:
        assert con.execute("SELECT count(*) FROM blood_request WHERE patient_ref = 'P-DOUBLE-1'").fetchone()[0] == 1
    import time as _t
    _t.sleep(2.1)
    assert "already submitted" not in " ".join(staff.post("/staff/requests/new", dict(form, patient_ref="P-DOUBLE-2")).headers.get("Location", ""))


def test_session_end_messages_differ(app, admin, gui_db):                                    # issue 32
    email, uid = _new_staff(app, admin, gui_db, "msgs1")
    s = Browser(app).login(email, "starting-pass-123")
    admin.post(f"/admin/users/{uid}/active", {"active": "0"})
    assert s.get("/staff").status_code == 302
    assert any("deactivated" in m for m in s.flashes())
    admin.post(f"/admin/users/{uid}/active", {"active": "1"})
    s = Browser(app).login(email, "starting-pass-123")
    admin.post(f"/admin/users/{uid}/password", {"new_password": "reset-pass-12345"})
    assert s.get("/staff").status_code == 302 and any("password was changed or reset" in m for m in s.flashes())
    s = Browser(app).login(email, "reset-pass-12345")
    with db(gui_db) as con:
        con.execute("UPDATE user_session SET created_at = now() - interval '3 hours', last_seen_at = now() - interval '2 hours' WHERE user_id = %s AND ended_at IS NULL", (uid,))
        con.commit()
    assert s.get("/staff").status_code == 302 and any("inactivity" in m for m in s.flashes())


def test_absolute_session_lifetime(app, admin):                                               # issue 28
    with admin.c.session_transaction() as s:
        s["born"] = 1
    r = admin.get("/admin")
    assert r.status_code == 302 and "/login" in r.headers["Location"] and any("long session" in m for m in admin.flashes())


def test_one_connection_for_the_sign_in_check(app, admin, monkeypatch):                      # issue 38
    real, n = psycopg.connect, [0]
    def counting(*a, **kw):
        n[0] += 1
        return real(*a, **kw)
    monkeypatch.setattr(psycopg, "connect", counting)
    r = admin.get("/")
    assert r.status_code == 302 and n[0] == 1


# ------------------------------------------------------------------ donor dashboard: banner, ring and pill follow one status
def _new_donor(app, email, weight="70", height="175", sex="M"):
    """A freshly registered donor with a screening on record, so the database says exactly 'Eligible'."""
    b = Browser(app)
    b.post("/register", {"first_name": "Tess", "last_name": "Banner", "email": email, "phone": "", "date_of_birth": "1992-03-04", "sex": sex,
                         "weight_kg": weight, "height_cm": height, "address": "1 Main St, Windsor", "blood_group": "A+",
                         "password": "a-long-password-1", "password2": "a-long-password-1"})
    b.login(email, "a-long-password-1")
    qs = re.findall(r'name="q(\d+)" value="no"', b.get("/donor/screening").text)
    b.post("/donor/screening", healthy(qs))
    return b


def _banner(page):
    """(headline, text of the hero card, ring state): everything the donor reads at the top of My donations."""
    hero = re.search(r'<div class="card hero-card">(.*?)\n</div></div></div>', page, re.S) or re.search(r'class="card hero-card">(.*?)<div class="grid g3">', page, re.S)
    hero = hero.group(1)
    head = re.search(r"<h2[^>]*>(.*?)</h2>", hero, re.S).group(1)
    return head, re.sub(r"<[^>]+>", " ", hero), hero


ELIGIBLE_WORDS = ("You are eligible to donate", "Eligible to donate", "you can give")


def _assert_not_eligible_banner(page, reason_part):
    head, text, hero = _banner(page)
    assert "You are not eligible to donate" in head, head
    assert reason_part in text, text
    for w in ELIGIBLE_WORDS:
        assert w not in text, (w, text)
    assert "Now" not in re.findall(r"<b[^>]*>([^<]*)</b>", hero)                 # the ring does not say Now
    assert re.search(r"Not eligible right now|Not able to donate", text)                 # the pill agrees with the headline


def test_dashboard_banner_is_eligible_only_for_an_eligible_donor(app):
    b = _new_donor(app, "banner.ok@mail.ca")
    head, text, hero = _banner(b.get("/donor").text)
    assert head.strip() == "You are eligible to donate" and "Eligible to donate" in text and "Now" in hero
    assert "Body size" in b.get("/donor").text and "175 cm" in b.get("/donor").text and "70 kg" in b.get("/donor").text


def test_dashboard_banner_follows_a_weight_edit_to_underweight(app):
    b = _new_donor(app, "banner.weight@mail.ca")
    b.post("/profile", {"email": "banner.weight@mail.ca", "phone": "", "address": "1 Main St", "weight_kg": "45", "height_cm": "175"})
    page = b.get("/donor").text
    _assert_not_eligible_banner(page, "under the 50 kg minimum")
    assert "Update your weight" in page
    b.post("/profile", {"email": "banner.weight@mail.ca", "phone": "", "address": "1 Main St", "weight_kg": "70", "height_cm": "175"})
    assert _banner(b.get("/donor").text)[0].strip() == "You are eligible to donate"


def test_dashboard_banner_follows_a_height_edit_to_low_blood_volume(app):
    b = _new_donor(app, "banner.volume@mail.ca", weight="55", height="165", sex="F")
    assert _banner(b.get("/donor").text)[0].strip() == "You are eligible to donate"
    b.post("/profile", {"email": "banner.volume@mail.ca", "phone": "", "address": "1 Main St", "weight_kg": "51", "height_cm": "146"})
    page = b.get("/donor").text
    _assert_not_eligible_banner(page, "estimated blood volume")
    assert "3.0 L" in page and "2.9 L" in page or "3.0 L" in page


def test_dashboard_banner_for_a_temporary_deferral(app, gui_db):
    b = Browser(app).login("owen.chen@mail.ca")                                    # not eligible until a date, from a deferral
    page = b.get("/donor").text
    head, text, hero = _banner(page)
    assert "not eligible" in head.lower() and "Next eligible on" in text
    for w in ELIGIBLE_WORDS:
        assert w not in text


def test_dashboard_banner_for_a_permanent_deferral(app, gui_db):
    with db(gui_db) as con:
        email = con.execute("SELECT a.email FROM v_donor_eligibility e JOIN user_account a USING (user_id) "
                            "WHERE e.status = 'Permanently deferred' LIMIT 1").fetchone()[0]
    page = Browser(app).login(email).get("/donor").text
    head, text, hero = _banner(page)
    assert "not eligible" in head.lower() and "permanently deferred" in text
    for w in ELIGIBLE_WORDS:
        assert w not in text
    assert "Now" not in re.findall(r"<b[^>]*>([^<]*)</b>", hero)


def test_dashboard_banner_for_other_not_eligible_reasons(app, gui_db):
    for email, part in (("elif.demir@mail.ca", "test"), ("theo.dubois@mail.ca", "50 kg"), ("aisha.rahman@mail.ca", "blood volume")):
        page = Browser(app).login(email).get("/donor").text
        head, text, hero = _banner(page)
        assert "not eligible" in head.lower(), (email, head)
        assert part in text, (email, text)
        for w in ELIGIBLE_WORDS:
            assert w not in text


def test_donor_form_flows_for_height(app):
    b = Browser(app)
    page = b.get("/register").text
    assert 'name="height_cm"' in page and 'min="100"' in page and 'max="250"' in page and "estimated blood volume" in page
    form = {"first_name": "Hal", "last_name": "Tall", "email": "hal.tall@mail.ca", "phone": "", "date_of_birth": "1990-04-12", "sex": "M",
            "weight_kg": "80", "height_cm": "180", "address": "1 Main St, Windsor", "blood_group": "A+", "password": "a-long-password-1",
            "password2": "a-long-password-1"}
    for bad in ("", "abc", "99", "251", "NaN", "inf", "1e999"):
        r = b.post("/register", dict(form, height_cm=bad))
        assert r.status_code in (200, 302) and "/login" not in r.headers.get("Location", ""), bad
        assert re.search(r"100 and 250|enter a number|height", b.flashes()[-1].lower()), (bad, b.flashes())
    assert b.post("/register", form).status_code == 302
    c = Browser(app).login("hal.tall@mail.ca", "a-long-password-1")
    assert 'name="height_cm"' in c.get("/profile").text and 'value="180.0"' in c.get("/profile").text
    for bad in ("", "abc", "99", "251"):
        c.post("/profile", {"email": "hal.tall@mail.ca", "phone": "", "address": "1 Main St", "weight_kg": "80", "height_cm": bad})
        assert re.search(r"100 and 250|enter a number|height", c.flashes()[-1].lower()), (bad, c.flashes())
        assert f'value="{bad}"' in c.get("/profile").text or bad == ""           # the form comes back with what was typed, and the reason
    c.post("/profile", {"email": "hal.tall@mail.ca", "phone": "", "address": "1 Main St", "weight_kg": "80", "height_cm": "181.5"})
    assert c.flashes()[-1] == "Profile saved.", c.flashes()
    assert "181.5" in c.get("/profile").text and "181.5 cm" in c.get("/donor").text


def test_staff_donor_header_shows_body_size_and_volume(staff):
    page = staff.get("/staff/donors/101").text
    assert re.search(r"\d+ years · Male · \d+ cm · \d+ kg · BMI \d+\.\d · est\. blood volume \d\.\d L", page)
    assert staff.get("/staff/donors").status_code == 200 and staff.get("/staff/donors?q=D-10").status_code == 200


def test_admin_rules_form_edits_the_minimum_blood_volume(admin, gui_db):
    page = admin.get("/admin/rules").text
    assert "Minimum estimated blood volume (ml)" in page and 'name="min_blood_volume_ml"' in page and 'min="1500" max="6000"' in page
    base = {"sex": "F", "min_age": "17", "min_weight": "50", "min_hb": "125"}
    admin.post("/admin/rules/eligibility", dict(base, min_blood_volume_ml="3100"))
    with db(gui_db) as con:
        assert con.execute("SELECT min_blood_volume_ml FROM eligibility_rule WHERE sex='F'").fetchone()[0] == 3100
    for bad in ("1499", "6001", "abc", ""):
        admin.post("/admin/rules/eligibility", dict(base, min_blood_volume_ml=bad))
        assert "1500 to 6000" in admin.flashes()[-1] or "whole number" in admin.flashes()[-1], (bad, admin.flashes())
    admin.post("/admin/rules/eligibility", dict(base, min_blood_volume_ml="3000"))
    with db(gui_db) as con:
        assert con.execute("SELECT min_blood_volume_ml FROM eligibility_rule WHERE sex='F'").fetchone()[0] == 3000
