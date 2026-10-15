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


def within_the_hour():
    """BR-10: an Emergency request is needed within 1 hour."""
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=50)


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
    req = svc.submit_request(w, "P-300", None, "RBC", 2, "Emergency", within_the_hour())
    assert req["shortfall"] == 2
    codes = [c for c, *_ in svc.find_donors(w, req["request_id"])]
    assert svc.donor_home(called)["donor_code"] in codes
    call = svc.call_donors(w, req["request_id"], codes)
    assert svc.respond_to_call(called, call, True) == "Windsor General, 1 St"            # UC-20: facility details on accept
    msg = "Windsor General is asking for donors to give red blood cells. Please book a donation at 1 St. " \
          "Accept or decline this call in Vytal."                                          # SE-07 fixed template only
    assert (call, msg, "Accepted") in svc.donor_home(called)["calls"]


def test_confirming_an_unknown_group_reserves_compatible_stock(world):
    w, l = world["windsor"], world["london"]
    donate_and_release(w, new_donor("ap@v.ca", "A+"), "A+", ("RBC",))
    req = svc.submit_request(w, "P-310", None, "RBC", 1, "Emergency", within_the_hour())
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


# ---------------------------------------------------------------------- items added for UC-01 2b, UC-18, UC-21, UC-24, UC-25, FR-21, FR-27
def test_registration_refuses_a_donor_under_17(world):
    teen = dt.date.today().replace(year=dt.date.today().year - 15)
    with pytest.raises(svc.InvalidInput, match="date_of_birth: donors must be at least 17"):
        svc.register_donor("teen@v.ca", PASSWORD, "Tia", "Lam", None, teen, "F", 55, "1 St", "O+")


def test_admin_rule_edits_audit_filter_and_csv(world, tmp_path):
    admin, staff = world["admin"], world["windsor"]
    svc.save_deferral_reason(admin, "SURG", "Surgery in the last 6 months", False, 180)
    with pytest.raises(psycopg.Error, match="Admin access only"):
        svc.set_shelf_life(staff, "PLT", 7)
    with pytest.raises(svc.InvalidInput):
        svc.set_eligibility_rule(admin, "M", 12, 50, 130)
    rows = svc.audit_log(admin, table="deferral_reason", record_id="SURG")
    assert [(r[2], r[3]) for r in rows] == [(admin, "INSERT")] and "Surgery" in rows[0][8]
    assert svc.audit_log(admin, table="deferral_reason", since=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)) == []
    text = svc.audit_csv(admin, path=tmp_path / "audit.csv", table="deferral_reason", record_id="SURG")
    assert text.splitlines()[0].startswith("audit_id,changed_at,user_id,action") and len(text.splitlines()) == 2
    with open(tmp_path / "audit.csv", newline="") as f:
        assert f.read() == text
    with pytest.raises(svc.AccessDenied):
        svc.audit_log(staff)


def test_lookback_export_lists_only_issued_units(world):
    admin, staff = world["admin"], world["windsor"]
    donor = new_donor("issued@v.ca", "AB-")
    _, units = donate_and_release(staff, donor, "AB-", ("RBC", "PLASMA"))
    req = svc.submit_request(staff, "P-900", "AB-", "PLASMA", 9, "Routine", SOON)
    with owner(world) as con:
        reserved = [u for (u,) in con.execute("SELECT unit_id FROM vytal.request_unit WHERE request_id=%s AND released_at IS NULL",
                                              (req["request_id"],))]
    plasma = next(u for u in units if u.endswith("PLASMA"))
    assert plasma in reserved
    assert svc.close_request(staff, req["request_id"], issued_units=[plasma]) == "Partially Fulfilled"
    code = svc.donor_home(donor)["donor_code"]
    lines = svc.lookback_issued_csv(admin, code).splitlines()
    assert lines[0] == "unit_id,component_code,issued_at,request_facility,request_id"
    assert len(lines) == 2 and lines[1].startswith(plasma + ",PLASMA,")
    with pytest.raises(svc.AccessDenied):
        svc.lookback_issued_csv(staff, code)


