"""Declarative integrity: CHECK, UNIQUE, NOT NULL and FOREIGN KEY constraints reject bad data (DI-02)."""
import pytest

INSERT_DONATION = ("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
                   "VALUES (%s,%s,'WB',now(),%s,140)")


@pytest.mark.parametrize("sql, needle", [
    ("INSERT INTO user_account (email,password_hash,first_name,last_name,role) VALUES ('z@v.ca','x','a','b','Nurse')", "check"),
    ("INSERT INTO facility (name,facility_type,address) VALUES ('X','Clinic','1 St')", "check"),
    ("INSERT INTO blood_group VALUES ('C+','C','+')", "check"),
    ("INSERT INTO component_type VALUES ('WBC','White cells',0)", "check"),
    ("INSERT INTO deferral_reason VALUES ('X','x',TRUE,30)", "check"),
    ("INSERT INTO screening_question (question_text, reason_code) VALUES ('Q','TATTOO')", "check"),
])
def test_reference_checks(db, sql, needle):
    assert needle in db.fails(sql)


def test_duplicate_email_rejected(db):
    assert "unique" in db.fails("INSERT INTO user_account (email,password_hash,first_name,last_name,role) "
                                "VALUES ('nurse@v.ca','x','a','b','Staff')")


def test_unknown_blood_group_rejected(db):
    assert "foreign key" in db.fails("UPDATE donor SET blood_group='C+' WHERE user_id=%s", (db.donor(verified=False),))


def test_negative_volume_rejected(db):
    d = db.donor()
    db.screen(d)
    assert "check" in db.fails(INSERT_DONATION, (d, db.nurse, -1))


def test_staff_needs_a_facility(db):
    uid = db.user("Staff")
    assert "null" in db.fails("INSERT INTO staff (user_id) VALUES (%s)", (uid,))


def test_verification_needs_both_columns_and_a_group(db):
    d = db.donor(group=None, verified=False)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))   # staff, so the FR-03/FR-09 guard lets the CHECK decide
    assert "check" in db.fails("UPDATE donor SET verified_by=%s, verified_at=now() WHERE user_id=%s", (db.nurse, d))
    v = db.donor()
    assert "check" in db.fails("UPDATE donor SET verified_at=NULL WHERE user_id=%s", (v,))


def test_one_unit_per_component_per_donation(db):
    dn = db.donate(db.donor())
    db.q("CALL separate_donation(%s, ARRAY['RBC'])", (dn,))
    assert "unique" in db.fails("INSERT INTO blood_unit (unit_id,donation_id,component_code) VALUES ('DUP',%s,'RBC')", (dn,))


def test_same_unit_cannot_be_reserved_twice(db):
    unit = db.stock("O+")[0]
    r1, r2 = db.request("O+"), db.request("O+")
    db.q("INSERT INTO request_unit (request_id, unit_id) VALUES (%s,%s)", (r1, unit))
    assert "unique" in db.fails("INSERT INTO request_unit (request_id, unit_id) VALUES (%s,%s)", (r2, unit))


def test_issued_and_released_are_exclusive(db):
    unit = db.stock("O+")[0]
    r = db.request("O+")
    assert "check" in db.fails("INSERT INTO request_unit VALUES (%s,%s,now(),now(),now())", (r, unit))


def test_alert_needs_a_threshold_and_only_one_can_be_open(db):
    assert "foreign key" in db.fails("INSERT INTO stock_alert (facility_id,blood_group,component_code) VALUES (%s,'AB-','PLT')", (db.fac_h,))
    db.q("INSERT INTO stock_threshold VALUES (%s,'O-','RBC',3)", (db.fac_h,))       # trigger opens the alert (0 < 3)
    assert "unique" in db.fails("INSERT INTO stock_alert (facility_id,blood_group,component_code) VALUES (%s,'O-','RBC')", (db.fac_h,))


def test_referenced_rows_cannot_be_deleted(db):
    db.donate(db.donor())
    assert "foreign key" in db.fails("DELETE FROM facility WHERE facility_id=%s", (db.fac_h,))
    assert "foreign key" in db.fails("DELETE FROM blood_group WHERE blood_group='O+'")
