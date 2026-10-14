"""End-to-end flows through the backend, connected as the least-privilege application role (SE-04)."""
import datetime as dt
import threading
from pathlib import Path

import psycopg
import pytest

from conftest import PREFIX, build, drop
from vytal import services as svc

PASSWORD = "correct-horse-42"
CLEAN = {"1": False, "2": False, "3": True, "4": False, "5": False, "6": False}
NEG = {"HIV": "Negative", "HBV": "Negative", "HCV": "Negative", "SYPH": "Negative"}
SOON = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)


@pytest.fixture(scope="module")
def world():
    """A fresh database; bootstrap rows are written by the owner, everything after goes through the backend."""
    name = build(PREFIX + "_integration")
    with psycopg.connect(dbname=name) as con:
        con.execute("SET search_path = vytal")
        fac = [con.execute("INSERT INTO facility (name, facility_type, address) VALUES (%s,'Hospital','1 St') RETURNING facility_id",
                           (n,)).fetchone()[0] for n in ("Windsor General", "London General")]
        admin = con.execute("INSERT INTO user_account (email,password_hash,first_name,last_name,role) "
                            "VALUES ('admin@v.ca','x','A','B','Admin') RETURNING user_id").fetchone()[0]
        con.execute("INSERT INTO admin VALUES (%s)", (admin,))
        staff = [con.execute("CALL create_staff(%s,%s,'x','S','T',%s,'Nurse')", (admin, f"s{i}@v.ca", f)).fetchone()[0]
                 for i, f in enumerate(fac)]
    return {"db": name, "admin": admin, "windsor": staff[0], "london": staff[1]}


@pytest.fixture(autouse=True)
def as_app(world, monkeypatch):
    """The backend acts as vytal_app; SET ROLE via options keeps this working under peer or password auth."""
    monkeypatch.setenv("PGDATABASE", world["db"])
    monkeypatch.setenv("PGOPTIONS", "-c role=vytal_app")


def owner(world):
    return psycopg.connect(dbname=world["db"], options="")


def new_donor(email, group="O+", sex="M"):
    return svc.register_donor(email, PASSWORD, "Ana", "Roy", "519-555-0100", dt.date(1990, 5, 1), sex, 72, "1 Main St", group)


def donate_and_release(staff, donor, group, components=("RBC", "PLASMA")):
    svc.verify_blood_group(staff, donor, group)
    svc.screen_donor(staff, donor, CLEAN)
    dn, units = svc.record_donation(staff, donor, "WB", 450, 145, list(components))
    assert svc.record_tests(staff, dn, NEG) == "Released"
    return dn, units


def test_donor_journey_from_registration_to_transfusion(world):
    s = world["windsor"]
    donor = new_donor("journey@v.ca")
    assert svc.login("JOURNEY@v.ca", PASSWORD) == (donor, "Donor")
    assert svc.donor_home(donor)["status"] == "Complete screening to confirm eligibility"    # UC-05 2a
    assert svc.complete_screening(donor, CLEAN) == "Eligible"
    _, units = donate_and_release(s, donor, "O+")
    assert any(g == "O+" and c == "RBC" and avail >= 1 for g, c, avail, *_ in svc.inventory(s))
    req = svc.submit_request(s, "P-100", "O+", "RBC", 1, "Routine", SOON)
    assert (req["reserved"], req["shortfall"]) == (1, 0)
    assert svc.close_request(s, req["request_id"]) == "Fulfilled"
    home = svc.donor_home(donor)
    assert home["status"] == f"Not eligible until {dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=56)}"
    assert [(f, t, n) for _, f, t, n in home["history"]] == [("Windsor General", "WB", len(units))]


def test_low_hemoglobin_reading_defers_the_donor(world):
    w = world["windsor"]
    donor = new_donor("anemic@v.ca", "A+", sex="F")
    svc.verify_blood_group(w, donor, "A+")
    svc.screen_donor(w, donor, CLEAN)
    assert svc.record_donation(w, donor, "WB", 450, 118, ["RBC"]) == (None, [])
    assert svc.donor_home(donor)["status"].startswith("Not eligible until")


def test_lockout_after_five_failed_logins(world):
    new_donor("lock@v.ca")
    for _ in range(4):
        with pytest.raises(svc.AccessDenied, match="wrong"):
            svc.login("lock@v.ca", "nope")
    with pytest.raises(svc.AccessDenied, match="locked"):
        svc.login("lock@v.ca", "nope")
    with pytest.raises(svc.AccessDenied, match="locked"):
        svc.login("lock@v.ca", PASSWORD)


