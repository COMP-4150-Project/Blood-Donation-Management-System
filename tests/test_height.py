"""Height and estimated blood volume (Nadler): the schema limits, the formula, the eligibility rule, the donation volume cap,
the services and the sample data."""
import datetime as dt
from decimal import Decimal

import pytest

from vytal import services as svc


def nadler(sex, h_cm, w_kg):
    h = h_cm / 100
    litres = 0.3669 * h ** 3 + 0.03219 * w_kg + 0.6041 if sex == "M" else 0.3561 * h ** 3 + 0.03308 * w_kg + 0.1833
    return round(litres * 1000)


def ebv(db, sex, h, w):
    return float(db.one("SELECT estimated_blood_volume_ml(%s,%s,%s)", (sex, h, w)))


# ---------------------------------------------------------------------- schema
@pytest.mark.parametrize("bad", ["99.9", "250.1", "0", "-5", "'NaN'", "'Infinity'", "'-Infinity'"])
def test_height_check_constraint_on_the_table(db, bad):
    d = db.donor()
    assert db.fails(f"UPDATE donor SET height_cm = {bad} WHERE user_id=%s", (d,)) != ""         # check violation or numeric overflow


def test_height_limits_are_inclusive_and_required(db):
    d = db.donor()
    for ok in (100, 250, 167.5):
        assert db.fails("UPDATE donor SET height_cm = %s WHERE user_id=%s", (ok, d)) == ""
    assert "null" in db.fails("UPDATE donor SET height_cm = NULL WHERE user_id=%s", (d,))
    assert "check" in db.fails("CALL register_donor('h1@v.ca','x','A','B',NULL,'1990-01-01','F',60,99,NULL,NULL)")
    assert "null" in db.fails("CALL register_donor('h2@v.ca','x','A','B',NULL,'1990-01-01','F',60,NULL,NULL,NULL)")


def test_min_blood_volume_rule_column(db):
    assert db.q("SELECT sex, min_blood_volume_ml FROM eligibility_rule ORDER BY sex") == [("F", 3000), ("M", 3000)]
    for bad in (1499, 6001):
        assert "check" in db.fails("UPDATE eligibility_rule SET min_blood_volume_ml=%s WHERE sex='M'", (bad,))


# ---------------------------------------------------------------------- formula
@pytest.mark.parametrize("sex, h, w", [("M", 180, 80), ("M", 164, 50), ("M", 193, 120), ("F", 150, 50), ("F", 168, 62), ("F", 176, 100)])
def test_estimated_blood_volume_matches_the_nadler_formula(db, sex, h, w):
    assert ebv(db, sex, h, w) == nadler(sex, h, w)


def test_estimated_blood_volume_known_values(db):
    assert ebv(db, "M", 180, 80) == 5319            # 0.3669 * 1.8^3 + 0.03219 * 80 + 0.6041 = 5.319 L
    assert ebv(db, "F", 150, 50) == 3039            # 0.3561 * 1.5^3 + 0.03308 * 50 + 0.1833 = 3.039 L
    assert ebv(db, "F", 146, 51) == 2979


# ---------------------------------------------------------------------- eligibility
def test_eligibility_flips_when_height_or_weight_drops_the_volume_below_the_rule(db):
    d = db.donor(sex="F", kg=55, cm=160, screened=True)             # about 3.2 L
    status = "SELECT status FROM v_donor_eligibility WHERE user_id=%s"
    assert db.one(status, (d,)) == "Eligible"
    db.q("UPDATE donor SET height_cm = 140 WHERE user_id=%s", (d,))     # 0.3561 * 2.744 + 1.819 + 0.183 = 2.979 L
    assert db.one(status, (d,)) == "Not eligible (estimated blood volume under 3.0 L: check height and weight)"
    reason, can = db.q("SELECT reason, can_be_called FROM v_donor_eligibility WHERE user_id=%s", (d,))[0]
    assert "estimated blood volume of 3.0 L" in reason and "at least 3.0 L" in reason and not can
    assert db.one("SELECT eligible_from FROM donor_eligibility(%s)", (d,)) is None
    db.q("UPDATE donor SET height_cm = 160, weight_kg = 50 WHERE user_id=%s", (d,))   # 3.039 - no: 1.459+1.654+0.183 = 3.296 L
    assert db.one(status, (d,)) == "Eligible"


