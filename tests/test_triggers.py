"""Business rules enforced by triggers, independent of any screen (DI-03)."""
import pytest

from test_constraints import INSERT_DONATION

def next_in(db, donor):
    return db.one("SELECT next_eligible_date - CURRENT_DATE FROM donor WHERE user_id=%s", (donor,))


# ---------------------------------------------------------------------- FR-07 / FR-11 eligibility before a donation
@pytest.mark.parametrize("kwargs, hb, needle", [
    (dict(age=16), 140, "under 17"),
    (dict(kg=45), 140, "50 kg"),
    (dict(sex="M"), 128, "hemoglobin"),
    (dict(sex="F"), 124, "hemoglobin"),
])
def test_threshold_rules(db, kwargs, hb, needle):
    assert needle in db.fails(INSERT_DONATION.replace(",140)", ",%s)"), (db.donor(**kwargs), db.nurse, 450, hb))


def test_thresholds_are_data_not_code(db):
    """MA-01: lowering the male hemoglobin minimum lets 128 g/L donate with no code change."""
    d = db.donor(sex="M")
    db.q("UPDATE eligibility_rule SET min_hemoglobin_gl = 125 WHERE sex='M'")
    assert db.donate(d, hb=128) > 0


def test_interval_by_sex_and_second_donation_blocked(db):
    m, f = db.donor(sex="M"), db.donor(sex="F")
    db.donate(m)
    db.donate(f, hb=130)
    assert next_in(db, m) == 56 and next_in(db, f) == 84
    assert "not eligible until" in db.fails(INSERT_DONATION, (m, db.nurse, 450))


def test_unverified_flagged_and_inactive_donors_blocked(db):
    unverified = db.donor(group=None, verified=False)
    assert "verify" in db.fails(INSERT_DONATION, (unverified, db.nurse, 450))
    flagged = db.donor()
    db.q("UPDATE donor SET is_flagged = TRUE WHERE user_id=%s", (flagged,))
    assert "flagged" in db.fails(INSERT_DONATION, (flagged, db.nurse, 450))
    inactive = db.donor()
    db.q("UPDATE user_account SET is_active = FALSE WHERE user_id=%s", (inactive,))
    assert "deactivated" in db.fails(INSERT_DONATION, (inactive, db.nurse, 450))


def test_donation_needs_a_screening_in_the_last_24_hours(db):
    d = db.donor()
    assert "no screening" in db.fails(INSERT_DONATION, (d, db.nurse, 450))
    db.q("INSERT INTO screening (donor_id, completed_at) VALUES (%s, now() - interval '25 hours')", (d,))
    assert "no screening" in db.fails(INSERT_DONATION, (d, db.nurse, 450))


def test_donation_keeps_the_group_measured_at_the_time(db):
    d = db.donor("O+")
    dn = db.donate(d)
    db.q("UPDATE donor SET blood_group='A+' WHERE user_id=%s", (d,))
    assert db.one("SELECT blood_group FROM donation WHERE donation_id=%s", (dn,)) == "O+"


# ---------------------------------------------------------------------- FR-08 / FR-10 deferrals drive the next eligible date
def test_deferral_rules_and_eligibility_dates(db):
    d = db.donor()
    assert "end date" in db.fails("INSERT INTO deferral (donor_id,reason_code,recorded_by) VALUES (%s,'TATTOO',%s)", (d, db.nurse))
    assert "permanent" in db.fails("INSERT INTO deferral (donor_id,reason_code,end_date,recorded_by) VALUES (%s,'HBV',CURRENT_DATE+5,%s)", (d, db.nurse))
    assert "check" in db.fails("INSERT INTO deferral (donor_id,reason_code,end_date) VALUES (%s,'TATTOO',CURRENT_DATE+1)", (d,))
    db.q("INSERT INTO deferral (donor_id,reason_code,end_date,recorded_by) VALUES (%s,'TATTOO',CURRENT_DATE+90,%s)", (d, db.nurse))
    assert next_in(db, d) == 90
    db.q("UPDATE deferral SET start_date=CURRENT_DATE-100, end_date=CURRENT_DATE-1 WHERE donor_id=%s", (d,))
    assert db.one("SELECT next_eligible_date FROM donor WHERE user_id=%s", (d,)) is None
    db.q("INSERT INTO deferral (donor_id,reason_code,recorded_by) VALUES (%s,'HBV',%s)", (d, db.nurse))
    assert db.one("SELECT next_eligible_date::text FROM donor WHERE user_id=%s", (d,)) == "infinity"


# ---------------------------------------------------------------------- FR-12 / FR-13 / FR-14 unit lifecycle
def test_units_get_expiry_location_and_donation_status(db):
    dn = db.donate(db.donor(), staff=db.phleb)
    assert db.one("SELECT status FROM donation WHERE donation_id=%s", (dn,)) == "Awaiting separation"
    db.q("SELECT separate_donation(%s, ARRAY['RBC','PLASMA','PLT'])", (dn,))
    assert db.one("SELECT status FROM donation WHERE donation_id=%s", (dn,)) == "Separated"
    days = dict(db.q("SELECT component_code, round(extract(epoch FROM expires_at - collected_at) / 86400)::int "
                     "FROM v_blood_unit WHERE donation_id=%s", (dn,)))
    assert days == {"RBC": 42, "PLASMA": 365, "PLT": 5}
    assert {f for (f,) in db.q("SELECT facility_id FROM blood_unit WHERE donation_id=%s", (dn,))} == {db.fac_b}


def test_expiry_before_collection_rejected(db):
    dn = db.donate(db.donor())
    assert "after collection" in db.fails("INSERT INTO blood_unit (unit_id,donation_id,component_code,expires_at) "
                                          "VALUES ('E',%s,'RBC','2000-01-01')", (dn,))


