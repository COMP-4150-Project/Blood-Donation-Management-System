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
from collections import deque
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
          "admin_facility_save", "admin_rule_reason", "admin_quarantine", "staff_unit_discard", "admin_unit_discard", "donor_screening",
          "staff_donor_screen", "admin_user_edit"}
DOUBLE_SUBMIT_SECONDS = 2.0
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


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


def flash_once(message, category="info"):
    """A flash, unless the same text (any case) is already waiting: a refused post that lands on a page which refuses too shows one toast."""
    waiting = [m for _, m in web.get("_flashes", [])]
    if not any(str(m).strip().lower() == str(message).strip().lower() for m in waiting):
        flash(message, category)


END_MESSAGES = {"deactivated": "Your account has been deactivated. Contact an administrator.",
                "facility": "Your facility has been deactivated, so you were signed out. Ask an administrator.",
                "ended": "You were signed out because your password was changed or reset, or you signed out elsewhere. Sign in again to continue."}


@app.before_request
def _guard():
    g.user = None
    if request.endpoint in (None, "static"):
        return
    g.scope = svc.request_scope()                                          # one database connection for the whole request
    g.scope.__enter__()
    for v in (request.view_args or {}).values():                       # a path id beyond the integer range can only be a wrong link
        if isinstance(v, int) and v > MAX_ID:
            abort(404)
    if request.method not in SAFE_METHODS:                                 # CSRF on every write, whatever the method
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
                flash(END_MESSAGES.get(why, "You were signed out after a period of inactivity. Sign in again to continue."), "info")
                return redirect(url_for("login"))
        else:
            g.user = card
    if request.method == "POST" and request.endpoint in DEDUPE and _is_double_submit():
        flash("That was already submitted a moment ago, so it was only handled once.", "info")
        return redirect(back_url())


@app.teardown_request
def _close_scope(exc):
    scope = g.pop("scope", None)
    if scope is not None:
        scope.__exit__(type(exc) if exc else None, exc, exc.__traceback__ if exc else None)


@app.after_request
def _headers(resp):
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
    if request.endpoint == "static":                                       # static files keep their cache lifetime (L9: same headers otherwise)
        return resp
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
        raise InvalidInput(f"form: {what[:1].upper() + what[1:]} is switched off for the demo accounts so the demo stays usable")


DEMO_RULES_LOCKED = ("Clinical rules are read-only on the demo, so every visitor sees the same eligibility and compatibility. "
                     "Stock minimums on Facilities can still be changed.")


def refuse_demo_rules():
    """Hosted demo (SEC-ISS-03): eligibility minimums, intervals, shelf life, compatibility and deferral reasons stay as seeded,
    because one visitor's change would make every donor read as ineligible for the next one. Thresholds stay editable."""
    if DEMO_MODE:
        raise InvalidInput("form: " + DEMO_RULES_LOCKED)


DEMO_FACILITIES = {a["note"] for a in DEMO_ACCOUNTS if a["role"] == "Staff" and a["note"]}


def refuse_demo_facility(facility_id):
    """Hosted demo: deactivating a facility would lock out the seeded staff who work there (SEC-ISS-03)."""
    if not DEMO_MODE:
        return
    names = {f["facility_id"]: f["name"] for f in reads.facilities(uid(), active_only=False)}
    if names.get(facility_id) in DEMO_FACILITIES:
        raise InvalidInput("form: Deactivating a facility with demo staff is switched off so the demo stays usable")


# ---------------------------------------------------------------- sign-in throttle (SEC-ISS-08)
LOGIN_BURST = int(os.environ.get("VYTAL_LOGIN_BURST", "10"))             # failed sign-ins per client ...
LOGIN_WINDOW = float(os.environ.get("VYTAL_LOGIN_WINDOW", "600"))        # ... within this many seconds
_login_fails = {}
_login_lock = threading.Lock()


TRUST_PROXY = os.environ.get("VYTAL_TRUST_PROXY") == "1"     # set only when a reverse proxy (Caddy) is the sole way in


def client_key():
    """The client's address. Only with VYTAL_TRUST_PROXY=1 (the app is reachable through the local reverse proxy alone) is
    X-Forwarded-For read: the proxy appends the real address as the last entry, so that one is used and a client cannot
    spoof it. Without it the header is ignored, since anyone could send one to dodge the throttle (NEW-06)."""
    peer = request.remote_addr or ""
    if not TRUST_PROXY:
        return peer
    fwd = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if fwd and (peer.startswith(("127.", "10.", "192.168.", "::1", "fd", "fc")) or re.match(r"172\.(1[6-9]|2[0-9]|3[01])\.", peer)):
        return fwd[-1]
    return peer


def _recent_fails(key, now):
    q = _login_fails.get(key)
    if q is None:
        return 0
    while q and now - q[0] > LOGIN_WINDOW:
        q.popleft()
    if not q:
        _login_fails.pop(key, None)
        return 0
    return len(q)


def login_throttled(key):
    with _login_lock:
        return _recent_fails(key, time.monotonic()) >= LOGIN_BURST


def note_login_failure(key):
    now = time.monotonic()
    with _login_lock:
        if len(_login_fails) > 10000:
            for k in list(_login_fails):
                _recent_fails(k, now)
        _login_fails.setdefault(key, deque(maxlen=LOGIN_BURST * 2)).append(now)


def sign_in(email, password):
    """svc.sign_in; on the hosted demo the seeded accounts are exempt from the lockout (C3), so nobody can lock them all out:
    the password is checked with exempt_ids first, so a wrong one never reaches the counting sign-in."""
    if DEMO_MODE:
        svc.login(email, password, exempt_ids=frozenset(DEMO_IDS))
    return svc.sign_in(email, password)


def clinic_today():
    return svc.clinic_today()


def local(value):
    """An aware time shown in the clinic's zone (C1); naive values are UTC from the database."""
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(svc.CLINIC_TZ)
    return value


@app.context_processor
def _ctx():
    return {"demo_accounts": DEMO_ACCOUNTS, "demo_password": DEMO_PASSWORD, "csrf": csrf_token, "user": g.get("user"), "form": web.pop("form", {}), "ferrs": web.pop("ferrs", {}),
            "GROUPS": GROUPS, "COMPONENTS": reads.COMPONENTS, "now": dt.datetime.now(timezone.utc), "today": clinic_today().isoformat(),
            "MAX_UNITS": svc.MAX_UNITS, "PATIENT_REF_MAX": svc.PATIENT_REF_MAX, "NAME_MAX": svc.NAME_MAX}


@app.template_filter("dt")
def _dt(value, fmt="%b %d, %H:%M"):
    return "" if value is None else local(value).strftime(fmt)


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


@app.template_filter("nicedates")
def _nicedates(text):
    """ISO dates inside a status text read like the other dates on screen: 'Not eligible until 2027-01-11' -> 'Jan 11, 2027'."""
    def one(m):
        try:
            return dt.date.fromisoformat(m.group(0)).strftime("%b %-d, %Y")
        except ValueError:
            return m.group(0)
    return re.sub(r"\b\d{4}-\d{2}-\d{2}\b", one, text or "")


