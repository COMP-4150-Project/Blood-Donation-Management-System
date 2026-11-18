"""Round 3 regressions: re-reserving a unit a request held before, the shelf lock order, the report role's privacy,
stock that expires between job runs, call dates within the request's time."""
import datetime as dt
import threading
import time

import psycopg
import pytest

from conftest import DB, PREFIX, build

NEG = '{"HIV":"Negative","HBV":"Negative","HCV":"Negative","SYPH":"Negative"}'


def as_user(db, uid):
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(uid),))


def reservations(db, r):
    return db.q("SELECT unit_id, released_at IS NULL, issued_at IS NULL FROM request_unit WHERE request_id=%s ORDER BY unit_id", (r,))


# ---------------------------------------------------------------------- NEW-01 a top-up re-picks a unit the request held before
def test_clearing_a_lookback_gives_the_request_its_unit_back(db):
    unit = db.stock("A+")[0]
    donor = db.one("SELECT donor_id FROM v_blood_unit WHERE unit_id=%s", (unit,))
    r = db.request("A+")
    as_user(db, db.nurse)
    assert db.one("CALL allocate(%s)", (r,)) == 1
    as_user(db, db.admin)
    db.q("CALL quarantine_donor(%s)", (donor,))
    assert reservations(db, r) == [(unit, False, True)]                       # released, the request is short
    assert db.one("CALL clear_lookback(%s,%s,'No risk found')", (db.admin, donor)) == 1
    assert reservations(db, r) == [(unit, True, True)]                        # the same row, active again
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Reserved"
    assert db.one("SELECT is_flagged FROM donor WHERE user_id=%s", (donor,)) is False
    # the audit log keeps the release and the new reservation of the one row
    assert db.q("SELECT field_name, new_value IS NULL FROM audit_log WHERE table_name='request_unit' AND record_id=%s "
                "AND field_name='released_at' ORDER BY audit_id", (f"{r}/{unit}",)) == [("released_at", False), ("released_at", True)]
    as_user(db, db.nurse)
    assert db.one("CALL close_request(%s)", (r,)) == "Fulfilled"
    assert reservations(db, r) == [(unit, True, False)]
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Issued"


def test_removing_and_readding_a_pairing_gives_the_unit_back(db):
    unit = db.stock("O+")[0]
    r = db.request("A+")
    as_user(db, db.nurse)
    assert db.one("CALL allocate(%s)", (r,)) == 1
    as_user(db, db.admin)
    db.q("CALL set_compatibility(%s,'RBC','A+','O+',FALSE)", (db.admin,))
    assert reservations(db, r) == [(unit, False, True)]
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Available"
    db.q("CALL set_compatibility(%s,'RBC','A+','O+',TRUE)", (db.admin,))
    assert reservations(db, r) == [(unit, True, True)]
    assert db.one("SELECT units_reserved FROM v_request_status WHERE request_id=%s", (r,)) == 1
    as_user(db, db.nurse)
    assert db.one("CALL close_request(%s)", (r,)) == "Fulfilled"


def test_a_reissued_reservation_can_be_cancelled_and_counts_once(db):
    unit = db.stock("B+")[0]
    donor = db.one("SELECT donor_id FROM v_blood_unit WHERE unit_id=%s", (unit,))
    r = db.request("B+", n=2)
    as_user(db, db.nurse)
    db.q("CALL allocate(%s)", (r,))
    as_user(db, db.admin)
    for _ in range(2):                                                          # held, released and re-held twice
        db.q("CALL quarantine_donor(%s)", (donor,))
        db.q("CALL clear_lookback(%s,%s,NULL)", (db.admin, donor))
    assert db.q("SELECT units_reserved, shortfall FROM v_request_status WHERE request_id=%s", (r,)) == [(1, 1)]
    as_user(db, db.nurse)
    assert db.one("CALL close_request(%s, TRUE)", (r,)) == "Cancelled"
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (unit,)) == "Available"