def test_staff_see_only_their_facility_and_roles_are_enforced(world):
    w, l = world["windsor"], world["london"]
    donor = new_donor("scope@v.ca", "A+")
    donate_and_release(w, donor, "A+")
    req = svc.submit_request(w, "P-200", "A+", "RBC", 1, "Urgent", SOON)
    for call in (lambda: svc.close_request(l, req["request_id"]), lambda: svc.find_donors(l, req["request_id"])):
        with pytest.raises(svc.AccessDenied, match="another facility"):
            call()
    assert all(g != "A+" or c != "RBC" for g, c, *_ in svc.inventory(l))
    with pytest.raises(svc.AccessDenied):
        svc.inventory(donor)
    other = new_donor("scope2@v.ca", "A+")
    svc.verify_blood_group(w, other, "A+")
    svc.screen_donor(w, other, CLEAN)
    dn, _ = svc.record_donation(w, other, "WB", 450, 145, ["RBC"])
    with pytest.raises(svc.AccessDenied, match="another facility"):
        svc.record_tests(l, dn, {"HIV": "Positive"})
    with pytest.raises(svc.AccessDenied):
        svc.admin_overview(w)
    assert svc.admin_overview(world["admin"])["open_urgent"] >= 1


def test_emergency_shortfall_triggers_a_donation_call(world):
    w = world["windsor"]
    called = new_donor("oneg@v.ca", "O-", sex="F")
    svc.verify_blood_group(w, called, "O-")
    svc.complete_screening(called, CLEAN)                                              # UC-05 2a: screened donors only
    req = svc.submit_request(w, "P-300", None, "RBC", 2, "Emergency", SOON)
    assert req["shortfall"] == 2
    codes = [c for c, *_ in svc.find_donors(w, req["request_id"])]
    assert svc.donor_home(called)["donor_code"] in codes
    call = svc.call_donors(w, req["request_id"], "O-negative donors needed today", codes)
    assert svc.respond_to_call(called, call, True) == "Windsor General, 1 St"            # UC-20: facility details on accept
    assert (call, "O-negative donors needed today", "Accepted") in svc.donor_home(called)["calls"]


def test_confirming_an_unknown_group_reserves_compatible_stock(world):
    w, l = world["windsor"], world["london"]
    donate_and_release(w, new_donor("ap@v.ca", "A+"), "A+", ("RBC",))
    req = svc.submit_request(w, "P-310", None, "RBC", 1, "Emergency", SOON)
    assert req["shortfall"] == 1
    with pytest.raises(svc.AccessDenied, match="another facility"):
        svc.confirm_request_group(l, req["request_id"], "A+")
    with pytest.raises(svc.InvalidInput):
        svc.confirm_request_group(w, req["request_id"], "Z+")
    assert svc.confirm_request_group(w, req["request_id"], "A+") == {"request_id": req["request_id"], "reserved": 1, "shortfall": 0}
    with pytest.raises(svc.InvalidInput, match="already has a confirmed group"):
        svc.confirm_request_group(w, req["request_id"], "B+")


def test_positive_test_then_lookback(world):
    w = world["windsor"]
    donor = new_donor("lookback@v.ca", "B+")
    _, earlier = donate_and_release(w, donor, "B+")
    with owner(world) as con:                                                 # the first donation was 60 days ago
        con.execute("SET search_path = vytal")
        con.execute("UPDATE donation SET donated_at = donated_at - interval '60 days' WHERE donor_id = %s", (donor,))
        con.execute("SELECT recompute_next_eligible(%s)", (donor,))
    dn, _ = svc.record_donation(w, donor, "WB", 450, 145, ["RBC"])
    assert svc.record_tests(w, dn, {"HBV": "Positive"}) == "Discarded"
    with pytest.raises(svc.AccessDenied):
        svc.quarantine_donor(w, donor)                                        # UC-22 is Admin-only
    trace = svc.quarantine_donor(world["admin"], donor)
    status = {u: s for _, _, u, _, s, *_ in trace}
    assert {status[u] for u in earlier} == {"Quarantined"} and status[f"U{dn}-RBC"] == "Discarded"