@app.template_filter("ml")
def _ml(value):
    """Blood volumes in whole ml with a thousands separator (C11): 2979 -> '2,979 ml'."""
    return "" if value is None else f"{int(round(float(value))):,} ml"


@app.template_filter("num")
def _num(value):
    """A stored decimal without a trailing .0: 91.0 -> '91', 181.5 -> '181.5'."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return value


@app.template_filter("plural")
def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


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


CONFLICT_MESSAGE = "Someone else changed the same records at the same moment. Please try again."
_DB_MESSAGES = {                                                       # sqlstate -> plain message (never the raw database text)
    "22001": "That text is too long.", "22003": "That number is out of range.", "22P02": "That value is not in the expected format.",
    "22007": "That date or time is not valid.", "22008": "That date or time is not valid.", "22021": "That text contains a character that is not allowed.",
    "22P05": "That text contains a character that is not allowed.", "22000": "That value is not valid.", "22012": "That value is not valid.",
    "23502": "A required field is missing.", "23503": "That record is linked to other records, so the change was refused.",
    "23505": "That record already exists.", "23514": "That value is outside the allowed range.", "40001": CONFLICT_MESSAGE,
    "40P01": CONFLICT_MESSAGE, "55P03": "The record is busy. Please try again.",
}


FIELD_RE = re.compile(r"^([a-z_][a-z0-9_]*): (.*)$", re.S)


def sentence(msg):
    """A message as a sentence: capital first letter and a full stop (C5: messages arrive capitalised, this is the safety net)."""
    msg = (msg or "").strip()
    if not msg:
        return msg
    msg = msg[0].upper() + msg[1:]
    return msg if msg[-1] in ".!?" else msg + "."


def friendly(exc):
    """(field or None, message) for anything the backend or the database can refuse (US-04). A 'field: Message' text, from the
    services or from one of our own procedures, marks that field."""
    if isinstance(exc, InvalidInput):
        m = FIELD_RE.match(str(exc))
        return (m.group(1), sentence(m.group(2))) if m else (None, sentence(str(exc)))
    if isinstance(exc, AccessDenied):
        return None, sentence(str(exc))
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return ("email", "That email is already registered.") if "email" in str(exc.diag.constraint_name or exc) else (None, "That record already exists.")
    if isinstance(exc, psycopg.errors.CheckViolation):
        return None, "That value is outside the allowed range."
    if isinstance(exc, psycopg.Error) and exc.sqlstate in ("40P01", "40001"):
        # NEW-02: DeadlockDetected and SerializationFailure are OperationalErrors too, but the database is fine
        return None, CONFLICT_MESSAGE
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
                m = FIELD_RE.match(msg)
                if m:
                    return m.group(1), sentence(m.group(2))
                return _guess_field(msg), sentence(msg)
        return None, "The change could not be saved. Check the values and try again."
    raise exc


_MESSAGE_FIELDS = ((re.compile(r"required[- _]by|needed within|in the future", re.I), "required_by"),)


def _guess_field(msg):
    """Older procedure messages carry no field; the few a form can trigger are pinned to their input (STF-11)."""
    for pattern, field in _MESSAGE_FIELDS:
        if pattern.search(msg):
            return field
    return None


CAUGHT = (InvalidInput, AccessDenied, psycopg.Error, ValueError, TypeError, ArithmeticError)


class FieldErrors(InvalidInput):
    """Several fields refused at once (RSP-11): {field: message}."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("form: Some details are missing or not valid. Check the highlighted fields.")


def keep_form():
    web["form"] = {k: v for k, v in request.form.items() if not k.startswith("_") and "password" not in k}


def refuse(exc, aliases=None, keep=True):
    """Flash the reason for a refused post, keep what was typed and mark the field(s). `aliases` maps a service field name to
    the form input that carries it (min_weight_kg -> min_weight)."""
    if not isinstance(exc, (InvalidInput, AccessDenied, psycopg.Error)):
        exc = InvalidInput("form: That value is not valid, check the highlighted fields")
    aliases = aliases or {}
    if keep:
        keep_form()
    if isinstance(exc, FieldErrors):
        web["ferrs"] = {aliases.get(k, k): sentence(v) for k, v in exc.errors.items()}
        msgs = list(web["ferrs"].values())
        flash(msgs[0] if len(msgs) == 1 else f"{len(msgs)} details need attention. Check the highlighted fields.", "err")
        return
    field, msg = friendly(exc)
    if field and field != "form":
        web["ferrs"] = {aliases.get(field, field): msg}
    flash(msg, "err")


def attempt(action, success=None, back=None, keep=True, aliases=None, **fmt):
    """Run one backend call. On success flash `success` and redirect; on refusal flash the reason, keep the typed values and
    mark the offending field. Returns a redirect response either way."""
    back = back or back_url()
    g.ok = False
    try:
        result = action()
    except CAUGHT as exc:
        refuse(exc, aliases, keep)
        return redirect(back)
    g.ok = True
    g.result = result
    if success:
        text = success(result) if callable(success) else success.format(result=result, **fmt)
        flash(text, "ok")
    return redirect(back)


def dec(value, field):
    """numeric(…) parameters need a Decimal: psycopg sends a Python float as double precision, which the procedures do not accept."""
    try:
        d = decimal.Decimal(str(value).strip())
    except decimal.InvalidOperation:
        raise InvalidInput(f"{field}: Enter a number")
    if not d.is_finite() or abs(d) >= 10**9:
        raise InvalidInput(f"{field}: Enter a number")
    return d


def post_int(name, default=None):
    v = request.form.get(name, "").strip()
    if v == "":
        return default
    if not re.fullmatch(r"[+-]?[0-9]{1,10}", v) or abs(int(v)) > MAX_ID:
        raise InvalidInput(f"{name}: Enter a whole number")
    return int(v)


def post_date(name):
    v = request.form.get(name, "").strip()
    if not v:
        return None
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise InvalidInput(f"{name}: Enter a date like 2026-12-31")


def post_dt(name):
    v = request.form.get(name, "").strip()
    if not v:
        raise InvalidInput(f"{name}: Choose a date and time")
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        d = d if d.tzinfo else d.replace(tzinfo=svc.CLINIC_TZ)                # a bare local time is the clinic's (C1)
        d.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidInput(f"{name}: Choose a date and time")
    return d


def need(name, label, source=None, maxlen=500):
    """A required text field: trimmed, and refused when blank or absurdly long (SE-09)."""
    v = (source if source is not None else request.form).get(name, "")
    v = v.strip() if isinstance(v, str) else ""
    if not v:
        raise InvalidInput(f"{name}: Enter {label}")
    if len(v) > maxlen:
        raise InvalidInput(f"{name}: Use at most {maxlen} characters")
    return v


