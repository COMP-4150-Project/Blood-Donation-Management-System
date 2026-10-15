"""Backend operations behind the Step 5 screens.

Each function is one unit of work for one acting user. Business rules live in the database (DI-03); this layer adds
what the database cannot know: who is calling, which facility they belong to (SE-02), and password checks (SE-01, SE-05).
"""
import csv
import datetime as dt
import hashlib
import io
import json
import re
import secrets

from vytal.auth import MAX_BYTES as MAX_PASSWORD_BYTES, hash_password, verify_password
from vytal.db import session
from vytal.queries import load

QUERIES = load()
GROUPS = {"O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"}
COMPONENTS = {"RBC", "PLASMA", "PLT"}
_DUMMY_HASH = hash_password("no-such-account")      # unknown emails cost the same bcrypt time as real ones (SE-05)


class AccessDenied(Exception):
    pass


class InvalidInput(ValueError):
    pass


def _role(con, user_id, role):
    """Returns the caller's facility (staff) or None; refuses any other role (SE-02)."""
    row = con.execute("SELECT a.role, s.facility_id FROM user_account a LEFT JOIN staff s USING (user_id) "
                      "WHERE a.user_id = %s AND a.is_active", (user_id,)).fetchone()
    if not row or row[0] != role:
        raise AccessDenied(f"{role} only")
    return row[1]


def _check_email(email):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""):
        raise InvalidInput("email: enter an address like name@example.com")


def _check_password(password):
    if len(password or "") < 10:
        raise InvalidInput("password: use at least 10 characters")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise InvalidInput(f"password: use at most {MAX_PASSWORD_BYTES} bytes (bcrypt ignores the rest)")


def _check_weight(weight_kg):
    if not 20 <= float(weight_kg) <= 300:
        raise InvalidInput("weight_kg: enter a weight between 20 and 300 kg")


def _own_request(con, staff_id, request_id):
    fac = _role(con, staff_id, "Staff")
    if con.execute("SELECT facility_id FROM v_request WHERE request_id = %s", (request_id,)).fetchone() != (fac,):
        raise AccessDenied("request belongs to another facility")
    return fac


# ---------------------------------------------------------------------- accounts
def register_donor(email, password, first, last, phone, dob, sex, weight_kg, address, blood_group=None):
    """FR-01, with server-side validation (SE-09) so the user gets a field-level message before the database refuses."""
    _check_email(email)
    _check_password(password)
    if sex not in ("M", "F"):
        raise InvalidInput("sex: choose M or F")
    _check_weight(weight_kg)
    if blood_group not in GROUPS | {None}:
        raise InvalidInput("blood_group: choose a group or 'I don't know'")
    if dob >= dt.date.today():
        raise InvalidInput("date_of_birth: must be in the past")
    with session() as con:
        min_age = con.execute("SELECT min_age_years FROM eligibility_rule WHERE sex = %s", (sex,)).fetchone()[0]
        if _years_between(dob, dt.date.today()) < min_age:                # UC-01 2b; the procedure enforces it too
            raise InvalidInput(f"date_of_birth: donors must be at least {min_age} years old")
        return con.execute("CALL register_donor(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (email, hash_password(password), first, last, phone, dob, sex, weight_kg, address,
                            blood_group)).fetchone()[0]


def login(email, password):
    """SE-01 / SE-05. Returns (user_id, role); raises AccessDenied with the reason shown to the user."""
    with session() as con:
        row = con.execute("SELECT user_id, role, password_hash FROM login_lookup(%s)", (email,)).fetchone()   # SE-07
        if not row:
            verify_password(password, _DUMMY_HASH)
            raise AccessDenied("wrong email or password")
        outcome = con.execute("CALL login_attempt(%s, %s)", (row[0], verify_password(password, row[2]))).fetchone()[0]
    if outcome != "ok":
        raise AccessDenied({"failed": "wrong email or password", "locked": "account locked for 15 minutes",
                            "inactive": "account is deactivated"}[outcome])
    return row[0], row[1]