def test_bad_input_is_refused_with_a_field_message(world):
    for kwargs, field in ((dict(email="not-an-email"), "email"), (dict(sex="X"), "sex"),
                          (dict(blood_group="C+"), "blood_group"), (dict(weight_kg=5), "weight_kg")):
        args = dict(email="ok@v.ca", password=PASSWORD, first="A", last="B", phone=None, dob=dt.date(1990, 1, 1),
                    sex="F", weight_kg=60, address=None, blood_group=None) | kwargs
        with pytest.raises(svc.InvalidInput, match=field):
            svc.register_donor(**args)


def test_sql_injection_is_inert(world):
    """SE-03: input travels as parameters, never as SQL text."""
    evil = "x');DROP/**/TABLE/**/vytal.donor;--@v.ca"
    uid = new_donor(evil)
    with pytest.raises(svc.AccessDenied):
        svc.login("' OR '1'='1", "' OR '1'='1")
    with owner(world) as con:
        assert con.execute("SELECT email FROM vytal.user_account WHERE user_id=%s", (uid,)).fetchone()[0] == evil.lower()


def test_a_reserved_red_cell_does_not_hide_its_plasma(world):
    """DI-01: allocate() locks unit rows only, so a session holding a donation's RBC unit cannot block its plasma."""
    w = world["windsor"]
    donate_and_release(w, new_donor("split@v.ca", "B-"), "B-", components=("RBC", "PLASMA"))
    with owner(world) as holder:                     # stays open, holding the RBC unit's row lock
        holder.execute("SET search_path = vytal")
        rid = holder.execute("INSERT INTO blood_request (requested_by,patient_ref,recipient_group,component_code,units_requested,"
                             "urgency,required_by) VALUES (%s,'H-1','B-','RBC',1,'Urgent',now()+interval '1 hour') RETURNING request_id",
                             (w,)).fetchone()[0]
        assert holder.execute("CALL allocate(%s)", (rid,)).fetchone()[0] == 1
        assert svc.submit_request(w, "H-2", "B-", "PLASMA", 1, "Urgent", SOON)["reserved"] == 1
        holder.rollback()


def test_fifty_concurrent_requests_never_share_a_unit(world):
    """PR-06 / DI-01: 50 sessions compete for the O- shelf; every unit is reserved once and nothing errors."""
    w = world["windsor"]
    for i in range(30):
        donate_and_release(w, new_donor(f"pool{i}@v.ca", "O-"), "O-", components=("RBC",))
    with owner(world) as con:                    # O- patients can only take O- red cells
        stock = con.execute("SELECT count(*) FROM vytal.v_blood_unit WHERE blood_group='O-' AND component_code='RBC' "
                            "AND status='Available'").fetchone()[0]
    assert stock >= 30
    barrier, results, errors = threading.Barrier(50), [], []

    def worker(i):
        try:
            barrier.wait()
            results.append(svc.submit_request(w, f"C-{i}", "O-", "RBC", 1, "Urgent", SOON)["reserved"])
        except Exception as e:              # collected so the assertion below can show it
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert sum(results) == min(stock, 50)
    with owner(world) as con:
        assert con.execute("SELECT count(*) - count(DISTINCT unit_id) FROM vytal.request_unit WHERE released_at IS NULL").fetchone()[0] == 0


def test_demo_script_replays_scenario_1(monkeypatch):
    """demo.py builds its own database and must finish with Scenario 1's outcome."""
    import os
    import subprocess
    import sys
    monkeypatch.delenv("PGOPTIONS")
    monkeypatch.delenv("PGDATABASE")
    env = dict(os.environ)
    done = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "demo.py")], env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    for line in ("units reserved: 3", "shortfall: 1", "eligible donors found: 14", "donors called: 14",
                 "units reserved: 4", "reserved groups: A+,O-,O-,O-", "donors who accepted: 2", "request status: Fulfilled",
                 "Scenario 1 replayed with every number as specified"):
        assert line in done.stdout, line
    assert "request status: Fulfilled" in done.stdout
    drop("vytal_demo")


def test_donor_completes_their_own_screening(world):
    """UC-04: the donor is the actor; staff use screen_donor to assist, not complete_screening."""
    w = world["windsor"]
    donor = new_donor("selfscreen@v.ca")
    assert svc.complete_screening(donor, CLEAN) == "Eligible"
    with pytest.raises(svc.AccessDenied):
        svc.complete_screening(w, CLEAN)
    svc.record_deferral(w, donor, "HIV")
    with pytest.raises(psycopg.Error, match="permanently deferred"):
        svc.complete_screening(donor, CLEAN)


