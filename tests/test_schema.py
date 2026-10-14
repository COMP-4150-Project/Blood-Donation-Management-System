"""Schema-wide rules: deletes are restricted, times are timestamptz, and the compatibility reference data is right."""


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