# ---------------------------------------------------------------------- donor screens
def donor_home(donor_id):
    """US-02: eligibility first, then the donor's own history (FR-15), never recipients."""
    with session(donor_id) as con:
        _role(con, donor_id, "Donor")
        status = con.execute("SELECT donor_code, blood_group, status FROM v_donor_eligibility WHERE user_id = %s",
                             (donor_id,)).fetchone()
        history = con.execute("SELECT d.donated_at, f.name, d.donation_type_code, count(u.unit_id) FROM donation d "
                              "JOIN staff s ON s.user_id = d.recorded_by JOIN facility f ON f.facility_id = s.facility_id "
                              "LEFT JOIN blood_unit u USING (donation_id) WHERE d.donor_id = %s "
                              "GROUP BY d.donation_id, f.name ORDER BY d.donated_at DESC", (donor_id,)).fetchall()
        calls = con.execute("SELECT c.call_id, c.message, r.response FROM call_recipient r JOIN donation_call c USING (call_id) "
                            "WHERE r.donor_id = %s ORDER BY c.sent_at DESC", (donor_id,)).fetchall()
    return {"donor_code": status[0], "blood_group": status[1], "status": status[2], "history": history, "calls": calls}


def respond_to_call(donor_id, call_id, accept):
    """FR-27 / UC-20: returns the facility's details on accept, 'declined', or 'closed' when no donation is needed now."""
    with session(donor_id) as con:
        _role(con, donor_id, "Donor")
        return con.execute("CALL respond_to_call(%s,%s,%s)", (call_id, donor_id, accept)).fetchone()[0]


def complete_screening(donor_id, answers):
    """UC-04 / FR-06: the donor answers their own screening; returns their eligibility status afterwards."""
    with session(donor_id) as con:
        _role(con, donor_id, "Donor")
        con.execute("CALL submit_screening(%s,%s)", (donor_id, json.dumps({str(k): v for k, v in answers.items()})))
        return con.execute("SELECT status FROM v_donor_eligibility WHERE user_id = %s", (donor_id,)).fetchone()[0]


def profile(user_id):
    """UC-03: the caller's own profile with contact details (the app role reads them only through my_profile())."""
    with session(user_id) as con:
        cur = con.execute("SELECT * FROM my_profile()")
        row = cur.fetchone()
        if not row:
            raise AccessDenied("no such account")
        return dict(zip([c.name for c in cur.description], row))


def change_password(user_id, old_password, new_password):
    """UC-01 2a: the user proves the old password, then sets a new one; open sessions end, so they sign in again."""
    _check_password(new_password)
    with session(user_id) as con:
        email = con.execute("SELECT email FROM my_profile()").fetchone()
        row = con.execute("SELECT password_hash FROM login_lookup(%s)", email).fetchone() if email else None
        if not row or not verify_password(old_password, row[0]):
            raise AccessDenied("current password is wrong")
        con.execute("CALL set_password(%s,%s)", (user_id, hash_password(new_password)))


def update_profile(donor_id, phone, email, address, weight_kg):
    """UC-03 / FR-03: contact details and weight; the blood group is not editable here once verified."""
    _check_email(email)
    _check_weight(weight_kg)
    return _call(donor_id, "CALL update_donor_profile(%s,%s,%s,%s,%s)", donor_id, phone, email, address, weight_kg)


# ---------------------------------------------------------------------- staff: clinic
def screening_answers(user_id, screening_id):
    """UC-04: a screening's answers, for the donor who gave them or for clinic staff (never in donor search, SE-07)."""
    with session(user_id) as con:
        return con.execute("SELECT question_id, question_text, answer FROM screening_answers(%s)", (screening_id,)).fetchall()


def screen_donor(staff_id, donor_id, answers):
    """FR-06, staff-assisted: screening is donor-owned (UC-04, complete_screening); staff may enter it at the clinic."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        con.execute("CALL submit_screening(%s,%s)", (donor_id, json.dumps({str(k): v for k, v in answers.items()})))
        return con.execute("SELECT status FROM v_donor_eligibility WHERE user_id = %s", (donor_id,)).fetchone()[0]


def verify_blood_group(staff_id, donor_id, blood_group):
    """FR-09: staff confirm a donor's group and are recorded as the verifier."""
    if blood_group not in GROUPS:
        raise InvalidInput("blood_group: choose one of the eight groups")
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        con.execute("CALL verify_blood_group(%s,%s,%s)", (staff_id, donor_id, blood_group))


