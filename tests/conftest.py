"""Test databases are built from the same SQL files and seed as production (./setup.sh)."""
import os
import subprocess
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = os.environ.get("VYTAL_TEST_DB", "vytal_test")     # lets two suites run side by side


def build(dbname, with_data=False):
    admin = psycopg.connect(dbname="postgres", autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()
    args = [str(ROOT / "setup.sh")] + ([] if with_data else ["--no-data"])
    done = subprocess.run(args, env={**os.environ, "PGDATABASE": dbname}, capture_output=True, text=True)
    if done.returncode:
        pytest.exit(f"setup.sh failed for {dbname}:\n{done.stderr}", returncode=1)
    return dbname



def connect(dbname, **kw):
    con = psycopg.connect(dbname=dbname, **kw)
    con.execute("SET search_path = vytal")
    return con


@pytest.fixture(scope="session")
def empty_db():
    return build(PREFIX)


@pytest.fixture(scope="session")
def seeded_db():
    return build(PREFIX + "_seeded", with_data=True)


@pytest.fixture
def con(empty_db):
    """One transaction per test, rolled back afterwards, so tests never see each other's rows."""
    c = connect(empty_db)
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def seeded(seeded_db):
    c = connect(seeded_db)
    yield c
    c.rollback()
    c.close()


class DB:
    """Small helpers that speak SQL; every row still goes through the real constraints and triggers."""

    def __init__(self, con):
        self.con, self.cur = con, con.cursor()
        self.seq = 0

    def q(self, sql, args=None):
        self.cur.execute(sql, args)
        return self.cur.fetchall() if self.cur.description else None

    def one(self, sql, args=None):
        return self.q(sql, args)[0][0]

    def fails(self, sql, args=None):
        """Runs sql inside a savepoint and returns the error message ('' if it was accepted)."""
        try:
            with self.con.transaction():
                self.cur.execute(sql, args)
        except psycopg.Error as e:
            return str(e).splitlines()[0].lower()
        return ""

    # ------------------------------------------------------------------ a minimal world
    def setup_world(self):
        self.fac_h = self.one("INSERT INTO facility (name,facility_type,address) VALUES ('Riverside General','Hospital','1 River Rd') RETURNING facility_id")
        self.fac_b = self.one("INSERT INTO facility (name,facility_type,address) VALUES ('Lakeshore Blood Centre','Blood Bank','2 Lake St') RETURNING facility_id")
        self.admin = self.user("Admin")
        self.q("INSERT INTO admin VALUES (%s)", (self.admin,))
        self.nurse = self.one("SELECT create_staff(%s,'nurse@v.ca','x','Maya','Ross',%s,'Nurse')", (self.admin, self.fac_h))
        self.phleb = self.one("SELECT create_staff(%s,'phleb@v.ca','x','Lee','Park',%s,'Phlebotomist')", (self.admin, self.fac_b))
        return self

    def user(self, role):
        self.seq += 1
        return self.one("INSERT INTO user_account (email,password_hash,first_name,last_name,role) VALUES (%s,'x','A','B',%s) RETURNING user_id",
                        (f"u{self.seq}@v.ca", role))

    def donor(self, group="O+", sex="M", age=30, kg=70, verified=True):
        self.seq += 1
        uid = self.one("SELECT register_donor(%s,'x','A','B',NULL,(CURRENT_DATE - make_interval(years => %s))::date,%s,%s,'1 Main St',%s)",
                       (f"d{self.seq}@v.ca", age, sex, kg, group))
        if verified:
            self.q("UPDATE donor SET verified_by=%s, verified_at=now() WHERE user_id=%s", (self.nurse, uid))
        return uid

    def screen(self, donor, days_ago=0):
        self.q("INSERT INTO screening (donor_id, completed_at) VALUES (%s, now() - make_interval(days => %s, mins => 10))",
               (donor, days_ago))

    def donate(self, donor, days_ago=0, hb=140, typ="WB", staff=None):
        self.screen(donor, days_ago)
        return self.one("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
                        "VALUES (%s,%s,%s,now() - make_interval(days => %s),450,%s) RETURNING donation_id",
                        (donor, staff or self.nurse, typ, days_ago, hb))

    def stock(self, group, comps=("RBC",), days_ago=1, n=1):
        """n released units per component of the given group at the hospital; returns the unit ids."""
        ids = []
        for _ in range(n):
            dn = self.donate(self.donor(group), days_ago=days_ago)
            ids += [u for (u,) in self.q("SELECT separate_donation(%s,%s)", (dn, list(comps)))]
            self.one("SELECT record_tests(%s, %s)", (dn, '{"HIV":"Negative","HBV":"Negative","HCV":"Negative","SYPH":"Negative"}'))
        return ids

    def request(self, group, n=1, comp="RBC", urgency="Routine"):
        return self.one("INSERT INTO blood_request (requested_by,patient_ref,recipient_group,component_code,units_requested,urgency,required_by) "
                        "VALUES (%s,'P-1',%s,%s,%s,%s,now() + interval '1 day') RETURNING request_id", (self.nurse, group, comp, n, urgency))

    def groups_of(self, request):
        return sorted(g for (g,) in self.q("SELECT b.blood_group FROM request_unit JOIN v_blood_unit b USING (unit_id) "
                                           "WHERE request_id=%s AND released_at IS NULL", (request,)))


@pytest.fixture
def db(con):
    return DB(con).setup_world()


@pytest.fixture
def sdb(seeded):
    return DB(seeded)