def opt(name, maxlen=500):
    v = (request.form.get(name, "") or "").strip()
    if len(v) > maxlen:
        raise InvalidInput(f"{name}: Use at most {maxlen} characters")
    return v or None


def check_password_shape(password, field="password", email=None):
    """The service's password rule (C4), reported at this form's field."""
    try:
        svc.check_password(password or "", email)
    except InvalidInput as exc:
        m = FIELD_RE.match(str(exc))
        raise InvalidInput(f"{field}: {m.group(2) if m else exc}") from None


PHONE_RE = re.compile(r"\+?[0-9][0-9 ().x-]{5,18}[0-9]")
OLDEST_AGE = 120


def check_phone(phone, field="phone"):
    """DON-11: a phone number has to look like one (the same pattern the services use for facilities)."""
    if phone and not PHONE_RE.fullmatch(phone):
        raise InvalidInput(f"{field}: Enter a phone number like 519-555-0100")
    return phone or None


def check_dob(dob, field="date_of_birth"):
    """DON-11: born in 1900 or later and at most 120 years old, and not in the future (clinic day, C1)."""
    today = clinic_today()
    if dob >= today:
        raise InvalidInput(f"{field}: The date of birth must be in the past")
    if dob.year < 1900 or svc._years_between(dob, today) > OLDEST_AGE:
        raise InvalidInput(f"{field}: Check the year, donors cannot be older than {OLDEST_AGE}")
    return dob


