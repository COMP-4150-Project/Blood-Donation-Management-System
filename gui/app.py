"""Vytal web GUI (Step 5): Flask on top of the Step 4 backend.

Three roles share one app. Every screen reads through gui/reads.py and every change goes through vytal/services.py, which
calls the stored procedures, so all business rules stay in the database (DI-03). This file adds sessions, CSRF protection,
role guards and field-level error messages (US-04).

Run:  PGDATABASE=vytal PGUSER=vytal_app PGPASSWORD=... python -m gui.app
"""
import datetime as dt
import decimal
import functools
import os
import re
import secrets
from datetime import timezone

import psycopg
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session as web,
                   url_for)

from gui import reads
from vytal import services as svc
from vytal.services import AccessDenied, InvalidInput

app = Flask(__name__)


class Prefix:
    """Lets the app live under a path prefix (a share proxy such as /s/<id>): links and cookies get the prefix, and a request that
    still carries it has it removed. With VYTAL_PREFIX unset this does nothing."""

    def __init__(self, wsgi, prefix):
        self.wsgi, self.prefix = wsgi, prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi(environ, start_response)


app.config["ASSET_V"] = int(__import__("time").time())
app.wsgi_app = Prefix(app.wsgi_app, os.environ.get("VYTAL_PREFIX", ""))
app.secret_key = os.environ.get("VYTAL_SECRET") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SEND_FILE_MAX_AGE_DEFAULT=3600,
                  TEMPLATES_AUTO_RELOAD=bool(os.environ.get("VYTAL_DEBUG")))
HOME = {"Donor": "donor_home", "Staff": "staff_home", "Admin": "admin_home"}
GROUPS = reads.GROUP_ORDER


# ---------------------------------------------------------------- plumbing
def csrf_token():
    if "csrf" not in web:
        web["csrf"] = secrets.token_urlsafe(24)
    return web["csrf"]


@app.before_request
def _guard():
    g.user = None
    if request.endpoint in (None, "static"):
        return
    if request.method == "POST":                                           # CSRF on every write
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not sent or not secrets.compare_digest(sent, web.get("csrf", "")):
            flash("That page expired. Please try again.", "err")
            return redirect(request.referrer or url_for("landing"))
    token = web.get("t")
    if token:
        try:
            uid, _ = svc.session_user(token)                               # SE-06: idle timeout refreshed here
            g.user = reads.user_card(uid)
        except AccessDenied:
            web.pop("t", None)
            if request.endpoint not in PUBLIC:
                flash("You were signed out after a period of inactivity. Sign in again to continue.", "info")
                return redirect(url_for("login"))


@app.context_processor
def _ctx():
    return {"csrf": csrf_token, "user": g.get("user"), "form": web.pop("form", {}), "ferrs": web.pop("ferrs", {}),
            "GROUPS": GROUPS, "COMPONENTS": reads.COMPONENTS, "now": dt.datetime.now(timezone.utc)}


@app.template_filter("dt")
def _dt(value, fmt="%b %d, %H:%M"):
    return "" if value is None else value.strftime(fmt)


@app.template_filter("iso")
def _iso(value):
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return value.isoformat()


@app.template_filter("comp")
def _comp(code):
    return dict(reads.COMPONENTS).get(code, code)


PUBLIC = {"landing", "login", "register", "static", "logout"}


def roles(*allowed):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if g.user["role"] not in allowed:
                flash("That area is for another kind of account.", "err")
                return redirect(url_for(HOME[g.user["role"]]))
            return fn(*a, **kw)
        return inner
    return wrap


def login_required(fn):
    @functools.wraps(fn)
    def inner(*a, **kw):
        return fn(*a, **kw) if g.user else redirect(url_for("login", next=request.path))
    return inner


def friendly(exc):
    """(field or None, message) for anything the backend or the database can refuse (US-04)."""
    if isinstance(exc, InvalidInput):
        m = re.match(r"^([a-z_]+): (.*)$", str(exc))
        return (m.group(1), m.group(2)) if m else (None, str(exc))
    if isinstance(exc, AccessDenied):
        return None, str(exc)
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return ("email", "That email is already registered.") if "email" in str(exc.diag.constraint_name or exc) else (None, "That record already exists.")
    if isinstance(exc, psycopg.errors.CheckViolation):
        return None, "That value is outside the allowed range."
    if isinstance(exc, psycopg.Error):
        msg = (exc.diag.message_primary or "The database refused that change.").strip()
        return None, msg[0].upper() + msg[1:] + ("" if msg.endswith(".") else ".")
    raise exc


