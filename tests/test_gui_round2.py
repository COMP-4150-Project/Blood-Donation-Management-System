"""Round-2 GUI regressions: one test (or a few) per issue fixed in the review of 2026-11-15. Same fixtures as test_gui.py,
on its own database so the order of the two modules does not matter."""
import datetime as _dt
import re

import pytest

from tests.conftest import PREFIX, build, drop
from tests.test_gui import PW, Browser, db, healthy, rendered


@pytest.fixture(scope="module")
def gui_db():
    name = build(PREFIX + "_gui2", with_data=True)
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
def donor(app):
    return Browser(app).login("liam.tremblay@mail.ca")


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


REG = {"first_name": "Rae", "last_name": "Round", "phone": "", "date_of_birth": "1991-05-06", "sex": "F", "weight_kg": "62",
       "height_cm": "168", "address": "2 Main St, Windsor", "blood_group": "", "password": "a-long-password-1", "password2": "a-long-password-1"}


# ------------------------------------------------------------------ security
def test_csrf_on_every_write_method_and_head_is_a_read(app, donor):                         # SEC-ISS-01
    for method in ("put", "delete", "patch"):
        r = getattr(donor.c, method)("/logout")
        assert r.status_code in (302, 405), method
        assert donor.get("/donor").status_code == 200, method                               # still signed in: nothing ran
    assert donor.c.head("/donor/screening").status_code == 200
    assert donor.c.head("/staff/requests/new").status_code in (302, 403)                   # a donor is not let in, HEAD or GET
    b = Browser(app)
    assert b.c.head("/login").status_code == 200 and b.c.head("/register").status_code == 200


def test_sign_in_throttle_per_client(app, monkeypatch):                                     # SEC-ISS-08 (C3)
    import gui.app as ga
    monkeypatch.setattr(ga, "LOGIN_BURST", 3)
    b = Browser(app)
    for _ in range(3):
        b.c.post("/login", data={"email": "nobody@example.com", "password": "nope", "_csrf": b.token()})
    r = b.c.post("/login", data={"email": "admin@vytal.example.ca", "password": PW, "_csrf": b.token()})
    assert r.status_code == 429 and "Too many failed sign-ins" in r.text
    other = Browser(app)
    r = other.c.post("/login", data={"email": "admin@vytal.example.ca", "password": PW, "_csrf": other.token()},
                     environ_base={"REMOTE_ADDR": "10.9.9.9"})
    assert r.status_code == 302                                                             # another client is not blocked


def test_wrong_and_unknown_logins_read_the_same(app):                                       # C3 / SEC-ISS-08
    said = []
    for email in ("nobody@example.com", "liam.tremblay@mail.ca"):
        b = Browser(app)
        b.c.post("/login", data={"email": email, "password": "x", "_csrf": b.token()})
        said.append(b.flashes())
    assert said[0] == said[1] and "Wrong email or password" in said[0][0]


def test_static_files_carry_the_security_headers(app):                                      # L9
    r = Browser(app).c.get("/static/app.js")
    assert r.status_code == 200
    for h in ("X-Frame-Options", "X-Content-Type-Options", "Content-Security-Policy", "Referrer-Policy"):
        assert h in r.headers, h
    assert r.headers.get("Cache-Control") != "no-store"


def test_one_connection_per_request(app, admin, monkeypatch):                                # C13 / issue 38
    import psycopg
    real, n = psycopg.connect, [0]

    def counting(*a, **kw):
        n[0] += 1
        return real(*a, **kw)
    monkeypatch.setattr(psycopg, "connect", counting)
    assert admin.get("/admin").status_code == 200 and n[0] == 1


# ------------------------------------------------------------------ registration and profile
def test_register_reports_every_missing_field_at_once(app):                                  # RSP-11
    b = Browser(app)
    b.post("/register", {"first_name": "", "last_name": "", "email": "", "password": "", "password2": ""})
    with b.c.session_transaction() as s:
        errs = s.get("ferrs") or {}
    for f in ("first_name", "last_name", "email", "date_of_birth", "sex", "weight_kg", "height_cm", "address", "password", "password2"):
        assert f in errs, (f, errs)
    assert all(m[:1].isupper() for m in errs.values()), errs


