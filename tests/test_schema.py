"""Schema shape matches the Step 3 design (schema.py is the single source of truth for the design document)."""
import subprocess
import sys
from pathlib import Path

import pytest

STEP3 = Path(__file__).resolve().parents[2] / "build" / "step3"
needs_design = pytest.mark.skipif(not (STEP3 / "schema.py").exists(), reason="Step 3 design sources not present")


@pytest.fixture(scope="module")
def design():
    sys.path.insert(0, str(STEP3))
    from schema import R
    return R


@needs_design
def test_schema_file_matches_design_generator():
    out = subprocess.run([sys.executable, "-c", "import ddl_gen; print(ddl_gen.ddl(), end='')"], cwd=STEP3,
                         capture_output=True, text=True, check=True).stdout
    assert out == (Path(__file__).resolve().parents[1] / "sql" / "01_schema.sql").read_text()


@needs_design
def test_tables_columns_and_keys_match_design(db, design):
    assert db.one("SELECT count(*) FROM information_schema.tables WHERE table_schema='vytal' AND table_type='BASE TABLE'") == len(design)
    assert db.one("SELECT count(*) FROM information_schema.columns c JOIN information_schema.tables t USING (table_schema, table_name) "
                  "WHERE c.table_schema='vytal' AND t.table_type='BASE TABLE'") == sum(len(r["cols"]) for r in design)
    assert db.one("SELECT count(*) FROM information_schema.table_constraints WHERE table_schema='vytal' "
                  "AND constraint_type='FOREIGN KEY'") == sum(len(r["fks"]) for r in design)


@needs_design
def test_nullability_matches_design(db):
    from schema import R, NULLABLE
    designed = {(r["name"].lower(), c) for r in R for c, _, _ in r["cols"] if c in NULLABLE.get(r["name"], set())}
    built = set(db.q("SELECT table_name, column_name FROM information_schema.columns c JOIN information_schema.tables t "
                     "USING (table_schema, table_name) WHERE c.table_schema='vytal' AND t.table_type='BASE TABLE' AND c.is_nullable='YES'"))
    assert built == designed


def test_every_foreign_key_restricts_delete(db):
    assert db.one("SELECT count(*) FROM information_schema.referential_constraints "
                  "WHERE constraint_schema='vytal' AND delete_rule <> 'RESTRICT'") == 0


def test_all_times_are_timestamptz(db):
    """DI-06: one standard time zone."""
    assert db.one("SELECT count(*) FROM information_schema.columns WHERE table_schema='vytal' "
                  "AND data_type = 'timestamp without time zone'") == 0


def test_compatibility_reference_data(db):
    counts = dict(db.q("SELECT component_code, count(*) FROM compatibility GROUP BY 1"))
    assert counts == {"RBC": 27, "PLT": 36, "PLASMA": 36}
    rbc = lambda r: {d for (d,) in db.q("SELECT donor_group FROM compatibility WHERE component_code='RBC' AND recipient_group=%s", (r,))}
    assert rbc("O-") == {"O-"}
    assert rbc("A+") == {"O-", "O+", "A-", "A+"}
    assert rbc("AB-") == {"O-", "A-", "B-", "AB-"}
    assert len(rbc("AB+")) == 8
    plasma_for_ab = {d for (d,) in db.q("SELECT donor_group FROM compatibility WHERE component_code='PLASMA' AND recipient_group='AB+'")}
    assert plasma_for_ab == {"AB-", "AB+"}
    platelets = set(db.q("SELECT recipient_group, donor_group FROM compatibility WHERE component_code='PLT'"))
    assert platelets == set(db.q("SELECT recipient_group, donor_group FROM compatibility WHERE component_code='PLASMA'"))
