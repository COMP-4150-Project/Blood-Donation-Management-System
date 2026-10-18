"""Key queries on the seeded database: each runs, is fast, and agrees with an independent recomputation."""
import time
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

import pytest

from vytal.queries import load

QUERIES = load()


def rows(sdb, name):
    sdb.cur.execute(QUERIES[name].sql)
    cols = [c.name for c in sdb.cur.description]
    return [dict(zip(cols, r)) for r in sdb.cur.fetchall()]


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_query_runs_returns_rows_and_is_fast(sdb, name):
    t = time.perf_counter()
    out = rows(sdb, name)
    assert out, f"{name} returned no rows on the demo data"
    assert time.perf_counter() - t < 2.0             # PR-01's 2-second bound; measured timings are in bench/


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_query_runs_as_the_read_only_report_role(sdb, name):
    """FR-34 / SE-04: dashboards connect as vytal_report, so every key query must work with its read-only grants."""
    sdb.cur.execute("SET LOCAL ROLE vytal_report")
    sdb.cur.execute(QUERIES[name].sql)
    assert sdb.cur.fetchall()


def test_every_query_names_a_requirement():
    assert all(q.serves and q.why and q.title for q in QUERIES.values())


def test_q01_queue_order_and_shortfall(sdb):
    out = rows(sdb, "Q01")
    rank = {"Emergency": 0, "Urgent": 1, "Routine": 2}
    keys = [(rank[r["urgency"]], r["hours_left"]) for r in out]
    assert keys == sorted(keys)
    assert all(r["shortfall"] == max(r["units_requested"] - r["units_reserved"], 0) for r in out)
    assert len(out) == sdb.one("SELECT count(*) FROM blood_request WHERE status='Open'")


def test_q02_low_flag_matches_a_raw_count(sdb):
    for r in rows(sdb, "Q02"):
        n = sdb.one("SELECT count(*) FROM blood_unit u JOIN donation d USING (donation_id) JOIN facility f USING (facility_id) "
                    "WHERE f.name=%s AND d.blood_group=%s AND u.component_code='RBC' AND u.status='Available'",
                    (r["facility"], r["blood_group"]))
        assert n == r["available"]
        assert (r["stock_level"] == "LOW") == (n < r["min_units"])


def test_q03_only_eligible_compatible_donors(sdb):
    out = rows(sdb, "Q03")
    req = out[0]["request_id"]
    rec, comp = sdb.q("SELECT recipient_group, component_code FROM blood_request WHERE request_id=%s", (req,))[0]
    for r in out:
        assert sdb.one("SELECT can_be_called FROM v_donor_eligibility WHERE donor_code=%s", (r["donor_code"],))
        allowed = sdb.one("SELECT count(*) FROM compatibility WHERE component_code=%s AND donor_group=%s "
                          "AND (recipient_group=%s OR %s::varchar IS NULL)", (comp, r["blood_group"], rec, rec))
        assert allowed >= (1 if rec else 8)
    assert "phone" not in out[0] and "email" not in out[0]


def test_q04_history_hides_recipients(sdb):
    out = rows(sdb, "Q04")
    assert not {"request_id", "patient_ref", "recipient_group"} & set(out[0])
    code = out[0]["donor_code"]
    assert len(out) == sdb.one("SELECT count(*) FROM donation d JOIN donor o ON o.user_id=d.donor_id WHERE o.donor_code=%s", (code,))


def test_q05_lookback_covers_every_unit_of_the_donor(sdb):
    out = rows(sdb, "Q05")
    code = out[0]["donor_code"]
    units = sdb.one("SELECT count(*) FROM v_blood_unit u JOIN donor o ON o.user_id = u.donor_id WHERE o.donor_code=%s", (code,))
    assert len({r["unit_id"] for r in out}) == units
    assert all(r["received_at"] for r in out if r["issued_on"])


def test_q06_overview_matches_raw_counts(sdb):
    o = rows(sdb, "Q06")[0]
    by_urgency = dict(sdb.q("SELECT urgency, count(*) FROM blood_request WHERE status='Open' GROUP BY 1"))
    assert (o["open_emergency"], o["open_urgent"], o["open_routine"]) == \
        (by_urgency.get("Emergency", 0), by_urgency.get("Urgent", 0), by_urgency.get("Routine", 0))
    assert o["open_alerts"] == sdb.one("SELECT count(*) FROM stock_alert WHERE cleared_at IS NULL")


def test_q07_wastage_recomputed_in_python(sdb):
    made, lost = Counter(), Counter()
    for month, comp, wasted in sdb.q("SELECT date_trunc('month', collected_at)::date, component_code, w.unit_id IS NOT NULL "
                                     "FROM v_blood_unit LEFT JOIN wastage_record w USING (unit_id)"):
        made[month, comp] += 1
        lost[month, comp] += wasted
    for r in rows(sdb, "Q07"):
        key = (r["month"], r["component_code"])
        assert (r["collected"], r["wasted"]) == (made[key], lost[key])


def test_q08_fill_rate_recomputed(sdb):
    for r in rows(sdb, "Q08"):
        assert r["units_issued"] <= r["units_requested"]
        expected = (Decimal(100) * r["units_issued"] / r["units_requested"]).quantize(Decimal("0.1"), ROUND_HALF_UP)
        assert r["fill_rate_pct"] == expected


def test_q10_no_gap_shorter_than_the_minimum_interval(sdb):
    for r in rows(sdb, "Q10"):
        assert r["shortest_gap"] >= r["minimum_days"]


@pytest.mark.parametrize("name", ["Q12", "Q13"])
def test_shares_add_up_to_100(sdb, name):
    assert abs(sum(float(r["share_pct"]) for r in rows(sdb, name)) - 100) < 0.5


def test_q11_only_lapsed_eligible_repeat_donors(sdb):
    for r in rows(sdb, "Q11"):
        assert r["donations"] >= 2 and r["rank_in_group"] <= 3
        assert sdb.one("SELECT CURRENT_DATE - %s > 180", (r["last_donation"],))
