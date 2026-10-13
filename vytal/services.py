"""Backend operations behind the Step 5 screens.

Each function is one unit of work for one acting user. Business rules live in the database (DI-03); this layer adds
what the database cannot know: who is calling, which facility they belong to (SE-02), and password checks (SE-01, SE-05).
"""
import datetime as dt
import json
import re

from vytal.auth import hash_password, verify_password
from vytal.db import session
from vytal.queries import load

QUERIES = load()
GROUPS = {"O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"}
_DUMMY_HASH = hash_password("no-such-account")      # unknown emails cost the same scrypt time as real ones (SE-05)


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


def _own_request(con, staff_id, request_id):
    fac = _role(con, staff_id, "Staff")
    if con.execute("SELECT facility_id FROM v_request WHERE request_id = %s", (request_id,)).fetchone() != (fac,):
        raise AccessDenied("request belongs to another facility")
    return fac


# ---------------------------------------------------------------------- accounts
def register_donor(email, password, first, last, phone, dob, sex, weight_kg, address, blood_group=None):
    """FR-01, with server-side validation (SE-09) so the user gets a field-level message before the database refuses."""
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""):
        raise InvalidInput("email: enter an address like name@example.com")
    if len(password or "") < 10:
        raise InvalidInput("password: use at least 10 characters")
    if sex not in ("M", "F"):
        raise InvalidInput("sex: choose M or F")
    if not 20 <= float(weight_kg) <= 300:
        raise InvalidInput("weight_kg: enter a weight between 20 and 300 kg")
    if blood_group not in GROUPS | {None}:
        raise InvalidInput("blood_group: choose a group or 'I don't know'")
    if dob >= dt.date.today():
        raise InvalidInput("date_of_birth: must be in the past")
    with session() as con:
        return con.execute("SELECT register_donor(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (email, hash_password(password), first, last, phone, dob, sex, weight_kg, address,
                            blood_group)).fetchone()[0]


def login(email, password):
    """SE-01 / SE-05. Returns (user_id, role); raises AccessDenied with the reason shown to the user."""
    with session() as con:
        row = con.execute("SELECT user_id, role, password_hash FROM user_account WHERE email = lower(%s)", (email,)).fetchone()
        if not row:
            verify_password(password, _DUMMY_HASH)
            raise AccessDenied("wrong email or password")
        outcome = con.execute("SELECT login_attempt(%s, %s)", (row[0], verify_password(password, row[2]))).fetchone()[0]
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
    """FR-27: a donor accepts or declines a donation call once."""
    with session(donor_id) as con:
        _role(con, donor_id, "Donor")
        con.execute("SELECT respond_to_call(%s,%s,%s)", (call_id, donor_id, accept))


# ---------------------------------------------------------------------- staff: clinic
def screen_donor(staff_id, donor_id, answers):
    """FR-06: answers maps question_id -> bool. Returns the donor's eligibility status after screening."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        con.execute("SELECT submit_screening(%s,%s)", (donor_id, json.dumps({str(k): v for k, v in answers.items()})))
        return con.execute("SELECT status FROM v_donor_eligibility WHERE user_id = %s", (donor_id,)).fetchone()[0]


def verify_blood_group(staff_id, donor_id, blood_group):
    """FR-09."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        con.execute("UPDATE donor SET blood_group = %s, verified_by = %s, verified_at = now() WHERE user_id = %s",
                    (blood_group, staff_id, donor_id))


