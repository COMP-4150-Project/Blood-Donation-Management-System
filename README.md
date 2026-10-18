# Vytal: Donor Eligibility and Blood Request Management System

COMP-4150 group project (University of Windsor). Vytal is a database-driven platform that connects blood donors, hospitals and blood inventory: donor registration and eligibility, blood type compatibility, hospital requests matched to eligible donors and stock, and low-stock alerts.

**Stack:** PostgreSQL 16 (database, triggers, stored procedures) and Python 3 (backend and tests).

| Team member | Role |
|---|---|
| Jainam Jaykumar Sheth | Systems Analyst |
| Mohammad Imaad | Data Modeling Specialist |
| Aakanksha Mandal | Backend Engineer |
| Shruti Dadhania | UX & Integration Specialist |

---

## Project structure

| Path | What it contains |
|---|---|
| `setup.sh` | Builds the database from scratch (`./setup.sh`, or `./setup.sh --no-data` for an empty schema) |
| `sql/01_schema.sql` | Tables, keys and constraints (generated from the Step 3 design) |
| `sql/02_logic.sql` | Triggers, views, the eligibility function and core procedures (unit lifecycle, alerts, allocation, lookback, audit) |
| `sql/03_procedures.sql` | Use-case routines: registration, screening, deferrals, donations, testing, discards, thresholds, donation calls, account and facility management, login lockout, the nightly expiry procedure |
| `sql/03_sessions.sql` | SE-06 sessions: hashed tokens and idle timeouts |
| `sql/04_indexes.sql` | Performance indexes, each backed by `bench/index_ablation.md` |
| `sql/05_reference_data.sql` | Blood groups, compatibility, shelf lives, donation intervals, deferral reasons |
| `sql/06_security.sql` | Owner, application and reporting roles: the app can only read and call procedures (least privilege, with passwords) |
| `sql/07_sample_data.sql` | Hand-written sample data for every table: three hospitals and a blood centre, ten months of activity, and Step 2 Scenarios 1 to 5 as they stand today |
| `sql/queries.sql` | The 13 key queries |
| `demo.py` | Replays Step 2 Scenario 1 (emergency trauma request) through the backend |
| `vytal/` | Python backend (password hashing, sessions, operations used by the GUI) |
| `tests/` | pytest suite (unit, integration, concurrency, security, data integrity) |
| `bench/` | Performance benchmark (`bench.py`), per-index evidence (`index_ablation.py`) and the bulk-data generator |
| `ops/` | Runnable nightly expiry and backup scripts, with a crontab example |

---

## Setup

Takes about 15 minutes the first time.

### 1. Install PostgreSQL 16

