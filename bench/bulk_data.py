"""Bulk data for the efficiency benchmark only; the submitted sample data is sql/07_sample_data.sql.

Usage: PGDATABASE=vytal_bench ./setup.sh --no-data && python bench/bulk_data.py && python bench/bench.py

Replays 18 months of clinic activity in time order through the real tables, so every row passes the same
constraints and triggers as live data (eligibility, expiry, status rules, discards on positive tests).
History is written with explicit timestamps; the few columns the system stamps with now() are backdated in
one final pass. Today's activity (open requests, alerts, a donation call, a lookback) uses the stored procedures.
"""
import argparse
import datetime as dt
import random
import sys
from collections import defaultdict
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vytal.auth import hash_password  # noqa: E402

SIZE = dict(donors=6000, hospitals=8, banks=2, staff=6)
DAYS = 540
DEMO_PASSWORD = "Vytal-demo-2026"
GROUP_SHARE = {"O+": .39, "A+": .36, "B+": .076, "O-": .07, "A-": .06, "AB+": .025, "B-": .014, "AB-": .005}
DEMAND = {"RBC": .80, "PLASMA": .55, "PLT": .70}
AVG_NEED = {"RBC": 15 / 7, "PLASMA": 1.5, "PLT": 1.5}      # mean of the unit counts drawn in request()
URGENCY = (("Routine", .6), ("Urgent", .3), ("Emergency", .1))
FIRST = ["Aarav", "Maya", "Liam", "Noor", "Ethan", "Priya", "Owen", "Sofia", "Lucas", "Amara", "Noah", "Zara",
         "Ravi", "Chloe", "Omar", "Leah", "Jonah", "Ines", "Kai", "Hana", "Mateo", "Elif", "Arjun", "Grace"]
LAST = ["Singh", "Tremblay", "Nguyen", "Patel", "Roy", "Martin", "Ali", "Chen", "Gagnon", "Wilson", "Khan",
        "Brown", "Lee", "Côté", "Shah", "Taylor", "Mehta", "Bouchard", "Park", "Ahmed"]
CITIES = ["Windsor", "Toronto", "London", "Ottawa", "Hamilton", "Kitchener", "Mississauga", "Waterloo"]


def pick(rng, weighted):
    return rng.choices([k for k, _ in weighted], [w for _, w in weighted])[0]


