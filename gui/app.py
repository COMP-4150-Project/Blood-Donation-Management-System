"""Vytal web GUI (Step 5): Flask on top of the Step 4 backend.

Three roles share one app. Every screen reads through gui/reads.py and every change goes through vytal/services.py, which
calls the stored procedures, so all business rules stay in the database (DI-03). This file adds sessions, CSRF protection,
role guards and field-level error messages (US-04).

Run:  PGDATABASE=vytal PGUSER=vytal_app PGPASSWORD=... python -m gui.app
"""
import csv
import datetime as dt
import decimal
import functools
import hashlib
import io
import os
import re
import secrets
import threading
import time
from datetime import timezone
from pathlib import Path
from urllib.parse import urlsplit

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


def _secure_cookie():
    """Secure cookies everywhere except a local or debug run; VYTAL_SECURE_COOKIE=1/0 overrides."""
    explicit = os.environ.get("VYTAL_SECURE_COOKIE")
    if explicit is not None:
        return explicit == "1"
    local = os.environ.get("VYTAL_HOST", "127.0.0.1") in ("127.0.0.1", "localhost", "::1")
    return not (local or os.environ.get("VYTAL_DEBUG"))


SESSION_HOURS = float(os.environ.get("VYTAL_SESSION_HOURS", "12"))                  # absolute lifetime, however active the user is
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SEND_FILE_MAX_AGE_DEFAULT=3600,
                  SESSION_COOKIE_SECURE=_secure_cookie(), MAX_CONTENT_LENGTH=1024 * 1024,
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=SESSION_HOURS), SESSION_REFRESH_EACH_REQUEST=False,
                  TEMPLATES_AUTO_RELOAD=bool(os.environ.get("VYTAL_DEBUG")))
MAX_ID = 2**31 - 1
HOME = {"Donor": "donor_home", "Staff": "staff_home", "Admin": "admin_home"}
GROUPS = reads.GROUP_ORDER


# ---------------------------------------------------------------- plumbing
def csrf_token():
    if "csrf" not in web:
        web["csrf"] = secrets.token_urlsafe(24)
    return web["csrf"]


def safe_path(target):
    """A same-site path or None: it must start with one slash and carry no scheme, host, backslash or control character, so a
    rejected form post or a login 'next' value can never send the browser to another site."""
    if not target or not isinstance(target, str) or len(target) > 2000:
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target or any(ord(c) < 32 or ord(c) == 127 for c in target):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


def back_url(default=None):
    """Where to send the browser after a rejected post: the page it came from, if that is on this site, else `default`."""
    ref = request.referrer
    if ref:
        try:
            parts = urlsplit(ref)
        except ValueError:
            parts = None
        if parts and parts.netloc == request.host and parts.scheme in ("http", "https"):
            path = parts.path + (("?" + parts.query) if parts.query else "")
            if safe_path(path):
                return path
    return default or url_for("landing")


_recent = {}                                                           # (session, form fingerprint) -> time of the last identical post
_recent_lock = threading.Lock()
DEDUPE = {"register", "staff_request_new", "staff_request_call", "staff_donor_donate", "staff_donor_defer", "admin_user_create",
          "admin_facility_save", "admin_rule_reason", "admin_quarantine", "staff_unit_discard", "admin_unit_discard"}
DOUBLE_SUBMIT_SECONDS = 2.0


def _is_double_submit():
    items = sorted((k, v) for k, v in request.form.items(multi=True) if k != "_csrf")
    key = hashlib.sha256(repr((web.get("csrf"), request.endpoint, tuple(request.view_args.items()), items)).encode()).hexdigest()
    now = time.monotonic()
    with _recent_lock:
        if len(_recent) > 2000:
            for k in [k for k, t in _recent.items() if now - t > DOUBLE_SUBMIT_SECONDS]:
                del _recent[k]
        last = _recent.get(key)
        _recent[key] = now
    return last is not None and now - last < DOUBLE_SUBMIT_SECONDS