def test_donor_updates_profile_with_field_validation(world):
    donor = new_donor("profile@v.ca")
    svc.update_profile(donor, "519-555-0104", "Profile2@v.ca", "3 Main St", 80)
    assert svc.login("profile2@v.ca", PASSWORD) == (donor, "Donor")
    with pytest.raises(svc.InvalidInput, match="weight_kg"):
        svc.update_profile(donor, None, "profile2@v.ca", None, 5)
    with pytest.raises(svc.InvalidInput, match="email"):
        svc.update_profile(donor, None, "nope", None, 80)
    with pytest.raises(psycopg.Error, match="Donor access only"):
        svc.update_profile(world["windsor"], None, "x@v.ca", None, 80)


def test_admin_lookback_by_donor_code_or_unit(world):
    w, a = world["windsor"], world["admin"]
    donor = new_donor("trace@v.ca", "A-")
    _, units = donate_and_release(w, donor, "A-")
    code = svc.donor_home(donor)["donor_code"]
    assert sorted(row[2] for row in svc.lookback(a, donor_code=code)) == sorted(units)
    with pytest.raises(svc.InvalidInput):
        svc.lookback(a)
    with pytest.raises(svc.AccessDenied):
        svc.lookback(w, donor_code=code)


def test_staff_defer_discard_and_set_thresholds(world):
    w, l = world["windsor"], world["london"]
    donor = new_donor("discard@v.ca", "AB+")
    _, units = donate_and_release(w, donor, "AB+", ("RBC",))
    with pytest.raises(svc.InvalidInput, match="reason"):
        svc.discard_unit(w, units[0], " ")
    svc.discard_unit(w, units[0], "Damaged bag")
    with owner(world) as con:
        assert con.execute("SELECT status FROM vytal.blood_unit WHERE unit_id=%s", (units[0],)).fetchone()[0] == "Discarded"
    with owner(world) as con:
        fac = con.execute("SELECT facility_id FROM vytal.staff WHERE user_id=%s", (w,)).fetchone()[0]
    with pytest.raises(svc.InvalidInput, match="min_units"):
        svc.set_threshold(w, fac, "AB+", "RBC", -1)
    svc.set_threshold(w, fac, "AB+", "RBC", 2)
    with pytest.raises(psycopg.Error, match="own facility"):
        svc.set_threshold(l, fac, "AB+", "RBC", 2)
    assert svc.record_deferral(w, donor, "TRAVEL") > 0


def test_admin_manages_accounts_and_facilities(world):
    a = world["admin"]
    with pytest.raises(svc.InvalidInput, match="password"):
        svc.create_admin(a, "admin2@v.ca", "short", "B", "C")
    second = svc.create_admin(a, "admin2@v.ca", PASSWORD, "B", "C")
    assert svc.login("admin2@v.ca", PASSWORD) == (second, "Admin")
    fac = svc.save_facility(second, None, "Chatham Blood Clinic", "Blood Bank", "5 King St")
    assert svc.save_facility(second, fac, "Chatham Blood Clinic", "Blood Bank", "6 King St", "519-555-0105") == fac
    with pytest.raises(svc.InvalidInput, match="facility_type"):
        svc.save_facility(second, None, "Bad", "Clinic", "1 St")
    svc.set_facility_active(second, fac, False)
    donor = new_donor("deactivate@v.ca")
    svc.set_account_active(second, donor, False)
    with pytest.raises(svc.AccessDenied, match="deactivated"):
        svc.login("deactivate@v.ca", PASSWORD)
    svc.set_account_active(second, donor, True)
    assert svc.login("deactivate@v.ca", PASSWORD) == (donor, "Donor")
    with pytest.raises(psycopg.Error, match="Admin access only"):
        svc.set_account_active(world["windsor"], donor, False)


def test_nightly_job_runs_through_the_backend(world):
    w = world["windsor"]
    _, units = donate_and_release(w, new_donor("expire@v.ca", "B-"), "B-", ("RBC", "PLASMA"))
    with owner(world) as con:
        con.execute("SET search_path = vytal")
        con.execute("UPDATE vytal.blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id = ANY(%s)", (units,))
        assert con.execute("SELECT count(*) FROM vytal.blood_unit WHERE expires_at <= now() "
                           "AND status IN ('Collected','Available','Reserved')").fetchone()[0] == 2
    assert svc.nightly_expiry() == 2
    with owner(world) as con:
        assert con.execute("SELECT DISTINCT status FROM vytal.blood_unit WHERE unit_id = ANY(%s)", (units,)).fetchall() == \
            [("Expired",)]


