"""Regression tests for the test-report fixes: each one drives a procedure or constraint through the case the report found."""
import json

import pytest

NEG = json.dumps({"HIV": "Negative", "HBV": "Negative", "HCV": "Negative", "SYPH": "Negative"})


def second_admin(db):
    uid = db.user("Admin")
    db.q("INSERT INTO admin VALUES (%s)", (uid,))
    return uid


# ---------------------------------------------------------------------- issue 0: admin accounts
def test_an_admin_cannot_deactivate_themselves_or_leave_no_admin(db):
    assert "own account" in db.fails("CALL set_account_active(%s,%s,FALSE)", (db.admin, db.admin))
    other = second_admin(db)
    assert db.fails("CALL set_account_active(%s,%s,FALSE)", (db.admin, other)) == ""        # another admin may be switched off
    assert "own account" in db.fails("CALL set_account_active(%s,%s,FALSE)", (db.admin, db.admin))
    assert "admin access only" in db.fails("CALL set_account_active(%s,%s,FALSE)", (other, db.admin))   # the deactivated one cannot act
    assert db.one("SELECT is_active FROM user_account WHERE user_id=%s", (db.admin,)) is True
    assert db.fails("CALL set_account_active(%s,%s,TRUE)", (db.admin, other)) == ""


# ---------------------------------------------------------------------- issue 1: compatibility matrix
def test_unsafe_pairings_are_refused(db):
    assert "unsafe pairing" in db.fails("CALL set_compatibility(%s,'RBC','O-','A+',TRUE)", (db.admin,))
    assert "unsafe pairing" in db.fails("CALL set_compatibility(%s,'RBC','A-','A+',TRUE)", (db.admin,))      # Rh+ red cells for Rh-
    assert "unsafe pairing" in db.fails("CALL set_compatibility(%s,'PLASMA','AB+','A+',TRUE)", (db.admin,))   # anti-B plasma for AB
    assert db.one("SELECT count(*) FROM compatibility WHERE component_code='RBC' AND recipient_group='O-' AND donor_group='A+'") == 0
    # a safe pairing that was removed can be added back
    assert db.fails("CALL set_compatibility(%s,'RBC','A+','O+',FALSE)", (db.admin,)) == ""
    assert db.fails("CALL set_compatibility(%s,'RBC','A+','O+',TRUE)", (db.admin,)) == ""


def test_removing_a_pairing_releases_what_was_reserved_under_it(db):
    o_neg = db.stock("O-")[0]
    r = db.request("A+")
    db.q("CALL allocate(%s)", (r,))
    assert db.groups_of(r) == ["O-"]
    a_pos = db.stock("A+")[0]                                   # arrives after the reservation
    assert db.fails("CALL set_compatibility(%s,'RBC','A+','O-',FALSE)", (db.admin,)) == ""
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (o_neg,)) == "Available"
    assert db.groups_of(r) == ["A+"]                            # the request reserved again from what is still compatible
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (a_pos,)) == "Reserved"


def test_removing_the_only_pairing_leaves_the_request_short(db):
    o_neg = db.stock("O-")[0]
    r = db.request("A+")
    db.q("CALL allocate(%s)", (r,))
    db.q("CALL set_compatibility(%s,'RBC','A+','O-',FALSE)", (db.admin,))
    assert db.groups_of(r) == []
    assert db.one("SELECT status FROM blood_unit WHERE unit_id=%s", (o_neg,)) == "Available"


# ---------------------------------------------------------------------- issue 4: nothing issued
def test_issuing_zero_units_never_closes_the_request(db):
    db.stock("O+")
    r = db.request("O+")
    db.q("CALL allocate(%s)", (r,))
    assert "select at least one" in db.fails("CALL close_request(%s, FALSE, '{}')", (r,))
    assert db.one("SELECT status FROM blood_request WHERE request_id=%s", (r,)) == "Open"


