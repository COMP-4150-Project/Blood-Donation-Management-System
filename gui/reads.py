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
TYPE_NAMES = {"WB": "Whole blood", "PLS": "Plasma", "PLT": "Platelets"}
ON_SHELF = "status = 'Available' AND expires_at > now()"                  # C10: one rule for stock everywhere
URGENCY_RANK = "CASE urgency WHEN 'Emergency' THEN 0 WHEN 'Urgent' THEN 1 ELSE 2 END"


def rows(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(con, sql, params=()):
    r = rows(con, sql, params)
    return r[0] if r else None


# ---------------------------------------------------------------- shared
def _card(con, user_id):
    card = one(con, "SELECT user_id, first_name, last_name, role FROM user_account WHERE user_id = %s AND is_active", (user_id,))
    if not card:
        raise AccessDenied("no such account")
    card["unread"] = con.execute("SELECT count(*) FROM notification WHERE user_id = %s AND read_at IS NULL", (user_id,)).fetchone()[0]
    card["facility"] = one(con, "SELECT f.facility_id, f.name FROM staff s JOIN facility f USING (facility_id) WHERE s.user_id = %s", (user_id,))
    return card


def user_card(user_id):
    with session(user_id) as con:
        return _card(con, user_id)


def session_card(token):
    """One connection for the whole sign-in check of a request: resume the session (SE-06 idle window) and read the user card.
    Returns None once the session has ended, timed out or its account was deactivated."""
    with session() as con:
        found, _role_name = con.execute("CALL resume_session(%s)", (svc._token_hash(token or ""),)).fetchone()
        if found is None:
            return None
        con.execute("SELECT set_config('vytal.user_id', %s, true)", (str(found),))
        return _card(con, found)


def end_reason(user_id):
    """Why a session ended, as far as the database can tell: 'deactivated', 'facility' (the staff member's facility was
    deactivated, ADM-12), 'timeout' or 'ended' (password changed or reset, or signed out elsewhere)."""
    try:
        with session(user_id) as con:
            active = con.execute("SELECT is_active FROM user_account WHERE user_id = %s", (user_id,)).fetchone()
            if active is not None and not active[0]:
                return "deactivated"
            fac = con.execute("SELECT f.is_active FROM staff s JOIN facility f USING (facility_id) WHERE s.user_id = %s", (user_id,)).fetchone()
            if fac is not None and not fac[0]:
                return "facility"
            last = con.execute("SELECT last_seen_at, ended_at FROM user_session WHERE user_id = %s ORDER BY session_id DESC LIMIT 1",
                               (user_id,)).fetchone()
    except Exception:
        return "timeout"
    if last and last[1] is not None and (last[1] - last[0]) < dt.timedelta(minutes=15):
        return "ended"
    return "timeout"


def facilities(user_id, active_only=True):
    with session(user_id) as con:
        return rows(con, "SELECT facility_id, name, facility_type, address, phone, email, is_active FROM facility "
                         "WHERE (NOT %s OR is_active) ORDER BY name", (active_only,))


def notifications(user_id, limit=200):
    """UC-09 / UC-20: the caller's notifications, newest first. A call notification carries its call, the caller's answer and
    whether the call can still be answered (C9), so the page can offer Accept and Decline."""
    with session(user_id) as con:
        return rows(con, "SELECT n.notification_id, n.message, n.created_at, n.read_at, n.call_id, cr.response, "
                         "(cr.response = 'Pending' AND r.status = 'Open' AND r.required_by > now()) AS answerable, "
                         "cr.donor_id IS NOT NULL AS for_donor, r.status AS request_status "
                         "FROM notification n LEFT JOIN call_recipient cr ON cr.call_id = n.call_id AND cr.donor_id = n.user_id "
                         "LEFT JOIN donation_call c ON c.call_id = n.call_id LEFT JOIN v_request r ON r.request_id = c.request_id "
                         "WHERE n.user_id = %s ORDER BY n.created_at DESC, n.notification_id DESC LIMIT %s", (user_id, limit))


# ---------------------------------------------------------------- donor
def donor_dashboard(donor_id):
    home = svc.donor_home(donor_id)
    today = svc.clinic_today()                                             # C1: the clinic's day, not the server's
    with session(donor_id) as con:
        d = one(con, "SELECT d.donor_code, d.blood_group, nullif(d.next_eligible_date, 'infinity') AS next_eligible_date, "
                     "d.next_eligible_date = 'infinity' AS permanent, d.sex, d.weight_kg, d.height_cm, d.verified_by IS NOT NULL AS verified, "
                     "estimated_blood_volume_ml(d.sex, d.height_cm, d.weight_kg) AS ebv_ml, r.min_blood_volume_ml, r.min_weight_kg "
                     "FROM donor d JOIN eligibility_rule r USING (sex) WHERE d.user_id = %s", (donor_id,))
        lt = con.execute("SELECT donation_type_code, donated_at FROM donation WHERE donor_id = %s ORDER BY donated_at DESC LIMIT 1", (donor_id,)).fetchone()
        last_type = lt[0] if lt else None
        # C11 / DON-02: the wait is the interval of the last donation's type (whole blood before a first donation), by sex
        interval = con.execute("SELECT i.interval_days FROM donation_interval i WHERE i.donation_type_code = %s AND i.sex = %s",
                               (last_type or "WB", d["sex"])).fetchone()
        interval = interval[0] if interval else None
        last_screen = con.execute("SELECT max(completed_at) FROM screening WHERE donor_id = %s", (donor_id,)).fetchone()[0]
        deferrals = rows(con, "SELECT DISTINCT r.description, f.start_date, f.end_date, r.is_permanent FROM deferral f "
                              "JOIN deferral_reason r USING (reason_code) WHERE f.donor_id = %s AND (f.end_date IS NULL OR f.end_date >= %s) "
                              "ORDER BY f.start_date DESC", (donor_id, today))
        lifetime = con.execute("SELECT count(*), coalesce(sum(volume_ml), 0) FROM donation WHERE donor_id = %s", (donor_id,)).fetchone()
        calls = rows(con, "SELECT c.call_id, cr.response, r.status <> 'Open' OR r.required_by <= now() AS closed, "
                          "f.name AS facility, f.address, f.phone FROM call_recipient cr JOIN donation_call c USING (call_id) "
                          "JOIN v_request r USING (request_id) JOIN facility f ON f.facility_id = r.facility_id WHERE cr.donor_id = %s", (donor_id,))
    closed = {c["call_id"] for c in calls if c["closed"]}
    where = {c["call_id"]: c for c in calls if c["response"] == "Accepted"}          # DON-04: the accepted call keeps the address and phone
    nxt = d["next_eligible_date"]
    permanent = bool(d["permanent"])
    banner = eligibility_banner(home["status"], d, permanent)
    # DON-07: a countdown only when the status is a waiting period with a date (never for a flagged or permanent donor)
    days_left = max((nxt - today).days, 0) if (banner["kind"] == "wait" and nxt is not None) else None
    ring = 0
    if banner["state"] in ("eligible", "screen"):
        ring = 100                                  # nothing but a screening stands in the way
    elif banner["kind"] == "wait" and lt and days_left and interval:
        ring = max(0, min(100, round(100 * (interval - days_left) / interval)))
    return {**home, "elig_status": home["status"], "banner": banner, "closed_calls": closed, "accepted_at": where, "last_donation_type": last_type,
            "type_name": TYPE_NAMES.get(last_type or "WB", "Whole blood"), "info": d, "interval": interval, "last_donation": lt[1] if lt else None,
            "last_screening": last_screen, "deferrals": deferrals, "days_left": days_left, "permanent": permanent, "ring": ring,
            "donations": lifetime[0], "litres": round(lifetime[1] / 1000, 2)}


def _ml(value):
    return f"{int(round(float(value))):,} ml"


def eligibility_banner(status, info, permanent):
    """The one value the donor's top banner is built from (headline, pill, ring text and reason), derived from the database's
    eligibility status alone, so they cannot disagree. Only the exact status 'Eligible' reads as eligible."""
    s = (status or "").strip()
    kg = f"{float(info['min_weight_kg']):g}"
    if s == "Eligible":
        return {"state": "eligible", "kind": "eligible", "tone": "green", "label": "Eligible to donate", "headline": "You are eligible to donate",
                "ring_value": "Now", "ring_text": "you can give", "reason": None}
    if s == "Complete screening to confirm eligibility":
        return {"state": "screen", "kind": "screen", "tone": "amber", "label": "Screening needed", "ring_value": "Next", "ring_text": "screening",
                "headline": "Complete your health screening to confirm you can donate", "reason": None}
    if s.startswith("Screening older than 24"):
        return {"state": "screen", "kind": "screen", "tone": "amber", "label": "Screening needed", "ring_value": "Next", "ring_text": "screening",
                "headline": "Repeat your health screening before you donate", "reason": None}
    if s == "Account inactive":
        return {"state": "inactive", "kind": "inactive", "tone": "red", "label": "Account not active", "ring_value": "\u2014", "ring_text": "inactive",
                "headline": "Your account is not active", "reason": None}
    base = {"state": "not_eligible", "tone": "red", "label": "Not eligible right now", "ring_value": "\u2014", "ring_text": "not yet",
            "headline": "You are not eligible to donate right now"}
    if permanent or s == "Permanently deferred":
        return base | {"state": "permanent", "kind": "permanent", "label": "Not able to donate", "ring_text": "not eligible",
                       "headline": "You are not eligible to donate",
                       "reason": "You are permanently deferred and unable to donate. If you have questions, please speak to the clinic team or Canadian Blood Services."}
    if "under" in s and " kg" in s:
        return base | {"kind": "weight", "ring_text": "not eligible",
                       "reason": f"Your weight is under the {kg} kg minimum. Update your weight in Profile if it has changed, or speak to the clinic team."}
    if "blood volume" in s:                                                # C11 / DON-06: whole ml, so the two figures never read the same
        return base | {"kind": "volume", "ring_text": "not eligible",
                       "reason": f"Your height and weight give an estimated blood volume of {_ml(info['ebv_ml'])}, under the "
                                 f"{_ml(info['min_blood_volume_ml'])} minimum for a safe donation. "
                                 "Update your height or weight in Profile if they have changed, or speak to the clinic team."}
    if "lookback" in s:                                                    # C11: flagged for an investigation, no positive test
        return base | {"kind": "flagged", "ring_text": "on hold",
                       "reason": "Your earlier donations are being reviewed, so donations are on hold for now. The clinic team will contact you."}
    if "flagged" in s:
        return base | {"kind": "flagged", "ring_text": "not eligible",
                       "reason": "A test on an earlier donation needs follow-up. Please speak to the clinic team."}
    if s.startswith("Not eligible until"):
        return base | {"kind": "wait", "ring_text": "days to go",
                       "reason": "A waiting period applies before your next donation."}
    return base | {"kind": "other", "ring_text": "not eligible",
                   "reason": "If you are not sure why, please speak to the clinic team. They can explain what happens next."}


def donor_group(user_id):
    """UC-03 2a: the donor's blood group and whether staff verified it (shown read-only on the profile)."""
    with session(user_id) as con:
        return one(con, "SELECT blood_group, verified_by IS NOT NULL AS verified FROM donor WHERE user_id = %s", (user_id,))


def donor_permanent(user_id):
    """UC-04 precondition: a permanently deferred donor is not asked to screen (DON-09)."""
    with session(user_id) as con:
        r = con.execute("SELECT next_eligible_date = 'infinity' FROM donor WHERE user_id = %s", (user_id,)).fetchone()
    return bool(r and r[0])


def donor_sex(user_id):
    with session(user_id) as con:
        r = con.execute("SELECT sex FROM donor WHERE user_id = %s", (user_id,)).fetchone()
    return r[0] if r else None


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
        stats = one(con, f"SELECT (SELECT count(*) FROM v_blood_unit WHERE facility_id = %s AND {ON_SHELF}) AS available, "
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
                     f"r.status, r.created_at, s.units_reserved, s.shortfall, "
                     f"(SELECT count(*) FROM request_unit ru WHERE ru.request_id = r.request_id AND ru.issued_at IS NOT NULL) AS units_issued "
                     f"FROM v_request r JOIN v_request_status s USING (request_id) "
                     f"WHERE r.facility_id = %s AND (%s::text IS NULL OR r.status = %s) "
                     f"ORDER BY (r.status <> 'Open'), {URGENCY_RANK.replace('urgency', 'r.urgency')}, r.required_by, r.request_id LIMIT %s",
                (fac, status, status, limit))


def requests_list(staff_id, status=None):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return request_queue(con, fac, status or None)


def requests_total(staff_id, status=None):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return con.execute("SELECT count(*) FROM v_request WHERE facility_id = %s AND (%s::text IS NULL OR status = %s)", (fac, status or None, status or None)).fetchone()[0]


def request_detail(staff_id, request_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        req = one(con, "SELECT r.*, s.units_reserved, s.shortfall, f.name AS facility_name, "
                       "(SELECT count(*) FROM request_unit ru WHERE ru.request_id = r.request_id AND ru.issued_at IS NOT NULL) AS units_issued "
                       "FROM v_request r JOIN v_request_status s USING (request_id) JOIN facility f USING (facility_id) WHERE r.request_id = %s", (request_id,))
        if not req:                                                        # C6 / STF-07: missing is not 'another facility'
            raise InvalidInput(f"request: Request #{request_id} was not found")
        if req["facility_id"] != fac:
            raise AccessDenied(f"Request #{request_id} belongs to another facility")   # the same text as the refused action: one toast (STF-07)
        units = rows(con, "SELECT u.unit_id, u.component_code, u.blood_group, u.expires_at, u.status, ru.issued_at FROM request_unit ru "
                          "JOIN v_blood_unit u USING (unit_id) WHERE ru.request_id = %s AND ru.released_at IS NULL ORDER BY u.expires_at", (request_id,))
        calls = rows(con, "SELECT c.call_id, c.sent_at, count(r.donor_id) AS recipients, count(*) FILTER (WHERE r.response = 'Accepted') AS accepted, "
                          "count(*) FILTER (WHERE r.response = 'Declined') AS declined FROM donation_call c LEFT JOIN call_recipient r USING (call_id) "
                          "WHERE c.request_id = %s GROUP BY c.call_id ORDER BY c.sent_at DESC", (request_id,))
        called = {r[0] for r in con.execute("SELECT o.donor_code FROM donation_call c JOIN call_recipient r USING (call_id) JOIN donor o ON o.user_id = r.donor_id "
                                            "WHERE c.request_id = %s", (request_id,)).fetchall()}
    donors = svc.find_donors(staff_id, request_id) if req["status"] == "Open" else []
    responses = svc.call_responses(staff_id, request_id)
    return {"req": req, "units": units, "calls": calls, "donors": donors, "responses": responses, "called": called}


def called_codes(staff_id, request_id):
    """Donor codes that already received a call for this request (FR-26: one call per donor and request)."""
    with session(staff_id) as con:
        svc._own_request(con, staff_id, request_id)
        return {r[0] for r in con.execute("SELECT o.donor_code FROM donation_call c JOIN call_recipient r USING (call_id) JOIN donor o ON o.user_id = r.donor_id "
                                          "WHERE c.request_id = %s", (request_id,)).fetchall()}


_UNITS_WHERE = ("WHERE facility_id = %(fac)s AND (%(status)s::text IS NULL OR (CASE WHEN status = 'Available' AND expires_at <= now() "
                "THEN 'Expired' ELSE status END) = %(status)s) AND (%(comp)s::text IS NULL OR component_code = %(comp)s) "
                "AND (%(grp)s::text IS NULL OR blood_group = %(grp)s) AND (%(q)s::text IS NULL OR unit_id ILIKE %(like)s)")


def _unit_args(fac, status, component, group, q):
    q = (q or "").strip() or None
    return {"fac": fac, "status": status, "comp": component, "grp": group, "q": q, "like": _like(q or "")}


def units_list(staff_id, status=None, component=None, group=None, q=None, limit=50, offset=0):
    """Inventory rows (L11: one page at a time). A unit past its expiry time reads as Expired even before the nightly job
    marks it (C10 / STF-01); `q` finds a unit by part of its id (STF-14)."""
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return rows(con, "SELECT unit_id, component_code, blood_group, CASE WHEN status = 'Available' AND expires_at <= now() THEN 'Expired' "
                         "ELSE status END AS status, status = 'Available' AND expires_at <= now() AS overdue, expires_at, collected_at, status_reason "
                         "FROM v_blood_unit " + _UNITS_WHERE + " ORDER BY (status NOT IN ('Available','Reserved','Collected','Quarantined') "
                         "OR expires_at <= now()), expires_at, unit_id LIMIT %(limit)s OFFSET %(offset)s",
                    _unit_args(fac, status, component, group, q) | {"limit": limit, "offset": offset})


def units_total(staff_id, status=None, component=None, group=None, q=None):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return con.execute("SELECT count(*) FROM v_blood_unit " + _UNITS_WHERE, _unit_args(fac, status, component, group, q)).fetchone()[0]


def thresholds(staff_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return fac, rows(con, "SELECT blood_group, component_code, min_units FROM stock_threshold WHERE facility_id = %s "
                              "ORDER BY component_code, blood_group", (fac,))


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


_DONORS_WHERE = ("WHERE (%s::text IS NULL OR donor_code ILIKE %s OR upper(blood_group) = upper(%s)) AND (%s::text IS NULL OR blood_group = %s) "
                 "AND (NOT %s OR (can_be_called AND group_verified))")


def donors_list(staff_id, q=None, group=None, ready=False):
    """The quick filter matches a donor code or a blood group, so 'O-' and 'ab+' find the negative and positive groups too."""
    q = (q or "").strip() or None
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        return rows(con, "SELECT user_id, donor_code, blood_group, status, nullif(next_eligible_date, 'infinity') AS next_eligible_date, "
                         "next_eligible_date = 'infinity' AS permanent, group_verified, can_be_called FROM v_donor_eligibility "
                         + _DONORS_WHERE + " ORDER BY donor_code LIMIT 150", (q, _like(q or ""), q, group or None, group or None, ready))


def donors_total(staff_id, q=None, group=None, ready=False):
    q = (q or "").strip() or None
    with session(staff_id) as con:
        _role(con, staff_id, "Staff")
        return con.execute("SELECT count(*) FROM v_donor_eligibility " + _DONORS_WHERE, (q, _like(q or ""), q, group or None, group or None, ready)).fetchone()[0]


def donor_detail(staff_id, donor_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        d = one(con, "SELECT o.user_id, o.donor_code, o.date_of_birth, o.sex, o.weight_kg, o.height_cm, estimated_blood_volume_ml(o.sex, o.height_cm, o.weight_kg) AS ebv_ml, o.blood_group, o.verified_at, o.is_flagged, "
                     "e.status, e.can_be_called, e.group_verified, nullif(e.next_eligible_date, 'infinity') AS next_eligible_date, "
                     "e.next_eligible_date = 'infinity' AS permanent, v.first_name, v.last_name "
                     "FROM donor o JOIN v_donor_eligibility e USING (user_id) JOIN user_account v ON v.user_id = o.user_id WHERE o.user_id = %s", (donor_id,))
        if not d:
            raise InvalidInput("donor: That donor was not found")
        donations = rows(con, "SELECT dn.donation_id, dn.donated_at, dn.donation_type_code, dn.volume_ml, dn.hemoglobin_gl, f.name AS facility, "
                              "(SELECT count(*) FROM blood_unit u WHERE u.donation_id = dn.donation_id) AS units, s.facility_id = %s AS mine "
                              "FROM donation dn JOIN staff s ON s.user_id = dn.recorded_by JOIN facility f ON f.facility_id = s.facility_id "
                              "WHERE dn.donor_id = %s ORDER BY dn.donated_at DESC LIMIT 12", (fac, donor_id))
        deferrals = rows(con, "SELECT f.deferral_id, r.description, r.is_permanent, f.start_date, f.end_date FROM deferral f JOIN deferral_reason r USING (reason_code) "
                              "WHERE f.donor_id = %s ORDER BY f.start_date DESC, f.deferral_id DESC LIMIT 8", (donor_id,))
        last_screen = con.execute("SELECT max(screening_id) FROM screening WHERE donor_id = %s", (donor_id,)).fetchone()[0]
        reasons = rows(con, "SELECT reason_code, description, is_permanent, default_days, is_active FROM deferral_reason "
                            "ORDER BY is_permanent, description")
        types = rows(con, "SELECT donation_type_code, name FROM donation_type ORDER BY 1")
    d["age"] = svc._years_between(d["date_of_birth"], svc.clinic_today())
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
        done = {}
        if pending:
            for r in con.execute("SELECT donation_id, test_code, result FROM test_result WHERE donation_id = ANY(%s)",
                                 ([p["donation_id"] for p in pending],)).fetchall():
                done.setdefault(r[0], {})[r[1]] = r[2]
    for p in pending:                                                      # C8: what is recorded shows read-only, the rest is offered
        p["recorded"] = done.get(p["donation_id"], {})
    return {"pending": pending, "tests": tests, "recent": donations}


def components_by_type():
    """REV-11: the components a donation type can give (svc.components_for_donation_type)."""
    out = {}
    for t in TYPE_NAMES:
        try:
            out[t] = list(svc.components_for_donation_type(t))
        except (InvalidInput, AttributeError):
            pass
    return out


def donation_units(staff_id, donation_id):
    with session(staff_id) as con:
        fac = _role(con, staff_id, "Staff")
        return rows(con, "SELECT u.unit_id, u.component_code, u.status FROM blood_unit u JOIN donation dn USING (donation_id) "
                         "JOIN staff s ON s.user_id = dn.recorded_by WHERE dn.donation_id = %s AND s.facility_id = %s ORDER BY u.unit_id", (donation_id, fac))


# ---------------------------------------------------------------- admin
WASTAGE_MONTHS = 8


def admin_dashboard(admin_id):
    stats = svc.admin_overview(admin_id)
    with session(admin_id) as con:
        # RSP-08 / L8: the last months in order, a month with no wastage counted as 0 rather than left out
        wastage = rows(con, "SELECT to_char(m.month, 'Mon YY') AS label, m.month, coalesce(sum(w.units_wasted), 0)::int AS wasted "
                            "FROM generate_series(date_trunc('month', now() AT TIME ZONE %s) - make_interval(months => %s - 1), "
                            "date_trunc('month', now() AT TIME ZONE %s), interval '1 month') AS m(month) "
                            "LEFT JOIN v_wastage w ON (w.month AT TIME ZONE %s) = m.month GROUP BY m.month ORDER BY m.month",
                       (svc.CLINIC_TZ.key, WASTAGE_MONTHS, svc.CLINIC_TZ.key, svc.CLINIC_TZ.key))   # v_wastage months are clinic months
        alerts = rows(con, "SELECT f.name AS facility, a.blood_group, a.component_code, a.opened_at FROM v_low_stock a "     # C10: live
                           "JOIN facility f USING (facility_id) ORDER BY a.opened_at, f.name")   # ADM-11: all of them
        users = rows(con, "SELECT role, count(*) FILTER (WHERE is_active)::int AS active, count(*)::int AS total FROM user_account GROUP BY role ORDER BY role")
        requests = rows(con, "SELECT r.request_id, r.patient_ref, r.recipient_group, r.component_code, r.units_requested, r.urgency, r.required_by, f.name AS facility "
                             "FROM v_request r JOIN facility f USING (facility_id) WHERE r.status = 'Open' "
                             f"ORDER BY {URGENCY_RANK.replace('urgency', 'r.urgency')}, r.required_by LIMIT 6")
    return {"stats": stats, "wastage": wastage, "wastage_max": max([w["wasted"] for w in wastage] + [0]), "alerts": alerts, "users": users,
            "requests": requests, "audit": svc.audit_log(admin_id, limit=8)}


_USERS_FROM = ("FROM user_account u LEFT JOIN staff s USING (user_id) LEFT JOIN facility f USING (facility_id) LEFT JOIN donor d USING (user_id) "
               "WHERE (%(role)s::text IS NULL OR u.role = %(role)s) AND (%(q)s::text IS NULL "
               "OR (u.first_name || ' ' || u.last_name || ' ' || coalesce(d.donor_code, '') || ' ' || coalesce(f.name, '')) ILIKE %(like)s "
               "OR u.user_id = ANY(%(ids)s))")


def _email_ids(con, q):
    """Accounts whose email contains q (C12, any case, 3 characters or more). The app login cannot read the email column,
    so this goes through match_email(), the same function as svc.accounts_matching_email."""
    if not q:
        return []
    return [r[0] for r in con.execute("SELECT user_id FROM match_email(%s)", (q,)).fetchall()]


def _user_args(con, role, q):
    q = (q or "").strip() or None
    ids = _email_ids(con, q)
    if q and q.isdigit() and int(q) < 2**31:                               # ADM-08: the number the audit log shows
        ids.append(int(q))
    return {"role": role or None, "q": q, "like": _like(q or ""), "ids": ids}


def users_list(admin_id, role=None, q=None):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return rows(con, "SELECT u.user_id, u.first_name, u.last_name, u.role, u.is_active, u.created_at, f.name AS facility, s.job_title, d.donor_code "
                         + _USERS_FROM + " ORDER BY u.role, u.last_name, u.first_name LIMIT 200", _user_args(con, role, q))


def users_total(admin_id, role=None, q=None):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return con.execute("SELECT count(*) " + _USERS_FROM, _user_args(con, role, q)).fetchone()[0]


def account(admin_id, user_id):
    """One account for the Admin edit page (C12): names, role and, through account_contact(), email and phone."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        a = one(con, "SELECT u.user_id, u.first_name, u.last_name, u.role, u.is_active, d.donor_code, f.name AS facility FROM user_account u "
                     "LEFT JOIN donor d USING (user_id) LEFT JOIN staff s USING (user_id) LEFT JOIN facility f USING (facility_id) WHERE u.user_id = %s",
                (user_id,))
        if a is None:
            return None
        a.update(one(con, "SELECT email, phone FROM account_contact(%s)", (user_id,)) or {"email": None, "phone": None})
        return a


def resolve_user(admin_id, text):
    """The audit log's user box (C2): a user number, an email address or part of a name -> the matching user ids."""
    text = (text or "").strip()
    if not text:
        return []
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        if text.isdigit():
            r = con.execute("SELECT user_id FROM user_account WHERE user_id = %s", (int(text),)).fetchone() if int(text) < 2**31 else None
            return [r[0]] if r else []
        if "@" in text:
            exact = con.execute("SELECT user_id FROM login_lookup(%s)", (text.lower(),)).fetchone()
            return [exact[0]] if exact else _email_ids(con, text)
        return [r[0] for r in con.execute("SELECT user_id FROM user_account WHERE (first_name || ' ' || last_name) ILIKE %s ORDER BY user_id LIMIT 20",
                                           (_like(text),)).fetchall()]


def facilities_admin(admin_id):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        return rows(con, "SELECT f.facility_id, f.name, f.facility_type, f.address, f.phone, f.email, f.is_active, "
                         "(SELECT count(*) FROM staff s WHERE s.facility_id = f.facility_id) AS staff, "
                         "(SELECT count(*) FROM v_request r WHERE r.facility_id = f.facility_id AND r.status = 'Open') AS open_requests, "
                         "(SELECT count(*) FROM v_blood_unit u WHERE u.facility_id = f.facility_id AND u.status = 'Available' AND u.expires_at > now()) AS units "
                         "FROM facility f ORDER BY f.is_active DESC, f.name")


def rules(admin_id):
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        compat = {(r[0], r[1], r[2]) for r in con.execute("SELECT component_code, recipient_group, donor_group FROM compatibility").fetchall()}
        return {"compat": compat,
                "components": rows(con, "SELECT component_code, name, shelf_life_days FROM component_type ORDER BY component_code"),
                "intervals": rows(con, "SELECT i.donation_type_code, t.name, i.sex, i.interval_days FROM donation_interval i "
                                       "JOIN donation_type t USING (donation_type_code) ORDER BY 1, 3"),
                "reasons": rows(con, "SELECT reason_code, description, is_permanent, default_days, is_active FROM deferral_reason "
                                     "ORDER BY is_permanent, reason_code"),
                "elig": rows(con, "SELECT sex, min_age_years, min_weight_kg, min_hemoglobin_gl, min_blood_volume_ml FROM eligibility_rule ORDER BY sex")}


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
        return rows(con, "SELECT u.unit_id, u.component_code, u.blood_group, f.name AS facility, u.status_changed_at, o.donor_code FROM v_blood_unit u "
                         "JOIN facility f USING (facility_id) JOIN donor o ON o.user_id = u.donor_id WHERE u.status = 'Quarantined' "
                         "ORDER BY u.status_changed_at DESC, u.unit_id")


def donor_by_code(admin_id, code):
    """A donor's user id from their code, any letter case (ADM-13)."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        r = con.execute("SELECT user_id FROM donor WHERE upper(donor_code) = upper(%s)", ((code or "").strip(),)).fetchone()
    return r[0] if r else None


def donor_code_for_unit(admin_id, unit_id):
    """ADM-05: a lookback from a unit id starts from the donor who gave it."""
    with session(admin_id) as con:
        _role(con, admin_id, "Admin")
        r = con.execute("SELECT o.donor_code FROM v_blood_unit u JOIN donor o ON o.user_id = u.donor_id WHERE upper(u.unit_id) = upper(%s)",
                        ((unit_id or "").strip(),)).fetchone()
    return r[0] if r else None