def attempt(action, success=None, back=None, keep=True, **fmt):
    """Run one backend call. On success flash `success` and redirect; on refusal flash the reason, keep the typed values and
    mark the offending field. Returns a redirect response either way."""
    back = back or request.referrer or url_for("landing")
    g.ok = False
    try:
        result = action()
    except (InvalidInput, AccessDenied, psycopg.Error, ValueError) as exc:
        if isinstance(exc, ValueError) and not isinstance(exc, InvalidInput):
            exc = InvalidInput("form: that value is not valid, check the highlighted fields")
        field, msg = friendly(exc)
        if keep:
            web["form"] = {k: v for k, v in request.form.items() if not k.startswith("_") and "password" not in k}
        if field:
            web["ferrs"] = {field: msg}
        flash(msg, "err")
        return redirect(back)
    g.ok = True
    if success:
        flash(success.format(result=result, **fmt) if callable(getattr(success, "format", None)) else success, "ok")
    return redirect(back)


def dec(value, field):
    """numeric(…) parameters need a Decimal: psycopg sends a Python float as double precision, which the procedures do not accept."""
    try:
        return decimal.Decimal(str(value).strip())
    except decimal.InvalidOperation:
        raise InvalidInput(f"{field}: enter a number")


def post_int(name, default=None):
    v = request.form.get(name, "").strip()
    if v == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise InvalidInput(f"{name}: enter a whole number")


def post_date(name):
    v = request.form.get(name, "").strip()
    if not v:
        return None
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise InvalidInput(f"{name}: enter a date like 2026-12-31")


def post_dt(name):
    v = request.form.get(name, "").strip()
    if not v:
        raise InvalidInput(f"{name}: choose a date and time")
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidInput(f"{name}: choose a date and time")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def uid():
    return g.user["user_id"]


# ---------------------------------------------------------------- public
@app.get("/")
def landing():
    if g.user:
        return redirect(url_for(HOME[g.user["role"]]))
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if g.user:
            return redirect(url_for(HOME[g.user["role"]]))
        return render_template("login.html", next=request.args.get("next", ""))
    try:
        token, user_id, role = svc.sign_in(request.form.get("email", "").strip().lower(), request.form.get("password", ""))
    except AccessDenied as exc:
        web["form"] = {"email": request.form.get("email", "")}
        flash(str(exc).capitalize() + ".", "err")
        return redirect(url_for("login"))
    web.clear()                                                            # new session id after login (no fixation)
    web["t"] = token
    csrf_token()
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for(HOME[role]))


@app.post("/logout")
def logout():
    if web.get("t"):
        svc.sign_out(web["t"])
    web.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", today=dt.date.today().isoformat())
    f = request.form

    def go():
        dob = post_date("date_of_birth")
        if dob is None:
            raise InvalidInput("date_of_birth: enter your date of birth")
        if f.get("password") != f.get("password2"):
            raise InvalidInput("password2: the two passwords do not match")
        for need, label in (("first_name", "first name"), ("last_name", "last name"), ("address", "address")):
            if not f.get(need, "").strip():
                raise InvalidInput(f"{need}: enter your {label}")
        weight = f.get("weight_kg", "").strip()
        if not weight:
            raise InvalidInput("weight_kg: enter your weight in kilograms")
        return svc.register_donor(f.get("email", "").strip(), f.get("password", ""), f["first_name"].strip(), f["last_name"].strip(),
                                  f.get("phone", "").strip() or None, dob, f.get("sex", ""), dec(weight, "weight_kg"), f["address"].strip(),
                                  f.get("blood_group") or None)
    resp = attempt(go, back=url_for("register"))
    if g.ok:
        flash("Welcome to Vytal. Sign in to see your eligibility.", "ok")
        return redirect(url_for("login"))
    return resp