class Seeder:
    def __init__(self, con, seed):
        self.con, self.cur, self.rng = con, con.cursor(), random.Random(seed)
        self.p = SIZE
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.start = self.now - dt.timedelta(days=DAYS)
        self.pw = hash_password(DEMO_PASSWORD)     # one salted hash shared by every seed account (same demo password)
        self.backdate = {"unit": {}, "wastage": {}, "notify": {}}
        self.load_rules()

    def q(self, sql, args=None):
        self.cur.execute(sql, args)
        return self.cur.fetchall() if self.cur.description else None

    def one(self, sql, args=None):
        return self.q(sql, args)[0][0]

    # ------------------------------------------------------------------ reference data is read, never duplicated
    def load_rules(self):
        self.compat = defaultdict(set)
        for comp, rec, don in self.q("SELECT component_code, recipient_group, donor_group FROM compatibility"):
            self.compat[comp, rec].add(don)
        self.universal = {c: {g for g in GROUP_SHARE if all(g in self.compat[c, r] for r in GROUP_SHARE)}
                          for c in ("RBC", "PLASMA", "PLT")}
        self.shelf = dict(self.q("SELECT component_code, shelf_life_days FROM component_type"))
        self.interval = {(t, s): d for t, s, d in self.q("SELECT * FROM donation_interval")}
        self.rule = {s: (a, float(w), float(h)) for s, a, w, h in self.q("SELECT * FROM eligibility_rule")}
        self.tests = [t for (t,) in self.q("SELECT test_code FROM test_type WHERE is_active ORDER BY 1")]
        self.questions = self.q("SELECT question_id, reason_code, defer_on_answer FROM screening_question WHERE is_active")
        self.reason_days = dict(self.q("SELECT reason_code, default_days FROM deferral_reason"))

    # ------------------------------------------------------------------ people and places
    def user(self, email, role, created, by=None):
        return self.one("INSERT INTO user_account (email,password_hash,first_name,last_name,phone,role,created_by,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING user_id",
                        (email, self.pw, self.rng.choice(FIRST), self.rng.choice(LAST),
                         f"519-555-{self.rng.randint(1000, 9999)}", role, by, created))

    def build_org(self):
        t0 = self.start - dt.timedelta(days=60)
        self.admin = self.user("admin@vytal.ca", "Admin", t0)
        self.q("INSERT INTO admin VALUES (%s)", (self.admin,))
        self.q("SELECT set_config('vytal.user_id', %s, false)", (str(self.admin),))
        self.facilities, self.hospitals, self.staff = [], [], defaultdict(list)
        for i in range(self.p["hospitals"] + self.p["banks"]):
            kind = "Hospital" if i < self.p["hospitals"] else "Blood Bank"
            city = CITIES[i % len(CITIES)]
            name = f"{city} {'General Hospital' if kind == 'Hospital' else 'Blood Centre'}" + (f" {i // len(CITIES) + 1}" if i >= len(CITIES) else "")
            fid = self.one("INSERT INTO facility (name,facility_type,address,phone,email) VALUES (%s,%s,%s,%s,%s) RETURNING facility_id",
                           (name, kind, f"{100 + i} Main St, {city}, ON", f"519-555-{2000 + i}", f"desk{i}@vytal.ca"))
            self.facilities.append(fid)
            if kind == "Hospital":
                self.hospitals.append(fid)
            for j in range(self.p["staff"]):
                sid = self.user(f"staff{i}.{j}@vytal.ca", "Staff", t0, self.admin)
                self.q("INSERT INTO staff VALUES (%s,%s,%s)", (sid, fid, "Nurse" if j % 2 else "Phlebotomist"))
                self.staff[fid].append(sid)

    def build_donors(self):
        self.donors = []
        rng = self.rng
        for i in range(self.p["donors"]):
            sex = rng.choice("MF")
            reg = self.start + dt.timedelta(days=rng.uniform(-200, DAYS - 5))
            group = pick(rng, GROUP_SHARE.items())
            age_at_start = rng.uniform(18, 68)
            kind = pick(rng, (("regular", .35), ("occasional", .40), ("once", .15), ("never", .10)))
            if i % 97 == 0:          # a few 16-year-olds and under-weight sign-ups that the eligibility view must explain
                age_at_start, kind = 16.4 - DAYS / 365.25, "never"
            weight = round(rng.uniform(52, 110), 1) if i % 89 else 47.5
            if weight < 50:
                kind = "never"
            dob = (self.start - dt.timedelta(days=age_at_start * 365.25)).date()
            uid = self.user(f"donor{i}@vytal.ca", "Donor", reg)
            self.q("INSERT INTO donor (user_id,donor_code,date_of_birth,sex,weight_kg,address,blood_group) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                   (uid, f"D-{uid:06d}", dob, sex, weight, f"{rng.randint(1, 999)} King St, {rng.choice(CITIES)}, ON",
                    group if kind != "never" or rng.random() < .5 else None))
            first_visit = max(reg, self.start) + dt.timedelta(days=rng.uniform(1, 60))
            home = rng.choice(self.facilities) if rng.random() < .15 else rng.choice(self.hospitals)
            self.donors.append(dict(id=uid, sex=sex, group=group, kind=kind, home=home,
                                    next=first_visit if kind != "never" else None, verified=False, flagged=False))

    # ------------------------------------------------------------------ one clinic visit
    def visit(self, d, ts):
        rng, fac = self.rng, d["home"]
        staff = rng.choice(self.staff[fac])
        sid = self.one("INSERT INTO screening (donor_id, completed_at) VALUES (%s,%s) RETURNING screening_id", (d["id"], ts))
        defer = None
        if rng.random() < .05:
            defer = rng.choice(["TATTOO", "TRAVEL", "UNWELL"])
        for qid, reason, on in self.questions:
            ans = on if (reason is not None and reason == defer) else (not on if reason else rng.random() < .1)
            self.q("INSERT INTO screening_answer VALUES (%s,%s,%s)", (sid, qid, ans))
        if defer:
            self.defer(d, ts, defer, screening=sid)
            return
        min_hb = self.rule[d["sex"]][2]
        if rng.random() < .03:
            self.defer(d, ts, "LOWHB", staff=staff)
            return
        if not d["verified"]:
            self.q("UPDATE donor SET blood_group=%s, verified_by=%s, verified_at=%s WHERE user_id=%s", (d["group"], staff, ts, d["id"]))
            d["verified"] = True
        dtype = pick(rng, (("WB", .78), ("PLS", .10), ("PLT", .12)))
        dn = self.one("INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
                      "VALUES (%s,%s,%s,%s,%s,%s) RETURNING donation_id",
                      (d["id"], staff, dtype, ts, 450 if dtype == "WB" else 600, round(min_hb + rng.uniform(2, 30), 1)))
        comps = {"WB": ["RBC", "PLASMA"] + (["PLT"] if rng.random() < .5 else []), "PLS": ["PLASMA"], "PLT": ["PLT"]}[dtype]
        tested = ts + dt.timedelta(hours=rng.uniform(6, 20))
        units = [u for (u,) in self.q("SELECT separate_donation(%s, %s)", (dn, comps))]
        positive = rng.random() < .004
        for t in self.tests:
            res = "Positive" if positive and t == "HBV" else "Negative"
            self.q("INSERT INTO test_result VALUES (%s,%s,%s,%s)", (dn, t, res, tested))
        if positive:                                    # trigger already discarded the units and flagged the donor
            self.q("INSERT INTO deferral (donor_id,reason_code,start_date,recorded_by) VALUES (%s,'HBV',%s,%s)", (d["id"], tested.date(), staff))
            for u in units:
                self.backdate["unit"][u] = self.backdate["wastage"][u] = tested
            self.backdate["notify"][dn] = tested
            d.update(flagged=True, next=None)
            return
        self.q("UPDATE blood_unit SET status='Available' WHERE donation_id=%s", (dn,))
        for u, c in zip(units, comps):
            self.stock[fac][c].append(dict(id=u, group=d["group"], exp=ts + dt.timedelta(days=self.shelf[c])))
            self.produced[fac][c][self.day] += 1
            self.backdate["unit"][u] = tested
        gap = self.interval[dtype, d["sex"]]
        if d["kind"] == "once" or rng.random() < .12:
            d["next"] = None
        else:
            extra = rng.uniform(5, 45) if d["kind"] == "regular" else rng.uniform(60, 240)
            d["next"] = ts + dt.timedelta(days=gap + extra)

    def defer(self, d, ts, reason, screening=None, staff=None):
        days = self.reason_days[reason]
        self.q("INSERT INTO deferral (donor_id,reason_code,start_date,end_date,screening_id,recorded_by) VALUES (%s,%s,%s,%s,%s,%s)",
               (d["id"], reason, ts.date(), ts.date() + dt.timedelta(days=days), screening, staff))
        d["next"] = ts + dt.timedelta(days=days + self.rng.uniform(3, 30))

    # ------------------------------------------------------------------ historical requests (same policy as allocate())
    def choose(self, units, comp, rec, urgency, need, issue_at):
        live = [u for u in units if u["exp"] > issue_at]
        ok = (lambda g: g in self.compat[comp, rec]) if rec else (lambda g: g in self.universal[comp])
        cand = [u for u in live if ok(u["group"])]
        others = [u for u in cand if u["group"] != "O-"]
        if comp == "RBC" and rec not in (None, "O-") and urgency != "Emergency" and len(others) >= need:
            cand = others
        cand.sort(key=lambda u: (u["group"] != rec, u["group"] == "O-", u["exp"]))
        return cand[:need]

    def request(self, fac, ts, comp):
        rng = self.rng
        urgency = pick(rng, URGENCY)
        rec = None if urgency == "Emergency" and rng.random() < .3 else pick(rng, GROUP_SHARE.items())
        need = rng.choice([1, 1, 2, 2, 2, 3, 4]) if comp == "RBC" else rng.choice([1, 2])
        staff = rng.choice(self.staff[fac])
        pool = self.stock[fac][comp]
        closed = ts + dt.timedelta(hours={"Emergency": 1, "Urgent": 4, "Routine": 20}[urgency] * rng.uniform(.5, 1.5))
        chosen = self.choose(pool, comp, rec, urgency, need, closed)
        status = "Fulfilled" if len(chosen) == need else "Partially Fulfilled"
        rid = self.one("INSERT INTO blood_request (requested_by,patient_ref,recipient_group,component_code,units_requested,urgency,"
                       "required_by,status,created_at,closed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING request_id",
                       (staff, f"P-{rng.randint(10000, 99999)}", rec, comp, need, urgency,
                        ts + dt.timedelta(hours={"Emergency": 1, "Urgent": 6, "Routine": 48}[urgency]), status, ts, closed))
        for u in chosen:
            pool.remove(u)
            self.q("UPDATE blood_unit SET status='Reserved' WHERE unit_id=%s", (u["id"],))
            self.q("INSERT INTO request_unit VALUES (%s,%s,%s,%s,NULL)", (rid, u["id"], ts + dt.timedelta(minutes=5), closed))
            self.q("UPDATE blood_unit SET status='Issued' WHERE unit_id=%s", (u["id"],))
            self.backdate["unit"][u["id"]] = closed

    # ------------------------------------------------------------------ main replay
    def history(self):
        rng = self.rng
        self.stock = {f: defaultdict(list) for f in self.facilities}
        self.produced = {f: defaultdict(lambda: [0] * DAYS) for f in self.facilities}
        for day in range(DAYS - 2):          # stop two days early so test and closing times never pass now()
            self.day = day
            base = self.start + dt.timedelta(days=day)
            events = [(d["next"], "visit", d) for d in self.donors
                      if d["next"] and base <= d["next"] < base + dt.timedelta(days=1)]
            # each hospital uses roughly what it collected over the last four weeks (DEMAND = share of supply)
            for fac in self.hospitals:
                for comp, share in DEMAND.items():
                    window = self.produced[fac][comp][max(0, day - 28):day]
                    lam = share * sum(window) / max(len(window), 7) / AVG_NEED[comp]
                    for _ in range(self.poisson(lam)):
                        events.append((base + dt.timedelta(hours=rng.uniform(0, 24)), comp, fac))
            for ts, kind, who in sorted(events, key=lambda e: e[0]):
                if kind == "visit":
                    self.visit(who, ts)
                else:
                    self.request(who, ts, kind)
            for fac in self.facilities:      # drop units that expired today from the simulated shelf
                for comp in self.stock[fac]:
                    self.stock[fac][comp] = [u for u in self.stock[fac][comp] if u["exp"] > base + dt.timedelta(days=1)]

    def poisson(self, lam):
        n, p, t = 0, 1.0, pow(2.718281828459045, -lam)
        while True:
            p *= self.rng.random()
            if p <= t:
                return n
            n += 1

    # ------------------------------------------------------------------ today, through the stored procedures
    def today(self):
        rng = self.rng
        n_expired = self.one("SELECT expire_units()")
        for (u,) in self.q("SELECT unit_id FROM blood_unit WHERE status='Expired'"):
            self.backdate["unit"][u] = self.backdate["wastage"][u] = None    # None = use the unit's own expiry time
        self.apply_backdates()                 # before today's actions, which keep their real now() stamps
        mins = {"O+": 8, "A+": 7, "O-": 4, "B+": 2, "A-": 2, "AB+": 1, "B-": 1, "AB-": 1}
        for fac in self.hospitals:
            for g, m in mins.items():
                self.q("INSERT INTO stock_threshold VALUES (%s,%s,'RBC',%s)", (fac, g, m))
            self.q("INSERT INTO stock_threshold VALUES (%s,'O+','PLT',2),(%s,'A+','PLT',2)", (fac, fac))
        open_reqs = []
        for fac in self.hospitals:
            for rec, comp, n, urg, hrs in ((None, "RBC", 4, "Emergency", 1), ("A+", "RBC", 3, "Urgent", 6),
                                           ("O+", "PLASMA", 2, "Routine", 48), ("AB-", "RBC", 2, "Routine", 72)):
                rid = self.one("INSERT INTO blood_request (requested_by,patient_ref,recipient_group,component_code,units_requested,"
                               "urgency,required_by) VALUES (%s,%s,%s,%s,%s,%s,now() + make_interval(hours => %s)) RETURNING request_id",
                               (rng.choice(self.staff[fac]), f"P-{rng.randint(10000, 99999)}", rec, comp, n, urg, hrs))
                self.one("SELECT allocate(%s)", (rid,))
                open_reqs.append((rid, fac))
        short = self.q("SELECT request_id FROM v_request_status s JOIN blood_request r USING (request_id) "
                       "WHERE r.status='Open' AND s.shortfall > 0 ORDER BY r.urgency='Emergency' DESC, request_id")
        if short:
            rid = short[0][0]
            fac = dict(open_reqs)[rid]
            call = self.one("SELECT send_donation_call(%s,%s,%s)",
                            (rid, self.staff[fac][0], "A patient near you needs blood. Please book a donation if you can."))
            for i, (donor,) in enumerate(self.q("SELECT donor_id FROM call_recipient WHERE call_id=%s ORDER BY donor_id LIMIT 6", (call,))):
                self.q("SELECT respond_to_call(%s,%s,%s)", (call, donor, i % 3 != 2))
        flagged = self.q("SELECT o.user_id FROM donor o WHERE is_flagged AND EXISTS (SELECT 1 FROM v_blood_unit u "
                         "WHERE u.donor_id = o.user_id AND u.status IN ('Available','Reserved')) ORDER BY 1 LIMIT 1")
        if flagged:
            self.one("SELECT quarantine_donor(%s)", (flagged[0][0],))
        return n_expired

    def apply_backdates(self):
        # timestamp-only corrections; triggers are paused so the audit log is not filled with seed noise
        self.q("SET LOCAL session_replication_role = replica")
        self.cur.executemany("UPDATE blood_unit SET status_changed_at = coalesce(%s, expires_at) WHERE unit_id = %s",
                             [(t, u) for u, t in self.backdate["unit"].items()])
        self.cur.executemany("UPDATE wastage_record w SET wasted_at = coalesce(%s, u.expires_at) FROM blood_unit u "
                             "WHERE u.unit_id = w.unit_id AND w.unit_id = %s",
                             [(t, u) for u, t in self.backdate["wastage"].items()])
        self.cur.executemany("UPDATE notification SET created_at = %s WHERE message LIKE %s",
                             [(t, f"Donation {dn} failed%") for dn, t in self.backdate["notify"].items()])
        self.q("SET LOCAL session_replication_role = origin")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=4150)
    a = ap.parse_args()
    with psycopg.connect(autocommit=False) as con:
        con.execute("SET search_path = vytal")
        s = Seeder(con, a.seed)
        # history is loaded like a data migration: the audit log starts with today's live actions
        con.execute("ALTER TABLE donor DISABLE TRIGGER audit_donor; ALTER TABLE deferral DISABLE TRIGGER audit_deferral; "
                    "ALTER TABLE blood_unit DISABLE TRIGGER audit_unit; ALTER TABLE blood_request DISABLE TRIGGER audit_request")
        s.build_org()
        s.build_donors()
        s.history()
        con.execute("ALTER TABLE donor ENABLE TRIGGER audit_donor; ALTER TABLE deferral ENABLE TRIGGER audit_deferral; "
                    "ALTER TABLE blood_unit ENABLE TRIGGER audit_unit; ALTER TABLE blood_request ENABLE TRIGGER audit_request")
        expired = s.today()
        con.commit()
        counts = s.q("SELECT 'donors', count(*) FROM donor UNION ALL SELECT 'donations', count(*) FROM donation "
                     "UNION ALL SELECT 'units', count(*) FROM blood_unit UNION ALL SELECT 'requests', count(*) FROM blood_request")
    print(f"bulk data (seed {a.seed}):", ", ".join(f"{k} {v}" for k, v in counts), f"| expired today {expired}")


if __name__ == "__main__":
    main()