def record_donation(staff_id, donor_id, donation_type, volume_ml, hemoglobin_gl, components):
    """FR-11 / FR-12: returns (donation_id, unit ids), or (None, []) when low hemoglobin deferred the donor (UC-07 2a)."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        dn = con.execute("SELECT record_donation(%s,%s,%s,%s,%s)",
                         (donor_id, staff_id, donation_type, volume_ml, hemoglobin_gl)).fetchone()[0]
        if dn is None:
            return None, []
        units = [u for (u,) in con.execute("SELECT separate_donation(%s,%s)", (dn, components))]
    return dn, units


def record_tests(staff_id, donation_id, results):
    """FR-13: results maps test_code -> 'Negative' | 'Positive'. Only the collecting facility records results (SE-02)."""
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        if con.execute("SELECT s.facility_id FROM donation d JOIN staff s ON s.user_id = d.recorded_by "
                       "WHERE d.donation_id = %s", (donation_id,)).fetchone() != (fac,):
            raise AccessDenied("donation was collected at another facility")
        return con.execute("SELECT record_tests(%s,%s)", (donation_id, json.dumps(results))).fetchone()[0]


# ---------------------------------------------------------------------- staff: requests and stock
def submit_request(staff_id, patient_ref, recipient_group, component, units, urgency, required_by):
    """FR-22 + FR-23 in one transaction: create the request and reserve what the shelf can cover."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        rid = con.execute("INSERT INTO blood_request (requested_by, patient_ref, recipient_group, component_code, "
                          "units_requested, urgency, required_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING request_id",
                          (staff_id, patient_ref, recipient_group, component, units, urgency, required_by)).fetchone()[0]
        con.execute("SELECT allocate(%s)", (rid,))
        reserved, shortfall = con.execute("SELECT units_reserved, shortfall FROM v_request_status WHERE request_id = %s",
                                          (rid,)).fetchone()
    return {"request_id": rid, "reserved": reserved, "shortfall": shortfall}


def confirm_request_group(staff_id, request_id, blood_group):
    """UC-16 step 6: the lab confirms an Unknown group; reserve whatever compatible stock now covers the shortfall."""
    if blood_group not in GROUPS:
        raise InvalidInput("blood_group: choose one of the eight groups")
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        if con.execute("UPDATE blood_request SET recipient_group = %s WHERE request_id = %s AND recipient_group IS NULL "
                       "AND status = 'Open'", (blood_group, request_id)).rowcount != 1:
            raise InvalidInput("request is closed or already has a confirmed group")
        con.execute("SELECT allocate(%s)", (request_id,))
        reserved, shortfall = con.execute("SELECT units_reserved, shortfall FROM v_request_status WHERE request_id = %s",
                                          (request_id,)).fetchone()
    return {"request_id": request_id, "reserved": reserved, "shortfall": shortfall}


def close_request(staff_id, request_id, cancel=False):
    """FR-28: issue the reserved units (or cancel and release them) for a request at the caller's facility."""
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        return con.execute("SELECT close_request(%s,%s)", (request_id, cancel)).fetchone()[0]


def find_donors(staff_id, request_id):
    """FR-25: eligible, compatible donors for the request; donor code, group and eligibility only (SE-07)."""
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        return con.execute("SELECT * FROM eligible_donors(%s) ORDER BY donor_code", (request_id,)).fetchall()


def call_donors(staff_id, request_id, message, donor_codes=None):
    """FR-26: send an in-app donation call to the chosen eligible donors (all of them if none are chosen)."""
    with session(staff_id) as con:
        _own_request(con, staff_id, request_id)
        return con.execute("SELECT send_donation_call(%s,%s,%s,%s)", (request_id, staff_id, message, donor_codes)).fetchone()[0]


def inventory(staff_id):
    """FR-17 for the caller's own facility only (SE-02)."""
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return con.execute("SELECT blood_group, component_code, available, reserved, expiring_48h FROM v_inventory "
                           "WHERE facility_id = %s ORDER BY component_code, blood_group", (fac,)).fetchall()


def quarantine_donor(staff_id, donor_id):
    """FR-31; returns the lookback rows (FR-30) so staff can see what was pulled and what was already issued."""
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        con.execute("SELECT quarantine_donor(%s)", (donor_id,))
        return con.execute("SELECT unit_id, component_code, status, request_facility, issued_at FROM v_lookback "
                           "WHERE donor_id = %s ORDER BY donated_at, unit_id", (donor_id,)).fetchall()


# ---------------------------------------------------------------------- admin
def admin_overview(admin_id):
    """FR-34: the same SQL as key query Q06."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        cur = con.execute(QUERIES["Q06"].sql)
        return dict(zip([c.name for c in cur.description], cur.fetchone()))


def nightly_expiry():
    """FR-20: run by the scheduler, not by a user."""
    with session() as con:
        return con.execute("SELECT expire_units()").fetchone()[0]
