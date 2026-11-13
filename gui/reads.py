"""Read-only queries behind the Step 5 screens.

Every function takes the acting user, checks their role with the same helper the services use (SE-02) and scopes rows to
their facility where the schema ties a record to one. All SQL is parameterized (SE-03); nothing here writes.
"""
import datetime as dt

from vytal import services as svc
from vytal.db import session
from vytal.services import AccessDenied, InvalidInput, _role

GROUP_ORDER = ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]
COMPONENTS = [("RBC", "Red cells"), ("PLASMA", "Plasma"), ("PLT", "Platelets")]
URGENCY_RANK = "CASE urgency WHEN 'Emergency' THEN 0 WHEN 'Urgent' THEN 1 ELSE 2 END"


def rows(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(con, sql, params=()):
    r = rows(con, sql, params)
    return r[0] if r else None


# ---------------------------------------------------------------- shared
def user_card(user_id):
    with session(user_id) as con:
        card = one(con, "SELECT user_id, first_name, last_name, role FROM user_account WHERE user_id = %s AND is_active", (user_id,))
        if not card:
            raise AccessDenied("no such account")
        card["unread"] = con.execute("SELECT count(*) FROM notification WHERE user_id = %s AND read_at IS NULL", (user_id,)).fetchone()[0]
        card["facility"] = one(con, "SELECT f.facility_id, f.name FROM staff s JOIN facility f USING (facility_id) WHERE s.user_id = %s", (user_id,))
        return card


def facilities(user_id, active_only=True):
    with session(user_id) as con:
        return rows(con, "SELECT facility_id, name, facility_type, address, phone, email, is_active FROM facility "
                         "WHERE (NOT %s OR is_active) ORDER BY name", (active_only,))


# ---------------------------------------------------------------- donor
def donor_dashboard(donor_id):
    home = svc.donor_home(donor_id)
    with session(donor_id) as con:
        d = one(con, "SELECT d.donor_code, d.blood_group, nullif(d.next_eligible_date, 'infinity') AS next_eligible_date, "
                     "d.next_eligible_date = 'infinity' AS permanent, d.sex, d.weight_kg, d.verified_by IS NOT NULL AS verified "
                     "FROM donor d WHERE d.user_id = %s", (donor_id,))
        interval = con.execute("SELECT i.interval_days FROM donation_interval i WHERE i.donation_type_code = 'WB' AND i.sex = %s",
                               (d["sex"],)).fetchone()[0]
        last = con.execute("SELECT max(donated_at) FROM donation WHERE donor_id = %s", (donor_id,)).fetchone()[0]
        last_screen = con.execute("SELECT max(completed_at) FROM screening WHERE donor_id = %s", (donor_id,)).fetchone()[0]
        deferrals = rows(con, "SELECT r.description, f.start_date, f.end_date, r.is_permanent FROM deferral f "
                              "JOIN deferral_reason r USING (reason_code) WHERE f.donor_id = %s AND (f.end_date IS NULL OR f.end_date >= CURRENT_DATE) "
                              "ORDER BY f.start_date DESC", (donor_id,))
        lifetime = con.execute("SELECT count(*), coalesce(sum(volume_ml), 0) FROM donation WHERE donor_id = %s", (donor_id,)).fetchone()
        today = con.execute("SELECT CURRENT_DATE").fetchone()[0]
    nxt = d["next_eligible_date"]
    permanent = bool(d["permanent"])
    days_left = None if (nxt is None or permanent) else max((nxt - today).days, 0)
    ring = 100
    if last and days_left is not None and days_left > 0 and interval:
        ring = max(0, min(100, round(100 * (interval - days_left) / interval)))
    elif permanent:
        ring = 0
    return {**home, "info": d, "interval": interval, "last_donation": last, "last_screening": last_screen, "deferrals": deferrals,
            "days_left": days_left, "permanent": permanent, "ring": ring, "donations": lifetime[0], "litres": round(lifetime[1] / 1000, 2)}


def screening_questions(user_id):
    with session(user_id) as con:
        return rows(con, "SELECT question_id, question_text, defer_on_answer FROM screening_question WHERE is_active ORDER BY question_id")


# ---------------------------------------------------------------- staff
def staff_overview(staff_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        facility = one(con, "SELECT facility_id, name, facility_type, address FROM facility WHERE facility_id = %s", (fac,))
        inv = rows(con, "SELECT blood_group, component_code, available, reserved, expiring_48h FROM v_inventory WHERE facility_id = %s", (fac,))
        thr = rows(con, "SELECT blood_group, component_code, min_units FROM stock_threshold WHERE facility_id = %s", (fac,))
        alerts = svc.open_alerts(staff_id)
        queue = request_queue(con, fac, limit=6)
        stats = one(con, "SELECT (SELECT count(*) FROM v_blood_unit WHERE facility_id = %s AND status = 'Available') AS available, "
                         "(SELECT coalesce(sum(expiring_48h), 0) FROM v_inventory WHERE facility_id = %s) AS expiring, "
                         "(SELECT count(*) FROM v_request WHERE facility_id = %s AND status = 'Open') AS open_requests, "
                         "(SELECT count(*) FROM v_donor_eligibility WHERE can_be_called AND group_verified) AS donors_ready", (fac, fac, fac))
    matrix = {c: {g: {"available": 0, "reserved": 0, "expiring": 0, "min": None} for g in GROUP_ORDER} for c, _ in COMPONENTS}
    for r in inv:
        matrix[r["component_code"]][r["blood_group"]].update(available=r["available"], reserved=r["reserved"], expiring=r["expiring_48h"])
    for t in thr:
        matrix[t["component_code"]][t["blood_group"]]["min"] = t["min_units"]
    return {"facility": facility, "matrix": matrix, "alerts": alerts, "queue": queue, "stats": stats}


def request_queue(con, fac, status=None, limit=200):
    """FR-29: urgency first, then the earliest required-by time."""
    return rows(con, f"SELECT r.request_id, r.patient_ref, r.recipient_group, r.component_code, r.units_requested, r.urgency, r.required_by, "
                     f"r.status, r.created_at, s.units_reserved, s.shortfall FROM v_request r JOIN v_request_status s USING (request_id) "
                     f"WHERE r.facility_id = %s AND (%s::text IS NULL OR r.status = %s) "
                     f"ORDER BY (r.status <> 'Open'), {URGENCY_RANK.replace('urgency', 'r.urgency')}, r.required_by, r.request_id LIMIT %s",
                (fac, status, status, limit))


def requests_list(staff_id, status=None):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return request_queue(con, fac, status or None)


def request_detail(staff_id, request_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        req = one(con, "SELECT r.*, s.units_reserved, s.shortfall, f.name AS facility_name FROM v_request r JOIN v_request_status s USING (request_id) "
                       "JOIN facility f USING (facility_id) WHERE r.request_id = %s", (request_id,))
        if not req or req["facility_id"] != fac:
            raise AccessDenied("request belongs to another facility")
        units = rows(con, "SELECT u.unit_id, u.component_code, u.blood_group, u.expires_at, u.status, ru.issued_at FROM request_unit ru "
                          "JOIN v_blood_unit u USING (unit_id) WHERE ru.request_id = %s AND ru.released_at IS NULL ORDER BY u.expires_at", (request_id,))
        calls = rows(con, "SELECT c.call_id, c.sent_at, count(r.donor_id) AS recipients, count(*) FILTER (WHERE r.response = 'Accepted') AS accepted, "
                          "count(*) FILTER (WHERE r.response = 'Declined') AS declined FROM donation_call c LEFT JOIN call_recipient r USING (call_id) "
                          "WHERE c.request_id = %s GROUP BY c.call_id ORDER BY c.sent_at DESC", (request_id,))
    donors = svc.find_donors(staff_id, request_id) if req["status"] == "Open" else []
    responses = svc.call_responses(staff_id, request_id)
    return {"req": req, "units": units, "calls": calls, "donors": donors, "responses": responses}


def units_list(staff_id, status=None, component=None, group=None, limit=250):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return rows(con, "SELECT unit_id, component_code, blood_group, status, expires_at, collected_at, status_reason FROM v_blood_unit "
                         "WHERE facility_id = %s AND (%s::text IS NULL OR status = %s) AND (%s::text IS NULL OR component_code = %s) "
                         "AND (%s::text IS NULL OR blood_group = %s) "
                         "ORDER BY (status NOT IN ('Available','Reserved','Collected','Quarantined')), expires_at LIMIT %s",
                    (fac, status, status, component, component, group, group, limit))


def thresholds(staff_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return fac, rows(con, "SELECT blood_group, component_code, min_units FROM stock_threshold WHERE facility_id = %s "
                              "ORDER BY component_code, blood_group", (fac,))


def donors_list(staff_id, q=None, group=None, ready=False):
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        return rows(con, "SELECT user_id, donor_code, blood_group, status, nullif(next_eligible_date, 'infinity') AS next_eligible_date, "
                         "next_eligible_date = 'infinity' AS permanent, group_verified, can_be_called FROM v_donor_eligibility "
                         "WHERE (%s::text IS NULL OR donor_code ILIKE %s) AND (%s::text IS NULL OR blood_group = %s) AND (NOT %s OR (can_be_called AND group_verified)) "
                         "ORDER BY donor_code LIMIT 150", (q or None, f"%{q}%", group or None, group or None, ready))


def donor_detail(staff_id, donor_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        d = one(con, "SELECT o.user_id, o.donor_code, o.date_of_birth, o.sex, o.weight_kg, o.blood_group, o.verified_at, o.is_flagged, "
                     "e.status, e.can_be_called, e.group_verified, nullif(e.next_eligible_date, 'infinity') AS next_eligible_date, "
                     "e.next_eligible_date = 'infinity' AS permanent, v.first_name, v.last_name "
                     "FROM donor o JOIN v_donor_eligibility e USING (user_id) JOIN user_account v ON v.user_id = o.user_id WHERE o.user_id = %s", (donor_id,))
        if not d:
            raise InvalidInput("no donor with that id")
        donations = rows(con, "SELECT dn.donation_id, dn.donated_at, dn.donation_type_code, dn.volume_ml, dn.hemoglobin_gl, f.name AS facility, "
                              "(SELECT count(*) FROM blood_unit u WHERE u.donation_id = dn.donation_id) AS units, s.facility_id = %s AS mine "
                              "FROM donation dn JOIN staff s ON s.user_id = dn.recorded_by JOIN facility f ON f.facility_id = s.facility_id "
                              "WHERE dn.donor_id = %s ORDER BY dn.donated_at DESC LIMIT 12", (fac, donor_id))
        deferrals = rows(con, "SELECT f.deferral_id, r.description, r.is_permanent, f.start_date, f.end_date FROM deferral f JOIN deferral_reason r USING (reason_code) "
                              "WHERE f.donor_id = %s ORDER BY f.start_date DESC, f.deferral_id DESC LIMIT 8", (donor_id,))
        last_screen = con.execute("SELECT max(screening_id) FROM screening WHERE donor_id = %s", (donor_id,)).fetchone()[0]
        reasons = rows(con, "SELECT reason_code, description, is_permanent, default_days FROM deferral_reason ORDER BY is_permanent, description")
        types = rows(con, "SELECT donation_type_code, name FROM donation_type ORDER BY 1")
    d["age"] = svc._years_between(d["date_of_birth"], dt.date.today())
    d["last_screening_id"] = last_screen
    return {"d": d, "donations": donations, "deferrals": deferrals, "reasons": reasons, "types": types}


def pending_tests(staff_id):
    """Donations collected here whose units are still waiting for test results (FR-13)."""
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        need = con.execute("SELECT count(*) FROM test_type WHERE is_active").fetchone()[0]
        tests = rows(con, "SELECT test_code, name FROM test_type WHERE is_active ORDER BY test_code")
        pending = rows(con, "SELECT dn.donation_id, dn.donated_at, o.donor_code, dn.donation_type_code, dn.status, "
                            "(SELECT count(*) FROM blood_unit u WHERE u.donation_id = dn.donation_id) AS units, "
                            "(SELECT count(*) FROM test_result t WHERE t.donation_id = dn.donation_id) AS tested "
                            "FROM donation dn JOIN staff s ON s.user_id = dn.recorded_by JOIN donor o ON o.user_id = dn.donor_id "
                            "WHERE s.facility_id = %s AND dn.status <> 'Discarded' AND "
                            "(SELECT count(*) FROM test_result t WHERE t.donation_id = dn.donation_id) < %s ORDER BY dn.donated_at", (fac, need))
        donations = rows(con, "SELECT dn.donation_id, dn.donated_at, dn.donation_type_code, dn.volume_ml, dn.status, o.donor_code, "
                              "(SELECT count(*) FROM blood_unit u WHERE u.donation_id = dn.donation_id) AS units FROM donation dn "
                              "JOIN staff s ON s.user_id = dn.recorded_by JOIN donor o ON o.user_id = dn.donor_id "
                              "WHERE s.facility_id = %s ORDER BY dn.donated_at DESC LIMIT 25", (fac,))
    return {"pending": pending, "tests": tests, "recent": donations}


def donation_units(staff_id, donation_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return rows(con, "SELECT u.unit_id, u.component_code, u.status FROM blood_unit u JOIN donation dn USING (donation_id) "
                         "JOIN staff s ON s.user_id = dn.recorded_by WHERE dn.donation_id = %s AND s.facility_id = %s ORDER BY u.unit_id", (donation_id, fac))


# ---------------------------------------------------------------- admin
def admin_dashboard(admin_id):
    stats = svc.admin_overview(admin_id)
    with session(admin_id) as con:
        wastage = rows(con, "SELECT to_char(month, 'Mon YY') AS label, month, sum(units_wasted)::int AS wasted FROM v_wastage "
                            "GROUP BY month ORDER BY month DESC LIMIT 8")
        alerts = rows(con, "SELECT f.name AS facility, a.blood_group, a.component_code, a.opened_at FROM stock_alert a "
                           "JOIN facility f USING (facility_id) WHERE a.cleared_at IS NULL ORDER BY a.opened_at LIMIT 8")
        users = rows(con, "SELECT role, count(*) FILTER (WHERE is_active)::int AS active, count(*)::int AS total FROM user_account GROUP BY role ORDER BY role")
        requests = rows(con, "SELECT r.request_id, r.patient_ref, r.recipient_group, r.component_code, r.units_requested, r.urgency, r.required_by, f.name AS facility "
                             "FROM v_request r JOIN facility f USING (facility_id) WHERE r.status = 'Open' "
                             f"ORDER BY {URGENCY_RANK.replace('urgency', 'r.urgency')}, r.required_by LIMIT 6")
    return {"stats": stats, "wastage": list(reversed(wastage)), "alerts": alerts, "users": users, "requests": requests,
            "audit": svc.audit_log(admin_id, limit=8)}


def users_list(admin_id, role=None, q=None):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return rows(con, "SELECT u.user_id, u.first_name, u.last_name, u.role, u.is_active, u.created_at, f.name AS facility, s.job_title, d.donor_code "
                         "FROM user_account u LEFT JOIN staff s USING (user_id) LEFT JOIN facility f USING (facility_id) LEFT JOIN donor d USING (user_id) "
                         "WHERE (%s::text IS NULL OR u.role = %s) AND (%s::text IS NULL OR (u.first_name || ' ' || u.last_name || ' ' || coalesce(d.donor_code, '')) ILIKE %s) "
                         "ORDER BY u.role, u.last_name, u.first_name LIMIT 200", (role or None, role or None, q or None, f"%{q}%"))


def facilities_admin(admin_id):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return rows(con, "SELECT f.facility_id, f.name, f.facility_type, f.address, f.phone, f.email, f.is_active, "
                         "(SELECT count(*) FROM staff s WHERE s.facility_id = f.facility_id) AS staff, "
                         "(SELECT count(*) FROM v_request r WHERE r.facility_id = f.facility_id AND r.status = 'Open') AS open_requests, "
                         "(SELECT count(*) FROM v_blood_unit u WHERE u.facility_id = f.facility_id AND u.status = 'Available') AS units "
                         "FROM facility f ORDER BY f.is_active DESC, f.name")


def rules(admin_id):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        compat = {(r[0], r[1], r[2]) for r in con.execute("SELECT component_code, recipient_group, donor_group FROM compatibility").fetchall()}
        return {"compat": compat,
                "components": rows(con, "SELECT component_code, name, shelf_life_days FROM component_type ORDER BY component_code"),
                "intervals": rows(con, "SELECT i.donation_type_code, t.name, i.sex, i.interval_days FROM donation_interval i "
                                       "JOIN donation_type t USING (donation_type_code) ORDER BY 1, 3"),
                "reasons": rows(con, "SELECT reason_code, description, is_permanent, default_days FROM deferral_reason ORDER BY is_permanent, reason_code"),
                "elig": rows(con, "SELECT sex, min_age_years, min_weight_kg, min_hemoglobin_gl FROM eligibility_rule ORDER BY sex")}


def audit_tables(admin_id):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return [r[0] for r in con.execute("SELECT DISTINCT table_name FROM audit_log ORDER BY 1").fetchall()]


def admin_thresholds(admin_id, facility_id):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return rows(con, "SELECT blood_group, component_code, min_units FROM stock_threshold WHERE facility_id = %s ORDER BY component_code, blood_group", (facility_id,))


def quarantined_units(admin_id):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return rows(con, "SELECT u.unit_id, u.component_code, u.blood_group, f.name AS facility, u.status_changed_at FROM v_blood_unit u "
                         "JOIN facility f USING (facility_id) WHERE u.status = 'Quarantined' ORDER BY u.status_changed_at DESC")