# ---------------------------------------------------------------------- issue 6 / 23: audit entries
def test_audit_entries_are_newest_first_with_paging_and_names(db):
    for n in range(4):
        db.q("UPDATE facility SET phone=%s WHERE facility_id=%s", (f"519-555-010{n}", db.fac_h))
    rows = db.q("SELECT audit_id, changed_at FROM audit_entries(NULL, 'facility')")
    assert [r[0] for r in rows] == sorted((r[0] for r in rows), reverse=True) and len(rows) >= 4
    page = db.q("SELECT audit_id, total_count FROM audit_entries(NULL, 'facility', NULL, NULL, NULL, 2, 1)")
    assert [p[0] for p in page] == [rows[1][0], rows[2][0]] and page[0][1] == len(rows)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.admin),))
    db.q("UPDATE facility SET phone='519-555-0199' WHERE facility_id=%s", (db.fac_h,))
    name, email = db.q("SELECT user_name, user_email FROM audit_entries(%s, 'facility', NULL, NULL, NULL, 1)", (db.admin,))[0]
    assert name == "A B" and email.endswith("@v.ca")


# ---------------------------------------------------------------------- issue 7: password reset is audited
def test_admin_password_reset_is_audited_without_the_password(db):
    d = db.donor()
    h = "$2b$12$" + "a" * 53
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.admin),))
    db.q("CALL admin_reset_password(%s,%s,%s)", (db.admin, d, h))
    rows = db.q("SELECT user_id, record_id, new_value, old_value, changed_at FROM audit_log "
                "WHERE table_name='user_account' AND field_name='password'")
    assert [(r[0], r[1]) for r in rows] == [(db.admin, str(d))] and rows[0][4] is not None
    assert h not in json.dumps(rows, default=str) and "aaaa" not in json.dumps(rows, default=str)


# ---------------------------------------------------------------------- issue 8: required text
def test_blank_required_fields_are_refused(db):
    a = db.admin
    assert "facility name is required" in db.fails("CALL save_facility(%s,NULL,'  ','Hospital','1 St',NULL,NULL)", (a,))
    assert "facility address is required" in db.fails("CALL save_facility(%s,NULL,'Z Clinic','Hospital',' ',NULL,NULL)", (a,))
    assert "first name is required" in db.fails("CALL create_staff(%s,'s1@v.ca','x','  ','Ross',%s,NULL)", (a, db.fac_h))
    assert "last name is required" in db.fails("CALL create_admin(%s,'a1@v.ca','x','Ann','')", (a,))
    assert "patient reference is required" in db.fails(
        "CALL submit_request(%s,'   ','O+','RBC',1,'Routine',now()+interval '1 day')", (db.nurse,))
    # the same rules hold for rows written directly
    assert "check" in db.fails("INSERT INTO facility (name,facility_type,address) VALUES (' ','Hospital','1 St')")
    assert "check" in db.fails("INSERT INTO facility (name,facility_type,address) VALUES ('Q','Hospital','')")
    assert "check" in db.fails("UPDATE user_account SET first_name='  ' WHERE user_id=%s", (db.nurse,))
    assert "check" in db.fails("INSERT INTO blood_request (requested_by,patient_ref,component_code,units_requested,urgency,required_by) "
                               "VALUES (%s,' ','RBC',1,'Routine',now()+interval '1 day')", (db.nurse,))
    # accepted values are stored trimmed
    fid = db.one("CALL save_facility(%s,NULL,'  Trim Clinic ','Blood Bank','  9 Rd  ','519-555-0100',NULL)", (a,))
    assert db.q("SELECT name, address FROM facility WHERE facility_id=%s", (fid,)) == [("Trim Clinic", "9 Rd")]


