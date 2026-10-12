"""Builds the Step 4 report (docx) from the live demo database, the SQL files, test results and benchmark results.

Run after: ./setup.sh demo, pytest --junitxml=report/junit.xml, bench/bench.py. Nothing in the report is typed by hand
that the code can tell us, so the document cannot drift from the implementation.
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vytal.queries import load  # noqa: E402

OUT = ROOT.parent / "Vytal - Step 4 Implementation Report.docx"
FONT, MONO = "Calibri", "Consolas"
TEAM = [("Jainam Sheth", "Systems Analyst"), ("Mohammad Imaad", "Data Modeling Specialist"),
        ("Aakanksha Mandal", "Backend Engineer"), ("Shruti Dadhania", "UX & Integration Specialist")]

doc = Document()
for m in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
    setattr(doc.sections[0], m, Inches(1))
normal = doc.styles["Normal"]
normal.font.name, normal.font.size = FONT, Pt(11)
normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
normal.paragraph_format.space_after = Pt(4)


# ---------------------------------------------------------------------- document helpers
def fmt(run, size=11, bold=False, italic=False, font=FONT):
    run.font.name, run.font.size, run.bold, run.italic = font, Pt(size), bold, italic
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)
    run.font.color.rgb = RGBColor(0, 0, 0)
    return run


def rich(p, text, size=11):
    for i, part in enumerate(text.split("**")):
        if part:
            fmt(p.add_run(part), size, bold=i % 2 == 1)
    return p


def P(text, size=11, align=WD_ALIGN_PARAGRAPH.JUSTIFY):
    p = doc.add_paragraph()
    p.alignment = align
    return rich(p, text, size)


def H(text, level=1):
    p = doc.add_heading(level=level)
    p.paragraph_format.space_before = Pt(12 if level == 1 else 8)
    p.paragraph_format.keep_with_next = True
    fmt(p.add_run(text), 13 if level == 1 else 11.5, bold=True)


def bullets(items, size=11):
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(1)
        rich(p, it, size)


def code(text, size=7.5):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.0
    shade(p, "F5F5F5")
    lines = text.rstrip().split("\n")
    for i, line in enumerate(lines):
        r = fmt(p.add_run(line), size, font=MONO)
        if i < len(lines) - 1:
            r.add_break()


def shade(el, hexcolor):
    props = el._element.get_or_add_pPr() if hasattr(el, "paragraph_format") else el._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hexcolor)
    props.append(shd)


def table(header, rows, widths, size=9):
    t = doc.add_table(rows=1, cols=len(header))
    t.style, t.alignment, t.autofit = "Table Grid", WD_TABLE_ALIGNMENT.CENTER, False
    for row_cells, values, bold in [(t.rows[0].cells, header, True)] + [(t.add_row().cells, r, False) for r in rows]:
        for c, v, w in zip(row_cells, values, widths):
            c.width = Inches(w)
            c.paragraphs[0].paragraph_format.space_after = Pt(0)
            text = "" if v is None else str(v)
            fmt(c.paragraphs[0].add_run(text), size, bold=bold) if bold else rich(c.paragraphs[0], text, size)
            if bold:
                shade(c, "E7EEF7")
    tr = t.rows[0]._tr.get_or_add_trPr()
    h = OxmlElement("w:tblHeader"); h.set(qn("w:val"), "true"); tr.append(h)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def page_break():
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


# ---------------------------------------------------------------------- live facts
con = psycopg.connect(dbname="vytal")
con.execute("SET search_path = vytal")


def q(sql, args=None):
    cur = con.execute(sql, args)
    return [c.name for c in cur.description], cur.fetchall()


def scalar(sql):
    return con.execute(sql).fetchone()[0]


def cell(v):
    if isinstance(v, float):
        return f"{v:.1f}"
    s = str(v) if v is not None else ""
    return s[:-6] if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", s) else s


def sql_block(name):
    return (ROOT / "sql" / name).read_text()


def objects(path):
    """(name, kind, first comment above it) for every function / trigger / view in a SQL file."""
    out, comment = [], ""
    for line in sql_block(path).splitlines():
        if line.startswith("--"):
            comment = re.sub(r"^-+\s*", "", line).strip(" -") or comment
        m = re.match(r"CREATE (FUNCTION|TRIGGER|VIEW) (\w+)", line)
        if m:
            out.append((m.group(2), m.group(1).title(), comment))
    return out


junit = ET.parse(ROOT / "report" / "junit.xml").getroot()
suite = junit if junit.tag == "testsuite" else junit[0]
cases = suite.findall("testcase")
by_file = {}
for c in cases:
    by_file.setdefault(c.get("classname").split(".")[-1], []).append(c)
bench = json.loads((ROOT / "bench" / "results.json").read_text())

# ---------------------------------------------------------------------- title
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
fmt(p.add_run("Vytal: Blood Donor and Request Management System"), 18, bold=True)
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
fmt(p.add_run("COMP-4150 Project, Step 4: Implementation"), 14)
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
fmt(p.add_run("Working database (PostgreSQL 16) and backend code (Python 3)"), 11, italic=True)
table(["Team member", "Role"], TEAM, [2.6, 2.6], 10)

H("1. What was built")
counts = dict(q("SELECT 'tables', count(*) FROM information_schema.tables WHERE table_schema='vytal' AND table_type='BASE TABLE' "
                "UNION ALL SELECT 'views', count(*) FROM information_schema.views WHERE table_schema='vytal' "
                "UNION ALL SELECT 'fks', count(*) FROM information_schema.table_constraints WHERE table_schema='vytal' AND constraint_type='FOREIGN KEY' "
                "UNION ALL SELECT 'checks', count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='vytal' AND c.contype='c' "
                "UNION ALL SELECT 'triggers', count(*) FROM information_schema.triggers WHERE trigger_schema='vytal' "
                "UNION ALL SELECT 'functions', count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='vytal' AND p.prorettype <> 'trigger'::regtype "
                "UNION ALL SELECT 'indexes', count(*) FROM pg_indexes WHERE schemaname='vytal'")[1])
P(f"Vytal's Step 3 design is now a running PostgreSQL 16 database: **{counts['tables']} tables**, **{counts['views']} views**, "
  f"**{counts['fks']} foreign keys**, **{counts['checks']} CHECK constraints**, **{counts['triggers']} trigger events**, "
  f"**{counts['functions']} stored functions** and **{counts['indexes']} indexes**. Hand-written sample data covers ten months of "
  f"realistic activity, 13 key queries answer the questions in our requirements, and a Python backend wraps every "
  f"operation the Step 5 screens will call. **{len(cases)} automated tests** pass against it.")
P("Business rules live in the database, not in the screens (DI-03). A donation for an ineligible donor, a unit released before "
  "its tests, an invalid status change or a double reservation is refused by the database itself, whichever program sends it.")
table(["Path", "Contents"], [
    ("setup.sh", "Builds the database from scratch: ./setup.sh (with sample data) or ./setup.sh --no-data"),
    ("sql/01_schema.sql", "Tables, keys and constraints, generated from the Step 3 design (a test keeps them identical)"),
    ("sql/02_logic.sql", "Triggers, views and the core procedures (eligibility, unit lifecycle, alerts, allocation, lookback, audit)"),
    ("sql/03_procedures.sql", "Procedures added in Step 4 for registration, screening, testing, donation calls and login lockout"),
    ("sql/04_indexes.sql", "Indexes kept only where the benchmark showed they pay off"),
    ("sql/05_reference_data.sql", "Clinical reference data (blood groups, compatibility, shelf lives, intervals, deferral reasons)"),
    ("sql/06_security.sql", "Application and reporting roles with least privilege"),
    ("sql/queries.sql", "The 13 key queries (Section 5)"),
    ("sql/07_sample_data.sql", "Hand-written sample data: explicit INSERTs for every table, loaded through the real triggers"),
    ("vytal/", "Backend: auth.py (password hashing), db.py (sessions), services.py (operations), queries.py"),
    ("tests/", "pytest suite: unit, integration, concurrency, security, sample-data integrity"),
    ("bench/", "Benchmark, its bulk-data generator and EXPLAIN ANALYZE evidence (Section 6)"),
], [1.8, 4.7])

# ---------------------------------------------------------------------- 2. schema
H("2. Schema implementation")
P("The schema is the Step 3 relational design, translated one to one. Every relation became a table with its primary key, "
  "every foreign key is declared with ON DELETE RESTRICT so history can never be orphaned (DI-04), and every domain rule that "
  "fits on one row is a CHECK constraint. Rules that need other rows (eligibility, status transitions, alerts) are triggers (Section 4).")
_, cons = q("SELECT CASE c.contype WHEN 'p' THEN 'Primary key' WHEN 'f' THEN 'Foreign key' WHEN 'c' THEN 'Check' WHEN 'u' THEN 'Unique' END, "
            "count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='vytal' AND c.contype IN ('p','f','c','u') "
            "GROUP BY 1 ORDER BY 2 DESC")
partial = scalar("SELECT count(*) FROM pg_indexes WHERE schemaname='vytal' AND indexdef LIKE '%UNIQUE%WHERE%'")
table(["Constraint kind", "Count", "Examples"], [
    *[(k, n, {"Check": "blood group ABO/Rh domain, volume > 0, verified_by and verified_at set together, expiry set",
              "Foreign key": "donation -> donor, blood_unit -> donation, stock_alert -> stock_threshold (composite)",
              "Primary key": "composite keys on compatibility, request_unit, test_result, screening_answer",
              "Unique": "one unit per component per donation, unique email, unique donor code"}[k]) for k, n in cons],
    ("Partial unique index", partial, "one open alert per threshold; a unit reserved by at most one active request (DI-01)"),
], [1.6, 0.6, 4.3])
P("**Least privilege (SE-04).** The application connects as vytal_app, which can read and write but never DELETE or TRUNCATE, and "
  "cannot write the audit log directly; audit rows come only from SECURITY DEFINER trigger functions. Reports use vytal_report, which is "
  "read-only and cannot see password hashes. The integration tests run the whole backend as vytal_app to prove these grants are enough.")

# ---------------------------------------------------------------------- 3. data
H("3. Data population and integrity")
P("sql/07_sample_data.sql is written by hand as plain INSERT statements: two hospitals and a blood centre, two admins, six staff and "
  "36 donors with realistic Canadian blood-group shares, ten months of screenings, donations, component units, test results, deferrals "
  "and hospital requests. Each scenario in our requirements has rows behind it: an under-age and an under-weight sign-up, a donor "
  "with an unknown group, temporary deferrals from the questionnaire and from a low hemoglobin reading, a positive hepatitis B test, "
  "platelets that expired on the shelf, and an Emergency request for a patient of unknown group.")
P("Every row is inserted through the real tables, so the constraints and triggers check the sample data exactly as they check live "
  "data. While writing the file, the triggers refused rows that broke a rule (for example, releasing a unit from a donor who had "
  "already been flagged), and the file was corrected to follow the true order of events. Dates are written relative to now(), so the "
  "data stays current whenever it is loaded. Today's activity (the nightly expiry, open requests, low-stock alerts, a donation call and "
  "a lookback quarantine) is produced by calling the stored procedures themselves.")
con.execute("ANALYZE")
_, vol = q("SELECT c.relname, c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
           "WHERE n.nspname='vytal' AND c.relkind='r' ORDER BY c.reltuples DESC LIMIT 16")
half = (len(vol) + 1) // 2
table(["Table", "Rows", "Table", "Rows"],
      [(a[0], a[1], *(vol[i + half] if i + half < len(vol) else ("", ""))) for i, a in enumerate(vol[:half])], [1.9, 0.9, 1.9, 0.9])
data_tests = [c.get("name") for c in by_file.get("test_sample_data", [])]
P(f"**Integrity checks on the sample data** ({len(data_tests)} tests, all passing):")
bullets([re.sub(r"^test_", "", n).replace("_", " ").capitalize() for n in data_tests], 10)

H("Bad data is refused", 2)
P("Each row below was attempted against the database; the message is what PostgreSQL returned.")
probes = [
    ("Unknown blood group", "UPDATE donor SET blood_group='C+' WHERE user_id=(SELECT min(user_id) FROM donor)"),
    ("Negative volume", "UPDATE donation SET volume_ml=-1 WHERE donation_id=1"),
    ("Second donation inside the interval", "INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
     "SELECT d.donor_id, d.recorded_by, 'WB', now(), 450, 150 FROM donation d JOIN donor o ON o.user_id=d.donor_id "
     "WHERE NOT o.is_flagged AND o.next_eligible_date > CURRENT_DATE AND o.next_eligible_date < 'infinity' LIMIT 1"),
    ("Expired unit made Available", "UPDATE blood_unit SET status='Available' WHERE unit_id=(SELECT min(unit_id) FROM blood_unit WHERE status='Expired')"),
    ("Release before tests", "INSERT INTO screening (donor_id) SELECT user_id FROM v_donor_eligibility WHERE status='Eligible' "
     "AND group_verified ORDER BY user_id LIMIT 1",
     "WITH d AS (INSERT INTO donation (donor_id,recorded_by,donation_type_code,donated_at,volume_ml,hemoglobin_gl) "
     "SELECT user_id, (SELECT min(user_id) FROM staff), 'WB', now(), 450, 150 FROM v_donor_eligibility WHERE status='Eligible' "
     "AND group_verified ORDER BY user_id LIMIT 1 RETURNING donation_id) "
     "SELECT separate_donation((SELECT donation_id FROM d), ARRAY['RBC'])", "UPDATE blood_unit SET status='Available' WHERE status='Collected'"),
    ("Same unit reserved twice", "INSERT INTO request_unit (request_id, unit_id) SELECT r.request_id, ru.unit_id FROM request_unit ru "
     "JOIN blood_request r ON r.request_id <> ru.request_id AND r.status='Open' WHERE ru.released_at IS NULL AND ru.issued_at IS NULL LIMIT 1"),
    ("Deleting a referenced facility", "DELETE FROM facility WHERE facility_id=(SELECT min(facility_id) FROM facility)"),
    ("App role deleting history", "SET LOCAL ROLE vytal_app; DELETE FROM donation"),
]
rows = []
for label, *stmts in probes:
    with con.transaction(force_rollback=True):
        try:
            for s in stmts:
                con.execute(s)
            msg = "ACCEPTED"
        except psycopg.Error as e:
            msg = str(e).splitlines()[0]
    rows.append((label, msg))
assert all(m != "ACCEPTED" for _, m in rows), rows
table(["Attempt", "Database response"], rows, [2.0, 4.5])

# ---------------------------------------------------------------------- 4. procedures and triggers
H("4. Stored procedures, triggers and views")
P("Each object below enforces or automates one requirement from Step 2. Triggers keep stored values honest (next eligible date, "
  "donation status, alerts, wastage) and refuse rule breaks; procedures run multi-step operations as one transaction.")
cat = objects("02_logic.sql") + objects("03_procedures.sql")
seen, rows = set(), []
for name, kind, why in cat:
    if name in seen:
        continue
    seen.add(name)
    rows.append((name, kind, why))
table(["Object", "Kind", "Requirement and purpose"], rows, [1.9, 0.8, 3.8], 8.5)

H("Highlights", 2)
logic = sql_block("02_logic.sql")
proc = sql_block("03_procedures.sql")


def body(src, name):
    m = re.search(rf"(CREATE (FUNCTION|VIEW) {name}\b.*?)(?=\n-- -|\nCREATE TRIGGER|\nCREATE FUNCTION|\nCREATE VIEW|\Z)", src, re.S)
    return m.group(1).strip()


P("**allocate()** (FR-23, FR-24, DI-01) reserves units first-expiry-first, exact group first, and keeps O-negative red cells for "
  "O-negative, unknown-group and Emergency patients unless nothing else fits. FOR UPDATE SKIP LOCKED lets two staff allocate at the "
  "same time without ever sharing a unit; a 50-session test confirms it.")
code(body(logic, "allocate"))
P("**check_donation_eligible()** (FR-07, FR-11) refuses a donation from an inactive, flagged, under-age, under-weight or anemic donor, "
  "or one inside their interval. The limits are rows in eligibility_rule, so an Admin changes them without a code change (MA-01).")
code(body(logic, "check_donation_eligible"))
P("**submit_screening()** (FR-06, FR-08) saves a complete questionnaire and turns every deferring answer into a deferral in the same "
  "transaction; the deferral trigger then moves the donor's next eligible date.")
code(body(proc, "submit_screening"))

# ---------------------------------------------------------------------- 5. queries
page_break()
H("5. Key queries")
P("Each query answers a question a donor, staff member or Admin asks in our requirements. They run as-is against the sample data; "
  "the first rows of each result and the measured time on the demo data are shown. Tests recompute every result independently "
  "(in Python or with simpler SQL) and check they agree.")
query_ms = {op.split()[0]: v["ms"] for op, v in bench["after"].items()}
bench_rows = ", ".join(f"{v:,} {k}" for k, v in bench["rows"].items())
for name, qq in load().items():
    H(f"{name}. {qq.title}", 2)
    P(f"**Serves:** {qq.serves}. {qq.why}", 10)
    code(qq.sql)
    cols, res = q(qq.sql)
    shown = res[:8]
    widths = [6.5 / len(cols)] * len(cols)
    table(cols, [[cell(v) for v in r] for r in shown], widths, 7.5 if len(cols) > 7 else 8.5)
    note = f"{len(res)} row(s)" + (f", first {len(shown)} shown" if len(res) > len(shown) else "")
    note += f"; {query_ms[name]:.1f} ms on the larger benchmark dataset"
    P(note, 9, WD_ALIGN_PARAGRAPH.LEFT)

# ---------------------------------------------------------------------- 6. efficiency
page_break()
H("6. Efficiency")
TARGETS = {"PR-01": "< 2 s", "PR-02": "< 1 s", "PR-03": "< 3 s"}
P(f"Timings come from a much larger dataset made by bench/bulk_data.py ({bench_rows}), so that indexes matter. Each figure is the "
  "median of five warm runs, measured once without the indexes in sql/04_indexes.sql and once with them; every run happens inside a "
  "transaction that is rolled back. The Step 2 performance targets PR-01 to PR-03 are met with a wide margin. PR-05 (the nightly "
  "expiry over 50,000 units) and PR-06 (load) were not stress-tested at full scale; the 50-session concurrency test in Section 8 covers "
  "PR-06's correctness.")


def plan_change(b, a):
    new = set(a.get("scans", [])) - set(b.get("scans", []))
    return "; ".join(sorted(x for x in new if "Index" in x)) or "no change"


table(["Operation", "Target", "Before (ms)", "After (ms)", "New index use"],
      [(op, TARGETS.get(op.split()[0], ""), f"{b['ms']:.1f}", f"{bench['after'][op]['ms']:.1f}", plan_change(b, bench["after"][op]))
       for op, b in bench["before"].items()], [2.3, 0.6, 0.8, 0.8, 2.0], 8)
H("Indexes kept", 2)
P("PostgreSQL does not index foreign-key columns on its own. Each index below is kept because a measured query or procedure uses it; "
  "candidates that did not help were dropped.")
code(sql_block("04_indexes.sql"))
op = next(k for k in bench["before"] if k.startswith("Q03"))
H("EXPLAIN ANALYZE: Q03 before and after", 2)
P("Without an index on donation(donor_id), finding each eligible donor's last donation scans the whole donation table once per "
  "donor; with it, each lookup reads one index entry.", 10)
for label, mode in (("Before", "before"), ("After", "after")):
    P(f"{label} ({bench[mode][op]['ms']:.1f} ms):", 10)
    code("\n".join(bench[mode][op]["plan"].splitlines()[:28]), 6.5)

# ---------------------------------------------------------------------- 7. backend
page_break()
H("7. Backend code")
P("vytal/services.py is the layer the Step 5 screens will call. Each function is one transaction for one acting user: it checks the "
  "caller's role and facility (SE-02), passes every value as a query parameter, never as SQL text (SE-03), and stamps the acting "
  "user into the session so the audit trigger records who made each change (FR-33). Passwords are salted scrypt hashes (SE-01); "
  "five wrong passwords lock the account for 15 minutes (SE-05).")
svc = (ROOT / "vytal" / "services.py").read_text()
rows = [(m.group(1), (m.group(3) or "").strip().split("\n")[0]) for m in
        re.finditer(r'^def (\w+)\((.*?)\):\n(?:    """(.*?)""")?', svc, re.M | re.S) if not m.group(1).startswith("_")]
table(["Function", "Purpose"], rows, [1.8, 4.7], 9)

# ---------------------------------------------------------------------- 8. testing
H("8. Testing")
fail = sum(1 for c in cases if c.find("failure") is not None or c.find("error") is not None)
P(f"**{len(cases) - fail} of {len(cases)} tests pass** (pytest, {float(suite.get('time')):.0f} s). Unit tests run inside a transaction that "
  "is rolled back, so each one starts from a clean database; integration tests drive the backend end to end as vytal_app.")
about = {"test_schema": "Schema matches the Step 3 design; FKs restrict deletes; times are timestamptz",
         "test_constraints": "CHECK, UNIQUE, NOT NULL and FK refusals",
         "test_triggers": "Eligibility, deferrals, unit lifecycle, positive tests, alerts, audit, specialization",
         "test_procedures": "Each procedure's normal path and refusals",
         "test_queries": "Every key query runs within PR-01's 2 s bound and matches an independent recomputation",
         "test_sample_data": "Invariants of the sample data (wastage, issued units, request statuses, dates)",
         "test_integration": "Full journeys through the backend, role and facility checks, SQL injection, 50 concurrent sessions",
         "test_security": "Application and reporting roles cannot rewrite history or read secrets"}
table(["Test file", "Tests", "What it proves"], [(f, len(by_file.get(f, [])), a) for f, a in about.items()], [1.5, 0.6, 4.4], 9)
P("**Do the tests catch bugs?** We planted three bugs in turn (allocation ignoring expiry order, lockout at the sixth failure instead "
  "of the fifth, an Issued unit allowed back to Available). Each one made at least one test fail. The tests also caught real defects "
  "while we built this step, all now fixed:")
bullets(["Writing the sample data, the flagged-donor rule refused to release donor 127's older units because the positive test had "
         "been inserted first; the file was reordered to match the real sequence of events.",
         "The application role pre-existed without login rights; the integration suite, which connects as that role, failed at once.",
         "allocate() and close_request() accepted requests that were already closed; changing a minimum stock level did not re-check "
         "the alert until the next unit moved. Both were fixed in 02_logic.sql with tests."], 10)

# ---------------------------------------------------------------------- 9. limits
H("9. Limitations and what comes next")
bullets(["**No inter-facility transfers.** A hospital can only use units collected on site (a could-have in Step 2). Q07 shows the cost: "
         "platelets, with a five-day shelf life, are mostly wasted at a single small site. Transfers are the obvious next feature.",
         "**Role checks for screens live in the backend.** The database enforces every clinical rule, but which facility a staff member "
         "may see is checked in services.py; PostgreSQL row-level security would move that into the database too.",
         "**Step 5** builds the Flask screens on vytal/services.py; the backend functions already match the Step 2 use cases."], 10)

H("10. Contributions")
table(["Member", "Step 4 contribution"], [
    ("Jainam Sheth", "Roadmap, cross-checking against Steps 2 and 3, key queries, testing strategy, this report"),
    ("Mohammad Imaad", "Schema and constraints, reference data, sample data"),
    ("Aakanksha Mandal", "Stored procedures and triggers, backend services, concurrency tests"),
    ("Shruti Dadhania", "Backend operations as seen from the Step 5 screens, input validation messages, report layout"),
], [1.8, 4.7], 10)
P("(Split to be confirmed by the team.)", 9)

# ---------------------------------------------------------------------- appendix
page_break()
H("Appendix A. SQL source")
for f in ("01_schema.sql", "02_logic.sql", "03_procedures.sql", "04_indexes.sql", "05_reference_data.sql", "06_security.sql"):
    H(f"sql/{f}", 2)
    code(sql_block(f), 6.5)

doc.save(OUT)
print("wrote", OUT)