# ---------------------------------------------------------------- shared account screens
@app.get("/notifications")
@login_required
def notifications():
    return render_template("notifications.html", items=svc.list_notifications(uid()))


@app.post("/notifications/read")
@login_required
def notifications_read():
    ids = request.form.getlist("id")

    def go():
        for i in ids:
            svc.mark_read(uid(), int(i))
    return attempt(go, "Marked as read.", url_for("notifications"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        f = request.form
        return attempt(lambda: svc.update_profile(uid(), f.get("phone", "").strip(), f.get("email", "").strip(),
                                                  f.get("address", "").strip(), dec(f.get("weight_kg") or 0, "weight_kg")),
                       "Profile saved.", url_for("profile"))
    return render_template("profile.html", p=svc.profile(uid()))


@app.post("/password")
@login_required
def password():
    f = request.form

    def go():
        if f.get("new_password") != f.get("confirm"):
            raise InvalidInput("confirm: the two passwords do not match")
        svc.change_password(uid(), f.get("old_password", ""), f.get("new_password", ""))
    resp = attempt(go, back=url_for("profile"))
    if g.ok:
        svc.sign_out(web.get("t"))
        web.clear()
        flash("Password changed. Sign in with the new one.", "ok")
        return redirect(url_for("login"))
    return resp


# ---------------------------------------------------------------- donor
@app.get("/donor")
@roles("Donor")
def donor_home():
    return render_template("donor_home.html", h=reads.donor_dashboard(uid()))


@app.route("/donor/screening", methods=["GET", "POST"])
@roles("Donor")
def donor_screening():
    qs = reads.screening_questions(uid())
    if request.method == "GET":
        return render_template("donor_screening.html", qs=qs)

    def go():
        if any(request.form.get(f"q{q['question_id']}") not in ("yes", "no") for q in qs):
            raise InvalidInput("form: answer every question")
        return svc.complete_screening(uid(), {q["question_id"]: request.form[f"q{q['question_id']}"] == "yes" for q in qs})
    return attempt(go, "Screening saved. Your status now reads: {result}.", url_for("donor_home"))


@app.post("/donor/calls/<int:call_id>")
@roles("Donor")
def donor_call(call_id):
    accept = request.form.get("answer") == "accept"

    def go():
        return svc.respond_to_call(uid(), call_id, accept)
    return attempt(go, "{result}", url_for("donor_home"))


# ---------------------------------------------------------------- staff
@app.get("/staff")
@roles("Staff")
def staff_home():
    return render_template("staff_home.html", o=reads.staff_overview(uid()))


@app.get("/staff/requests")
@roles("Staff")
def staff_requests():
    status = request.args.get("status", "")
    return render_template("staff_requests.html", items=reads.requests_list(uid(), status or None), status=status)


@app.route("/staff/requests/new", methods=["GET", "POST"])
@roles("Staff")
def staff_request_new():
    if request.method == "GET":
        return render_template("staff_request_new.html")
    f = request.form

    def go():
        units = post_int("units")
        if not units or units < 1:
            raise InvalidInput("units: enter at least 1 unit")
        if not f.get("patient_ref", "").strip():
            raise InvalidInput("patient_ref: enter the patient reference")
        if f.get("component") not in svc.COMPONENTS:
            raise InvalidInput("component: choose a component")
        group = f.get("recipient_group") or None
        if group is not None and group not in svc.GROUPS:
            raise InvalidInput("recipient_group: choose a group")
        if f.get("urgency") not in ("Emergency", "Urgent", "Routine"):
            raise InvalidInput("urgency: choose an urgency")
        return svc.submit_request(uid(), f["patient_ref"].strip(), group, f["component"], units, f["urgency"], post_dt("required_by"))
    try:
        res = go()
    except (InvalidInput, AccessDenied, psycopg.Error) as exc:
        field, msg = friendly(exc)
        web["form"] = {k: v for k, v in f.items() if not k.startswith("_")}
        if field:
            web["ferrs"] = {field: msg}
        flash(msg, "err")
        return redirect(url_for("staff_request_new"))
    if res["shortfall"]:
        flash(f"Request {res['request_id']} saved. {res['reserved']} of {f['units']} units reserved, {res['shortfall']} short. Call donors to close the gap.", "info")
    else:
        flash(f"Request {res['request_id']} saved. All {res['reserved']} units are reserved.", "ok")
    return redirect(url_for("staff_request", request_id=res["request_id"]))


@app.get("/staff/requests/<int:request_id>")
@roles("Staff")
def staff_request(request_id):
    try:
        data = reads.request_detail(uid(), request_id)
    except AccessDenied as exc:
        flash(str(exc).capitalize() + ".", "err")
        return redirect(url_for("staff_requests"))
    return render_template("staff_request.html", **data)


@app.post("/staff/requests/<int:request_id>/call")
@roles("Staff")
def staff_request_call(request_id):
    codes = request.form.getlist("donor") or None
    pref = post_date("preferred") if request.form.get("preferred") else None
    return attempt(lambda: svc.call_donors(uid(), request_id, codes, pref), "Donation call sent. Donors will see it in Vytal.",
                   url_for("staff_request", request_id=request_id))


@app.post("/staff/requests/<int:request_id>/group")
@roles("Staff")
def staff_request_group(request_id):
    return attempt(lambda: svc.confirm_request_group(uid(), request_id, request.form.get("blood_group", "")),
                   "Group confirmed. Compatible stock was reserved.", url_for("staff_request", request_id=request_id))


@app.post("/staff/requests/<int:request_id>/close")
@roles("Staff")
def staff_request_close(request_id):
    cancel = request.form.get("action") == "cancel"
    units = request.form.getlist("unit") if not cancel and request.form.get("pick") else None
    return attempt(lambda: svc.close_request(uid(), request_id, cancel, units),
                   "Request cancelled. Reserved units went back on the shelf." if cancel else "Units issued and the request is closed.",
                   url_for("staff_request", request_id=request_id))


@app.route("/staff/inventory", methods=["GET"])
@roles("Staff")
def staff_inventory():
    a = request.args
    units = reads.units_list(uid(), a.get("status") or None, a.get("component") or None, a.get("group") or None)
    fac, thr = reads.thresholds(uid())
    return render_template("staff_inventory.html", units=units, thresholds=thr, f=a, overview=reads.staff_overview(uid()))


@app.post("/staff/units/<unit_id>/discard")
@roles("Staff")
def staff_unit_discard(unit_id):
    return attempt(lambda: svc.discard_unit(uid(), unit_id, request.form.get("reason", "")), f"Unit {unit_id} discarded.",
                   url_for("staff_inventory"))


@app.post("/staff/thresholds")
@roles("Staff")
def staff_threshold():
    f = request.form
    return attempt(lambda: svc.set_threshold(uid(), g.user["facility"]["facility_id"], f.get("blood_group"), f.get("component"), post_int("min_units", -1)),
                   "Minimum saved.", url_for("staff_inventory") + "#thresholds")


@app.get("/staff/donors")
@roles("Staff")
def staff_donors():
    a = request.args
    ready = a.get("ready") == "1"
    return render_template("staff_donors.html", items=reads.donors_list(uid(), a.get("q"), a.get("group"), ready), f=a)


@app.get("/staff/donors/<int:donor_id>")
@roles("Staff")
def staff_donor(donor_id):
    try:
        data = reads.donor_detail(uid(), donor_id)
        return render_template("staff_donor.html", **data, questions=reads.screening_questions(uid()),
                               type_opts=[(t["donation_type_code"], t["name"]) for t in data["types"]],
                               reason_opts=[(r["reason_code"], r["description"] + (" (permanent)" if r["is_permanent"] else "")) for r in data["reasons"]])
    except InvalidInput as exc:
        flash(str(exc), "err")
        return redirect(url_for("staff_donors"))


@app.post("/staff/donors/<int:donor_id>/screen")
@roles("Staff")
def staff_donor_screen(donor_id):
    qs = reads.screening_questions(uid())

    def go():
        if any(request.form.get(f"q{q['question_id']}") not in ("yes", "no") for q in qs):
            raise InvalidInput("form: answer every screening question")
        return svc.screen_donor(uid(), donor_id, {q["question_id"]: request.form[f"q{q['question_id']}"] == "yes" for q in qs})
    return attempt(go, "Screening recorded. Status: {result}.", url_for("staff_donor", donor_id=donor_id))


@app.post("/staff/donors/<int:donor_id>/verify")
@roles("Staff")
def staff_donor_verify(donor_id):
    return attempt(lambda: svc.verify_blood_group(uid(), donor_id, request.form.get("blood_group", "")),
                   "Blood group verified and recorded under your name.", url_for("staff_donor", donor_id=donor_id))


@app.post("/staff/donors/<int:donor_id>/donate")
@roles("Staff")
def staff_donor_donate(donor_id):
    f = request.form

    def go():
        comps = f.getlist("components")
        if not comps:
            raise InvalidInput("components: choose at least one component")
        vol, hb = post_int("volume_ml"), f.get("hemoglobin_gl", "").strip()
        if not vol or not (50 <= vol <= 1000):
            raise InvalidInput("volume_ml: enter a volume between 50 and 1000 ml")
        if not hb:
            raise InvalidInput("hemoglobin_gl: enter the hemoglobin reading in g/L")
        did, units = svc.record_donation(uid(), donor_id, f.get("donation_type", "WB"), vol, dec(hb, "hemoglobin_gl"), comps)
        return did, units
    try:
        did, units = go()
    except (InvalidInput, AccessDenied, psycopg.Error, ValueError) as exc:
        if isinstance(exc, ValueError) and not isinstance(exc, InvalidInput):
            exc = InvalidInput("hemoglobin_gl: enter a number such as 138")
        field, msg = friendly(exc)
        web["form"] = {k: v for k, v in f.items() if not k.startswith("_")}
        if field:
            web["ferrs"] = {field: msg}
        flash(msg, "err")
        return redirect(url_for("staff_donor", donor_id=donor_id) + "#donate")
    if did is None:
        flash("Hemoglobin was below the minimum, so no donation was recorded and the donor was deferred.", "info")
    else:
        flash(f"Donation {did} recorded. Units created: {', '.join(units)}. They are released once test results are entered.", "ok")
    return redirect(url_for("staff_donor", donor_id=donor_id))


@app.post("/staff/donors/<int:donor_id>/defer")
@roles("Staff")
def staff_donor_defer(donor_id):
    return attempt(lambda: svc.record_deferral(uid(), donor_id, request.form.get("reason", ""), post_date("end_date")),
                   "Deferral recorded. The next eligible date was recalculated.", url_for("staff_donor", donor_id=donor_id))


@app.get("/staff/tests")
@roles("Staff")
def staff_tests():
    return render_template("staff_tests.html", **reads.pending_tests(uid()))


@app.post("/staff/tests/<int:donation_id>")
@roles("Staff")
def staff_tests_save(donation_id):
    results = {k[2:]: v for k, v in request.form.items() if k.startswith("t_")}
    return attempt(lambda: svc.record_tests(uid(), donation_id, results), "Results saved: {result}.", url_for("staff_tests"))


@app.get("/staff/search")
@roles("Staff")
def staff_search():
    group, comp = request.args.get("group", ""), request.args.get("component", "RBC")
    found = None
    if group:
        try:
            found = svc.search_donors(uid(), group, comp)
        except InvalidInput as exc:
            flash(str(exc), "err")
    return render_template("staff_search.html", found=found, group=group, comp=comp)


# ---------------------------------------------------------------- admin
@app.get("/admin")
@roles("Admin")
def admin_home():
    return render_template("admin_home.html", **reads.admin_dashboard(uid()))


@app.post("/admin/expiry")
@roles("Admin")
def admin_expiry():
    return attempt(lambda: svc.nightly_expiry(uid()), "Expiry job finished: {result} unit(s) expired.", url_for("admin_home"))


@app.get("/admin/users")
@roles("Admin")
def admin_users():
    a = request.args
    facs = reads.facilities(uid())
    return render_template("admin_users.html", items=reads.users_list(uid(), a.get("role"), a.get("q")), fac_opts=[(x["facility_id"], x["name"]) for x in facs], f=a)


@app.post("/admin/users/create")
@roles("Admin")
def admin_user_create():
    f = request.form

    def go():
        if f.get("role") == "Admin":
            return svc.create_admin(uid(), f.get("email", "").strip(), f.get("password", ""), f.get("first_name", "").strip(), f.get("last_name", "").strip())
        return svc.create_staff(uid(), f.get("email", "").strip(), f.get("password", ""), f.get("first_name", "").strip(), f.get("last_name", "").strip(),
                                post_int("facility_id"), f.get("job_title", "").strip() or None)
    return attempt(go, "Account created.", url_for("admin_users") + "#create")


@app.post("/admin/users/<int:user_id>/active")
@roles("Admin")
def admin_user_active(user_id):
    active = request.form.get("active") == "1"
    return attempt(lambda: svc.set_account_active(uid(), user_id, active), "Account reactivated." if active else "Account deactivated.", url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/password")
@roles("Admin")
def admin_user_password(user_id):
    return attempt(lambda: svc.admin_reset_password(uid(), user_id, request.form.get("new_password", "")), "Password reset. Open sessions were ended.",
                   url_for("admin_users"), keep=False)


@app.get("/admin/facilities")
@roles("Admin")
def admin_facilities():
    items = reads.facilities_admin(uid())
    th = None
    fid = request.args.get("thresholds", type=int)
    if fid:
        th = (fid, reads.admin_thresholds(uid(), fid))
    return render_template("admin_facilities.html", items=items, th=th)


@app.post("/admin/facilities/save")
@roles("Admin")
def admin_facility_save():
    f = request.form
    return attempt(lambda: svc.save_facility(uid(), post_int("facility_id"), f.get("name", "").strip(), f.get("facility_type", ""),
                                             f.get("address", "").strip(), f.get("phone", "").strip() or None, f.get("email", "").strip() or None),
                   "Facility saved.", url_for("admin_facilities"))


@app.post("/admin/facilities/<int:facility_id>/active")
@roles("Admin")
def admin_facility_active(facility_id):
    active = request.form.get("active") == "1"
    return attempt(lambda: svc.set_facility_active(uid(), facility_id, active), "Facility reactivated." if active else "Facility deactivated.", url_for("admin_facilities"))


@app.post("/admin/facilities/<int:facility_id>/threshold")
@roles("Admin")
def admin_facility_threshold(facility_id):
    f = request.form
    return attempt(lambda: svc.set_threshold(uid(), facility_id, f.get("blood_group"), f.get("component"), post_int("min_units", -1)),
                   "Minimum saved.", url_for("admin_facilities", thresholds=facility_id))


@app.get("/admin/rules")
@roles("Admin")
def admin_rules():
    return render_template("admin_rules.html", r=reads.rules(uid()))


@app.post("/admin/rules/compat")
@roles("Admin")
def admin_rule_compat():
    d = request.get_json(silent=True) or {}
    try:
        svc.set_compatibility(uid(), d.get("component"), d.get("recipient"), d.get("donor"), bool(d.get("allowed")))
        return jsonify(ok=True)
    except (InvalidInput, AccessDenied, psycopg.Error) as exc:
        return jsonify(ok=False, message=friendly(exc)[1]), 422


@app.post("/admin/rules/shelf")
@roles("Admin")
def admin_rule_shelf():
    return attempt(lambda: svc.set_shelf_life(uid(), request.form.get("component"), post_int("days")), "Shelf life saved.", url_for("admin_rules") + "#shelf")


@app.post("/admin/rules/interval")
@roles("Admin")
def admin_rule_interval():
    f = request.form
    return attempt(lambda: svc.set_donation_interval(uid(), f.get("type"), f.get("sex"), post_int("days")), "Donation interval saved.", url_for("admin_rules") + "#interval")


@app.post("/admin/rules/eligibility")
@roles("Admin")
def admin_rule_eligibility():
    f = request.form
    return attempt(lambda: svc.set_eligibility_rule(uid(), f.get("sex"), post_int("min_age"), dec(f.get("min_weight") or 0, "min_weight_kg"), dec(f.get("min_hb") or 0, "min_hemoglobin_gl")),
                   "Eligibility rule saved.", url_for("admin_rules") + "#eligibility")


@app.post("/admin/rules/reason")
@roles("Admin")
def admin_rule_reason():
    f = request.form
    perm = f.get("permanent") == "1"
    return attempt(lambda: svc.save_deferral_reason(uid(), f.get("code", "").strip(), f.get("description", "").strip(), perm,
                                                    None if perm else post_int("default_days")),
                   "Deferral reason saved.", url_for("admin_rules") + "#reasons")


@app.get("/admin/audit")
@roles("Admin")
def admin_audit():
    a = request.args
    flt = _audit_filters(a)
    try:
        items = svc.audit_log(uid(), limit=300, **flt)
    except (InvalidInput, psycopg.Error) as exc:
        flash(friendly(exc)[1], "err")
        items = []
    return render_template("admin_audit.html", items=items, tables=reads.audit_tables(uid()), f=a)


def _audit_filters(a):
    def d(name):
        v = a.get(name, "").strip()
        if not v:
            return None
        try:
            return dt.datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return {"user_id": int(a["user"]) if a.get("user", "").isdigit() else None, "table": a.get("table") or None,
            "record_id": a.get("record") or None, "since": d("since"), "until": d("until")}


@app.get("/admin/audit.csv")
@roles("Admin")
def admin_audit_csv():
    text = svc.audit_csv(uid(), **_audit_filters(request.args))
    return Response(text, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=vytal-audit.csv"})


@app.route("/admin/lookback", methods=["GET"])
@roles("Admin")
def admin_lookback():
    a = request.args
    trace, error = None, None
    code, unit = a.get("code", "").strip() or None, a.get("unit", "").strip() or None
    if code or unit:
        try:
            trace = svc.lookback(uid(), code, unit)
        except InvalidInput as exc:
            error = str(exc)
    donor = None
    if trace is not None and trace:
        donor = trace[0]
    return render_template("admin_lookback.html", trace=trace, error=error, f=a, quarantined=reads.quarantined_units(uid()), code=code, unit=unit)


@app.post("/admin/lookback/quarantine")
@roles("Admin")
def admin_quarantine():
    code = request.form.get("code", "")
    from vytal.db import session as dbs
    with dbs(uid()) as con:
        row = con.execute("SELECT user_id FROM donor WHERE donor_code = %s", (code,)).fetchone()
    if not row:
        flash("No donor with that code.", "err")
        return redirect(url_for("admin_lookback"))
    return attempt(lambda: len(svc.quarantine_donor(uid(), row[0])), "Quarantine applied. Open requests were re-allocated where possible.",
                   url_for("admin_lookback", code=code))


@app.post("/admin/lookback/clear")
@roles("Admin")
def admin_clear():
    code = request.form.get("code", "")
    from vytal.db import session as dbs
    with dbs(uid()) as con:
        row = con.execute("SELECT user_id FROM donor WHERE donor_code = %s", (code,)).fetchone()
    if not row:
        flash("No donor with that code.", "err")
        return redirect(url_for("admin_lookback"))
    return attempt(lambda: svc.clear_lookback(uid(), row[0], request.form.get("note", "")), "Investigation cleared. {result} unit(s) went back on the shelf.",
                   url_for("admin_lookback", code=code))


@app.post("/admin/units/<unit_id>/discard")
@roles("Admin")
def admin_unit_discard(unit_id):
    return attempt(lambda: svc.discard_unit(uid(), unit_id, request.form.get("reason", "Confirmed by lookback")), f"Unit {unit_id} discarded.",
                   url_for("admin_lookback"))


@app.get("/admin/lookback.csv")
@roles("Admin")
def admin_lookback_csv():
    text = svc.lookback_issued_csv(uid(), request.args.get("code", ""))
    return Response(text, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=vytal-issued-units.csv"})


# ---------------------------------------------------------------- errors
@app.errorhandler(404)
def _404(_):
    return render_template("error.html", code=404, title="We could not find that page", text="The link may be old, or the record was removed."), 404


@app.errorhandler(500)
def _500(_):
    return render_template("error.html", code=500, title="Something went wrong on our side", text="Your data was not changed. Try again in a moment."), 500


if __name__ == "__main__":
    app.run(host=os.environ.get("VYTAL_HOST", "127.0.0.1"), port=int(os.environ.get("VYTAL_PORT", "5050")), debug=bool(os.environ.get("VYTAL_DEBUG")))