# ---------------------------------------------------------------------- issue 9: inactive facility
def test_staff_of_an_inactive_facility_cannot_work_or_sign_in(db):
    db.q("CALL set_facility_active(%s,%s,FALSE)", (db.admin, db.fac_b))
    msg = "facility is deactivated"
    assert msg in db.fails("CALL submit_request(%s,'P-9','O+','RBC',1,'Routine',now()+interval '1 day')", (db.phleb,))
    d = db.donor(screened=True)
    assert msg in db.fails("CALL record_donation(%s,%s,'WB',450,140)", (d, db.phleb))
    assert msg in db.fails("CALL set_threshold(%s,%s,'O+','RBC',3)", (db.phleb, db.fac_b))
    assert msg in db.fails("CALL record_deferral(%s,%s,'TATTOO')", (db.phleb, d))
    assert db.one("CALL login_attempt(%s, TRUE)", (db.phleb,)) == "facility_inactive"
    assert db.one("CALL login_attempt(%s, FALSE)", (db.phleb,)) == "failed"
    assert db.one("CALL login_attempt(%s, TRUE)", (db.nurse,)) == "ok"            # other facilities are unaffected
    db.q("CALL set_facility_active(%s,%s,TRUE)", (db.admin, db.fac_b))
    assert db.fails("CALL set_threshold(%s,%s,'O+','RBC',3)", (db.phleb, db.fac_b)) == ""


def test_a_live_session_of_an_inactive_facility_ends(db):
    h = "a" * 64
    db.q("CALL start_session(%s,%s)", (db.phleb, h))
    assert db.q("CALL resume_session(%s)", (h,)) == [(db.phleb, "Staff")]
    db.q("UPDATE facility SET is_active = FALSE WHERE facility_id=%s", (db.fac_b,))
    assert db.q("CALL resume_session(%s)", (h,)) == [(None, None)]


# ---------------------------------------------------------------------- issue 14: components follow the donation type
def test_plasma_and_platelet_donations_do_not_yield_red_cells(db):
    pls, plt, wb = (db.donate(db.donor(), typ=t) for t in ("PLS", "PLT", "WB"))
    assert "plasma donation cannot yield red blood cells" in db.fails("CALL separate_donation(%s,%s)", (pls, ["RBC"]))
    assert "platelets donation cannot yield red blood cells" in db.fails("CALL separate_donation(%s,%s)", (plt, ["PLT", "RBC"]))
    assert "cannot yield platelets" in db.fails("CALL separate_donation(%s,%s)", (pls, ["PLT"]))
    assert db.one("CALL separate_donation(%s,%s)", (pls, ["PLASMA"])) == [f"U{pls}-PLASMA"]
    assert db.one("CALL separate_donation(%s,%s)", (plt, ["PLT"])) == [f"U{plt}-PLT"]
    assert len(db.one("CALL separate_donation(%s,%s)", (wb, ["RBC", "PLASMA", "PLT"]))) == 3
    assert "at least one component" in db.fails("CALL separate_donation(%s,%s)", (db.donate(db.donor()), []))


# ---------------------------------------------------------------------- issue 15: hemoglobin readings
@pytest.mark.parametrize("reading", ["'NaN'", "'Infinity'", "'-Infinity'", "-5", "0", "400", "NULL"])
def test_absurd_hemoglobin_is_refused(db, reading):
    d = db.donor(screened=True)
    assert "hemoglobin must be a reading" in db.fails(f"CALL record_donation(%s,%s,'WB',450,({reading})::numeric)", (d, db.nurse))
    assert db.one("SELECT count(*) FROM donation WHERE donor_id=%s", (d,)) == 0
    assert db.one("SELECT count(*) FROM deferral WHERE donor_id=%s", (d,)) == 0


def test_hemoglobin_and_weight_checks_hold_on_the_tables(db):
    d = db.donor(screened=True)
    for bad in ("'NaN'", "999"):                                 # the other bad readings stop at the eligibility trigger first
        assert "check" in db.fails("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
                                   f"VALUES (%s,%s,'WB',now(),450,{bad})", (d, db.nurse))
    assert "check" in db.fails("UPDATE donor SET weight_kg = 'NaN' WHERE user_id=%s", (d,))
    assert "volume must be" in db.fails("CALL record_donation(%s,%s,'WB',5000,140)", (d, db.nurse))
    assert "unknown donation type" in db.fails("CALL record_donation(%s,%s,'XX',450,140)", (d, db.nurse))
    assert "not found" in db.fails("CALL record_donation(%s,%s,'WB',450,140)", (999999, db.nurse))