def test_lookback_by_unit_id_lists_everything_the_donor_gave(world):
    """UC-22 step 2 / FR-30: one unit id resolves to its donor; each row shows where the unit is held now."""
    w, a = world["windsor"], world["admin"]
    donor = new_donor("trace2@v.ca", "A-")
    _, units = donate_and_release(w, donor, "A-")
    rows = svc.lookback(a, unit_id=units[0])
    assert sorted(r[2] for r in rows) == sorted(units) and len(units) == 2
    with owner(world) as con:
        fac = con.execute("SELECT facility_id FROM vytal.staff WHERE user_id=%s", (w,)).fetchone()[0]
    assert {r[5] for r in rows} == {fac}                                       # held_at
    with pytest.raises(svc.InvalidInput, match="no donor or unit"):
        svc.lookback(a, unit_id="U0-RBC")


def test_admin_creates_staff_through_the_backend(world):
    a = world["admin"]
    with owner(world) as con:
        fac = con.execute("SELECT facility_id FROM vytal.staff WHERE user_id=%s", (world["windsor"],)).fetchone()[0]
    with pytest.raises(svc.InvalidInput, match="password"):
        svc.create_staff(a, "nurse2@v.ca", "short", "N", "T", fac, "Nurse")
    s = svc.create_staff(a, "Nurse2@v.ca", PASSWORD, "N", "T", fac, "Nurse")
    assert svc.login("nurse2@v.ca", PASSWORD) == (s, "Staff")
    assert isinstance(svc.inventory(s), list)                                 # works as Staff at that facility
    with pytest.raises(psycopg.Error, match="Admin access only"):
        svc.create_staff(world["windsor"], "nurse3@v.ca", PASSWORD, "N", "T", fac, "Nurse")


def test_quarantine_waits_for_a_concurrent_allocation(world):
    """DI-01 / FR-31: an allocate() holding the unit commits first; quarantine must then release that reservation."""
    w = world["windsor"]
    donor = new_donor("race@v.ca", "AB-")
    _, units = donate_and_release(w, donor, "AB-", ("RBC",))
    errors = []
    with owner(world) as a, owner(world) as b, owner(world) as watch:
        for c in (a, b):
            c.execute("SET search_path = vytal")
        rid = a.execute("INSERT INTO blood_request (requested_by,patient_ref,recipient_group,component_code,units_requested,"
                        "urgency,required_by) VALUES (%s,'R-1','AB-','RBC',1,'Routine',now()+interval '1 day') RETURNING request_id",
                        (w,)).fetchone()[0]
        assert a.execute("SELECT unit_id FROM request_unit WHERE request_id=%s", (rid,)).fetchone() is None
        a.execute("CALL allocate(%s)", (rid,))
        assert a.execute("SELECT unit_id FROM request_unit WHERE request_id=%s", (rid,)).fetchone() == (units[0],)

        def quarantine():
            try:
                b.execute("CALL quarantine_donor(%s)", (donor,))
                b.commit()
            except Exception as e:          # surfaced by the assertion below
                errors.append(e)

        t = threading.Thread(target=quarantine)
        t.start()
        for _ in range(200):                # wait until the quarantine is blocked on the allocation's row lock
            if watch.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (b.info.backend_pid,)).fetchone() == ("Lock",):
                break
            watch.rollback()
            threading.Event().wait(0.05)
        else:
            pytest.fail("quarantine never waited on the allocation")
        a.commit()
        t.join(10)
    assert errors == []
    with owner(world) as con:
        con.execute("SET search_path = vytal")
        assert con.execute("SELECT status FROM blood_unit WHERE unit_id=%s", (units[0],)).fetchone() == ("Quarantined",)
        assert con.execute("SELECT count(*) FROM request_unit ru JOIN blood_unit u USING (unit_id) WHERE u.status = 'Quarantined' "
                           "AND ru.released_at IS NULL AND ru.issued_at IS NULL").fetchone()[0] == 0
        assert con.execute("CALL close_request(%s, TRUE)", (rid,)).fetchone()[0] == "Cancelled"