def record_donation(staff_id, donor_id, donation_type, volume_ml, hemoglobin_gl, components):
    """FR-11 / FR-12: returns (donation_id, unit ids), or (None, []) when low hemoglobin deferred the donor (UC-07 2a)."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        dn = con.execute("CALL record_donation(%s,%s,%s,%s,%s)",
                         (donor_id, staff_id, donation_type, volume_ml, hemoglobin_gl)).fetchone()[0]
        if dn is None:
            return None, []
        units = con.execute("CALL separate_donation(%s,%s)", (dn, components)).fetchone()[0]
    return dn, units


def record_tests(staff_id, donation_id, results):
    """FR-13: results maps test_code -> 'Negative' | 'Positive'. Only the collecting facility records results (SE-02)."""
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        if con.execute("SELECT s.facility_id FROM donation d JOIN staff s ON s.user_id = d.recorded_by "
                       "WHERE d.donation_id = %s", (donation_id,)).fetchone() != (fac,):
            raise AccessDenied("donation was collected at another facility")
        return con.execute("CALL record_tests(%s,%s)", (donation_id, json.dumps(results))).fetchone()[0]


# ---------------------------------------------------------------------- staff: requests and stock
def submit_request(staff_id, patient_ref, recipient_group, component, units, urgency, required_by):
    """FR-22 + FR-23 in one transaction: create the request and reserve what the shelf can cover."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        rid, reserved, shortfall = con.execute("CALL submit_request(%s,%s,%s,%s,%s,%s,%s)",
                                               (staff_id, patient_ref, recipient_group, component, units, urgency,
                                                required_by)).fetchone()
    return {"request_id": rid, "reserved": reserved, "shortfall": shortfall}


def confirm_request_group(staff_id, request_id, blood_group):
    """UC-16 step 6: the lab confirms an Unknown group; reserve whatever compatible stock now covers the shortfall."""
    if blood_group not in GROUPS:
        raise InvalidInput("blood_group: choose one of the eight groups")
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        if con.execute("SELECT recipient_group IS NULL AND status = 'Open' FROM blood_request WHERE request_id = %s",
                       (request_id,)).fetchone() != (True,):
            raise InvalidInput("request is closed or already has a confirmed group")
        reserved, shortfall = con.execute("CALL confirm_request_group(%s,%s,%s)",
                                          (staff_id, request_id, blood_group)).fetchone()
    return {"request_id": request_id, "reserved": reserved, "shortfall": shortfall}


def close_request(staff_id, request_id, cancel=False, issued_units=None):
    """FR-28 / UC-21: issue the reserved units staff confirm (all of them if none are listed; the rest go back to
    Available), or cancel and release them, for a request at the caller's facility."""
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        return con.execute("CALL close_request(%s,%s,%s)",
                           (request_id, cancel, list(issued_units) if issued_units is not None else None)).fetchone()[0]


def call_responses(staff_id, request_id):
    """FR-27: donors' answers to the calls for a request; donor code only, never contact details (SE-07)."""
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        return con.execute("SELECT o.donor_code, r.response, r.responded_at FROM donation_call c "
                           "JOIN call_recipient r USING (call_id) JOIN donor o ON o.user_id = r.donor_id "
                           "WHERE c.request_id = %s ORDER BY r.response, o.donor_code", (request_id,)).fetchall()


def search_donors(staff_id, recipient_group, component="RBC"):
    """UC-18 manual search by blood group: eligible, verified, compatible donors; code, group and eligibility only (SE-07)."""
    if recipient_group not in GROUPS:
        raise InvalidInput("blood_group: choose one of the eight groups")
    if component not in COMPONENTS:
        raise InvalidInput("component: choose RBC, PLASMA or PLT")
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        return con.execute("SELECT * FROM donors_for_group(%s,%s) ORDER BY donor_code", (recipient_group, component)).fetchall()


def find_donors(staff_id, request_id):
    """FR-25: eligible, compatible donors for the request; donor code, group and eligibility only (SE-07)."""
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        return con.execute("SELECT * FROM eligible_donors(%s) ORDER BY donor_code", (request_id,)).fetchall()


def call_donors(staff_id, request_id, donor_codes=None, preferred_date=None):
    """FR-26: send an in-app donation call to the chosen eligible donors (all of them if none are chosen). The message is the
    database's fixed template (facility, component, optional date), so a call can never carry health information (SE-07)."""
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        return con.execute("CALL send_donation_call(%s,%s,%s,%s)",
                           (request_id, staff_id, donor_codes, preferred_date)).fetchone()[0]