def test_register_password2_mismatch_is_at_its_field(app):
    b = Browser(app)
    b.post("/register", dict(REG, email="rae.pw2@mail.ca", password2="a-long-password-2"))
    with b.c.session_transaction() as s:
        assert "do not match" in (s.get("ferrs") or {}).get("password2", "")


def test_register_signs_in_and_signed_in_users_skip_register(app):                          # UC-01 step 3, DON-14
    b = Browser(app)
    r = b.post("/register", dict(REG, email="rae.round@mail.ca"))
    assert r.status_code == 302 and r.headers["Location"].endswith("/donor")
    assert b.get("/donor").status_code == 200
    r = b.get("/register")
    assert r.status_code == 302 and r.headers["Location"].endswith("/donor")


@pytest.mark.parametrize("field,value,part", [("phone", "call me", "phone"), ("date_of_birth", "1850-01-01", "120"),
                                              ("date_of_birth", (_dt.date.today() + _dt.timedelta(days=3)).isoformat(), "past")])
def test_register_checks_phone_and_birth_date(app, field, value, part):                     # DON-11
    b = Browser(app)
    b.post("/register", dict(REG, email=f"rae.{field}.{len(value)}@mail.ca", **{field: value}))
    with b.c.session_transaction() as s:
        msg = (s.get("ferrs") or {}).get(field, "")
    assert part in msg.lower() or part in msg, (field, msg)


def test_profile_shows_blood_group_read_only(app, donor):                                    # UC-03 2a
    page = donor.get("/profile").text
    assert "Blood group" in page and 'name="blood_group"' not in page


# ------------------------------------------------------------------ donor
def test_screening_resubmit_is_saved_once(app):                                             # DON-08
    b = Browser(app)
    b.post("/register", dict(REG, email="rae.once@mail.ca"))
    page = b.get("/donor/screening").text
    once = re.search(r'name="_once" value="([^"]+)"', page).group(1)
    qs = re.findall(r'name="q(\d+)" value="no"', page)
    form = dict(healthy(qs), _once=once)
    b.post("/donor/screening", form)
    b.post("/donor/screening", form)
    assert any("already submitted" in m for m in b.flashes())


def test_permanently_deferred_donor_gets_no_screening_form(app, gui_db):                     # DON-09
    with db(gui_db) as con:
        email = con.execute("SELECT a.email FROM donor d JOIN user_account a USING (user_id) WHERE d.next_eligible_date = 'infinity' LIMIT 1").fetchone()[0]
    b = Browser(app).login(email)
    page = b.get("/donor/screening").text
    assert "permanently deferred" in page and 'name="q1"' not in page
    r = b.post("/donor/screening", {"q1": "no"})
    assert r.status_code == 302 and any("permanently deferred" in m for m in b.flashes())


def test_dashboard_numbers_are_in_the_markup(app, donor):                                    # RSP-01
    page = donor.get("/donor").text
    for value, text in re.findall(r'data-count="([^"]*)">([^<]*)<', page):
        assert value == text.strip(), (value, text)


def test_low_blood_volume_banner_uses_ml(app, gui_db):                                       # DON-05 / REV-10 (C11)
    page = Browser(app).login("aisha.rahman@mail.ca").get("/donor").text
    assert re.search(r"\d,\d{3} ml, under the \d,\d{3} ml minimum", page) and " L, under" not in page


def test_flagged_donor_has_no_countdown(app, gui_db):                                       # DON-06 / ADM-15
    page = Browser(app).login("elif.demir@mail.ca").get("/donor").text
    assert "to go</small>" not in page


