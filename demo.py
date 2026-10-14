"""Replays Step 2 Scenario 1 (Emergency Trauma Request) through the vytal backend package.

    PGHOST=... PGUSER=postgres python demo.py

Builds a clean database named vytal_demo (dropping any earlier copy), so the story starts from the exact stock
the scenario describes: 3 O-negative red cell units, one A-positive unit, a threshold of 6 for O-negative, and 14
screened O-negative donors who are eligible today. Every step asserts the numbers in Scenario 1, so the demo fails loudly
if the rules or the data drift. Needs the same superuser connection as setup.sh.
"""
import datetime as dt
import os
import subprocess
from pathlib import Path

import psycopg

DB = "vytal_demo"
PASSWORD = "correct-horse-42"
CLEAN = {"1": False, "2": False, "3": True, "4": False, "5": False, "6": False}
NEG = {"HIV": "Negative", "HBV": "Negative", "HCV": "Negative", "SYPH": "Negative"}


def build_database():
    with psycopg.connect(dbname="postgres", autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{DB}"')
    subprocess.run([str(Path(__file__).with_name("setup.sh")), "--no-data"], env={**os.environ, "PGDATABASE": DB},
                   check=True)
    with psycopg.connect(dbname=DB) as con:
        con.execute("SET search_path = vytal")
        fac = con.execute("INSERT INTO facility (name, facility_type, address) VALUES "
                          "('Riverside General Hospital','Hospital','1 River Rd') RETURNING facility_id").fetchone()[0]
        admin = con.execute("INSERT INTO user_account (email,password_hash,first_name,last_name,role) "
                            "VALUES ('admin@demo.ca','x','Ada','Admin','Admin') RETURNING user_id").fetchone()[0]
        con.execute("INSERT INTO admin VALUES (%s)", (admin,))
        from vytal.auth import hash_password
        con.execute("CALL create_staff(%s,'maya@demo.ca',%s,'Maya','Ross',%s,'Nurse')",
                    (admin, hash_password(PASSWORD), fac))
        con.execute("INSERT INTO stock_threshold (facility_id, blood_group, component_code, min_units) "
                    "VALUES (%s,'O-','RBC',6)", (fac,))


def say(text):
    print(f"\n{text}")


def check(label, got, expected):
    print(f"  {label}: {got}")
    assert got == expected, f"Scenario 1 expects {label} = {expected}, got {got}"


def scalar(sql, args=()):
    with psycopg.connect(dbname=DB, options="") as con:          # superuser read, outside the app role
        con.execute("SET search_path = vytal")
        return con.execute(sql, args).fetchone()[0]


def main():
    build_database()
    os.environ["PGDATABASE"] = DB
    os.environ["PGOPTIONS"] = "-c role=vytal_app"          # the backend runs as the least-privilege role (SE-04)
    from vytal import services as svc

    maya, role = svc.login("maya@demo.ca", PASSWORD)
    say(f"Maya logs in as {role} (user {maya}).")

    def donor(email, group, first, sex="M"):
        return svc.register_donor(email, PASSWORD, first, "Demo", "519-555-0100", dt.date(1990, 5, 1), sex, 75,
                                  "1 Main St", group)

    say("Earlier today: 3 O- donors and 1 A+ donor give whole blood, tests pass, units are released.")
    for i, group in enumerate(["O-", "O-", "O-", "A+"]):
        d = donor(f"stock{i}@demo.ca", group, f"Donor{i}")
        svc.verify_blood_group(maya, d, group)
        svc.screen_donor(maya, d, CLEAN)
        dn, _ = svc.record_donation(maya, d, "WB", 450, 145, ["RBC"])
        svc.record_tests(maya, dn, NEG)
    say("14 more O- donors have their group verified and a screening on record (UC-05 2a), so they are eligible today.")
    ready = []
    for i in range(14):
        d = donor(f"ready{i}@demo.ca", "O-", f"Ready{i}", "F" if i % 2 else "M")
        svc.verify_blood_group(maya, d, "O-")
        svc.screen_donor(maya, d, CLEAN)
        ready.append(d)

    say("Steps 1-3: TR-2291, group Unknown, 4 units of red cells, Emergency, needed within 1 hour (BR-14: O- only).")
    need_by = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    req = svc.submit_request(maya, "TR-2291", None, "RBC", 4, "Emergency", need_by)
    rid = req["request_id"]
    check("units reserved", req["reserved"], 3)
    check("shortfall", req["shortfall"], 1)
    check("reserved groups", scalar("SELECT string_agg(b.blood_group, ',' ORDER BY b.expires_at) FROM request_unit r "
                                    "JOIN v_blood_unit b USING (unit_id) WHERE r.request_id = %s", (rid,)), "O-,O-,O-")

    say("Step 4: O- red cell stock is now 0, below the threshold of 6. The low-stock alert the trigger raised when stock first fell "
        "below 6 stays open, so the shortage is visible to all staff.")
    stock = {(g, c): avail for g, c, avail, _, _ in svc.inventory(maya)}
    check("O- red cells available", stock.get(("O-", "RBC"), 0), 0)
    check("open O- red cell alerts", scalar("SELECT count(*) FROM stock_alert WHERE cleared_at IS NULL "
                                            "AND blood_group = 'O-' AND component_code = 'RBC'"), 1)

    say("Step 5: Maya searches donors: 14 O-negative donors are eligible today. She calls all of them.")
    found = svc.find_donors(maya, rid)
    check("eligible donors found", len(found), 14)
    check("their groups", sorted({g for _, g, _ in found}), ["O-"])
    call_id = svc.call_donors(maya, rid, "O-negative donors are needed today. Please book a donation.")
    check("donors called", scalar("SELECT count(*) FROM call_recipient WHERE call_id = %s", (call_id,)), 14)

    say("Step 6: the lab confirms A-positive; the last unit comes from A+ stock, so O- is kept for other emergencies (BR-09).")
    done = svc.confirm_request_group(maya, rid, "A+")
    check("units reserved", done["reserved"], 4)
    check("shortfall", done["shortfall"], 0)
    check("reserved groups", scalar("SELECT string_agg(b.blood_group, ',' ORDER BY b.blood_group) FROM request_unit r "
                                    "JOIN v_blood_unit b USING (unit_id) WHERE r.request_id = %s AND r.released_at IS NULL",
                                    (rid,)), "A+,O-,O-,O-")

    say("Outcome: two donors accept the call for the next morning (UC-20), and Maya issues the 4 units.")
    for d in ready[:2]:
        print(f"  donor {d} accepts; they are told where to go: {svc.respond_to_call(d, call_id, True)}")
    check("donors who accepted", scalar("SELECT count(*) FROM call_recipient WHERE call_id = %s "
                                        "AND response = 'Accepted'", (call_id,)), 2)
    check("request status", svc.close_request(maya, rid), "Fulfilled")
    print(f"\nScenario 1 replayed with every number as specified. Database {DB} is left in place; inspect it with psql -d {DB}.")


if __name__ == "__main__":
    main()
