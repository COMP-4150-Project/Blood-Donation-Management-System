"""Replays Step 2 Scenario 1 (Emergency Trauma Request) through the vytal backend package.

    PGHOST=... PGUSER=postgres python demo.py

Builds a clean database named vytal_demo (dropping any earlier copy), so the story starts from the exact stock
the scenario describes: 3 O-negative red cell units, one A-positive unit, and a threshold of 6 for O-negative.
Needs the same superuser connection as setup.sh.
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
        con.execute("SELECT create_staff(%s,'maya@demo.ca',%s,'Maya','Ross',%s,'Nurse')",
                    (admin, hash_password(PASSWORD), fac))
        con.execute("INSERT INTO stock_threshold (facility_id, blood_group, component_code, min_units) "
                    "VALUES (%s,'O-','RBC',6)", (fac,))


def say(text):
    print(f"\n{text}")


def main():
    build_database()
    os.environ["PGDATABASE"] = DB
    os.environ["PGOPTIONS"] = "-c role=vytal_app"          # the backend runs as the least-privilege role (SE-04)
    from vytal import services as svc

    maya, role = svc.login("maya@demo.ca", PASSWORD)
    say(f"Maya logs in as {role} (user {maya}).")

    def donor(email, group, first):
        return svc.register_donor(email, PASSWORD, first, "Demo", "519-555-0100", dt.date(1990, 5, 1), "M", 75,
                                  "1 Main St", group)

    say("Morning stock: 3 O- donors and 1 A+ donor give whole blood, tests pass, units are released.")
    for i, group in enumerate(["O-", "O-", "O-", "A+"]):
        d = donor(f"stock{i}@demo.ca", group, f"Donor{i}")
        svc.verify_blood_group(maya, d, group)
        svc.screen_donor(maya, d, CLEAN)
        dn, _ = svc.record_donation(maya, d, "WB", 450, 145, ["RBC"])
        svc.record_tests(maya, dn, NEG)
    say("Four more O- donors are screened and eligible, but have not donated.")
    for i in range(4):
        d = donor(f"ready{i}@demo.ca", "O-", f"Ready{i}")
        svc.verify_blood_group(maya, d, "O-")
        svc.screen_donor(maya, d, CLEAN)

    say("Step 1-3: TR-2291, group Unknown, 4 units of red cells, Emergency. Only O- is safe for an unknown group.")
    need_by = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    req = svc.submit_request(maya, "TR-2291", None, "RBC", 4, "Emergency", need_by)
    print(f"  reserved {req['reserved']}, shortfall {req['shortfall']}")

    say("Step 4: O- stock is now 0, below the threshold of 6.")
    print("  inventory (group, component, available, reserved, expiring 48h):")
    for row in svc.inventory(maya):
        print("   ", row)
    with psycopg.connect(dbname=DB, options="") as con:
        n = con.execute("SELECT count(*) FROM vytal.stock_alert WHERE cleared_at IS NULL AND blood_group = 'O-'").fetchone()[0]
    print(f"  open O- red cell alerts: {n}")

    say("Step 5: Maya searches eligible donors and sends them a donation call.")
    found = svc.find_donors(maya, req["request_id"])
    print(f"  {len(found)} eligible donors found (code, group, status only)")
    call_id = svc.call_donors(maya, req["request_id"], "O-negative donors are needed today. Please book a donation.")
    print(f"  donation call {call_id} sent to all {len(found)}")

    say("Step 6: the lab confirms A-positive; the last unit comes from A+ stock, O- is kept for others.")
    done = svc.confirm_request_group(maya, req["request_id"], "A+")
    print(f"  reserved {done['reserved']} of 4, shortfall {done['shortfall']}")

    say("Outcome: Maya issues the reserved units.")
    print(f"  request status: {svc.close_request(maya, req['request_id'])}")
    print(f"\nDatabase {DB} is left in place; inspect it with psql -d {DB}.")


if __name__ == "__main__":
    main()