class Collect:
    """Runs several field checks and keeps every refusal, so a form reports all its problems at once (RSP-11)."""

    def __init__(self):
        self.errors = {}

    def __call__(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except InvalidInput as exc:
            m = FIELD_RE.match(str(exc))
            self.errors.setdefault(m.group(1) if m else "form", m.group(2) if m else str(exc))
            return None

    def done(self):
        if self.errors:
            raise FieldErrors(self.errors)


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


WRONG_LOGIN = "Wrong email or password. After 5 wrong attempts an account is locked for 15 minutes."     # C3: one text for every failure
THROTTLED = "Too many failed sign-ins from this device. Wait a few minutes and try again."


def start_web_session(token, user_id):
    web.clear()                                                            # new session id after login (no fixation)
    web["t"] = token
    web["uid"] = user_id
    web["born"] = time.time()
    web.permanent = True
    csrf_token()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method != "POST":                                           # HEAD behaves like GET (SEC-ISS-01)
        if g.user:
            return redirect(url_for(HOME[g.user["role"]]))
        return render_template("login.html", next=safe_path(request.args.get("next", "")) or "")
    key = client_key()
    if login_throttled(key):                                               # SEC-ISS-08: password spraying from one client
        web["form"] = {"email": request.form.get("email", "").replace("\x00", "")[:254]}
        flash(THROTTLED, "err")
        return render_template("login.html", next=safe_path(request.form.get("next", "")) or ""), 429
    try:
        email = request.form.get("email", "").strip().lower()
        if len(email) > 254 or len(request.form.get("password", "")) > 1000:
            raise AccessDenied(WRONG_LOGIN)
        token, user_id, role = sign_in(email, request.form.get("password", ""))
    except AccessDenied as exc:
        note_login_failure(key)
        web["form"] = {"email": request.form.get("email", "")[:254]}
        flash(sentence(str(exc)), "err")
        return redirect(url_for("login"))
    except (ValueError, psycopg.Error):                                    # a NUL byte, an overlong value: same answer as a wrong password
        note_login_failure(key)
        web["form"] = {"email": request.form.get("email", "").replace("\x00", "")[:254]}
        flash(WRONG_LOGIN, "err")
        return redirect(url_for("login"))
    start_web_session(token, user_id)
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


def _register_checks(f):
    """Every field of the sign-up form checked at once (RSP-11); returns the values for svc.register_donor."""
    c = Collect()
    first = c(need, "first_name", "your first name", maxlen=svc.NAME_MAX)
    last = c(need, "last_name", "your last name", maxlen=svc.NAME_MAX)
    email = f.get("email", "").strip()
    if not email:
        c.errors["email"] = "Enter your email address"
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        c.errors["email"] = "Enter an address like name@example.com"
    phone = c(lambda: check_phone(opt("phone", 40)))
    dob = c(post_date, "date_of_birth")
    if dob is None and "date_of_birth" not in c.errors:
        c.errors["date_of_birth"] = "Enter your date of birth"
    elif dob is not None:
        c(check_dob, dob)
    sex = f.get("sex", "")
    if sex not in ("M", "F"):
        c.errors["sex"] = "Choose Female or Male"
    weight = f.get("weight_kg", "").strip()
    if not weight:
        c.errors["weight_kg"] = "Enter your weight in kilograms"
    else:
        weight = c(dec, weight, "weight_kg")
    height = f.get("height_cm", "").strip()
    if not height:
        c.errors["height_cm"] = "Enter your height in centimetres"
    else:
        height = c(dec, height, "height_cm")
    address = c(need, "address", "your address")
    group = f.get("blood_group") or None
    if group is not None and group not in GROUPS_SET:
        c.errors["blood_group"] = "Choose a group or I don't know"
    password = f.get("password", "")
    c(check_password_shape, password, "password", email)
    if not f.get("password2"):
        c.errors["password2"] = "Repeat the password"
    elif password != f.get("password2"):
        c.errors["password2"] = "The two passwords do not match"
    c.done()
    return email, password, first, last, phone, dob, sex, weight, height, address, group


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:                                                             # DON-14: already signed in
        return redirect(url_for(HOME[g.user["role"]]))
    if request.method != "POST":
        return render_template("register.html", today=clinic_today().isoformat())
    f = request.form
    resp = attempt(lambda: svc.register_donor(*_register_checks(f)), back=url_for("register"))
    if not g.ok:
        return resp
    try:                                                                   # UC-01 step 3: the new donor is signed in straight away
        token, user_id, _role_name = sign_in(f.get("email", "").strip().lower(), f.get("password", ""))
    except (AccessDenied, psycopg.Error, ValueError):
        flash("Welcome to Vytal. Sign in to see your eligibility.", "ok")
        return redirect(url_for("login"))
    start_web_session(token, user_id)
    flash("Welcome to Vytal. Complete your health screening to see whether you can donate.", "ok")
    return redirect(url_for("donor_home"))


# ---------------------------------------------------------------- shared account screens
@app.get("/notifications")
@login_required
def notifications():
    return render_template("notifications.html", items=reads.notifications(uid()))


@app.post("/notifications/read")
@login_required
def notifications_read():
    ids = request.form.getlist("id")

    def go():
        for i in ids[:500]:
            if not re.fullmatch(r"[0-9]{1,10}", i) or int(i) > MAX_ID:
                raise InvalidInput("form: That notification does not exist")
            svc.mark_read(uid(), int(i))
    return attempt(go, "Marked as read.", url_for("notifications"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        f = request.form

        def go():
            if f.get("email", "").strip() and f.get("email", "").strip().lower() != (svc.profile(uid()).get("email") or "").lower():
                refuse_demo(uid(), "changing the email")
            c = Collect()
            phone = c(lambda: check_phone(opt("phone", 40)))
            address = c(need, "address", "your address")
            weight = c(dec, f.get("weight_kg") or 0, "weight_kg")
            height = c(dec, f.get("height_cm") or 0, "height_cm")
            c.done()
            return svc.update_profile(uid(), phone or "", f.get("email", "").strip(), address, weight, height)
        return attempt(go, "Profile saved.", url_for("profile"))
    p = svc.profile(uid())
    return render_template("profile.html", p=p, group=reads.donor_group(uid()) if p.get("role") == "Donor" else None)


@app.post("/password")
@login_required
def password():
    f = request.form

    def go():
        refuse_demo(uid(), "changing the password")
        c = Collect()
        if not f.get("old_password"):
            c.errors["old_password"] = "Enter your current password"
        c(check_password_shape, f.get("new_password", ""), "new_password", (svc.profile(uid()).get("email") or ""))
        if f.get("new_password") != f.get("confirm"):
            c.errors["confirm"] = "The two passwords do not match"
        c.done()
        try:
            svc.change_password(uid(), f.get("old_password", ""), f.get("new_password", ""))
        except AccessDenied as exc:                                         # older services: wrong current password
            raise InvalidInput("old_password: The current password is wrong") from exc
    resp = attempt(go, back=url_for("profile"), aliases={"current_password": "old_password", "password": "new_password"})
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


def _once_token(name):
    """A one-use token for a form (DON-08): the page holds it, the first post spends it, so a resubmit from Back is recognised."""
    tok = secrets.token_urlsafe(12)
    web[name] = (web.get(name) or [])[-4:] + [tok]
    return tok


def _spend_once(name):
    sent, live = request.form.get("_once", ""), web.get(name) or []
    if not sent:                                                           # a form without the token (an API-style post) is not a repeat
        return True
    if sent in live:
        live.remove(sent)
        web[name] = live
        return True
    return False


@app.route("/donor/screening", methods=["GET", "POST"])
@roles("Donor")
def donor_screening():
    qs = reads.screening_questions(uid())
    if reads.donor_permanent(uid()):                                       # DON-09: UC-04 precondition, no permanent deferral
        if request.method == "POST":
            flash("You are permanently deferred, so a health screening is not needed. Please speak to the clinic team if you have questions.", "info")
            return redirect(url_for("donor_home"))
        return render_template("donor_screening.html", qs=qs, answers={}, donor_sex=reads.donor_sex(uid()), permanent=True)
    if request.method != "POST":                                           # HEAD behaves like GET (SEC-ISS-01)
        return render_template("donor_screening.html", qs=qs, answers={}, donor_sex=reads.donor_sex(uid()), once=_once_token("screening_once"))
    answers = _answers(qs)
    if len(answers) < len(qs):                                             # keep what was ticked and show the form again
        flash("Answer every question before you submit.", "err")
        return render_template("donor_screening.html", qs=qs, answers=answers, donor_sex=reads.donor_sex(uid()), once=request.form.get("_once", ""),
                               missing=[q["question_id"] for q in qs if q["question_id"] not in answers]), 200
    if not _spend_once("screening_once"):
        flash("This screening was already submitted, so it was saved only once. Your dashboard shows the result.", "info")
        return redirect(url_for("donor_home"))
    # DON-08: Back reloads the page with a fresh token, so the server also treats the same answers again within a few
    # minutes as the screening already saved
    return attempt(lambda: svc.complete_screening_once(uid(), {q["question_id"]: answers[q["question_id"]] == "yes" for q in qs}),
                   lambda result: (f"Screening saved. Your status now reads: {_nicedates(result[0])}." if result[1] else
                                   f"Screening already saved. Your status reads: {_nicedates(result[0])}."), url_for("donor_home"))


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
    back = url_for("notifications") if request.form.get("from") == "notifications" else url_for("donor_home")
    if request.form.get("answer") not in ("accept", "decline"):
        flash("Choose whether you can help.", "err")
        return redirect(back)
    accept = request.form.get("answer") == "accept"
    try:
        result = svc.respond_to_call(uid(), call_id, accept)
    except (AccessDenied, InvalidInput, psycopg.Error) as exc:
        msg = _call_refusal(exc)
        flash(msg, "info" if re.search(r"no longer open|already answered", msg, re.I) else "err")      # C9: not the donor's mistake
        return redirect(back)
    if not accept:
        flash("Thanks for letting us know. You declined this call, and you can still answer future calls.", "ok")
    elif result and result.strip().endswith("."):                           # C9: the database writes a full sentence
        flash(result.strip(), "ok")
    else:
        flash(f"Thank you for helping. Book your donation with {result}." if result else "Thank you for helping.", "ok")
    return redirect(back)


def _call_refusal(exc):
    return friendly(exc)[1]


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
    if request.method != "POST":                                           # HEAD behaves like GET (SEC-ISS-01)
        return render_template("staff_request_new.html")
    f = request.form

    def go():
        c = Collect()
        units = c(post_int, "units")
        if "units" not in c.errors and (not units or not 1 <= units <= svc.MAX_UNITS):
            c.errors["units"] = f"Enter a whole number of units from 1 to {svc.MAX_UNITS}"
        patient_ref = c(need, "patient_ref", "the patient reference", maxlen=svc.PATIENT_REF_MAX)
        if f.get("component") not in svc.COMPONENTS:
            c.errors["component"] = "Choose a component"
        group = f.get("recipient_group") or None
        if group is not None and group not in svc.GROUPS:
            c.errors["recipient_group"] = "Choose a group"
        if f.get("urgency") not in ("Emergency", "Urgent", "Routine"):
            c.errors["urgency"] = "Choose an urgency"
        due = c(post_dt, "required_by")
        c.done()
        return svc.submit_request(uid(), patient_ref, group, f["component"], units, f["urgency"], due)
    try:
        res = go()
    except CAUGHT as exc:
        refuse(exc)
        web["form"] = {k: v for k, v in f.items() if not k.startswith("_")}
        return redirect(url_for("staff_request_new"))
    if not f.get("recipient_group") and res["shortfall"]:                  # STF-02: BR-14 holds stock for an unknown group only in an emergency
        held = (f"{res['reserved']} O-negative unit{'' if res['reserved'] == 1 else 's'} reserved for the emergency. " if res["reserved"]
                else "Nothing is reserved yet. ")
        flash(f"Request {res['request_id']} saved. {held}Confirm the patient's group on this page once the lab has it to reserve compatible stock.", "info")
    elif res["shortfall"]:
        flash(f"Request {res['request_id']} saved. {res['reserved']} of {f['units']} units reserved, {res['shortfall']} short. Call donors to close the gap.", "info")
    else:
        flash(f"Request {res['request_id']} saved. All {res['reserved']} units are reserved.", "ok")
    return redirect(url_for("staff_request", request_id=res["request_id"]))


@app.get("/staff/requests/<int:request_id>")
@roles("Staff")
def staff_request(request_id):
    try:
        data = reads.request_detail(uid(), request_id)
    except (AccessDenied, InvalidInput, psycopg.Error) as exc:
        flash_once(friendly(exc)[1], "err")
        return redirect(url_for("staff_requests"))
    data["needed_day"] = local(data["req"]["required_by"]).date()          # DA-N4: a call's date is at most the day it is needed
    return render_template("staff_request.html", **data)


@app.post("/staff/requests/<int:request_id>/call")
@roles("Staff")
def staff_request_call(request_id):
    back = url_for("staff_request", request_id=request_id)

    def go():
        pref = post_date("preferred")
        if pref is not None and pref < clinic_today():                     # REV-03: the clinic's day, not the server's
            raise InvalidInput("preferred: Choose today or a later date")
        picked = [c.strip() for c in request.form.getlist("donor") if c.strip()]
        if any(not re.fullmatch(r"[A-Za-z0-9-]{1,20}", c) for c in picked):
            raise InvalidInput("form: One of the ticked donors is not valid")
        call_all = request.form.get("all") == "1"                          # the explicit 'call all eligible donors' action
        if not picked and not call_all:
            raise InvalidInput("form: Tick at least one donor")
        already = reads.called_codes(uid(), request_id)
        if call_all:
            picked = [d[0] for d in svc.find_donors(uid(), request_id)]
        fresh = [c for c in dict.fromkeys(picked) if c not in already]
        if not fresh:
            raise InvalidInput("form: Those donors were already called for this request")
        try:
            sent = svc.call_donors(uid(), request_id, fresh, pref)
        except InvalidInput as exc:                                        # DA-N4: shown at the date field of this form
            if str(exc).startswith("preferred_date: "):
                raise InvalidInput("preferred: " + str(exc).split(": ", 1)[1]) from exc
            raise
        skipped = len(set(picked)) - len(fresh)
        if skipped:
            flash(f"{_plural(skipped, 'donor')} had already been called and {'was' if skipped == 1 else 'were'} skipped.", "info")
        return sent
    return attempt(go, "Donation call sent. Donors will see it in Vytal.", back)


@app.post("/staff/requests/<int:request_id>/group")
@roles("Staff")
def staff_request_group(request_id):
    def done(res):
        if res and res.get("shortfall"):
            return f"Group confirmed. {res['reserved']} unit(s) reserved, {res['shortfall']} short. Call donors to close the gap."
        return "Group confirmed. Compatible stock was reserved."
    return attempt(lambda: svc.confirm_request_group(uid(), request_id, request.form.get("blood_group", "")),
                   done, url_for("staff_request", request_id=request_id))


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


INVENTORY_PAGE = 50


@app.route("/staff/inventory", methods=["GET"])
@roles("Staff")
def staff_inventory():
    a = request.args
    status = arg_choice("status", UNIT_STATUSES, "status")
    comp = arg_choice("component", dict(reads.COMPONENTS), "component")
    group = arg_choice("group", GROUPS_SET, "blood group", norm_group)
    q = a.get("q", "").strip()[:40] or None                                # STF-14: scan or type a unit id
    page = int(a["page"]) if re.fullmatch(r"[0-9]{1,6}", a.get("page", "")) and int(a["page"]) >= 1 else 1
    total = reads.units_total(uid(), status, comp, group, q)
    pages = max(1, -(-total // INVENTORY_PAGE))
    page = min(page, pages)
    units = reads.units_list(uid(), status, comp, group, q, limit=INVENTORY_PAGE, offset=(page - 1) * INVENTORY_PAGE)
    fac, thr = reads.thresholds(uid())
    keep = {k: v for k, v in (("status", status), ("component", comp), ("group", group), ("q", q)) if v}
    here = url_for("staff_inventory", **keep, **({"page": page} if page > 1 else {}))
    return render_template("staff_inventory.html", units=units, thresholds=thr, f=a, overview=reads.staff_overview(uid()), keep=keep, here=here,
                           shown=len(units), total=total, page=page, pages=pages, first=(page - 1) * INVENTORY_PAGE + 1 if units else 0)


@app.post("/staff/units/<unit_id>/discard")
@roles("Staff")
def staff_unit_discard(unit_id):
    back = safe_path(request.form.get("back", "")) or url_for("staff_inventory")
    if not back.startswith(url_for("staff_inventory")):
        back = url_for("staff_inventory")
    return attempt(lambda: svc.discard_unit(uid(), unit_id, need("reason", "a reason for discarding the unit", maxlen=300)), f"Unit {unit_id} discarded.", back)


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
        flash_once(friendly(exc)[1], "err")
        return redirect(url_for("staff_donors"))


def _render_donor(donor_id, answers=None, missing=()):
    data = reads.donor_detail(uid(), donor_id)
    return render_template("staff_donor.html", **data, questions=reads.screening_questions(uid()), answers=answers or {}, missing=list(missing),
                           comps_by_type=reads.components_by_type(), tomorrow=(clinic_today() + dt.timedelta(days=1)).isoformat(),
                           type_opts=[(t["donation_type_code"], t["name"]) for t in data["types"]],
                           reason_opts=[(r["reason_code"], r["description"] + (" (permanent)" if r["is_permanent"] else ""))
                                        for r in data["reasons"] if r.get("is_active", True)])


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
            flash(friendly(exc)[1], "err")
            return redirect(url_for("staff_donors"))
    return attempt(lambda: svc.screen_donor(uid(), donor_id, {q["question_id"]: answers[q["question_id"]] == "yes" for q in qs}),
                   lambda result: f"Screening recorded. Status: {_nicedates(result)}.", url_for("staff_donor", donor_id=donor_id))


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
        c = Collect()
        dtype = f.get("donation_type", "WB")
        comps = [x.strip() for x in f.getlist("components")]
        if not comps:
            c.errors["components"] = "Choose at least one component"
        else:
            allowed = reads.components_by_type().get(dtype)
            if allowed is not None and not set(comps) <= set(allowed):       # REV-11: red cells only come from whole blood
                names = dict(reads.COMPONENTS)
                c.errors["components"] = f"A {reads.TYPE_NAMES.get(dtype, dtype).lower()} donation gives {' or '.join(names[x].lower() for x in allowed)} only"
        vol, hb = c(post_int, "volume_ml"), f.get("hemoglobin_gl", "").strip()
        if "volume_ml" not in c.errors and (not vol or not (50 <= vol <= 1000)):
            c.errors["volume_ml"] = "Enter a volume between 50 and 1000 ml"
        if not hb:
            c.errors["hemoglobin_gl"] = "Enter the hemoglobin reading in g/L"
        else:
            hb = c(dec, hb, "hemoglobin_gl")
        c.done()
        return svc.record_donation(uid(), donor_id, dtype, vol, hb, comps)
    try:
        did, units = go()
    except CAUGHT as exc:
        if not isinstance(exc, (InvalidInput, AccessDenied, psycopg.Error)):
            exc = InvalidInput("hemoglobin_gl: Enter a number such as 138")
        refuse(exc)
        web["form"] = {k: v for k, v in f.items() if not k.startswith("_")}
        web["form"]["components"] = f.getlist("components")
        return redirect(url_for("staff_donor", donor_id=donor_id) + "#donate")
    if did is None:
        flash("Hemoglobin was below the minimum, so no donation was recorded and the donor was deferred.", "info")
    else:
        flash(f"Donation {did} recorded. Units created: {', '.join(units)}. They are released once test results are entered.", "ok")
    return redirect(url_for("staff_donor", donor_id=donor_id))


@app.post("/staff/donors/<int:donor_id>/defer")
@roles("Staff")
def staff_donor_defer(donor_id):
    def go():
        reason = need("reason", "a deferral reason", maxlen=40)
        end = post_date("end_date")
        if end is not None and end <= clinic_today():                      # STF-11: said at the field, not as a range error
            raise InvalidInput("end_date: Choose an end date after today, or leave it blank to use the reason's default length")
        return svc.record_deferral(uid(), donor_id, reason, end)
    return attempt(go, "Deferral recorded. The next eligible date was recalculated.", url_for("staff_donor", donor_id=donor_id))


@app.get("/staff/tests")
@roles("Staff")
def staff_tests():
    return render_template("staff_tests.html", **reads.pending_tests(uid()))


@app.post("/staff/tests/<int:donation_id>")
@roles("Staff")
def staff_tests_save(donation_id):
    results = {k[2:]: v for k, v in request.form.items() if k.startswith("t_") and v}      # C8: blank 'Choose' means not entered yet
    if not results:
        flash("Choose a result for at least one test.", "err")
        return redirect(url_for("staff_tests"))
    if any(v not in ("Negative", "Positive") for v in results.values()):
        flash("Choose Negative or Positive for each test.", "err")
        return redirect(url_for("staff_tests"))
    return attempt(lambda: svc.record_tests(uid(), donation_id, results), _tests_outcome, url_for("staff_tests"))


def _tests_outcome(result):
    """C8: svc.record_tests answers 'Awaiting N test(s)', 'Released' or 'Discarded'."""
    m = re.fullmatch(r"Awaiting (\d+) test\(s\)", result or "")
    if m:
        return f"Results saved. {_plural(int(m.group(1)), 'test')} still to enter before the units are released."
    return {"Released": "Results saved. All tests are in and the units are released.",
            "Discarded": "Results saved. A test was positive, so the units were discarded and the donor was flagged."}.get(result, f"Results saved: {result}.")


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
    return attempt(lambda: svc.nightly_expiry(uid()), lambda n: f"Expiry job finished: {_plural(n or 0, 'unit')} expired.", url_for("admin_home"))


@app.get("/admin/users")
@roles("Admin")
def admin_users():
    a = request.args
    facs = reads.facilities(uid())
    role = arg_choice("role", ("Donor", "Staff", "Admin"), "role")
    q = a.get("q", "").strip()[:100] or None
    items = reads.users_list(uid(), role, q)
    return render_template("admin_users.html", items=items, fac_opts=[(x["facility_id"], x["name"]) for x in facs], f=a, role=role or "", q=q or "",
                           shown=len(items), total=reads.users_total(uid(), role, q))


@app.post("/admin/users/create")
@roles("Admin")
def admin_user_create():
    f = request.form

    def go():
        c = Collect()
        if f.get("role") not in ("Admin", "Staff"):
            c.errors["role"] = "Choose Staff or Admin"
        email = c(need, "email", "an email address", maxlen=254)
        first, last = c(need, "first_name", "a first name", maxlen=svc.NAME_MAX), c(need, "last_name", "a last name", maxlen=svc.NAME_MAX)
        c(check_password_shape, f.get("password", ""), "password", email)
        fac = c(post_int, "facility_id")
        if f.get("role") == "Staff" and fac is None and "facility_id" not in c.errors:
            c.errors["facility_id"] = "Choose a facility"
        title = c(opt, "job_title", 100)
        c.done()
        if f.get("role") == "Admin":
            return svc.create_admin(uid(), email, f.get("password", ""), first, last)
        return svc.create_staff(uid(), email, f.get("password", ""), first, last, fac, title)
    return attempt(go, "Account created.", url_for("admin_users") + "#create")


@app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@roles("Admin")
def admin_user_edit(user_id):
    """C12: an Admin corrects an account's name, email or phone (audited by the service)."""
    acct = reads.account(uid(), user_id)
    if acct is None:
        flash("That account was not found.", "err")
        return redirect(url_for("admin_users"))
    if request.method != "POST":                                           # HEAD behaves like GET (SEC-ISS-01)
        return render_template("admin_user_edit.html", a=acct)
    f = request.form

    def go():
        c = Collect()
        first, last = c(need, "first_name", "a first name", maxlen=svc.NAME_MAX), c(need, "last_name", "a last name", maxlen=svc.NAME_MAX)
        email = f.get("email", "").strip()
        if email and (not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254):
            c.errors["email"] = "Enter an address like name@example.com"
        if not email and acct.get("email"):
            c.errors["email"] = "Enter an email address"
        phone = c(lambda: check_phone(opt("phone", 40)))
        c.done()
        if email and acct.get("email") is not None and email.lower() != acct["email"].lower():
            refuse_demo(user_id, "changing the email")
        svc.update_account(uid(), user_id, first, last, email or None, phone)
        return f"{first} {last}"
    # DA-N2: the message and the list use the name as saved, so a renamed account is the one shown
    resp = attempt(go, lambda name: f"Account of {name} saved.", url_for("admin_user_edit", user_id=user_id))
    return redirect(url_for("admin_users", q=g.result)) if g.ok else resp


@app.post("/admin/users/<int:user_id>/active")
@roles("Admin")
def admin_user_active(user_id):
    active = request.form.get("active") == "1"

    def go():
        if not active:
            refuse_demo(user_id, "deactivating an account")
        return svc.set_account_active(uid(), user_id, active)
    return attempt(go, "Account reactivated." if active else "Account deactivated.", back_url(url_for("admin_users")))


@app.post("/admin/users/<int:user_id>/password")
@roles("Admin")
def admin_user_password(user_id):
    def go():
        refuse_demo(user_id, "resetting the password")
        acct = reads.account(uid(), user_id) or {}
        check_password_shape(request.form.get("new_password", ""), "new_password", acct.get("email"))
        return svc.admin_reset_password(uid(), user_id, request.form.get("new_password", ""))
    return attempt(go, "Password reset. Open sessions were ended.", back_url(url_for("admin_users")), keep=False, aliases={"password": "new_password"})


@app.get("/admin/facilities")
@roles("Admin")
def admin_facilities():
    items = reads.facilities_admin(uid())
    th = None
    raw = request.args.get("thresholds", "").strip()
    fid = int(raw) if re.fullmatch(r"[0-9]{1,9}", raw) else None
    if raw and fid is None:
        flash("That facility link is not valid.", "err")
    elif fid is not None:
        fac = next((x for x in items if x["facility_id"] == fid), None)
        if fac is None:                                                    # ADM-07: never the error page
            flash("That facility was not found.", "err")
        else:
            th = (fac, reads.admin_thresholds(uid(), fid))
    return render_template("admin_facilities.html", items=items, th=th)


@app.post("/admin/facilities/save")
@roles("Admin")
def admin_facility_save():
    f = request.form

    def go():
        c = Collect()
        name, address = c(need, "name", "the facility name", maxlen=150), c(need, "address", "the facility address", maxlen=300)
        if f.get("facility_type") not in ("Hospital", "Blood Bank"):
            c.errors["facility_type"] = "Choose Hospital or Blood Bank"
        phone = c(lambda: check_phone(opt("phone", 40)))
        email = c(opt, "email", 254)
        fid = c(post_int, "facility_id")
        c.done()
        return svc.save_facility(uid(), fid, name, f.get("facility_type", ""), address, phone, email)
    return attempt(go, lambda _r: f"Facility saved: {f.get('name', '').strip()}.", url_for("admin_facilities"))


@app.post("/admin/facilities/<int:facility_id>/active")
@roles("Admin")
def admin_facility_active(facility_id):
    active = request.form.get("active") == "1"
    name = next((x["name"] for x in reads.facilities(uid(), active_only=False) if x["facility_id"] == facility_id), None)
    if name is None:
        flash("That facility was not found.", "err")
        return redirect(url_for("admin_facilities"))

    def go():
        if not active:
            refuse_demo_facility(facility_id)                              # SEC-ISS-03
        return svc.set_facility_active(uid(), facility_id, active)
    return attempt(go, f"{name} reactivated." if active else f"{name} deactivated. Its staff are signed out.", url_for("admin_facilities"))


@app.post("/admin/facilities/<int:facility_id>/threshold")
@roles("Admin")
def admin_facility_threshold(facility_id):
    f = request.form
    return attempt(lambda: svc.set_threshold(uid(), facility_id, f.get("blood_group"), f.get("component"), post_int("min_units", -1)),
                   "Minimum saved.", url_for("admin_facilities", thresholds=facility_id))


# ADM-16: the limits the services enforce, so the inputs refuse the same values (vytal/services.py set_* checks)
RULE_LIMITS = {"min_age": (16, 25), "min_weight": (30, 100), "min_hb": (100, 180), "min_blood_volume_ml": (1500, 6000),
               "shelf_days": (1, 1095), "interval_days": (1, 365), "default_days": (1, 3650)}
RULE_ALIASES = {"min_age_years": "min_age", "min_weight_kg": "min_weight", "min_hemoglobin_gl": "min_hb", "shelf_life_days": "days",
                "interval_days": "days", "reason_code": "code", "min_hemoglobin": "min_hb", "min_blood_volume": "min_blood_volume_ml",
                "reason": "code"}


@app.get("/admin/rules")
@roles("Admin")
def admin_rules():
    return render_template("admin_rules.html", r=reads.rules(uid()), limits=RULE_LIMITS, rules_locked=DEMO_RULES_LOCKED if DEMO_MODE else None)


@app.post("/admin/rules/compat")
@roles("Admin")
def admin_rule_compat():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("component", "recipient", "donor")):
        return jsonify(ok=False, message="Choose a component and two of the eight blood groups."), 422
    if DEMO_MODE:
        return jsonify(ok=False, message=DEMO_RULES_LOCKED), 403
    try:
        svc.set_compatibility(uid(), d.get("component"), d.get("recipient"), d.get("donor"), d.get("allowed") is True)
        return jsonify(ok=True)
    except (InvalidInput, AccessDenied, psycopg.Error) as exc:
        return jsonify(ok=False, message=friendly(exc)[1]), 422


def _rule(action, success, anchor):
    """A rule form: refusals land on the input that caused them (the service names fields after the columns)."""
    def go():
        refuse_demo_rules()
        return action()
    return attempt(go, success, url_for("admin_rules") + anchor, aliases=RULE_ALIASES)


@app.post("/admin/rules/shelf")
@roles("Admin")
def admin_rule_shelf():
    return _rule(lambda: svc.set_shelf_life(uid(), request.form.get("component"), post_int("days")), "Shelf life saved.", "#shelf")


@app.post("/admin/rules/interval")
@roles("Admin")
def admin_rule_interval():
    f = request.form
    return _rule(lambda: svc.set_donation_interval(uid(), f.get("type"), f.get("sex"), post_int("days")), "Donation interval saved.", "#interval")


@app.post("/admin/rules/eligibility")
@roles("Admin")
def admin_rule_eligibility():
    f = request.form

    def go():
        c = Collect()
        age = c(post_int, "min_age")
        weight = c(dec, f.get("min_weight") or 0, "min_weight")
        hb = c(dec, f.get("min_hb") or 0, "min_hb")
        vol = c(post_int, "min_blood_volume_ml")
        c.done()
        return svc.set_eligibility_rule(uid(), f.get("sex"), age, weight, hb, vol)
    return _rule(go, "Eligibility rule saved.", "#eligibility")


@app.post("/admin/rules/reason")
@roles("Admin")
def admin_rule_reason():
    f = request.form
    perm = f.get("permanent") == "1"

    def go():
        c = Collect()
        code = c(need, "code", "a reason code", maxlen=10)
        if code and not re.fullmatch(r"[A-Za-z]{2,10}", code):
            c.errors["code"] = "Use 2 to 10 letters for the code, such as TRAVEL"
        desc = c(need, "description", "a description", maxlen=200)
        days = None if perm else c(post_int, "default_days")
        if not perm and days is None and "default_days" not in c.errors:
            c.errors["default_days"] = "Enter how many days a deferral lasts, or tick Permanent"
        c.done()
        return svc.save_deferral_reason(uid(), code.upper(), desc, perm, days)
    return _rule(go, "Deferral reason saved.", "#reasons")


@app.post("/admin/rules/reason/<code>/active")
@roles("Admin")
def admin_rule_reason_active(code):
    """C12: a retired reason is hidden from new deferrals; deferrals already recorded keep it."""
    active = request.form.get("active") == "1"
    code = code.strip()[:10]
    return _rule(lambda: svc.set_deferral_reason_active(uid(), code, active),
                 f"Reason {code} restored, staff can choose it again." if active else f"Reason {code} retired. Past deferrals keep it.", "#reasons")


AUDIT_PAGE = 50
AUDIT_FIRST_DAY, AUDIT_LAST_DAY = dt.date(1900, 1, 1), dt.date(2999, 12, 31)   # a typed year 0001 or 9999 is a slip, not a range


@app.get("/admin/audit")
@roles("Admin")
def admin_audit():
    a = request.args
    flt, ok = _audit_filters(a, flash_errors=True)
    page = int(a["page"]) if re.fullmatch(r"[0-9]{1,6}", a.get("page", "")) and int(a["page"]) >= 1 else 1
    items, total = [], 0
    if ok:
        try:
            items, total = _audit_page(flt, page)
        except (InvalidInput, psycopg.Error) as exc:
            refuse(exc, keep=False)
    pages = max(1, -(-total // AUDIT_PAGE))
    keep = {k: v for k, v in a.items() if k != "page" and v.strip()}
    first = (page - 1) * AUDIT_PAGE + 1 if items else 0
    return render_template("admin_audit.html", items=items, tables=reads.audit_tables(uid()), f=a, page=page, per_page=AUDIT_PAGE, pages=pages,
                           has_next=page < pages and bool(items), has_prev=page > 1, shown=len(items), total=total, first=first,
                           last=first + len(items) - 1 if items else 0,
                           first_url=url_for("admin_audit", **keep), prev_url=url_for("admin_audit", **keep, page=page - 1) if page > 1 else None,
                           next_url=url_for("admin_audit", **keep, page=page + 1) if page < pages and items else None)


def _audit_page(flt, page):
    """One page of the audit log (newest first) and the number of matching entries (row index 11, C2)."""
    rows_ = svc.audit_log(uid(), limit=AUDIT_PAGE, offset=(page - 1) * AUDIT_PAGE, **flt)
    if rows_:
        return rows_, rows_[0][11]
    if page > 1:                                                           # past the end: the total still comes from the first page
        head = svc.audit_log(uid(), limit=1, offset=0, **flt)
        return [], head[0][11] if head else 0
    return [], 0


def _audit_filters(a, flash_errors=False):
    """(filters for svc.audit_log, usable). Dates are plain clinic days, both ends included (C2); the user box takes a user
    number, an email address or part of a name (resolved to one account)."""
    ok = True

    def d(name, label):
        v = a.get(name, "").strip()
        if not v:
            return None
        try:
            day = dt.date.fromisoformat(v[:10])
        except ValueError:
            day = None
        if day is None or not AUDIT_FIRST_DAY <= day <= AUDIT_LAST_DAY:
            if flash_errors:
                flash(f"The {label} date was not understood (use a date like 2026-12-31), so it was ignored.", "err")
            return None
        return day
    since, until = d("since", "from"), d("until", "to")
    if since and until and since > until:
        if flash_errors:
            web["ferrs"] = {"until": "The 'to' date is before the 'from' date."}
            flash("The 'to' date is before the 'from' date.", "err")
        ok = False
    user = a.get("user", "").strip()[:100]
    user_id = None
    if user:
        found = reads.resolve_user(uid(), user)
        if len(found) == 1:
            user_id = found[0]
        else:
            ok = False
            if flash_errors:
                flash("No account matches that user." if not found else
                      f"{len(found)} accounts match that user. Enter more of the name, the full email address or the user number.", "err")
    return {"user_id": user_id, "table": a.get("table", "").strip()[:63] or None, "record_id": a.get("record", "").strip()[:40] or None,
            "since": since, "until": until}, ok


@app.get("/admin/audit.csv")
@roles("Admin")
def admin_audit_csv():
    flt, ok = _audit_filters(request.args, flash_errors=True)
    if not ok:
        return redirect(url_for("admin_audit", **request.args))
    try:
        text = csv_safe(svc.audit_csv(uid(), **flt))
    except (InvalidInput, psycopg.Error) as exc:
        refuse(exc, keep=False)
        return redirect(url_for("admin_audit", **request.args))
    return Response(text, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=vytal-audit.csv"})


@app.route("/admin/lookback", methods=["GET"])
@roles("Admin")
def admin_lookback():
    a = request.args
    trace, error = None, None
    code, unit = (a.get("code", "").strip()[:40].upper() or None), (a.get("unit", "").strip()[:40].upper() or None)
    if "code" in a or "unit" in a:
        if not (code or unit):                                             # ADM-13: an empty Trace says what to enter
            flash("Enter a donor code such as D-1028 or a unit id such as U6-RBC to trace.", "err")
        else:
            if unit and not code:                                          # ADM-05: a unit leads to its donor, with the same actions
                code = reads.donor_code_for_unit(uid(), unit)
                if code is None:
                    error = f"No unit with the id {unit} was found."
            if error is None:
                try:
                    trace = svc.lookback(uid(), code, None)
                except (InvalidInput, psycopg.Error) as exc:
                    error = friendly(exc)[1]
    return render_template("admin_lookback.html", trace=trace, error=error, f=a, quarantined=reads.quarantined_units(uid()),
                           code=code if trace is not None else None, unit=unit,
                           facility_names={x["facility_id"]: x["name"] for x in reads.facilities(uid(), active_only=False)})


def _donor_by_code(code):
    return reads.donor_by_code(uid(), code)


@app.post("/admin/lookback/quarantine")
@roles("Admin")
def admin_quarantine():
    code = request.form.get("code", "").strip()[:40].upper()
    donor = _donor_by_code(code)
    if donor is None:
        flash("No donor with that code.", "err")
        return redirect(url_for("admin_lookback"))
    held = lambda rows_: sum(1 for r in rows_ if r[4] == "Quarantined")    # noqa: E731

    def go():
        before = held(svc.lookback(uid(), code, None))
        return held(svc.quarantine_donor(uid(), donor)) - before

    def done(n):                                                           # ADM-13: say what happened, also when nothing did
        if n <= 0:
            return "No units were quarantined: none of this donor's units were on the shelf or reserved."
        return f"{_plural(n, 'unit')} quarantined. Open requests were re-allocated where possible."
    return attempt(go, done, url_for("admin_lookback", code=code))


@app.post("/admin/lookback/clear")
@roles("Admin")
def admin_clear():
    code = request.form.get("code", "").strip()[:40].upper()
    donor = _donor_by_code(code)
    if donor is None:
        flash("No donor with that code.", "err")
        return redirect(url_for("admin_lookback"))
    return attempt(lambda: svc.clear_lookback(uid(), donor, need("note", "a note explaining why the investigation is cleared", maxlen=500)),
                   lambda n: f"Investigation cleared. {_plural(n or 0, 'unit')} went back on the shelf.", url_for("admin_lookback", code=code))


@app.post("/admin/units/<unit_id>/discard")
@roles("Admin")
def admin_unit_discard(unit_id):
    code = request.form.get("code", "").strip()[:40].upper()
    back = url_for("admin_lookback", code=code) if code else url_for("admin_lookback")      # ADM-13: stay on the donor's trace
    return attempt(lambda: svc.discard_unit(uid(), unit_id, (request.form.get("reason", "") or "").strip() or "Confirmed by lookback"),
                   f"Unit {unit_id} discarded.", back)


@app.get("/admin/lookback.csv")
@roles("Admin")
def admin_lookback_csv():
    code = request.args.get("code", "").strip()[:40].upper()
    try:
        text = csv_safe(svc.lookback_issued_csv(uid(), code))
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