def _call_one_donor(staff, gui_db):
    """A fresh O+ red cell request at Riverside that cannot be filled, and a call to one eligible donor; returns that donor's email."""
    staff.post("/staff/requests/new", {"urgency": "Routine", "patient_ref": "P-CALL-R2", "recipient_group": "O+", "component": "RBC",
                                       "units": "50", "required_by": later(5)})
    with db(gui_db) as con:
        rid = con.execute("SELECT max(request_id) FROM blood_request WHERE patient_ref = 'P-CALL-R2'").fetchone()[0]
    page = staff.get(f"/staff/requests/{rid}").text
    code = re.search(r'name="donor" value="(D-\d+)"', page).group(1)
    staff.post(f"/staff/requests/{rid}/call", {"donor": code})
    with db(gui_db) as con:
        email = con.execute("SELECT a.email FROM donor d JOIN user_account a USING (user_id) WHERE d.donor_code = %s", (code,)).fetchone()[0]
    return rid, email


def test_notifications_offer_accept_and_decline(app, staff, gui_db):                         # DON-03 (C9)
    rid, email = _call_one_donor(staff, gui_db)
    b = Browser(app).login(email)
    page = b.get("/notifications").text
    m = re.search(r'action="/donor/calls/(\d+)"', page)
    assert m and 'value="accept"' in page and 'name="from" value="notifications"' in page
    r = b.post(f"/donor/calls/{m.group(1)}", {"answer": "accept", "from": "notifications"})
    assert r.headers["Location"].endswith("/notifications")
    page = b.get("/notifications").text
    assert f'action="/donor/calls/{m.group(1)}"' not in page                                # answered: a status, not the buttons
    home = b.get("/donor").text
    assert "Riverside" in home                                                              # DON-07: where to go after accepting


def test_staff_notifications_page_reads_for_staff(app, staff):                               # STF-12
    page = staff.get("/notifications").text
    assert "Donors who accept a call for one of your facility's requests" in page.replace("&#39;", "'")
    assert 'action="/donor/calls/' not in page


# ------------------------------------------------------------------ staff
def test_request_limits_come_from_the_services(app, staff):                                  # REV-14 / STF-04 / STF-05 (C7)
    from vytal import services as svc
    page = staff.get("/staff/requests/new").text
    assert f'max="{svc.MAX_UNITS}"' in page and f'maxlength="{svc.PATIENT_REF_MAX}"' in page
    staff.post("/staff/requests/new", {"urgency": "Routine", "patient_ref": "X" * (svc.PATIENT_REF_MAX + 1), "recipient_group": "A+",
                                       "component": "RBC", "units": str(svc.MAX_UNITS + 1), "required_by": later(3)})
    with staff.c.session_transaction() as s:
        errs, form = s.get("ferrs") or {}, s.get("form") or {}
    assert f"1 to {svc.MAX_UNITS}" in errs["units"] and "patient_ref" in errs and form.get("units") == str(svc.MAX_UNITS + 1)


def test_unknown_group_request_says_what_happens(app, staff):                                # STF-02
    staff.post("/staff/requests/new", {"urgency": "Routine", "patient_ref": "P-UNK-R2", "recipient_group": "", "component": "RBC",
                                       "units": "2", "required_by": later(4)})
    msg = staff.flashes()[-1]
    assert "Nothing is reserved yet" in msg and "Confirm the patient's group" in msg


def test_request_not_found_and_other_facility(app, staff):                                   # STF-07 (C6)
    r = staff.get("/staff/requests/999999")
    assert r.status_code == 302 and any("not found" in m for m in staff.flashes())
    staff.get("/staff/requests/1")                                                          # request 1 is Essex County's
    assert any("another facility" in m for m in staff.flashes())


def test_closed_request_reads_as_closed(app, staff):                                         # STF-08
    page = staff.get("/staff/requests/9").text                                              # Partially fulfilled
    assert "Outcome" in page and "Closed with 1 not supplied" in page and "Issued units" in page and "Reserved units" not in page