@app.before_request
def _guard():
    g.user = None
    if request.endpoint in (None, "static"):
        return
    for v in (request.view_args or {}).values():                       # a path id beyond the integer range can only be a wrong link
        if isinstance(v, int) and v > MAX_ID:
            abort(404)
    if request.method == "POST":                                           # CSRF on every write
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF") or ""
        if not secrets.compare_digest(sent.encode("utf8", "replace"), str(web.get("csrf", "")).encode("utf8", "replace")) or not web.get("csrf"):
            flash("That page expired. Please try again.", "err")
            return redirect(back_url())
    token = web.get("t")
    if token:
        born = web.get("born")
        if born is not None and time.time() - born > SESSION_HOURS * 3600:
            try:
                svc.sign_out(token)
            except psycopg.Error:
                pass
            web.clear()
            if request.endpoint not in PUBLIC:
                flash("For your security you are signed out after a long session. Sign in again to continue.", "info")
                return redirect(url_for("login"))
            return
        try:
            card = reads.session_card(token)                              # SE-06: idle timeout refreshed here, one connection
        except psycopg.Error:
            card = None
        if card is None:
            who = web.get("uid")
            web.pop("t", None)
            if request.endpoint not in PUBLIC:
                why = reads.end_reason(who) if who else "timeout"
                flash({"deactivated": "Your account has been deactivated. Contact an administrator.",
                       "ended": "You were signed out because your password was changed or reset, or you signed out elsewhere. Sign in again to continue.",
                       }.get(why, "You were signed out after a period of inactivity. Sign in again to continue."), "info")
                return redirect(url_for("login"))
        else:
            g.user = card
    if request.method == "POST" and request.endpoint in DEDUPE and _is_double_submit():
        flash("That was already submitted a moment ago, so it was only handled once.", "info")
        return redirect(back_url())


@app.after_request
def _headers(resp):
    if request.endpoint == "static":
        return resp
    h = resp.headers
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                 "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                 "base-uri 'self'; form-action 'self'; object-src 'none'")
    if request.is_secure or request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    h["Cache-Control"] = "no-store"                                        # signed-in pages never sit in a browser or proxy cache
    h["Pragma"] = "no-cache"
    return resp


DEMO_PASSWORD = "Vytal-demo-2026"


def _demo_accounts():
    """Hosted demo only (VYTAL_DEMO=1): every sample account, read from sql/07_sample_data.sql, for the sign-in and sign-up pages."""
    if not os.environ.get("VYTAL_DEMO"):
        return []
    sql = (Path(__file__).resolve().parent.parent / "sql" / "07_sample_data.sql").read_text(encoding="utf8")
    fac = {int(i): n for i, n in re.findall(r"\((\d+), '([^']+)', '(?:Hospital|Blood Bank)'", sql)}
    staff_fac = {int(u): fac.get(int(f), "") for u, f in re.findall(r"\((\d+), (\d), '", sql.split("INSERT INTO staff", 1)[1].split(";", 1)[0])}
    rows = re.findall(r"\(\s*(\d+),\s*'([^']+@[^']+)',\s*:'pw',\s*'([^']*)',\s*'([^']*)',\s*'[^']*',\s*'(Admin|Staff|Donor)'", sql)
    order = {"Admin": 0, "Staff": 1, "Donor": 2}
    out = [{"role": r, "email": e, "name": f"{fn} {ln}", "note": staff_fac.get(int(i), ""), "user_id": int(i)} for i, e, fn, ln, r in rows]
    return sorted(out, key=lambda a: order[a["role"]])


DEMO_ACCOUNTS = _demo_accounts()
DEMO_MODE = bool(os.environ.get("VYTAL_DEMO"))
DEMO_IDS = {a["user_id"] for a in DEMO_ACCOUNTS}


def demo_locked(user_id):
    """Hosted demo: the seeded accounts keep their password and stay active, so no visitor can lock the others out."""
    return DEMO_MODE and user_id in DEMO_IDS


def refuse_demo(user_id, what):
    if demo_locked(user_id):
        raise InvalidInput(f"form: {what} is switched off for the demo accounts so the demo stays usable")