def inventory(staff_id):
    """FR-17 for the caller's own facility only (SE-02)."""
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return con.execute("SELECT blood_group, component_code, available, reserved, expiring_48h FROM v_inventory "
                           "WHERE facility_id = %s ORDER BY component_code, blood_group", (fac,)).fetchall()


def open_alerts(staff_id):
    """FR-21 / UC-13 step 3: open low-stock alerts at the caller's facility, with current stock against the minimum."""
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return con.execute("SELECT a.blood_group, a.component_code, coalesce(i.available, 0), t.min_units, a.opened_at "
                           "FROM stock_alert a JOIN stock_threshold t USING (facility_id, blood_group, component_code) "
                           "LEFT JOIN v_inventory i USING (facility_id, blood_group, component_code) "
                           "WHERE a.facility_id = %s AND a.cleared_at IS NULL ORDER BY a.opened_at", (fac,)).fetchall()


def list_notifications(user_id, unread_only=False):
    """UC-09 / UC-20: the caller's own notifications, newest first."""
    with session(user_id) as con:
        return con.execute("SELECT notification_id, message, created_at, read_at FROM notification WHERE user_id = %s "
                           "AND (NOT %s OR read_at IS NULL) ORDER BY created_at DESC, notification_id DESC",
                           (user_id, unread_only)).fetchall()


def mark_read(user_id, notification_id):
    """Marks one of the caller's own notifications read (only the first time counts)."""
    return _call(user_id, "CALL mark_notification_read(%s,%s)", user_id, notification_id)


def record_deferral(staff_id, donor_id, reason, end=None):
    """UC-06 / FR-08: returns the deferral_id."""
    return _call(staff_id, "CALL record_deferral(%s,%s,%s,%s)", staff_id, donor_id, reason, end)


def discard_unit(user_id, unit_id, reason):
    """UC-15 / FR-16 (staff, own facility); an Admin discards Quarantined units after a lookback (UC-22)."""
    if not (reason or "").strip():
        raise InvalidInput("reason: choose a discard reason")
    return _call(user_id, "CALL discard_unit(%s,%s,%s)", user_id, unit_id, reason)


def set_threshold(user_id, facility_id, blood_group, component, min_units):
    """UC-12 / FR-18: staff for their own facility, an Admin for any."""
    if not isinstance(min_units, int) or min_units < 0:
        raise InvalidInput("min_units: enter a whole number of 0 or more")
    return _call(user_id, "CALL set_threshold(%s,%s,%s,%s,%s)", user_id, facility_id, blood_group, component, min_units)


# ---------------------------------------------------------------------- admin
_LOOKBACK = ("SELECT donation_id, donated_at, unit_id, component_code, status, held_at, request_id, request_facility, "
             "issued_at FROM v_lookback WHERE donor_id = %s ORDER BY donated_at, unit_id")


def lookback(admin_id, donor_code=None, unit_id=None):
    """UC-22 / FR-30: everything the donor gave, found by donor code or by any one of their unit ids."""
    if not (donor_code or unit_id):
        raise InvalidInput("enter a donor code or a unit id")
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        if donor_code:
            row = con.execute("SELECT user_id FROM donor WHERE donor_code = %s", (donor_code,)).fetchone()
        else:
            row = con.execute("SELECT donor_id FROM v_blood_unit WHERE unit_id = %s", (unit_id,)).fetchone()
        if not row:
            raise InvalidInput("no donor or unit with that code")
        return con.execute(_LOOKBACK, row).fetchall()


def quarantine_donor(admin_id, donor_id):
    """UC-22 / FR-31; returns the lookback rows (FR-30) so the Admin sees what was pulled and what was already issued."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        con.execute("CALL quarantine_donor(%s)", (donor_id,))
        return con.execute(_LOOKBACK, (donor_id,)).fetchall()


def clear_lookback(admin_id, donor_id, note):
    """UC-22 "investigation cleared": the donor's Quarantined units go back on the shelf and the flag is lifted. Refused
    when the donor has a permanent deferral or a positive test (then the units are discarded). Returns units released."""
    if not (note or "").strip():
        raise InvalidInput("note: say why the investigation was cleared")
    return _call(admin_id, "CALL clear_lookback(%s,%s,%s)", admin_id, donor_id, note)


_LOOKBACK_ISSUED = ("SELECT l.unit_id, l.component_code, l.issued_at, f.name AS request_facility, l.request_id "
                    "FROM v_lookback l LEFT JOIN facility f ON f.facility_id = l.request_facility "
                    "WHERE l.donor_id = %s AND l.issued_at IS NOT NULL ORDER BY l.issued_at, l.unit_id")


def lookback_issued_csv(admin_id, donor_code, path=None):
    """UC-22 step 4: CSV of the donor's Issued units so hospitals can follow up with patients."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        row = con.execute("SELECT user_id FROM donor WHERE donor_code = %s", (donor_code,)).fetchone()
        if not row:
            raise InvalidInput("no donor with that code")
        cur = con.execute(_LOOKBACK_ISSUED, row)
        return _csv(cur, path)