# ---------------------------------------------------------------------- INT-11 / C10 stock that expires between job runs
def test_a_unit_past_its_time_is_not_stock_anywhere_before_the_job(db):
    db.q("INSERT INTO stock_threshold VALUES (%s,'B-','PLT',2) ON CONFLICT DO NOTHING", (db.fac_h,))
    db.q("UPDATE stock_threshold SET min_units=2 WHERE facility_id=%s AND blood_group='B-' AND component_code='PLT'", (db.fac_h,))
    units = db.stock("B-", comps=("PLT",), n=2)
    assert db.one("SELECT count(*) FROM v_low_stock WHERE facility_id=%s AND blood_group='B-' AND component_code='PLT'", (db.fac_h,)) == 0
    # time passes: the unit's expiry time is now behind us, but nothing has touched the row (no trigger has run)
    db.q("ALTER TABLE blood_unit DISABLE TRIGGER USER")
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (units[0],))
    db.q("ALTER TABLE blood_unit ENABLE TRIGGER USER")
    assert db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group='B-' AND component_code='PLT' "
                  "AND cleared_at IS NULL", (db.fac_h,)) == 0
    assert db.one("SELECT available FROM v_inventory WHERE facility_id=%s AND blood_group='B-' AND component_code='PLT'", (db.fac_h,)) == 1
    assert db.q("SELECT available, min_units FROM v_low_stock WHERE facility_id=%s AND blood_group='B-' AND component_code='PLT'",
                (db.fac_h,)) == [(1, 2)]
    from vytal.services import QUERIES
    db.q(QUERIES["Q06"].sql)
    assert db.one("SELECT count(*) FROM v_low_stock") >= 1
    as_user(db, db.admin)
    db.q("CALL run_nightly_expiry()")                                           # the hourly job then opens the stored alert
    assert db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group='B-' AND component_code='PLT' "
                  "AND cleared_at IS NULL", (db.fac_h,)) == 1