| OS | How |
|---|---|
| **Windows** | Download the installer from [postgresql.org/download/windows](https://www.postgresql.org/download/windows/). Keep port `5432`, remember the `postgres` password, and tick **Command Line Tools**. |
| **macOS** | `brew install postgresql@16 && brew services start postgresql@16` |
| **Ubuntu / Debian** | `sudo apt install postgresql-16` (the service starts automatically) |

Check it works:

```bash
psql --version     # should print 16.x
```

On Windows, if `psql` is not found, add `C:\Program Files\PostgreSQL\16\bin` to your PATH and open a new terminal.

### 2. Install Python 3.10+ and the libraries

```bash
git clone -b step4 https://github.com/COMP-4150-Project/Blood-Donation-Management-System.git
cd Blood-Donation-Management-System
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Set the connection variables

The scripts use the standard PostgreSQL environment variables. Set them in the terminal you will use.

**macOS / Linux**

```bash
export PGHOST=localhost PGPORT=5432 PGUSER=postgres PGPASSWORD=yourpassword
```

**Windows (PowerShell)**

```powershell
$env:PGHOST="localhost"; $env:PGPORT="5432"; $env:PGUSER="postgres"; $env:PGPASSWORD="yourpassword"
```

Notes:

- **Ubuntu:** the `postgres` user has no password by default. Set one first:
  `sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'yourpassword';"`
- **macOS (Homebrew):** use your Mac username as `PGUSER` and leave out `PGPASSWORD`.
- The user must be a **superuser**, because setup creates two database roles.

### 4. Build the database with sample data

**macOS / Linux / Git Bash**

```bash
createdb vytal
PGDATABASE=vytal ./setup.sh
```

**Windows (PowerShell, without Git Bash)**

```powershell
createdb vytal
$env:PGDATABASE="vytal"
psql -c "ALTER DATABASE vytal SET timezone='UTC'"
foreach ($f in Get-ChildItem sql\0*.sql | Sort-Object Name) { psql -v ON_ERROR_STOP=1 -f $f.FullName }
```

No output means it worked. To start over, run `dropdb vytal` and repeat this step.

Least privilege (SE-04): every routine is owned by `vytal_owner`, a non-superuser role that cannot log in, and every procedure runs as that owner (`SECURITY DEFINER`, fixed `search_path`). The application login `vytal_app` can call procedures but cannot insert, update or delete any table itself. Writes are enforced in the database: every procedure that has an acting user checks that the user is active, holds the right role and, for staff, works at the facility the record belongs to, and a connection with no acting user is refused. Reads are not row restricted in the database (there is no row-level security): vytal_app has table SELECT, so which donors or facility records a user may see is scoped by the Python backend (`vytal/services.py`). The sensitive columns are restricted in the database, though: the app cannot read password hashes, lockout counters, contact details, screening answers, audit values or session token hashes (SE-07), and login, the user's own profile, screening answers (to the donor or clinic staff) and the audit trail (to an Admin) come through scoped functions. Donation calls use a fixed message template, so they can never carry health information. The database trusts the acting user that the backend stamps into `vytal.user_id` after login; that is the trust boundary between the web tier and the database, and a stated limitation: a compromised backend could read any non-sensitive row. PostgreSQL row-level security with one database login per user is the production hardening step.

Setup gives the three database roles, `vytal_app` (the application), `vytal_report` (read-only reporting) and `vytal_scheduler` (runs only the nightly expiry job), development passwords. To choose your own, set them before running setup: `VYTAL_APP_PW=... VYTAL_REPORT_PW=... VYTAL_SCHEDULER_PW=... ./setup.sh` (PowerShell: add `-v app_pw=... -v report_pw=... -v scheduler_pw=...` to the `psql` command).

---

## Explore the data

```bash
psql -d vytal
```

```sql
SET search_path = vytal;

SELECT * FROM v_donor_eligibility LIMIT 10;   -- donors and their eligibility status
SELECT * FROM v_inventory;                     -- stock per facility, group and component
SELECT * FROM v_request_status;                -- reserved units and shortfall per request
\i sql/queries.sql                             -- run all 13 key queries
```

The database enforces the rules itself. Try breaking one:

```sql
UPDATE donor SET blood_group = 'C+' WHERE user_id = 101;   -- refused: only staff can change a verified blood group (FR-03)
UPDATE blood_unit SET status = 'Issued' WHERE status = 'Available';  -- refused: Available -> Issued skips Reserved (Table 1)
DELETE FROM facility WHERE facility_id = 1;                -- refused: facility is still referenced
DELETE FROM compatibility WHERE recipient_group = 'O-' AND component_code = 'RBC';  -- refused: O- patients would have no donor group
```

Type `\q` to exit.

Every sample account uses the password `Vytal-demo-2026` (stored as a salted bcrypt hash). For example: `admin@vytal.example.ca` (Admin), `m.ross@riverside.example.ca` (Staff, Emergency nurse) and `liam.tremblay@mail.ca` (Donor).

The sample data follows the Step 2 scenarios at today's date: TR-2291 (request 23) holds the only 3 O- units with a shortfall of 1 and 14 O- donors called, two of whom accepted (Scenario 1); Priya (D-1072) is deferred for a recent tattoo (Scenario 2); Marcus's (D-1073) bag from yesterday went on the shelf this morning and cleared Lakeshore's O+ alert (Scenario 3); two of Riverside's four A+ platelet units expired and the minimum was lowered (Scenario 4); and D-1042's lookback quarantined 3 units and re-allocated request 31 (Scenario 5). `tests/test_sample_data.py` checks each scenario's numbers.

---

## Run the tests

From the project folder, with the virtual environment active and the variables from step 3 set:

```bash
pytest -q
```

Expected: **347 passed** (all tests run; nothing is skipped).

- The tests create and drop their own `vytal_test*` databases, so your `vytal` database is never touched.
- Two security tests log in for real as `vytal_app` and `vytal_scheduler`. They connect to `PGHOST`/`PGPORT` and use the role passwords setup gave (`VYTAL_APP_PW` / `VYTAL_SCHEDULER_PW`, or the development defaults), so they pass with password authentication as well as with trust authentication. Export the same `VYTAL_*_PW` values for `pytest` that you gave `setup.sh`.

**Sessions (SE-06).** `vytal.services.sign_in()` checks the password and opens a server-side session: the caller gets a random token and the database keeps only its SHA-256 hash in `user_session`. Every request calls `session_user(token)`, which refreshes the idle window or refuses the token once it has lapsed; `sign_out(token)` ends it at once. The timeouts (15 minutes for staff and Admins, 30 for donors) live in one place, `session_timeout()` in `sql/03_sessions.sql`. `tests/test_sessions.py` is the session test.

---

## Run the demo

```bash
python demo.py
```

This builds a separate `vytal_demo` database and replays Scenario 1 from the Step 2 specification through the `vytal/` package: Maya's Emergency request TR-2291 for 4 red cell units with an unknown group reserves the only 3 O- units (shortfall 1), O- stock is left at 0, below its threshold of 6, with the low-stock alert open for all staff, the donor search finds 14 eligible O- donors and Maya calls them all, the lab confirms A+ so the last unit comes from A+ stock, two donors accept, and the request is fulfilled. Each step checks the scenario's numbers and stops with an error if one differs. It uses the same superuser connection as `setup.sh` and never touches your `vytal` database.

---

## Stored procedures, nightly job and backups

Every command that changes data is a stored procedure (`CREATE PROCEDURE`, run with `CALL`); results come back through `INOUT` parameters, for example `CALL vytal.allocate(23, NULL)` returns the number of units it reserved. Functions (`CREATE FUNCTION`, run with `SELECT`) are kept for triggers, read-only calculations used inside queries such as `donor_eligibility()` and `eligible_donors()`, and small helpers.

`run_nightly_expiry()` is the nightly expiry job (FR-20): `CALL vytal.run_nightly_expiry();` expires every unit past its date and returns how many. It runs only for an Admin session or the `vytal_scheduler` login, so an ordinary app session cannot trigger it. The `ops/` folder makes the scheduled jobs runnable:

| Script | What it does |
|---|---|
| `ops/nightly_expiry.sh` | Calls `run_nightly_expiry()` once and logs how many units expired |
| `ops/backup.sh [dir]` | Daily `pg_dump` (DI-05), then restores it into a scratch database and checks the row counts match |
| `ops/crontab.example` | Cron entries for both jobs; edit the paths and install with `crontab -e` |

```bash
PGDATABASE=vytal ./ops/nightly_expiry.sh
PGDATABASE=vytal ./ops/backup.sh backups
```

If the `pg_cron` extension is installed, the expiry job can be scheduled inside the database instead:

```sql
SELECT cron.schedule('vytal-expiry', '5 0 * * *', 'CALL vytal.run_nightly_expiry()');
```

---

## Benchmark (optional)

Measures query and procedure times with and without the indexes on the full-size dataset that Step 2's performance targets assume (10,000 donors, about 54,000 donations and 117,000 units), including PR-05 (expiring 50,000 units). Generating the data takes about 3 minutes; the benchmark takes longer, mostly for the PR-05 run without indexes.

```bash
createdb vytal_bench
PGDATABASE=vytal_bench ./setup.sh --no-data
PGDATABASE=vytal_bench python bench/bulk_data.py --donors 10000 --days 1460 --regular .60 --churn .04
PGDATABASE=vytal_bench python bench/bench.py --out /tmp/vytal_bench_results      # leave out --out to overwrite bench/results.*
PGDATABASE=vytal_bench python bench/index_ablation.py                            # per-index evidence, writes bench/index_ablation.*
```

The benchmark needs the full-size data above; a smaller run does not have enough stock for the PR-02 allocation it times.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `psql: command not found` | Add PostgreSQL's `bin` folder to your PATH (step 1) and open a new terminal. |
| `password authentication failed` | `PGPASSWORD` is wrong or not set in this terminal. |
| `permission denied to create role` | `PGUSER` is not a superuser; use `postgres`. |
| `database "vytal" already exists` | Run `dropdb vytal` first, or skip `createdb`. |
| `ModuleNotFoundError: No module named 'psycopg'` | The virtual environment is not active; run the `activate` command from step 2. |
| `./setup.sh: Permission denied` | Run `chmod +x setup.sh` once. |