# old and new values are readable only through audit_entries(), which checks the Admin role itself (SE-07)
_AUDIT = ("SELECT audit_id, changed_at, user_id, action, table_name, record_id, field_name, old_value, new_value "
          "FROM audit_entries(%(user)s::int, %(table)s::text, %(record)s::text, %(since)s::timestamptz, %(until)s::timestamptz, "
          "%(limit)s::int)")


def audit_log(admin_id, user_id=None, table=None, record_id=None, since=None, until=None, limit=500):
    """UC-25 step 2-3: who changed what and when, with old and new values, filtered by user, table, record or date."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return con.execute(_AUDIT, {"user": user_id, "table": table, "record": record_id, "since": since, "until": until,
                                    "limit": limit}).fetchall()


def audit_csv(admin_id, path=None, **filters):
    """UC-25 2a: the filtered audit log as CSV text (also written to path when given)."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        args = {"user": filters.get("user_id"), "table": filters.get("table"), "record": filters.get("record_id"),
                "since": filters.get("since"), "until": filters.get("until"), "limit": filters.get("limit", 100000)}
        return _csv(con.execute(_AUDIT, args), path)


# FR-32 / UC-24 clinical rules (MA-01): the procedures check the Admin role, audit the change and refuse stranding edits
def set_compatibility(admin_id, component, recipient_group, donor_group, allowed):
    if component not in COMPONENTS or recipient_group not in GROUPS or donor_group not in GROUPS:
        raise InvalidInput("choose a component and two of the eight blood groups")
    return _call(admin_id, "CALL set_compatibility(%s,%s,%s,%s,%s)", admin_id, component, recipient_group, donor_group,
                 bool(allowed))


def set_shelf_life(admin_id, component, days):
    if component not in COMPONENTS:
        raise InvalidInput("component: choose RBC, PLASMA or PLT")
    _check_days(days, "shelf_life_days")
    return _call(admin_id, "CALL set_shelf_life(%s,%s,%s)", admin_id, component, days)


def set_donation_interval(admin_id, donation_type, sex, days):
    if sex not in ("M", "F"):
        raise InvalidInput("sex: choose M or F")
    _check_days(days, "interval_days")
    return _call(admin_id, "CALL set_donation_interval(%s,%s,%s,%s)", admin_id, donation_type, sex, days)


def save_deferral_reason(admin_id, code, description, permanent, default_days=None):
    if not re.fullmatch(r"[A-Za-z]{2,10}", code or ""):
        raise InvalidInput("reason_code: 2 to 10 letters")
    if not (description or "").strip():
        raise InvalidInput("description: required")
    if permanent and default_days is not None:
        raise InvalidInput("default_days: a permanent reason has no default length")
    if not permanent:
        _check_days(default_days, "default_days")
    return _call(admin_id, "CALL save_deferral_reason(%s,%s,%s,%s,%s)", admin_id, code, description, bool(permanent),
                 default_days)


def set_eligibility_rule(admin_id, sex, min_age, min_weight_kg, min_hemoglobin_gl):
    if sex not in ("M", "F"):
        raise InvalidInput("sex: choose M or F")
    if not (isinstance(min_age, int) and 16 <= min_age <= 25):
        raise InvalidInput("min_age_years: enter a whole number from 16 to 25")
    if not 30 <= float(min_weight_kg) <= 100:
        raise InvalidInput("min_weight_kg: enter a weight from 30 to 100 kg")
    if not 100 <= float(min_hemoglobin_gl) <= 180:
        raise InvalidInput("min_hemoglobin_gl: enter a value from 100 to 180 g/L")
    return _call(admin_id, "CALL set_eligibility_rule(%s,%s,%s,%s,%s)", admin_id, sex, min_age, min_weight_kg,
                 min_hemoglobin_gl)