def test_release_needs_every_test_negative(db):
    dn = db.donate(db.donor())
    unit = db.one("SELECT separate_donation(%s, ARRAY['RBC'])", (dn,))
    assert "tests missing" in db.fails("UPDATE blood_unit SET status='Available' WHERE unit_id=%s", (unit,))
    db.q("INSERT INTO test_result (donation_id,test_code,result) VALUES (%s,'HIV','Negative')", (dn,))
    assert "tests missing" in db.fails("UPDATE blood_unit SET status='Available' WHERE unit_id=%s", (unit,))


@pytest.mark.parametrize("path, bad", [
    ([], "Collected"),
    (["Expired"], "Available"),
    (["Discarded"], "Available"),
    (["Reserved", "Issued"], "Available"),
])
def test_invalid_status_changes(db, path, bad):
    unit = db.stock("O+")[0]
    for s in path:
        db.q("UPDATE blood_unit SET status=%s WHERE unit_id=%s", (s, unit))
    assert "invalid unit status" in db.fails("UPDATE blood_unit SET status=%s WHERE unit_id=%s", (bad, unit))


def test_positive_test_discards_flags_and_notifies(db):
    d = db.donor()
    dn = db.donate(d)
    db.q("SELECT separate_donation(%s, ARRAY['RBC','PLASMA'])", (dn,))
    db.q("INSERT INTO test_result (donation_id,test_code,result) VALUES (%s,'HBV','Positive')", (dn,))
    assert db.q("SELECT DISTINCT status FROM blood_unit WHERE donation_id=%s", (dn,)) == [("Discarded",)]
    assert db.one("SELECT count(*) FROM wastage_record w JOIN blood_unit u USING (unit_id) WHERE donation_id=%s AND cause='Discarded'", (dn,)) == 2
    assert db.one("SELECT status FROM donation WHERE donation_id=%s", (dn,)) == "Discarded"
    assert db.one("SELECT is_flagged FROM donor WHERE user_id=%s", (d,))
    assert db.one("SELECT count(*) FROM notification WHERE user_id=%s AND message LIKE %s", (db.admin, f"Donation {dn} failed%")) == 1


def test_wastage_recorded_once_per_unit(db):
    unit = db.stock("O+")[0]
    db.q("UPDATE blood_unit SET status='Discarded', status_reason='Bag damaged' WHERE unit_id=%s", (unit,))
    assert db.one("SELECT cause FROM wastage_record WHERE unit_id=%s", (unit,)) == "Discarded"
    assert "unique" in db.fails("INSERT INTO wastage_record (unit_id,cause) VALUES (%s,'Expired')", (unit,))


# ---------------------------------------------------------------------- FR-19 low-stock alerts
def open_alerts(db, group):
    return db.one("SELECT count(*) FROM stock_alert WHERE facility_id=%s AND blood_group=%s AND cleared_at IS NULL", (db.fac_h, group))


def test_alert_opens_and_clears_with_stock(db):
    db.q("INSERT INTO stock_threshold VALUES (%s,'O+','RBC',2)", (db.fac_h,))
    assert open_alerts(db, "O+") == 1
    units = db.stock("O+", n=2)
    assert open_alerts(db, "O+") == 0
    r = db.request("O+")
    db.q("SELECT allocate(%s)", (r,))
    assert open_alerts(db, "O+") == 1
    db.q("SELECT close_request(%s, TRUE)", (r,))
    assert open_alerts(db, "O+") == 0
    assert db.one("SELECT count(*) FROM blood_unit WHERE unit_id = ANY(%s) AND status='Available'", (units,)) == 2


def test_changing_a_minimum_rechecks_stock_at_once(db):
    db.stock("A+", n=2)
    db.q("INSERT INTO stock_threshold VALUES (%s,'A+','RBC',2)", (db.fac_h,))
    assert open_alerts(db, "A+") == 0
    db.q("UPDATE stock_threshold SET min_units = 5 WHERE facility_id=%s AND blood_group='A+'", (db.fac_h,))
    assert open_alerts(db, "A+") == 1


# ---------------------------------------------------------------------- FR-33 / EER
def test_audit_log_records_who_field_old_new(db):
    d = db.donor(kg=70)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    db.q("UPDATE donor SET weight_kg = 72 WHERE user_id=%s", (d,))
    assert db.q("SELECT user_id, old_value, new_value FROM audit_log WHERE table_name='donor' AND record_id=%s "
                "AND field_name='weight_kg'", (str(d),)) == [(db.nurse, "70.0", "72.0")]


def test_temp_table_cannot_shadow_the_audit_log(db):
    """SE-08: SECURITY DEFINER functions search pg_temp last."""
    d = db.donor()
    db.q("SET LOCAL ROLE vytal_app")
    db.q("CREATE TEMP TABLE audit_log (LIKE vytal.audit_log)")
    db.q("UPDATE donor SET weight_kg = 80 WHERE user_id=%s", (d,))
    assert db.one("SELECT count(*) FROM pg_temp.audit_log") == 0
    db.q("RESET ROLE")
    assert db.one("SELECT count(*) FROM vytal.audit_log WHERE record_id=%s AND field_name='weight_kg'", (str(d),)) == 1


def test_disjoint_specialization(db):
    assert "role" in db.fails("INSERT INTO donor (user_id,donor_code,date_of_birth,sex,weight_kg) VALUES (%s,'X','1990-01-01','M',70)", (db.nurse,))
    assert "role" in db.fails("INSERT INTO admin VALUES (%s)", (db.nurse,))
