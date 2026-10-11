"""Efficiency evidence: times every key query and the requirement-backed operations with and without sql/04_indexes.sql.

Usage: PGDATABASE=vytal_bench ./setup.sh --no-data && python bench/bulk_data.py && python bench/bench.py
Each mode runs inside a transaction that is rolled back, so the database is left exactly as it was.
"""
import json
import re
import statistics
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vytal.queries import load  # noqa: E402

INDEX_SQL = (ROOT / "sql" / "04_indexes.sql").read_text()
INDEXES = re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)", INDEX_SQL)
TARGET_MS = {"PR-01 eligible_donors()": 2000, "PR-02 allocate() 10 units": 1000,
             "PR-03 inventory dashboard": 3000}
RUNS = 5


def median_ms(cur, fn, runs=RUNS):
    fn(cur)                                                      # warm-up
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        fn(cur)
        times.append((time.perf_counter() - t) * 1000)
    return round(statistics.median(times), 2)


def scans(cur, sql):
    """The scan nodes of the actual plan, e.g. 'Index Scan using donation_donor_time on donation'."""
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql)
    plan = "\n".join(r[0] for r in cur.fetchall())
    nodes = re.findall(r"((?:Parallel )?(?:Seq|Index|Index Only|Bitmap Heap|Bitmap Index) Scan(?: using \w+)? on \w+)", plan)
    return plan, sorted(set(nodes))


def in_savepoint(con, fn):
    """Runs fn and rolls its writes back; returns what fn returned."""
    with con.transaction(force_rollback=True):
        return fn()


def pr01(con, cur):
    cur.execute("SELECT request_id FROM blood_request r WHERE status='Open' "
                "ORDER BY (SELECT count(*) FROM eligible_donors(r.request_id)) DESC LIMIT 1")
    rid = cur.fetchone()[0]
    return median_ms(cur, lambda c: c.execute("SELECT count(*) FROM eligible_donors(%s)", (rid,)).fetchone())


def pr02(con, cur):
    # the hospital and group with the most Available red cells, so 10 units can really be reserved
    cur.execute("SELECT facility_id, blood_group FROM v_blood_unit WHERE status='Available' AND component_code='RBC' "
                "AND expires_at > now() GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1")
    fac, group = cur.fetchone()
    staff = cur.execute("SELECT user_id FROM staff WHERE facility_id=%s LIMIT 1", (fac,)).fetchone()[0]

    def once():
        rid = cur.execute("INSERT INTO blood_request (requested_by,patient_ref,recipient_group,component_code,units_requested,"
                          "urgency,required_by) VALUES (%s,'BENCH',%s,'RBC',10,'Routine',now()+interval '1 day') "
                          "RETURNING request_id", (staff, group)).fetchone()[0]
        t = time.perf_counter()
        got = cur.execute("SELECT allocate(%s)", (rid,)).fetchone()[0]
        return (time.perf_counter() - t) * 1000, got

    in_savepoint(con, once)
    runs = [in_savepoint(con, once) for _ in range(RUNS)]
    assert all(got == 10 for _, got in runs), runs
    return round(statistics.median(ms for ms, _ in runs), 2)


def pr03(con, cur, q02):
    return median_ms(cur, lambda c: (c.execute(q02).fetchall(), c.execute("SELECT * FROM v_inventory").fetchall()))


def run_mode(con, with_indexes):
    queries = load()
    out = {}

    def record(op, ms, **extra):
        out[op] = {"ms": ms, **extra}
        print(f"[{'after' if with_indexes else 'before'}] {op}: {ms} ms", flush=True)
    with con.transaction(force_rollback=True):
        cur = con.cursor()
        for name in INDEXES:
            cur.execute(f"DROP INDEX IF EXISTS {name}")
        if with_indexes:
            cur.execute(INDEX_SQL)
        cur.execute("ANALYZE")
        for q in queries.values():
            plan, nodes = scans(cur, q.sql)
            record(f"{q.name} {q.title}", median_ms(cur, lambda c: c.execute(q.sql).fetchall()), scans=nodes, plan=plan)
        record("PR-01 eligible_donors()", pr01(con, cur))
        record("PR-02 allocate() 10 units", pr02(con, cur))
        record("PR-03 inventory dashboard", pr03(con, cur, queries["Q02"].sql))
    return out


def changed(before, after):
    gone, new = set(before) - set(after), set(after) - set(before)
    return f"{', '.join(sorted(gone))} -> {', '.join(sorted(new))}" if gone or new else "no change"


def main():
    with psycopg.connect() as con:
        con.execute("SET search_path = vytal")
        counts = dict(con.execute("SELECT 'donors', count(*) FROM donor UNION ALL SELECT 'donations', count(*) FROM donation "
                                  "UNION ALL SELECT 'units', count(*) FROM blood_unit "
                                  "UNION ALL SELECT 'requests', count(*) FROM blood_request").fetchall())
        con.commit()
        results = {"rows": counts, "indexes": INDEXES, "before": run_mode(con, False), "after": run_mode(con, True)}
    (ROOT / "bench" / "results.json").write_text(json.dumps(results, indent=2, default=str))
    lines = [f"Dataset: {', '.join(f'{k} {v:,}' for k, v in counts.items())}. Median of {RUNS} warm runs.", "",
             "| Operation | Target | Before (ms) | After (ms) | Plan change |", "|---|---|---|---|---|"]
    for op, b in results["before"].items():
        a = results["after"][op]
        target = f"< {TARGET_MS[op] / 1000:g} s" if op in TARGET_MS else ""
        plan = changed(b.get("scans", []), a.get("scans", [])) if "scans" in b else ""
        lines.append(f"| {op} | {target} | {b['ms']} | {a['ms']} | {plan} |")
    (ROOT / "bench" / "results.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