def admin_overview(admin_id):
    """FR-34: the same SQL as key query Q06."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        cur = con.execute(QUERIES["Q06"].sql)
        return dict(zip([c.name for c in cur.description], cur.fetchone()))


def create_admin(admin_id, email, password, first, last):
    """UC-23 / FR-04: returns the new Admin's user_id."""
    _check_email(email)
    _check_password(password)
    return _call(admin_id, "CALL create_admin(%s,%s,%s,%s,%s)", admin_id, email, hash_password(password), first, last)


def create_staff(admin_id, email, password, first, last, facility_id, job_title=None):
    """UC-23 / FR-04: a staff account tied to one active facility; returns its user_id."""
    _check_email(email)
    _check_password(password)
    return _call(admin_id, "CALL create_staff(%s,%s,%s,%s,%s,%s,%s)",
                 admin_id, email, hash_password(password), first, last, facility_id, job_title)


def save_facility(admin_id, facility_id, name, facility_type, address, phone=None, email=None):
    """UC-23 / FR-35: facility_id None creates a facility; returns its id."""
    if facility_type not in ("Hospital", "Blood Bank"):
        raise InvalidInput("facility_type: choose Hospital or Blood Bank")
    if email:
        _check_email(email)
    return _call(admin_id, "CALL save_facility(%s,%s,%s,%s,%s,%s,%s)",
                 admin_id, facility_id, name, facility_type, address, phone, email)


def admin_reset_password(admin_id, user_id, new_password):
    """UC-01 2a / UC-23: an Admin sets a new password for an account; it also clears a lockout and ends open sessions."""
    _check_password(new_password)
    return _call(admin_id, "CALL admin_reset_password(%s,%s,%s)", admin_id, user_id, hash_password(new_password))


def set_account_active(admin_id, user_id, active):
    """UC-23 / FR-05: deactivate or reactivate an account (never deleted)."""
    return _call(admin_id, "CALL set_account_active(%s,%s,%s)", admin_id, user_id, active)


def set_facility_active(admin_id, facility_id, active):
    """UC-23 2a: refused while the facility has open requests."""
    return _call(admin_id, "CALL set_facility_active(%s,%s,%s)", admin_id, facility_id, active)


def nightly_expiry():
    """FR-20: run by the scheduler, not by a user."""
    with session() as con:
        return con.execute("CALL run_nightly_expiry()").fetchone()[0]


# ---------------------------------------------------------------------- helpers
def _years_between(born, on):
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def _check_days(days, field):
    if not isinstance(days, int) or days <= 0:
        raise InvalidInput(f"{field}: enter a whole number of days above 0")


def _csv(cur, path=None):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow([c.name for c in cur.description])
    out.writerows(cur.fetchall())
    text = buf.getvalue()
    if path:
        with open(path, "w", newline="") as f:
            f.write(text)
    return text


def _call(user_id, sql, *args):
    """One stored procedure in one transaction for one acting user; the procedure checks the user's role itself.
    Returns its first INOUT value, or None for a procedure that hands nothing back."""
    with session(user_id) as con:
        cur = con.execute(sql, args)
        row = cur.fetchone() if cur.description else None
        return row[0] if row else None


# ---------------------------------------------------------------------- SE-06 sessions
def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def sign_in(email, password):
    """SE-01 / SE-05 login, then a server-side session (SE-06). Returns (token, user_id, role); only the token's hash is stored."""
    user_id, role = login(email, password)
    token = secrets.token_urlsafe(32)
    with session(user_id) as con:
        con.execute("CALL start_session(%s, %s)", (user_id, _token_hash(token)))
    return token, user_id, role


def session_user(token):
    """SE-06: (user_id, role) for a live session, refreshing its idle window; raises AccessDenied once it has expired."""
    with session() as con:
        user_id, role = con.execute("CALL resume_session(%s)", (_token_hash(token or ""),)).fetchone()
    if user_id is None:
        raise AccessDenied("session expired")
    return user_id, role


def sign_out(token):
    """SE-06 logout: the token stops working at once."""
    with session() as con:
        con.execute("CALL end_session(%s)", (_token_hash(token or ""),))