def test_inventory_search_paging_and_discard_keeps_the_filter(app, staff, gui_db):           # STF-14, L11
    with rendered(app) as seen:
        staff.get("/staff/inventory")
    ctx = ctx_of(seen, "staff_inventory.html")
    assert ctx["shown"] <= 50 and ctx["pages"] == -(-ctx["total"] // 50)
    if ctx["pages"] > 1:
        page = staff.get("/staff/inventory?page=2").text
        assert "page=1" in page or "/staff/inventory" in page
    with rendered(app) as seen:
        staff.get("/staff/inventory?q=u103-plas")
    ctx = ctx_of(seen, "staff_inventory.html")
    assert [u["unit_id"] if isinstance(u, dict) else u[0] for u in ctx["units"]] == ["U103-PLASMA"]
    page = staff.get("/staff/inventory?status=Available&component=PLASMA").text
    assert 'name="back" value="/staff/inventory?status=Available&amp;component=PLASMA"' in page
    r = staff.post("/staff/units/U104-PLASMA/discard", {"reason": "Bag damaged", "back": "/staff/inventory?status=Available&component=PLASMA"})
    assert r.headers["Location"].endswith("/staff/inventory?status=Available&component=PLASMA")
    r = staff.post("/staff/units/U108-PLASMA/discard", {"reason": "Bag damaged", "back": "//evil.example/x"})
    assert r.headers["Location"].endswith("/staff/inventory")


def test_expired_by_time_is_not_on_the_shelf(app, staff, gui_db):                            # REV-08 / STF-01 (C10)
    with db(gui_db) as con:
        con.execute("UPDATE blood_unit SET expires_at = now() - interval '1 hour' WHERE unit_id = 'U111-PLASMA'")
        con.commit()
    with rendered(app) as seen:
        staff.get("/staff/inventory?status=Available&q=U111-PLASMA")
    assert ctx_of(seen, "staff_inventory.html")["units"] == []
    with rendered(app) as seen:
        staff.get("/staff/inventory?q=U111-PLASMA")
    u = ctx_of(seen, "staff_inventory.html")["units"][0]
    assert (u["status"] if isinstance(u, dict) else u[3]) == "Expired"


def test_tests_page_has_no_default_result(app, staff):                                       # C8 / STF-06 GUI half
    page = staff.get("/staff/tests").text
    if 'name="t_' in page:
        assert ">Choose<" in page and 'selected value="Negative"' not in page
    r = staff.post("/staff/tests/1", {"t_HIV": ""})
    assert r.status_code == 302 and any("at least one test" in m for m in staff.flashes())
    staff.post("/staff/tests/1", {"t_HIV": "Maybe"})
    assert any("Negative or Positive" in m for m in staff.flashes())


def test_components_follow_the_donation_type(app, staff):                                    # REV-11
    from gui import reads
    by_type = reads.components_by_type()
    assert "RBC" not in by_type.get("PLASMA", []) and "RBC" in by_type.get("WB", [])
    staff.post("/staff/donors/101/donate", {"donation_type": "PLS", "volume_ml": "450", "hemoglobin_gl": "140", "components": ["RBC"]})
    with staff.c.session_transaction() as s:
        assert "plasma donation gives" in (s.get("ferrs") or {}).get("components", "").lower()
        assert s["form"]["components"] == ["RBC"]


def test_deferral_end_date_must_be_after_today(app, staff):                                  # STF-11
    staff.post("/staff/donors/101/defer", {"reason": "TRAVEL", "end_date": _dt.date.today().isoformat()})
    with staff.c.session_transaction() as s:
        assert "after today" in (s.get("ferrs") or {}).get("end_date", "")
    page = staff.get("/staff/donors/101").text
    assert re.search(r'name="end_date"[^>]*min="\d{4}-\d\d-\d\d"', page)


def test_staff_donor_unknown_id(app, staff):
    r = staff.get("/staff/donors/999999")
    assert r.status_code == 302 and any("not found" in m for m in staff.flashes())


# ------------------------------------------------------------------ admin
def test_audit_plain_dates_range_error_and_paging(app, admin):                               # ADM-01/02/REV-01, ADM-03/REV-02
    page = admin.get("/admin/audit?since=2026-05-02&until=2026-05-01").text
    assert "before the &#39;from&#39; date" in page or "before the 'from' date" in page
    with rendered(app) as seen:
        page = admin.get("/admin/audit").text
    ctx = ctx_of(seen, "admin_audit.html")
    assert ctx["total"] >= ctx["shown"] and f"of {ctx['total']:,}" in page.replace(" ", "") or f"of {ctx['total']}" in page
    if ctx["pages"] > 1:
        assert ctx["next_url"] and "page=2" in ctx["next_url"] and "Older entries" in page
        with rendered(app) as seen:
            admin.get(ctx["next_url"])
        c2 = ctx_of(seen, "admin_audit.html")
        assert c2["prev_url"] and c2["first"] == 51
    assert "was not understood" in admin.get("/admin/audit?until=9999-12-31").text


def test_audit_user_box_takes_an_email_or_name(app, admin):                                  # ADM-01 (C2 user text)
    with rendered(app) as seen:
        admin.get("/admin/audit?user=admin@vytal.example.ca")
    ctx = ctx_of(seen, "admin_audit.html")
    assert all(r[9] for r in ctx["items"])                                                  # names, not ids (ADM-08)
    assert "No account matches" in admin.get("/admin/audit?user=zzzz-nobody").text


def test_admin_home_names_alerts_and_wastage_months(app, admin):                             # ADM-08/L7, ADM-11/RSP-07/L6, RSP-08/L8
    from gui import reads
    with rendered(app) as seen:
        page = admin.get("/admin").text
    ctx = ctx_of(seen, "admin_home.html")
    assert len(ctx["wastage"]) == reads.WASTAGE_MONTHS
    assert re.search(r'<div class="sr"><table><caption>Units wasted by month', page)
    for value, text in re.findall(r'data-count="([^"]*)">([^<]*)<', page):
        assert value == text.strip()


def test_every_scrolling_table_is_a_labelled_region(app, admin, staff, donor):              # RSP-03
    for who, path in ((admin, "/admin"), (admin, "/admin/users"), (admin, "/admin/audit"), (staff, "/staff"), (staff, "/staff/inventory"),
                      (staff, "/staff/requests"), (staff, "/staff/donors"), (donor, "/donor")):
        page = who.get(path).text
        for tag in re.findall(r'<div class="tablewrap[^"]*"[^>]*>', page):
            assert 'tabindex="0"' in tag and 'role="region"' in tag and "aria-label=" in tag, (path, tag)


def test_users_search_keeps_role_and_says_when_empty(app, admin):                            # ADM-10, C12 partial search
    page = admin.get("/admin/users?role=Staff&q=zzzz").text
    assert 'name="role" value="Staff"' in page and "No accounts match" in page
    with rendered(app) as seen:
        admin.get("/admin/users?q=ross")
    assert any(i["last_name"] == "Ross" for i in ctx_of(seen, "admin_users.html")["items"])


def test_account_edit_page(app, admin, gui_db):                                              # C12 GUI
    with db(gui_db) as con:
        uid = con.execute("SELECT user_id FROM user_account WHERE email = 'm.ross@riverside.example.ca'").fetchone()[0]
    page = admin.get(f"/admin/users/{uid}/edit").text
    assert 'name="first_name" type="text" value="Maya"' in page and 'name="email"' in page
    admin.post(f"/admin/users/{uid}/edit", {"first_name": "", "last_name": "Ross", "email": "not-an-email", "phone": "x"})
    with admin.c.session_transaction() as s:
        errs = s.get("ferrs") or {}
    assert {"first_name", "email", "phone"} <= set(errs)
    r = admin.get("/admin/users/999999/edit")
    assert r.status_code == 302 and any("not found" in m for m in admin.flashes())


def test_facility_thresholds_for_an_unknown_id(app, admin):                                  # ADM-07
    r = admin.get("/admin/facilities?thresholds=999999")
    assert r.status_code == 200 and "That facility was not found." in r.text


def test_demo_facility_cannot_be_deactivated(app, admin, gui_db, monkeypatch):               # SEC-ISS-03 / REV-07
    import gui.app as ga
    with db(gui_db) as con:
        fid, name = con.execute("SELECT f.facility_id, f.name FROM facility f JOIN staff s USING (facility_id) "
                                "JOIN user_account a ON a.user_id = s.user_id WHERE a.email = 'm.ross@riverside.example.ca'").fetchone()
    monkeypatch.setattr(ga, "DEMO_MODE", True)
    monkeypatch.setattr(ga, "DEMO_FACILITIES", {name})
    admin.post(f"/admin/facilities/{fid}/active", {"active": "0"})
    assert any("switched off" in m for m in admin.flashes())
    assert Browser(app).login("m.ross@riverside.example.ca").get("/staff").status_code == 200       # its staff can still sign in


def test_facility_names_wrap_and_messages_use_names(app, admin):                              # RSP-13, C14
    page = admin.get("/admin/facilities").text
    assert '<h2 class="h3" style="overflow-wrap:anywhere">' in page
    admin.post("/admin/facilities/3/active", {"active": "1"})
    assert re.match(r".+ reactivated\.$", admin.flashes()[-1]) and "#3" not in admin.flashes()[-1]


def test_rule_limits_and_field_errors(app, admin):                                           # ADM-16 / REV-12
    page = admin.get("/admin/rules").text
    assert 'min="16" max="25"' in page and "data-confirm" in page
    assert "&lt;b&gt;" not in page                                                           # labels are text, not escaped markup
    admin.post("/admin/rules/eligibility", {"sex": "F", "min_age": "abc", "min_weight": "50", "min_hb": "125", "min_blood_volume_ml": "3000"})
    with admin.c.session_transaction() as s:
        assert "min_age" in (s.get("ferrs") or {})
    admin.post("/admin/rules/reason", {"code": "T1", "description": "x", "default_days": "5"})
    with admin.c.session_transaction() as s:
        assert "2 to 10 letters" in (s.get("ferrs") or {}).get("code", "")
    admin.post("/admin/rules/reason", {"code": "NEWR", "description": "New reason"})
    with admin.c.session_transaction() as s:
        assert "Permanent" in (s.get("ferrs") or {}).get("default_days", "")


def test_rule_alias_lands_on_the_input():                                                    # ADM-16
    from gui.app import RULE_ALIASES
    assert RULE_ALIASES["min_age_years"] == "min_age" and RULE_ALIASES["min_blood_volume"] == "min_blood_volume_ml"


def test_one_day_is_singular(app, admin):                                                    # RSP-10
    from gui.app import _plural
    assert _plural(1, "unit") == "1 unit" and _plural(2, "unit") == "2 units"
    page = admin.get("/admin/rules").text
    assert "1 days" not in page


def test_lookback_by_unit_empty_trace_and_lowercase_code(app, admin):                        # ADM-05, ADM-13
    assert "Enter a donor code" in admin.get("/admin/lookback?code=").text
    with rendered(app) as seen:
        admin.get("/admin/lookback?unit=u114-plasma")
    ctx = ctx_of(seen, "admin_lookback.html")
    assert ctx["code"] == "D-1071" and ctx["trace"] is not None
    with rendered(app) as seen:
        admin.get("/admin/lookback?code=d-1071")
    assert ctx_of(seen, "admin_lookback.html")["trace"] is not None
    page = admin.get("/admin/lookback?unit=U0-NONE").text
    assert "No unit with the id U0-NONE" in page


def test_quarantine_counts_and_discard_returns_to_the_trace(app, admin):                     # ADM-13
    admin.post("/admin/lookback/quarantine", {"code": "d-1071"})
    first = admin.flashes()[-1]
    assert re.match(r"\d+ units? quarantined\.", first) or first.startswith("No units were quarantined"), first
    admin.post("/admin/lookback/quarantine", {"code": "D-1071"})
    assert admin.flashes()[-1].startswith("No units were quarantined")
    r = admin.post("/admin/units/U114-PLASMA/discard", {"code": "d-1071"})
    assert r.headers["Location"].endswith("/admin/lookback?code=D-1071")


def test_end_reason_for_a_deactivated_facility(app, admin, gui_db):                          # SEC (session end messages)
    from gui.app import END_MESSAGES
    assert "facility" in END_MESSAGES and "facility" in END_MESSAGES["facility"].lower()


# ------------------------------------------------------------------ round-2 database features (C8, C9, C12)
def test_retired_reason_is_not_offered_but_can_be_restored(app, admin, staff):              # C12
    admin.post("/admin/rules/reason", {"code": "rtest", "description": "Round two test reason", "default_days": "1"})
    assert admin.flashes()[-1] == "Deferral reason saved."
    assert 'value="RTEST"' in staff.get("/staff/donors/101").text
    admin.post("/admin/rules/reason/RTEST/active", {"active": "0"})
    assert "RTEST retired" in admin.flashes()[-1]
    assert 'value="RTEST"' not in staff.get("/staff/donors/101").text
    staff.post("/staff/donors/101/defer", {"reason": "RTEST"})
    assert any("retired" in m for m in staff.flashes())
    assert ">Restore<" in admin.get("/admin/rules").text
    admin.post("/admin/rules/reason/RTEST/active", {"active": "1"})
    assert "restored" in admin.flashes()[-1] and 'value="RTEST"' in staff.get("/staff/donors/101").text


def test_account_edit_saves_and_search_finds_part_of_an_email(app, admin, gui_db):           # C12
    with db(gui_db) as con:
        uid = con.execute("SELECT user_id FROM user_account WHERE email = 'aisha.rahman@mail.ca'").fetchone()[0]
    assert 'value="aisha.rahman@mail.ca"' in admin.get(f"/admin/users/{uid}/edit").text
    r = admin.post(f"/admin/users/{uid}/edit", {"first_name": "Aisha", "last_name": "Rahman-Ali", "email": "aisha.rahman@mail.ca", "phone": "519-555-0199"})
    assert r.status_code == 302 and "/admin/users?q=" in r.headers["Location"]
    with db(gui_db) as con:
        assert con.execute("SELECT last_name, phone FROM user_account WHERE user_id = %s", (uid,)).fetchone() == ("Rahman-Ali", "519-555-0199")
    with rendered(app) as seen:
        admin.get("/admin/users?q=RAHMAN@MAIL")
    assert [i["user_id"] for i in ctx_of(seen, "admin_users.html")["items"]] == [uid]


def test_accepting_tells_the_staff_and_a_second_answer_is_refused(app, staff, gui_db):      # C9, STF-12
    rid, email = _call_one_donor(staff, gui_db)
    b = Browser(app).login(email)
    call = re.search(r'action="/donor/calls/(\d+)"', b.get("/notifications").text).group(1)
    b.post(f"/donor/calls/{call}", {"answer": "accept"})
    assert b.flashes()[-1].startswith("Thank you. Please come to")
    b.post(f"/donor/calls/{call}", {"answer": "decline"})
    assert "already answered" in b.flashes()[-1]
    assert f"accepted the donation call for request #{rid}" in staff.get("/notifications").text


def test_test_results_say_what_happens_next(app, staff):                                     # C8
    page = staff.get("/staff/tests").text
    m = re.search(r'action="/staff/tests/(\d+)"(.*?)</form>', page, re.S)
    if not m:
        pytest.skip("no donation is waiting for tests in the sample data")
    code = re.search(r'name="t_([A-Z0-9_]+)"', m.group(2)).group(1)
    staff.post(f"/staff/tests/{m.group(1)}", {f"t_{code}": "Negative"})
    msg = staff.flashes()[-1]
    assert msg.startswith("Results saved.") and "(s)" not in msg, msg