# ---------------------------------------------------------------------- issue 19: upper limits
def test_limits_on_units_shelf_life_and_intervals(db):
    assert "between 1 and 50" in db.fails("CALL submit_request(%s,'P-1','O+','RBC',51,'Routine',now()+interval '1 day')", (db.nurse,))
    assert "between 1 and 50" in db.fails("CALL submit_request(%s,'P-1','O+','RBC',0,'Routine',now()+interval '1 day')", (db.nurse,))
    r = db.request("O+")
    assert "check" in db.fails("UPDATE blood_request SET units_requested = 500 WHERE request_id=%s", (r,))
    assert "between 1 and 1095" in db.fails("CALL set_shelf_life(%s,'RBC',5000)", (db.admin,))
    assert "between 1 and 365" in db.fails("CALL set_donation_interval(%s,'WB','M',1000)", (db.admin,))
    assert "check" in db.fails("UPDATE component_type SET shelf_life_days = 100000")
    assert "check" in db.fails("UPDATE donation_interval SET interval_days = 100000")
    assert "between 1 and 3650" in db.fails("CALL save_deferral_reason(%s,'ZZZ','Test',FALSE,99999)", (db.admin,))
    assert "between 30 and 100" in db.fails("CALL set_eligibility_rule(%s,'M',17,'NaN',130,3000)", (db.admin,))
    assert "between 100 and 180" in db.fails("CALL set_eligibility_rule(%s,'M',17,50,500,3000)", (db.admin,))
    assert db.fails("CALL set_shelf_life(%s,'RBC',42)", (db.admin,)) == ""


# ---------------------------------------------------------------------- issue 29: expired stock
def test_a_unit_past_its_date_is_not_stock_before_the_nightly_job(db):
    unit = db.stock("O+")[0]
    db.q("INSERT INTO stock_threshold (facility_id,blood_group,component_code,min_units) VALUES (%s,'O+','RBC',1)", (db.fac_h,))
    assert db.q("SELECT available FROM v_inventory WHERE facility_id=%s AND blood_group='O+'", (db.fac_h,)) == [(1,)]
    assert db.q("SELECT count(*) FROM stock_alert WHERE cleared_at IS NULL") == [(0,)]
    db.q("UPDATE blood_unit SET expires_at = now() - interval '1 minute' WHERE unit_id=%s", (unit,))
    assert db.q("SELECT available, expiring_48h FROM v_inventory WHERE facility_id=%s AND blood_group='O+'", (db.fac_h,)) == [(0, 0)]
    db.q("SELECT refresh_alert(%s,'O+','RBC')", (db.fac_h,))
    assert db.q("SELECT count(*) FROM stock_alert WHERE cleared_at IS NULL") == [(1,)]


# ---------------------------------------------------------------------- issue 30: calls and contact details
def test_an_inactive_donor_cannot_answer_a_call(db):
    d = db.donor("O+", screened=True)
    r = db.request("O+")
    call = db.one("CALL send_donation_call(%s,%s)", (r, db.nurse))
    db.q("UPDATE user_account SET is_active = FALSE WHERE user_id=%s", (d,))
    assert "active donor" in db.fails("CALL respond_to_call(%s,%s,TRUE)", (call, d))
    assert db.one("SELECT response FROM call_recipient WHERE call_id=%s", (call,)) == "Pending"


@pytest.mark.parametrize("phone", ["call me", "abc-defg", "12", "519-555-0100; DROP"])
def test_facility_phone_must_look_like_a_phone_number(db, phone):
    assert "phone" in db.fails("CALL save_facility(%s,NULL,'Phone Test','Hospital','1 St',%s,NULL)", (db.admin, phone))
    assert "check" in db.fails("UPDATE facility SET phone=%s WHERE facility_id=%s", (phone, db.fac_h))


@pytest.mark.parametrize("phone", ["519-555-0100", "+1 (519) 555-0100", "519.555.0100 x12", None])
def test_facility_phone_accepts_ordinary_formats(db, phone):
    assert db.fails("CALL save_facility(%s,NULL,'Phone OK','Hospital','1 St',%s,NULL)", (db.admin, phone)) == ""