# ---------------------------------------------------------------------- NEW-05 the report role cannot tie a person to a positive test
def test_report_role_cannot_link_names_to_positive_tests(db):
    dn = db.donate(db.donor("AB-"))
    db.q("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))
    db.q("CALL record_tests(%s, '{\"HIV\":\"Positive\",\"HBV\":\"Negative\",\"HCV\":\"Negative\",\"SYPH\":\"Negative\"}')", (dn,))
    assert db.one("SELECT count(*) FROM notification WHERE message LIKE '%%failed test%%'") >= 1
    db.q("SET LOCAL ROLE vytal_report")
    for sql in ("SELECT message FROM notification", "SELECT count(*) FROM notification",
                "SELECT first_name FROM user_account", "SELECT last_name FROM user_account"):
        assert "permission denied" in db.fails(sql)
    # what reports need still works
    assert db.one("SELECT count(*) FROM user_account WHERE role='Donor'") >= 1
    assert db.one("SELECT count(*) FROM v_donor_eligibility") >= 1
    assert db.one("SELECT count(*) FROM v_inventory") >= 0
    assert db.one("SELECT count(*) FROM v_wastage") >= 1
    assert db.one("SELECT count(*) FROM v_low_stock") >= 0
    db.q("RESET ROLE")


# ---------------------------------------------------------------------- NEW-03 the expiry job and a quarantine at the same moment
@pytest.fixture(scope="module")
def lockdb():
    name = build(PREFIX + "_round3")
    with psycopg.connect(dbname=name) as con:
        con.execute("SET search_path = vytal")
        w = DB(con).setup_world()
        con.commit()
    return {"db": name, "admin": w.admin, "nurse": w.nurse, "fac": w.fac_h}


def _waiting(pid, con):
    return con.execute("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone()[0]


def _wait_for_lock(pid, con, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if _waiting(pid, con):
            return True
        time.sleep(0.05)
    return False


def test_expiry_job_and_quarantine_wait_instead_of_deadlocking(lockdb):
    """X has a reserved unit and an expired Available unit, Y an expired reserved unit, all on one shelf. The quarantine of X
    holds X's unit rows while it waits; the job then expires Y's reservation and wants X's expired unit. Before the shelf
    lock order the job held the shelf lock while waiting for that row, and the quarantine then waited for the shelf: deadlock."""
    w = DB(psycopg.connect(dbname=lockdb["db"]))
    w.cur.execute("SET search_path = vytal")
    w.admin, w.nurse, w.fac_h, w.seq = lockdb["admin"], lockdb["nurse"], lockdb["fac"], 500
    w.q("SELECT set_config('vytal.user_id', %s, false)", (str(w.nurse),))
    w.q("INSERT INTO stock_threshold VALUES (%s,'A-','PLASMA',1), (%s,'A-','RBC',1)", (w.fac_h, w.fac_h))   # alerts lock the shelf
    x = w.donor("A-")
    dn = w.donate(x)
    ids = w.one("CALL separate_donation(%s, ARRAY['RBC','PLASMA'])", (dn,))
    ux, ua = [w.one("SELECT unit_id FROM blood_unit WHERE unit_id = ANY(%s) AND component_code=%s", (ids, c)) for c in ("RBC", "PLASMA")]
    w.one("CALL record_tests(%s, %s)", (dn, NEG))
    uy = w.stock("A-", comps=("PLASMA",))[0]
    rx, ry = w.request("A-", comp="RBC"), w.request("A-", comp="PLASMA")
    w.q("CALL allocate(%s)", (rx,))                                          # rx holds X's RBC unit ux
    w.q("CALL allocate(%s)", (ry,))                                          # ry holds Y's plasma uy (FEFO: older)
    assert w.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (rx,)) == ux
    if w.one("SELECT unit_id FROM request_unit WHERE request_id=%s", (ry,)) != uy:    # make ry hold uy, ua stays on the shelf
        pytest.skip("allocation picked another unit")
    w.q("ALTER TABLE blood_unit DISABLE TRIGGER USER")                       # time passes: both plasma units are past their date
    w.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id IN (%s,%s)", (ua, uy))
    w.q("ALTER TABLE blood_unit ENABLE TRIGGER USER")
    w.con.commit()

    blocker = psycopg.connect(dbname=lockdb["db"])                            # holds X's reservation row for a moment
    blocker.execute("SELECT 1 FROM vytal.request_unit WHERE request_id=%s AND unit_id=%s FOR UPDATE", (rx, ux))
    watch = psycopg.connect(dbname=lockdb["db"], autocommit=True)
    out = {}

    def run(name, sql, uid):
        c = psycopg.connect(dbname=lockdb["db"])
        c.execute("SET search_path = vytal")
        c.execute("SELECT set_config('vytal.user_id', %s, false)", (str(uid),))
        out[name + "_pid"] = c.info.backend_pid
        try:
            c.execute(sql, (x,) if "%s" in sql else None)
            c.commit()
            out[name] = "ok"
        except psycopg.Error as e:
            out[name] = type(e).__name__
        finally:
            c.close()

    q = threading.Thread(target=run, args=("quarantine", "CALL quarantine_donor(%s)", lockdb["admin"]))
    q.start()
    while "quarantine_pid" not in out:
        time.sleep(0.02)
    assert _wait_for_lock(out["quarantine_pid"], watch)                     # the quarantine waits on the blocker
    j = threading.Thread(target=run, args=("job", "CALL run_nightly_expiry()", lockdb["admin"]))
    j.start()
    while "job_pid" not in out:
        time.sleep(0.02)
    assert _wait_for_lock(out["job_pid"], watch)                            # the job waits on the quarantine
    blocker.commit()
    blocker.close()
    q.join(30)
    j.join(30)
    watch.close()
    assert (out["quarantine"], out["job"]) == ("ok", "ok")
    statuses = dict(w.q("SELECT unit_id, status FROM blood_unit WHERE unit_id IN (%s,%s,%s)", (ux, ua, uy)))
    w.con.commit()
    assert statuses[ux] == "Quarantined" and statuses[uy] == "Expired" and statuses[ua] in ("Quarantined", "Expired")
    w.con.close()