def test_a_low_volume_donor_is_left_off_every_call_list(db):
    ok = db.donor(group="B-", sex="F", kg=55, cm=160, screened=True)
    low = db.donor(group="B-", sex="F", kg=51, cm=146, screened=True)
    codes = {c for (c,) in db.q("SELECT donor_code FROM donors_for_group('B-','RBC')")}
    code = lambda u: db.one("SELECT donor_code FROM donor WHERE user_id=%s", (u,))
    assert code(ok) in codes and code(low) not in codes


def test_weight_rule_is_reported_before_the_volume_rule(db):
    d = db.donor(sex="F", kg=45, cm=140, screened=True)
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == "Not eligible (under 50 kg)"


def test_admin_changing_the_minimum_volume_changes_eligibility(db):
    d = db.donor(sex="M", kg=70, cm=175, screened=True)             # 0.3669 * 5.359 + 2.253 + 0.604 = 4.82 L
    status = "SELECT status FROM v_donor_eligibility WHERE user_id=%s"
    assert db.one(status, (d,)) == "Eligible"
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.admin),))
    db.q("CALL set_eligibility_rule(%s,'M',17,50,130,5500)", (db.admin,))
    assert db.one(status, (d,)) == "Not eligible (estimated blood volume under 5.5 L: check height and weight)"
    db.q("CALL set_eligibility_rule(%s,'M',17,50,130,4000)", (db.admin,))
    assert db.one(status, (d,)) == "Eligible"
    assert db.q("SELECT old_value, new_value FROM audit_log WHERE table_name='eligibility_rule' AND field_name='min_blood_volume_ml' "
                "ORDER BY audit_id") == [("3000", "5500"), ("5500", "4000")]


@pytest.mark.parametrize("value", [1499, 6001, None])
def test_set_eligibility_rule_refuses_a_volume_outside_1500_to_6000(db, value):
    assert "between 1500 and 6000" in db.fails("CALL set_eligibility_rule(%s,'M',17,50,130,%s)", (db.admin, value))


# ---------------------------------------------------------------------- recording a donation
def test_record_donation_refuses_a_donor_under_the_volume_minimum(db):
    d = db.donor(sex="F", kg=51, cm=146, screened=True)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    assert "estimated blood volume" in db.fails("CALL record_donation(%s,%s,'WB',450,140)", (d, db.nurse))
    assert db.fails("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
                    "VALUES (%s,%s,'WB',now(),450,140)", (d, db.nurse)).startswith("donor's estimated blood volume")


def test_record_donation_caps_a_draw_at_15_percent_of_the_estimated_volume(db):
    d = db.donor(sex="F", kg=55, cm=160, screened=True)             # 3461 ml, 15% is 519.15 ml
    assert nadler("F", 160, 55) == 3461
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(db.nurse),))
    assert "15% of the donor's estimated blood volume (3461 ml)" in db.fails("CALL record_donation(%s,%s,'WB',520,140)", (d, db.nurse))
    assert db.one("SELECT count(*) FROM donation WHERE donor_id=%s", (d,)) == 0
    assert db.fails("CALL record_donation(%s,%s,'WB',519,140)", (d, db.nurse)) == ""
    assert db.one("SELECT count(*) FROM donation WHERE donor_id=%s", (d,)) == 1


# ---------------------------------------------------------------------- procedures and audit
def test_update_profile_changes_and_audits_height(db):
    d = db.donor(sex="M", kg=80, cm=180)
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    db.q("CALL update_donor_profile(%s,'519-555-0101','n@v.ca','2 Main St',81,182)", (d,))
    assert db.q("SELECT height_cm, weight_kg FROM donor WHERE user_id=%s", (d,)) == [(182, 81)]
    assert db.q("SELECT old_value, new_value FROM audit_log WHERE table_name='donor' AND record_id=%s AND field_name='height_cm'",
                (str(d),)) == [("180.0", "182.0")]
    assert "check" in db.fails("CALL update_donor_profile(%s,NULL,'n@v.ca',NULL,81,99)", (d,))


