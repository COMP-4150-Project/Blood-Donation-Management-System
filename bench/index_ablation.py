"""Leave-one-out evidence for every index in sql/04_indexes.sql.

Every candidate index's target workloads (the queries, triggers and routines it exists for) are timed with all candidates
present and again with only that index removed. The whole measurement is repeated --rounds times, alternating which side runs
first, and the keep/drop verdict is computed from the rule in RULE, so sql/04_indexes.sql can be checked against the output.
Everything runs in transactions that are rolled back, so the database is unchanged.
Plan evidence comes from EXPLAIN ANALYZE for plain queries and from auto_explain (nested statements) for routines and triggers.

Usage: PGDATABASE=vytal_bench_full python bench/index_ablation.py [--rounds 3]     # writes bench/index_ablation.md and .json
       PGDATABASE=vytal_bench_full python bench/index_ablation.py --only deferral_donor   # re-check one index, no files
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vytal.queries import load  # noqa: E402

# every index considered; which ones belong in 04_indexes.sql is decided by the measurement, not listed here
CANDIDATES = {
    "donation_donor_time": "ON donation (donor_id, donated_at DESC)",
    "unit_available": "ON blood_unit (facility_id, component_code, expires_at) WHERE status = 'Available'",
    "unit_expiring": "ON blood_unit (expires_at) WHERE status IN ('Collected', 'Available', 'Reserved')",
    "deferral_donor": "ON deferral (donor_id)",
    "request_unit_unit": "ON request_unit (unit_id)",
    "screening_donor_time": "ON screening (donor_id, completed_at)",
    "audit_record": "ON audit_log (table_name, record_id)",
}
MIN_MS, MIN_X = 1.0, 1.5
RULE = (f"an index is kept if, on at least one of its workloads, it is used in the plan and in every round it saves at least "
        f"{MIN_MS:g} ms or is at least {MIN_X:g}x faster")
QUERIES = load()
RUNS = 7            # warm runs per measurement (median); quick sub-millisecond paths use FAST_RUNS
FAST_RUNS = 25
PR05_UNITS = 50000


# ---------------------------------------------------------------------- routine calls (function or procedure)
def call(cur, name, *args):
    """Calls a vytal routine whether it is a function (SELECT) or a procedure (CALL, trailing INOUT args as NULL)."""
    kind, nargs = cur.execute("SELECT p.prokind, p.pronargs FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                              "WHERE n.nspname = 'vytal' AND p.proname = %s", (name,)).fetchone()
    if kind == "p":
        holes = ",".join(["%s"] * len(args) + ["NULL"] * (nargs - len(args)))
        row = cur.execute(f"CALL {name}({holes})", args).fetchone()
    else:
        row = cur.execute(f"SELECT {name}({','.join(['%s'] * len(args))})", args).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------- workloads
class Workloads:
    """Each workload returns (one_run_callable, plan_callable). Runs that write are wrapped in a rolled-back savepoint."""

    def __init__(self, con):
        self.con, self.cur = con, con.cursor()
        c = self.cur
        # an eligible, verified, unflagged donor and a staff member, for the donation-insert path
        self.donor = c.execute("SELECT user_id FROM v_donor_eligibility WHERE status = 'Eligible' AND group_verified "
                               "ORDER BY user_id LIMIT 1").fetchone()[0]
        self.staff = c.execute("SELECT min(user_id) FROM staff").fetchone()[0]
        # a donor with a long history and deferrals, for recompute_next_eligible
        self.busy = c.execute("SELECT donor_id FROM deferral GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1").fetchone()[0]
        self.fac, self.group = c.execute(
            "SELECT facility_id, blood_group FROM v_blood_unit WHERE status = 'Available' AND component_code = 'RBC' "
            "AND expires_at > now() GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1").fetchone()
        self.req_staff = c.execute("SELECT user_id FROM staff WHERE facility_id = %s ORDER BY 1 LIMIT 1", (self.fac,)).fetchone()[0]
        self.audit_rec = c.execute("SELECT record_id FROM audit_log WHERE table_name = 'blood_unit' GROUP BY 1 "
                                   "ORDER BY count(*) DESC, 1 LIMIT 1").fetchone()[0]
        self.flagged = c.execute("SELECT d.donor_id FROM donation d JOIN blood_unit u USING (donation_id) "
                                 "JOIN request_unit ru USING (unit_id) GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1").fetchone()[0]

    def rolled_back(self, fn):
        with self.con.transaction(force_rollback=True):
            return fn()

    def query(self, name):
        sql = QUERIES[name].sql
        return (lambda: self.cur.execute(sql).fetchall()), (lambda: explain(self.cur, sql))

    def sql(self, sql, args=()):
        return (lambda: self.cur.execute(sql, args).fetchall()), (lambda: explain(self.cur, sql, args))

    def donation_insert(self):
        """check_donation_eligible (screening lookup) + recompute_next_eligible (latest donation, active deferrals)."""
        def once():
            def body():
                self.cur.execute("INSERT INTO screening (donor_id, completed_at) VALUES (%s, now() - interval '10 minutes')",
                                 (self.donor,))
                t = time.perf_counter()
                self.cur.execute("INSERT INTO donation (donor_id, recorded_by, donation_type_code, donated_at, volume_ml, "
                                 "hemoglobin_gl) VALUES (%s, %s, 'WB', now(), 450, 150)", (self.donor, self.staff))
                return (time.perf_counter() - t) * 1000
            return self.rolled_back(body)
        return once, (lambda: nested_plans(self, once))

    def recompute(self):
        once = lambda: self.rolled_back(lambda: call(self.cur, "recompute_next_eligible", self.busy))
        return once, (lambda: nested_plans(self, once))

    def allocate(self):
        """PR-02: reserve 10 red cells (FEFO search over Available units, then the alert re-check)."""
        def once():
            def body():
                rid = self.cur.execute(
                    "INSERT INTO blood_request (requested_by, patient_ref, recipient_group, component_code, units_requested, "
                    "urgency, required_by) VALUES (%s, 'BENCH', %s, 'RBC', 10, 'Routine', now() + interval '1 day') "
                    "RETURNING request_id", (self.req_staff, self.group)).fetchone()[0]
                t = time.perf_counter()
                got = call(self.cur, "allocate", rid)
                assert got == 10, got
                return (time.perf_counter() - t) * 1000
            return self.rolled_back(body)
        return once, (lambda: nested_plans(self, once))

    def refresh_alert(self):
        once = lambda: self.rolled_back(lambda: call(self.cur, "refresh_alert", self.fac, self.group, "RBC"))
        return once, (lambda: nested_plans(self, once))

    def _expire(self, units):
        """Puts `units` past expiry without firing triggers, then times the nightly job (all triggers on)."""
        def once():
            def body():
                self.cur.execute("SET LOCAL session_replication_role = replica")
                if units == "tomorrow":     # a real night: the units that will be past expiry in 24 hours
                    self.cur.execute("UPDATE blood_unit SET expires_at = now() - interval '1 minute' "
                                     "WHERE status IN ('Collected','Available','Reserved') AND expires_at <= now() + interval '1 day'")
                else:
                    self.cur.execute("CREATE TEMP TABLE pr05_units ON COMMIT DROP AS SELECT unit_id FROM blood_unit "
                                     "ORDER BY unit_id LIMIT %s", (units,))
                    self.cur.execute("DELETE FROM wastage_record WHERE unit_id IN (SELECT unit_id FROM pr05_units)")
                    self.cur.execute("UPDATE blood_unit SET status = 'Available', expires_at = now() - interval '1 minute' "
                                     "WHERE unit_id IN (SELECT unit_id FROM pr05_units)")
                self.cur.execute("SET LOCAL session_replication_role = origin")
                self.cur.execute("ANALYZE blood_unit")
                t = time.perf_counter()
                call(self.cur, "run_nightly_expiry")
                return (time.perf_counter() - t) * 1000
            return self.rolled_back(body)
        return once, (lambda: nested_plans(self, once))

    def expire_nightly(self):
        return self._expire("tomorrow")

    def expire_pr05(self):
        return self._expire(PR05_UNITS)

    def pr01(self):
        """PR-01: eligible_donors() for the open request with the most matches (donor_eligibility() per donor)."""
        rid = self.cur.execute("SELECT request_id FROM blood_request r WHERE status = 'Open' "
                               "ORDER BY (SELECT count(*) FROM eligible_donors(r.request_id)) DESC, 1 LIMIT 1").fetchone()[0]
        return self.sql("SELECT count(*) FROM eligible_donors(%s)", (rid,))

    def audit_trail(self):
        return self.sql("SELECT * FROM audit_log WHERE table_name = 'blood_unit' AND record_id = %s ORDER BY changed_at",
                        (self.audit_rec,))

    def lookback_donor(self):
        return self.sql("SELECT * FROM v_lookback WHERE donor_id = %s ORDER BY donated_at, unit_id", (self.flagged,))


# every index and the workloads its comment in 04_indexes.sql names
TARGETS = {
    "donation_donor_time": [("Q03 donors to call", "q:Q03"), ("Q04 donor history", "q:Q04"), ("Q05 lookback", "q:Q05"),
                            ("Q10 return interval", "q:Q10"), ("donation insert (triggers)", "donation_insert"),
                            ("recompute_next_eligible()", "recompute")],
    "unit_available": [("PR-02 allocate() 10 units", "allocate"), ("refresh_alert()", "refresh_alert"), ("Q02 stock vs minimum", "q:Q02")],
    "unit_expiring": [("run_nightly_expiry(), real night", "expire_nightly"), ("PR-05 run_nightly_expiry() 50k units", "expire_pr05")],
    "deferral_donor": [("recompute_next_eligible()", "recompute"), ("donation insert (triggers)", "donation_insert")],
    "request_unit_unit": [("Q05 lookback", "q:Q05"), ("Q12 O-neg tracing", "q:Q12"), ("v_lookback for one donor", "lookback_donor")],
    "screening_donor_time": [("donation insert (triggers)", "donation_insert"), ("PR-01 eligible_donors()", "pr01"),
                             ("Q03 donors to call", "q:Q03")],
    "audit_record": [("audit trail of one unit", "audit_trail")],
}
SLOW = {"expire_pr05": 3}


def explain(cur, sql, args=()):
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql, args)
    return "\n".join(r[0] for r in cur.fetchall())


def nested_plans(w, once):
    """Plans of every statement a routine or trigger ran, via auto_explain notices."""
    notes = []
    handler = lambda d: notes.append(d.message_primary or "")
    w.con.add_notice_handler(handler)
    try:
        w.cur.execute("SET client_min_messages = log")
        w.cur.execute("SET auto_explain.log_min_duration = 0")
        w.cur.execute("SET auto_explain.log_nested_statements = on")
        w.cur.execute("SET auto_explain.log_analyze = on")
        once()
    finally:
        w.cur.execute("RESET auto_explain.log_min_duration")
        w.cur.execute("SET client_min_messages = warning")
        w.con.remove_notice_handler(handler)
    return "\n".join(notes)


def measure(fn, runs):
    fn()                                                         # warm-up
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        got = fn()
        elapsed = (time.perf_counter() - t) * 1000
        times.append(got if isinstance(got, float) else elapsed)     # routines time themselves (setup excluded)
    return round(statistics.median(times), 3)


def runs_for(key):
    return SLOW.get(key, FAST_RUNS if key in ("recompute", "refresh_alert", "audit_trail", "donation_insert",
                                              "lookback_donor", "q:Q04") else RUNS)


def workload(w, key):
    return w.query(key[2:]) if key.startswith("q:") else getattr(w, key)()


def one_round(con, index, targets, rnd, first_round):
    """Times every workload of `index` with all candidates present and with `index` dropped; alternates the order by round."""
    out = {}
    with con.transaction(force_rollback=True):
        w = Workloads(con)
        for name, definition in CANDIDATES.items():                       # exactly as defined here, not as loaded
            con.execute(f"DROP INDEX IF EXISTS {name}")
            con.execute(f"CREATE INDEX {name} {definition}")
        con.execute("ANALYZE")

        def side(present):
            if present:
                con.execute(f"CREATE INDEX IF NOT EXISTS {index} {CANDIDATES[index]}")
            else:
                con.execute(f"DROP INDEX IF EXISTS {index}")
            con.execute("ANALYZE")
            for label, key in targets:
                once, plan = workload(w, key)
                r = out.setdefault(label, {})
                if present and first_round:
                    r["used"] = index in plan()
                r["with_ms" if present else "without_ms"] = measure(once, runs_for(key))
                print(f"round {rnd} {index:22} {label:38} {'with   ' if present else 'without'} "
                      f"{r['with_ms' if present else 'without_ms']:>10} ms", flush=True)

        order = (True, False) if rnd % 2 else (False, True)
        for present in order:
            side(present)
    return out


def passes(w, wo):
    return (wo - w) >= MIN_MS or (w > 0 and wo / w >= MIN_X)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    con = psycopg.connect(autocommit=False)
    con.execute("SET search_path = vytal")
    con.execute("LOAD 'auto_explain'")
    con.execute("SET client_min_messages = warning")
    con.commit()
    assert set(TARGETS) == set(CANDIDATES), set(TARGETS) ^ set(CANDIDATES)
    per = {}                                       # (index, label) -> list of {with_ms, without_ms} per round
    used = {}
    for rnd in range(1, a.rounds + 1):
        for index, targets in TARGETS.items():
            if a.only and index not in a.only:
                continue
            res = one_round(con, index, targets, rnd, rnd == 1)
            for label, r in res.items():
                if "used" in r:
                    used[(index, label)] = r["used"]
                per.setdefault((index, label), []).append((r["with_ms"], r["without_ms"]))
    rows, verdict = [], {}
    for index, targets in TARGETS.items():
        if a.only and index not in a.only:
            continue
        deciding = None
        for label, key in targets:
            pairs = per[(index, label)]
            w = round(statistics.median(p[0] for p in pairs), 3)
            wo = round(statistics.median(p[1] for p in pairs), 3)
            xs = [p[1] / p[0] for p in pairs if p[0]]
            ok = used[(index, label)] and all(passes(*p) for p in pairs)
            rows.append({"index": index, "workload": label, "with_ms": w, "without_ms": wo,
                         "speedup": round(wo / w, 2) if w else None, "speedup_min": round(min(xs), 2),
                         "speedup_max": round(max(xs), 2), "used": used[(index, label)], "passes": ok,
                         "runs": runs_for(key), "rounds": len(pairs)})
            if ok and (deciding is None or wo - w > deciding["without_ms"] - deciding["with_ms"]):
                deciding = rows[-1]
        verdict[index] = deciding
    for r in rows:
        r["kept"] = verdict[r["index"]] is not None
    sizes = dict(con.execute("SELECT 'donations', count(*) FROM donation UNION ALL SELECT 'units', count(*) FROM blood_unit "
                             "UNION ALL SELECT 'screenings', count(*) FROM screening UNION ALL SELECT 'deferrals', count(*) FROM deferral "
                             "UNION ALL SELECT 'request_unit', count(*) FROM request_unit UNION ALL SELECT 'audit_log', count(*) FROM audit_log").fetchall())
    con.rollback()
    for index, d in verdict.items():
        print(f"{index:22} {'KEEP' if d else 'DROP'}" + (f"  ({d['workload']}: {d['without_ms']} -> {d['with_ms']} ms, "
                                                        f"{d['speedup_min']}-{d['speedup_max']}x)" if d else ""))
    if a.only:
        return
    out = ROOT / "bench"
    (out / "index_ablation.json").write_text(json.dumps({"sizes": sizes, "rule": RULE, "rounds": a.rounds,
                                                         "candidates": CANDIDATES, "rows": rows}, indent=1))
    lines = ["# Index evidence (leave-one-out)", "",
             "Dataset: " + ", ".join(f"{v:,} {k}" for k, v in sizes.items()) + ". Each workload is timed with every candidate "
             f"index present and again with only that index removed, in {a.rounds} rounds that alternate which side runs first. "
             "Within a round each figure is the median of warm runs (routines and triggers exclude their setup); the table shows the "
             "median across rounds and the lowest and highest speedup seen in any round. 'Used' means the index appears in the actual "
             "plan (EXPLAIN ANALYZE, or auto_explain for statements run inside routines and triggers).", "",
             f"Decision rule (computed by the script): {RULE}.", "", "## Verdicts", ""]
    for index, d in verdict.items():
        if d:
            lines.append(f"- **{index}: kept.** Deciding workload: {d['workload']}, {d['without_ms']} -> {d['with_ms']} ms "
                         f"(speedup {d['speedup_min']}-{d['speedup_max']}x across rounds).")
        else:
            best = max((r for r in rows if r["index"] == index), key=lambda r: r["without_ms"] - r["with_ms"])
            why = "not used in the plan" if not best["used"] else "the gain was below the rule in at least one round"
            lines.append(f"- **{index}: dropped.** Best workload: {best['workload']}, {best['without_ms']} -> {best['with_ms']} ms "
                         f"(speedup {best['speedup_min']}-{best['speedup_max']}x across rounds); {why}.")
    lines += ["", "## Measurements", "",
              "| Index | Kept | Workload | With (ms) | Without (ms) | Speedup (median) | Speedup range | Used in plan | Passes rule |",
              "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['index']} | {'yes' if r['kept'] else 'no'} | {r['workload']} | {r['with_ms']} | {r['without_ms']} | "
              f"{r['speedup']}x | {r['speedup_min']}-{r['speedup_max']}x | {'yes' if r['used'] else 'no'} | {'yes' if r['passes'] else 'no'} |"
              for r in rows]
    (out / "index_ablation.md").write_text("\n".join(lines) + "\n")
    print("wrote bench/index_ablation.md")


if __name__ == "__main__":
    main()