def test_staff_search_by_group_see_call_responses_and_open_alerts(world):
    w, l = world["windsor"], world["london"]
    donor = new_donor("caller@v.ca", "O-")
    svc.verify_blood_group(w, donor, "O-")
    assert svc.complete_screening(donor, CLEAN) == "Eligible"
    code = svc.donor_home(donor)["donor_code"]
    assert code in {c for c, _, _ in svc.search_donors(w, "AB+", "RBC")}
    with pytest.raises(svc.InvalidInput):
        svc.search_donors(w, "C+")
    with pytest.raises(svc.AccessDenied):
        svc.search_donors(donor, "O-")
    rid = svc.submit_request(w, "P-910", None, "RBC", 40, "Emergency", within_the_hour())["request_id"]
    svc.call_donors(w, rid, [code])
    svc.respond_to_call(donor, svc.donor_home(donor)["calls"][0][0], True)
    assert [(c, r) for c, r, _ in svc.call_responses(w, rid)] == [(code, "Accepted")]
    with pytest.raises(svc.AccessDenied):
        svc.call_responses(l, rid)
    with owner(world) as con:
        fac = con.execute("SELECT facility_id FROM vytal.staff WHERE user_id=%s", (w,)).fetchone()[0]
    svc.set_threshold(w, fac, "AB+", "PLT", 5)
    assert ("AB+", "PLT", 0, 5) in {a[:4] for a in svc.open_alerts(w)}
    assert ("AB+", "PLT", 0, 5) not in {a[:4] for a in svc.open_alerts(l)}


def test_donor_reads_and_marks_notifications(world):
    donor = new_donor("reader@v.ca")
    with owner(world) as con:
        con.execute("INSERT INTO vytal.notification (user_id, message) VALUES (%s,'one'), (%s,'two')", (donor, donor))
    unread = svc.list_notifications(donor, unread_only=True)
    assert len(unread) == 2
    svc.mark_read(donor, unread[0][0])
    assert len(svc.list_notifications(donor, unread_only=True)) == 1 and len(svc.list_notifications(donor)) == 2
    with pytest.raises(psycopg.Error, match="not found"):
        svc.mark_read(world["windsor"], unread[1][0])


def test_password_change_and_admin_reset_through_the_backend(world):
    """UC-01 2a: the old password is proved first; an Admin reset clears a lockout; login uses only login_lookup (SE-07)."""
    d = new_donor("pw@v.ca")
    with pytest.raises(svc.AccessDenied, match="current password is wrong"):
        svc.change_password(d, "not-my-password", "brand-new-pass-1")
    with pytest.raises(svc.InvalidInput, match="password"):
        svc.change_password(d, PASSWORD, "short")
    svc.change_password(d, PASSWORD, "brand-new-pass-1")
    with pytest.raises(svc.AccessDenied):
        svc.login("pw@v.ca", PASSWORD)
    assert svc.login("pw@v.ca", "brand-new-pass-1") == (d, "Donor")
    for _ in range(5):
        with pytest.raises(svc.AccessDenied):
            svc.login("pw@v.ca", "wrong-password-x")
    with pytest.raises(svc.AccessDenied, match="locked"):
        svc.login("pw@v.ca", "brand-new-pass-1")
    with pytest.raises(Exception, match="Admin access only"):
        svc.admin_reset_password(world["windsor"], d, "reset-by-admin-7")
    svc.admin_reset_password(world["admin"], d, "reset-by-admin-7")
    assert svc.login("pw@v.ca", "reset-by-admin-7") == (d, "Donor")


def test_profile_and_screening_answers_are_scoped(world):
    """SE-07: contact details and screening answers reach only their owner (and clinic staff for answers)."""
    d, other = new_donor("prof@v.ca"), new_donor("prof2@v.ca")
    assert svc.profile(d)["email"] == "prof@v.ca" and svc.profile(d)["address"] == "1 Main St"
    svc.complete_screening(d, CLEAN)
    with owner(world) as con:
        sid = con.execute("SELECT max(screening_id) FROM vytal.screening WHERE donor_id = %s", (d,)).fetchone()[0]
    assert len(svc.screening_answers(d, sid)) == 6
    assert len(svc.screening_answers(world["windsor"], sid)) == 6
    with pytest.raises(Exception, match="only to the donor and to clinic staff"):
        svc.screening_answers(other, sid)


def test_admin_clears_a_lookback_through_the_backend(world):
    s = world["windsor"]
    d = new_donor("clear@v.ca", "B+")
    _, units = donate_and_release(s, d, "B+")
    rows = svc.quarantine_donor(world["admin"], d)
    assert {r[4] for r in rows} == {"Quarantined"}                     # lookback rows: (..., unit_id, component, status, ...)
    with pytest.raises(svc.InvalidInput):
        svc.clear_lookback(world["admin"], d, " ")
    with pytest.raises(Exception, match="Admin access only"):
        svc.clear_lookback(s, d, "cleared")
    assert svc.clear_lookback(world["admin"], d, "malaria test negative") == len(units)
    assert svc.donor_home(d)["status"] != "Not eligible (flagged after a failed test)"