def test_my_profile_returns_the_height(db):
    d = db.donor(sex="F", kg=60, cm=Decimal("171.5"))
    db.q("SET LOCAL ROLE vytal_app")
    db.q("SELECT set_config('vytal.user_id', %s, true)", (str(d),))
    assert db.one("SELECT height_cm FROM my_profile()") == Decimal('171.5')


def test_report_login_reads_eligibility_but_not_the_private_columns(db):
    d = db.donor(sex="F", kg=55, cm=160, screened=True)
    db.q("SET LOCAL ROLE vytal_report")
    assert db.one("SELECT status FROM v_donor_eligibility WHERE user_id=%s", (d,)) == "Eligible"
    assert "permission denied" in db.fails("SELECT email FROM user_account")


# ---------------------------------------------------------------------- services
def donor_args(**kw):
    a = dict(email="h@v.ca", password="a-long-password-1", first="A", last="B", phone=None, dob=dt.date(1990, 1, 1), sex="F",
             weight_kg=60, height_cm=165, address=None, blood_group=None)
    return a | kw


@pytest.mark.parametrize("bad", [99.9, 250.1, "abc", "", None, float("nan"), float("inf"), "-inf"])
def test_service_register_refuses_a_bad_height_before_touching_the_database(bad, monkeypatch):
    monkeypatch.setattr(svc, "session", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no database work")))
    with pytest.raises(svc.InvalidInput, match="height_cm"):
        svc.register_donor(**donor_args(height_cm=bad))
    with pytest.raises(svc.InvalidInput, match="height_cm"):
        svc.update_profile(1, None, "a@v.ca", None, 60, bad)


@pytest.mark.parametrize("bad", [1499, 6001, 3000.5, "3000", None, True])
def test_service_rule_refuses_a_bad_minimum_volume(bad, monkeypatch):
    monkeypatch.setattr(svc, "session", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no database work")))
    with pytest.raises(svc.InvalidInput, match="min_blood_volume_ml"):
        svc.set_eligibility_rule(1, "M", 17, 50, 130, bad)


# ---------------------------------------------------------------------- sample data
INTENDED_LOW_VOLUME = ["D-1050"]          # Aisha Rahman, 51 kg and 146 cm; documented in sql/07_sample_data.sql


def test_every_sample_donor_has_a_plausible_height_and_weight(sdb):
    assert sdb.one("SELECT count(*) FROM donor WHERE height_cm IS NULL") == 0
    assert sdb.one("SELECT count(*) FROM donor WHERE sex='F' AND donor_code <> ALL(%s) AND height_cm NOT BETWEEN 152 AND 176",
                   (INTENDED_LOW_VOLUME,)) == 0
    assert sdb.one("SELECT count(*) FROM donor WHERE sex='M' AND height_cm NOT BETWEEN 164 AND 193") == 0
    assert sdb.one("SELECT count(*) FROM donor WHERE weight_kg / ((height_cm / 100) ^ 2) NOT BETWEEN 17 AND 36") == 0


def test_only_the_intended_sample_donors_fail_on_blood_volume(sdb):
    low = [c for (c,) in sdb.q("SELECT donor_code FROM v_donor_eligibility WHERE status LIKE 'Not eligible (estimated blood volume%' ORDER BY 1")]
    assert low == INTENDED_LOW_VOLUME
    assert sdb.one("SELECT count(*) FROM donor d JOIN eligibility_rule r USING (sex) "
                   "WHERE estimated_blood_volume_ml(d.sex, d.height_cm, d.weight_kg) < r.min_blood_volume_ml") == 1


def test_sample_donations_stay_within_15_percent_of_the_estimated_volume(sdb):
    assert sdb.one("SELECT count(*) FROM donation dn JOIN donor d ON d.user_id = dn.donor_id "
                   "WHERE dn.volume_ml > 0.15 * estimated_blood_volume_ml(d.sex, d.height_cm, d.weight_kg)") == 0