@app.context_processor
def _ctx():
    return {"demo_accounts": DEMO_ACCOUNTS, "demo_password": DEMO_PASSWORD, "csrf": csrf_token, "user": g.get("user"), "form": web.pop("form", {}), "ferrs": web.pop("ferrs", {}),
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


_DB_MESSAGES = {                                                       # sqlstate -> plain message (never the raw database text)
    "22001": "That text is too long.", "22003": "That number is out of range.", "22P02": "That value is not in the expected format.",
    "22007": "That date or time is not valid.", "22008": "That date or time is not valid.", "22021": "That text contains a character that is not allowed.",
    "22P05": "That text contains a character that is not allowed.", "22000": "That value is not valid.", "22012": "That value is not valid.",
    "23502": "A required field is missing.", "23503": "That record is linked to other records, so the change was refused.",
    "23505": "That record already exists.", "23514": "That value is outside the allowed range.", "40001": "Another change happened at the same time. Please try again.",
    "40P01": "Another change happened at the same time. Please try again.", "55P03": "The record is busy. Please try again.",
}


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
    if isinstance(exc, psycopg.OperationalError):
        return None, "The database is not reachable right now. Please try again in a moment."
    if isinstance(exc, psycopg.Error):
        state = exc.sqlstate or ""
        if state in _DB_MESSAGES:
            return None, _DB_MESSAGES[state]
        if state[:2] == "22":
            return None, "That value is not valid."
        if state == "P0001":                                           # a rule raised by our own procedures: written for people
            msg = (exc.diag.message_primary or "").strip()
            if msg:
                return None, msg[0].upper() + msg[1:] + ("" if msg.endswith(".") else ".")
        return None, "The change could not be saved. Check the values and try again."
    raise exc


CAUGHT = (InvalidInput, AccessDenied, psycopg.Error, ValueError, TypeError, ArithmeticError)


def attempt(action, success=None, back=None, keep=True, **fmt):
    """Run one backend call. On success flash `success` and redirect; on refusal flash the reason, keep the typed values and
    mark the offending field. Returns a redirect response either way."""
    back = back or back_url()
    g.ok = False
    try:
        result = action()
    except CAUGHT as exc:
        if not isinstance(exc, (InvalidInput, AccessDenied, psycopg.Error)):
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
        d = decimal.Decimal(str(value).strip())
    except decimal.InvalidOperation:
        raise InvalidInput(f"{field}: enter a number")
    if not d.is_finite() or abs(d) >= 10**9:
        raise InvalidInput(f"{field}: enter a number")
    return d


def post_int(name, default=None):
    v = request.form.get(name, "").strip()
    if v == "":
        return default
    if not re.fullmatch(r"[+-]?[0-9]{1,10}", v) or abs(int(v)) > MAX_ID:
        raise InvalidInput(f"{name}: enter a whole number")
    return int(v)


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
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        d.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidInput(f"{name}: choose a date and time")
    return d


def need(name, label, source=None, maxlen=500):
    """A required text field: trimmed, and refused when blank or absurdly long (SE-09)."""
    v = (source if source is not None else request.form).get(name, "")
    v = v.strip() if isinstance(v, str) else ""
    if not v:
        raise InvalidInput(f"{name}: enter {label}")
    if len(v) > maxlen:
        raise InvalidInput(f"{name}: use at most {maxlen} characters")
    return v


def opt(name, maxlen=500):
    v = (request.form.get(name, "") or "").strip()
    if len(v) > maxlen:
        raise InvalidInput(f"{name}: use at most {maxlen} characters")
    return v or None


def check_password_shape(password, field="password"):
    """At least 10 characters (the service enforces that too) and not only digits."""
    if len(password or "") < 10:
        raise InvalidInput(f"{field}: use at least 10 characters")
    if (password or "").isdigit():
        raise InvalidInput(f"{field}: add letters or symbols, a password of only digits is too easy to guess")


def norm_group(v):
    """A group from a URL: 'A+' arrives as 'A ' because a bare plus is a space in a query string."""
    v = v or ""
    if v.endswith(" ") and v.strip() + "+" in GROUPS_SET:
        return v.strip() + "+"
    return v.strip()


GROUPS_SET = set(reads.GROUP_ORDER)
UNIT_STATUSES = ("Collected", "Available", "Reserved", "Issued", "Discarded", "Expired", "Quarantined")
REQUEST_STATUSES = ("Open", "Fulfilled", "Partially Fulfilled", "Cancelled")


def arg_choice(name, allowed, label, norm=None):
    """A GET filter that must be one of `allowed`; anything else gets a friendly message and is ignored (never a 500)."""
    v = request.args.get(name, "")
    v = norm(v) if norm else v.strip()
    if not v:
        return None
    if v not in allowed:
        flash(f"The {label} filter was not recognised, so it was ignored.", "err")
        return None
    return v


def csv_safe(text):
    """Spreadsheet formula neutralisation: a cell starting with = + - @ tab or CR is prefixed with an apostrophe."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    for row in csv.reader(io.StringIO(text)):
        w.writerow(["'" + c if c[:1] in ("=", "+", "-", "@", "\t", "\r") else c for c in row])
    return out.getvalue()


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
        return render_template("login.html", next=safe_path(request.args.get("next", "")) or "")
    try:
        email = request.form.get("email", "").strip().lower()
        if len(email) > 254 or len(request.form.get("password", "")) > 1000:
            raise AccessDenied("wrong email or password")
        token, user_id, role = svc.sign_in(email, request.form.get("password", ""))
    except AccessDenied as exc:
        web["form"] = {"email": request.form.get("email", "")[:254]}
        flash(str(exc).capitalize() + ".", "err")
        return redirect(url_for("login"))
    except (ValueError, psycopg.Error):                                    # a NUL byte, an overlong value: same answer as a wrong password
        web["form"] = {"email": request.form.get("email", "").replace("\x00", "")[:254]}
        flash("Wrong email or password.", "err")
        return redirect(url_for("login"))
    web.clear()                                                            # new session id after login (no fixation)
    web["t"] = token
    web["uid"] = user_id
    web["born"] = time.time()
    web.permanent = True
    csrf_token()
    nxt = safe_path(request.form.get("next", ""))
    return redirect(nxt or url_for(HOME[role]))


@app.post("/logout")
def logout():
    if web.get("t"):
        try:
            svc.sign_out(web["t"])
        except psycopg.Error:
            pass
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
        first, last, address = need("first_name", "your first name", maxlen=100), need("last_name", "your last name", maxlen=100), need("address", "your address")
        weight = f.get("weight_kg", "").strip()
        if not weight:
            raise InvalidInput("weight_kg: enter your weight in kilograms")
        height = f.get("height_cm", "").strip()
        if not height:
            raise InvalidInput("height_cm: enter your height in centimetres")
        check_password_shape(f.get("password", ""))
        return svc.register_donor(f.get("email", "").strip(), f.get("password", ""), first, last, opt("phone", 40), dob, f.get("sex", ""),
                                  dec(weight, "weight_kg"), dec(height, "height_cm"), address, f.get("blood_group") or None)
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
        for i in ids[:500]:
            if not re.fullmatch(r"[0-9]{1,10}", i) or int(i) > MAX_ID:
                raise InvalidInput("form: that notification does not exist")
            svc.mark_read(uid(), int(i))
    return attempt(go, "Marked as read.", url_for("notifications"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        f = request.form

        def go():
            refuse_demo(uid(), "changing the email") if f.get("email", "").strip() and f.get("email", "").strip() != svc.profile(uid()).get("email") else None
            return svc.update_profile(uid(), f.get("phone", "").strip(), f.get("email", "").strip(), need("address", "your address"),
                                      dec(f.get("weight_kg") or 0, "weight_kg"), dec(f.get("height_cm") or 0, "height_cm"))
        return attempt(go, "Profile saved.", url_for("profile"))
    return render_template("profile.html", p=svc.profile(uid()))


@app.post("/password")
@login_required
def password():
    f = request.form

    def go():
        refuse_demo(uid(), "changing the password")
        if f.get("new_password") != f.get("confirm"):
            raise InvalidInput("confirm: the two passwords do not match")
        check_password_shape(f.get("new_password", ""), "new_password")
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
    h = reads.donor_dashboard(uid())
    return render_template("donor_home.html", h=h, elig_status=h["elig_status"], closed_calls=h["closed_calls"])


@app.route("/donor/screening", methods=["GET", "POST"])
@roles("Donor")
def donor_screening():
    qs = reads.screening_questions(uid())
    if request.method == "GET":
        return render_template("donor_screening.html", qs=qs, answers={}, donor_sex=reads.donor_sex(uid()))
    answers = _answers(qs)
    if len(answers) < len(qs):                                             # keep what was ticked and show the form again
        flash("Answer every question before you submit.", "err")
        return render_template("donor_screening.html", qs=qs, answers=answers, donor_sex=reads.donor_sex(uid()), missing=[q["question_id"] for q in qs if q["question_id"] not in answers]), 200
    return attempt(lambda: svc.complete_screening(uid(), {q["question_id"]: answers[q["question_id"]] == "yes" for q in qs}),
                   "Screening saved. Your status now reads: {result}.", url_for("donor_home"))


def _answers(qs):
    """{question_id: 'yes'|'no'} for the questions that were answered in the posted form."""
    out = {}
    for q in qs:
        v = request.form.get(f"q{q['question_id']}")
        if v in ("yes", "no"):
            out[q["question_id"]] = v
    return out


@app.post("/donor/calls/<int:call_id>")
@roles("Donor")
def donor_call(call_id):
    if request.form.get("answer") not in ("accept", "decline"):
        flash("Choose whether you can help.", "err")
        return redirect(url_for("donor_home"))
    accept = request.form.get("answer") == "accept"
    try:
        result = svc.respond_to_call(uid(), call_id, accept)
    except (AccessDenied, InvalidInput, psycopg.Error) as exc:
        flash(friendly(exc)[1], "err")
        return redirect(url_for("donor_home"))
    if result == "closed":
        flash("This call has closed, thank you.", "info")
    else:
        flash(result, "ok")
    return redirect(url_for("donor_home"))


# ---------------------------------------------------------------- staff
@app.get("/staff")
@roles("Staff")
def staff_home():
    return render_template("staff_home.html", o=reads.staff_overview(uid()))


@app.get("/staff/requests")
@roles("Staff")
def staff_requests():
    status = arg_choice("status", REQUEST_STATUSES, "status") or ""
    items = reads.requests_list(uid(), status or None)
    return render_template("staff_requests.html", items=items, status=status, shown=len(items), total=reads.requests_total(uid(), status or None))


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
        if units > 100:
            raise InvalidInput("units: a single request is limited to 100 units")
        patient_ref = need("patient_ref", "the patient reference", maxlen=60)
        if f.get("component") not in svc.COMPONENTS:
            raise InvalidInput("component: choose a component")
        group = f.get("recipient_group") or None
        if group is not None and group not in svc.GROUPS:
            raise InvalidInput("recipient_group: choose a group")
        if f.get("urgency") not in ("Emergency", "Urgent", "Routine"):
            raise InvalidInput("urgency: choose an urgency")
        return svc.submit_request(uid(), patient_ref, group, f["component"], units, f["urgency"], post_dt("required_by"))
    try:
        res = go()
    except CAUGHT as exc:
        if not isinstance(exc, (InvalidInput, AccessDenied, psycopg.Error)):
            exc = InvalidInput("form: that value is not valid, check the highlighted fields")
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
    except (AccessDenied, psycopg.Error) as exc:
        flash(friendly(exc)[1].capitalize() if isinstance(exc, AccessDenied) else friendly(exc)[1], "err")
        return redirect(url_for("staff_requests"))
    return render_template("staff_request.html", **data)


@app.post("/staff/requests/<int:request_id>/call")
@roles("Staff")
def staff_request_call(request_id):
    back = url_for("staff_request", request_id=request_id)

    def go():
        pref = post_date("preferred")
        if pref is not None and pref < dt.date.today():
            raise InvalidInput("preferred: choose today or a later date")
        picked = [c.strip() for c in request.form.getlist("donor") if c.strip()]
        if any(not re.fullmatch(r"[A-Za-z0-9-]{1,20}", c) for c in picked):
            raise InvalidInput("form: one of the ticked donors is not valid")
        call_all = request.form.get("all") == "1"                          # the explicit 'call all eligible donors' action
        if not picked and not call_all:
            raise InvalidInput("form: Tick at least one donor")
        already = reads.called_codes(uid(), request_id)
        if call_all:
            picked = [d[0] for d in svc.find_donors(uid(), request_id)]
        fresh = [c for c in dict.fromkeys(picked) if c not in already]
        if not fresh:
            raise InvalidInput("form: Those donors were already called for this request")
        sent = svc.call_donors(uid(), request_id, fresh, pref)
        if len(fresh) < len(set(picked)):
            flash(f"{len(set(picked)) - len(fresh)} donor(s) had already been called and were skipped.", "info")
        return sent
    return attempt(go, "Donation call sent. Donors will see it in Vytal.", back)


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
    if units is not None and not [u for u in units if u.strip()]:
        flash("Tick at least one unit to issue.", "err")
        return redirect(url_for("staff_request", request_id=request_id))
    return attempt(lambda: svc.close_request(uid(), request_id, cancel, [u.strip() for u in units] if units is not None else None),
                   "Request cancelled. Reserved units went back on the shelf." if cancel else "Units issued and the request is closed.",
                   url_for("staff_request", request_id=request_id))


@app.route("/staff/inventory", methods=["GET"])
@roles("Staff")
def staff_inventory():
    a = request.args
    status = arg_choice("status", UNIT_STATUSES, "status")
    comp = arg_choice("component", dict(reads.COMPONENTS), "component")
    group = arg_choice("group", GROUPS_SET, "blood group", norm_group)
    units = reads.units_list(uid(), status, comp, group)
    fac, thr = reads.thresholds(uid())
    return render_template("staff_inventory.html", units=units, thresholds=thr, f=a, overview=reads.staff_overview(uid()),
                           shown=len(units), total=reads.units_total(uid(), status, comp, group))


@app.post("/staff/units/<unit_id>/discard")
@roles("Staff")
def staff_unit_discard(unit_id):
    return attempt(lambda: svc.discard_unit(uid(), unit_id, need("reason", "a reason for discarding the unit", maxlen=300)), f"Unit {unit_id} discarded.",
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
    q = a.get("q", "").strip()[:60] or None
    group = arg_choice("group", GROUPS_SET, "blood group", norm_group)
    items = reads.donors_list(uid(), q, group, ready)
    return render_template("staff_donors.html", items=items, f=a, shown=len(items), total=reads.donors_total(uid(), q, group, ready))


@app.get("/staff/donors/<int:donor_id>")
@roles("Staff")
def staff_donor(donor_id):
    try:
        return _render_donor(donor_id)
    except InvalidInput as exc:
        flash(str(exc).capitalize() + ".", "err")
        return redirect(url_for("staff_donors"))


def _render_donor(donor_id, answers=None, missing=()):
    data = reads.donor_detail(uid(), donor_id)
    return render_template("staff_donor.html", **data, questions=reads.screening_questions(uid()), answers=answers or {}, missing=list(missing),
                           type_opts=[(t["donation_type_code"], t["name"]) for t in data["types"]],
                           reason_opts=[(r["reason_code"], r["description"] + (" (permanent)" if r["is_permanent"] else "")) for r in data["reasons"]])


@app.post("/staff/donors/<int:donor_id>/screen")
@roles("Staff")
def staff_donor_screen(donor_id):
    qs = reads.screening_questions(uid())

    answers = _answers(qs)
    if len(answers) < len(qs):
        flash("Answer every screening question before you submit.", "err")
        try:
            return _render_donor(donor_id, answers, [q["question_id"] for q in qs if q["question_id"] not in answers]), 200
        except InvalidInput as exc:
            flash(str(exc).capitalize() + ".", "err")
            return redirect(url_for("staff_donors"))
    return attempt(lambda: svc.screen_donor(uid(), donor_id, {q["question_id"]: answers[q["question_id"]] == "yes" for q in qs}),
                   "Screening recorded. Status: {result}.", url_for("staff_donor", donor_id=donor_id))


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
        did, units = svc.record_donation(uid(), donor_id, f.get("donation_type", "WB"), vol, dec(hb, "hemoglobin_gl"), [c.strip() for c in comps])
        return did, units
    try:
        did, units = go()
    except CAUGHT as exc:
        if not isinstance(exc, (InvalidInput, AccessDenied, psycopg.Error)):
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
    return attempt(lambda: svc.record_deferral(uid(), donor_id, need("reason", "a deferral reason", maxlen=40), post_date("end_date")),
                   "Deferral recorded. The next eligible date was recalculated.", url_for("staff_donor", donor_id=donor_id))


@app.get("/staff/tests")
@roles("Staff")
def staff_tests():
    return render_template("staff_tests.html", **reads.pending_tests(uid()))


@app.post("/staff/tests/<int:donation_id>")
@roles("Staff")
def staff_tests_save(donation_id):
    results = {k[2:]: v for k, v in request.form.items() if k.startswith("t_")}
    if not results:
        flash("Enter at least one test result.", "err")
        return redirect(url_for("staff_tests"))
    return attempt(lambda: svc.record_tests(uid(), donation_id, results), "Results saved: {result}.", url_for("staff_tests"))


@app.get("/staff/search")
@roles("Staff")
def staff_search():
    group, comp = norm_group(request.args.get("group", "")), request.args.get("component", "RBC").strip()
    found = None
    if group:
        try:
            found = svc.search_donors(uid(), group, comp)
        except (InvalidInput, psycopg.Error) as exc:
            flash(friendly(exc)[1], "err")
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
    role = arg_choice("role", ("Donor", "Staff", "Admin"), "role")
    q = a.get("q", "").strip()[:100] or None
    items = reads.users_list(uid(), role, q)
    return render_template("admin_users.html", items=items, fac_opts=[(x["facility_id"], x["name"]) for x in facs], f=a,
                           shown=len(items), total=reads.users_total(uid(), role, q))


@app.post("/admin/users/create")
@roles("Admin")
def admin_user_create():
    f = request.form

    def go():
        if f.get("role") not in ("Admin", "Staff"):
            raise InvalidInput("role: choose Staff or Admin")
        email, first, last = need("email", "an email address", maxlen=254), need("first_name", "a first name", maxlen=100), need("last_name", "a last name", maxlen=100)
        check_password_shape(f.get("password", ""))
        if f.get("role") == "Admin":
            return svc.create_admin(uid(), email, f.get("password", ""), first, last)
        if post_int("facility_id") is None:
            raise InvalidInput("facility_id: choose a facility")
        return svc.create_staff(uid(), email, f.get("password", ""), first, last, post_int("facility_id"), opt("job_title", 100))
    return attempt(go, "Account created.", url_for("admin_users") + "#create")


@app.post("/admin/users/<int:user_id>/active")
@roles("Admin")
def admin_user_active(user_id):
    active = request.form.get("active") == "1"

    def go():
        if not active:
            refuse_demo(user_id, "deactivating an account")
        return svc.set_account_active(uid(), user_id, active)
    return attempt(go, "Account reactivated." if active else "Account deactivated.", url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/password")
@roles("Admin")
def admin_user_password(user_id):
    def go():
        refuse_demo(user_id, "resetting the password")
        check_password_shape(request.form.get("new_password", ""), "new_password")
        return svc.admin_reset_password(uid(), user_id, request.form.get("new_password", ""))
    return attempt(go, "Password reset. Open sessions were ended.", url_for("admin_users"), keep=False)


@app.get("/admin/facilities")
@roles("Admin")
def admin_facilities():
    items = reads.facilities_admin(uid())
    th = None
    raw = request.args.get("thresholds", "").strip()
    fid = int(raw) if re.fullmatch(r"[0-9]{1,9}", raw) else None
    if raw and fid is None:
        flash("That facility link is not valid.", "err")
    if fid:
        th = (fid, reads.admin_thresholds(uid(), fid))
    return render_template("admin_facilities.html", items=items, th=th)


@app.post("/admin/facilities/save")
@roles("Admin")
def admin_facility_save():
    f = request.form
    def go():
        name, address = need("name", "the facility name", maxlen=150), need("address", "the facility address", maxlen=300)
        if f.get("facility_type") not in ("Hospital", "Blood Bank"):
            raise InvalidInput("facility_type: choose Hospital or Blood Bank")
        return svc.save_facility(uid(), post_int("facility_id"), name, f.get("facility_type", ""), address, opt("phone", 40), opt("email", 254))
    return attempt(go, "Facility saved.", url_for("admin_facilities"))


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
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("component", "recipient", "donor")):
        return jsonify(ok=False, message="Choose a component and two of the eight blood groups."), 422
    try:
        svc.set_compatibility(uid(), d.get("component"), d.get("recipient"), d.get("donor"), d.get("allowed") is True)
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
    return attempt(lambda: svc.set_eligibility_rule(uid(), f.get("sex"), post_int("min_age"), dec(f.get("min_weight") or 0, "min_weight_kg"), dec(f.get("min_hb") or 0, "min_hemoglobin_gl"), post_int("min_blood_volume_ml")),
                   "Eligibility rule saved.", url_for("admin_rules") + "#eligibility")


@app.post("/admin/rules/reason")
@roles("Admin")
def admin_rule_reason():
    f = request.form
    perm = f.get("permanent") == "1"
    return attempt(lambda: svc.save_deferral_reason(uid(), need("code", "a reason code", maxlen=40), need("description", "a description", maxlen=200), perm,
                                                    None if perm else post_int("default_days")),
                   "Deferral reason saved.", url_for("admin_rules") + "#reasons")


AUDIT_PAGE = 50


@app.get("/admin/audit")
@roles("Admin")
def admin_audit():
    a = request.args
    flt = _audit_filters(a, flash_errors=True)
    page = int(a["page"]) if re.fullmatch(r"[0-9]{1,6}", a.get("page", "")) and int(a["page"]) >= 1 else 1
    try:
        items, has_next = _audit_page(flt, page)
    except (InvalidInput, psycopg.Error) as exc:
        flash(friendly(exc)[1], "err")
        items, has_next = [], False
    return render_template("admin_audit.html", items=items, tables=reads.audit_tables(uid()), f=a, page=page, per_page=AUDIT_PAGE,
                           has_next=has_next, has_prev=page > 1, shown=len(items))


def _audit_page(flt, page):
    """One page of the audit log (oldest first). Uses limit/offset when the service has them, otherwise slices a longer read."""
    import inspect
    params = inspect.signature(svc.audit_log).parameters
    if "offset" in params:
        rows_ = svc.audit_log(uid(), limit=AUDIT_PAGE + 1, offset=(page - 1) * AUDIT_PAGE, **flt)
        return rows_[:AUDIT_PAGE], len(rows_) > AUDIT_PAGE
    rows_ = svc.audit_log(uid(), limit=page * AUDIT_PAGE + 1, **flt)
    return rows_[(page - 1) * AUDIT_PAGE: page * AUDIT_PAGE], len(rows_) > page * AUDIT_PAGE


def _audit_filters(a, flash_errors=False):
    def d(name, end=False):
        v = a.get(name, "").strip()
        if not v:
            return None
        try:
            when = dt.datetime.fromisoformat(v)
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            when.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            if flash_errors:
                flash(f"The {'to' if end else 'from'} date was not understood (use a date like 2026-12-31), so it was ignored.", "err")
            return None
        if end and len(v) <= 10:                                           # a bare date means the whole day: the log is read up to, not including, the next midnight
            try:
                when = when + dt.timedelta(days=1)
            except OverflowError:
                return None
        return when
    user = a.get("user", "").strip()
    if user and flash_errors and not re.fullmatch(r"[0-9]{1,9}", user):
        flash("The user filter must be a user number, so it was ignored.", "err")
    return {"user_id": int(user) if re.fullmatch(r"[0-9]{1,9}", user) else None, "table": a.get("table", "").strip()[:63] or None,
            "record_id": a.get("record", "").strip()[:40] or None, "since": d("since"), "until": d("until", end=True)}


@app.get("/admin/audit.csv")
@roles("Admin")
def admin_audit_csv():
    text = csv_safe(svc.audit_csv(uid(), **_audit_filters(request.args)))
    return Response(text, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=vytal-audit.csv"})


@app.route("/admin/lookback", methods=["GET"])
@roles("Admin")
def admin_lookback():
    a = request.args
    trace, error = None, None
    code, unit = a.get("code", "").strip()[:40] or None, a.get("unit", "").strip()[:40] or None
    if code or unit:
        try:
            trace = svc.lookback(uid(), code, unit)
        except (InvalidInput, psycopg.Error) as exc:
            error = friendly(exc)[1]
    donor = None
    if trace is not None and trace:
        donor = trace[0]
    return render_template("admin_lookback.html", trace=trace, error=error, f=a, quarantined=reads.quarantined_units(uid()), code=code, unit=unit)


def _donor_by_code(code):
    from vytal.db import session as dbs
    try:
        with dbs(uid()) as con:
            return con.execute("SELECT user_id FROM donor WHERE donor_code = %s", (code,)).fetchone()
    except (psycopg.Error, ValueError):
        return None


@app.post("/admin/lookback/quarantine")
@roles("Admin")
def admin_quarantine():
    code = request.form.get("code", "").strip()[:40]
    row = _donor_by_code(code)
    if not row:
        flash("No donor with that code.", "err")
        return redirect(url_for("admin_lookback"))
    return attempt(lambda: len(svc.quarantine_donor(uid(), row[0])), "Quarantine applied. Open requests were re-allocated where possible.",
                   url_for("admin_lookback", code=code))


@app.post("/admin/lookback/clear")
@roles("Admin")
def admin_clear():
    code = request.form.get("code", "").strip()[:40]
    row = _donor_by_code(code)
    if not row:
        flash("No donor with that code.", "err")
        return redirect(url_for("admin_lookback"))
    return attempt(lambda: svc.clear_lookback(uid(), row[0], need("note", "a note explaining why the investigation is cleared", maxlen=500)), "Investigation cleared. {result} unit(s) went back on the shelf.",
                   url_for("admin_lookback", code=code))


@app.post("/admin/units/<unit_id>/discard")
@roles("Admin")
def admin_unit_discard(unit_id):
    return attempt(lambda: svc.discard_unit(uid(), unit_id, (request.form.get("reason", "") or "").strip() or "Confirmed by lookback"), f"Unit {unit_id} discarded.",
                   url_for("admin_lookback"))


@app.get("/admin/lookback.csv")
@roles("Admin")
def admin_lookback_csv():
    try:
        text = csv_safe(svc.lookback_issued_csv(uid(), request.args.get("code", "").strip()[:40]))
    except (InvalidInput, psycopg.Error) as exc:
        flash(friendly(exc)[1], "err")
        return redirect(url_for("admin_lookback"))
    return Response(text, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=vytal-issued-units.csv"})


# ---------------------------------------------------------------- errors
def _error(code, title, text):
    return render_template("error.html", code=code, title=title, text=text), code


@app.errorhandler(400)
def _400(_):
    return _error(400, "We could not read that request", "Something in the link or form was not valid. Go back and check what you entered.")


@app.errorhandler(404)
def _404(_):
    return _error(404, "We could not find that page", "The link may be old, or the record was removed.")


@app.errorhandler(405)
def _405(_):
    return _error(405, "That action is not available here", "Go back to the page and use its buttons.")


@app.errorhandler(413)
def _413(_):
    return _error(413, "That request was too large", "Send less data and try again.")


@app.errorhandler(500)
def _500(_):
    return _error(500, "Something went wrong on our side", "Your data was not changed. Try again in a moment.")


@app.errorhandler(InvalidInput)
@app.errorhandler(ValueError)
def _bad_value(exc):
    return _error(400, "That value was not valid", friendly(exc)[1] if isinstance(exc, InvalidInput) else "Check the link or form and try again.")


@app.errorhandler(AccessDenied)
def _denied(exc):
    return _error(403, "That is not available to you", str(exc).capitalize() + ".")


@app.errorhandler(psycopg.Error)
def _db(exc):
    state = getattr(exc, "sqlstate", "") or ""
    code = 503 if isinstance(exc, psycopg.OperationalError) else (400 if state[:2] in ("22", "23") else 422)
    return _error(code, "We could not complete that", friendly(exc)[1])


if __name__ == "__main__":
    app.run(host=os.environ.get("VYTAL_HOST", "127.0.0.1"), port=int(os.environ.get("VYTAL_PORT", "5050")), debug=bool(os.environ.get("VYTAL_DEBUG")))
