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
| `sql/02_logic.sql` | Triggers, views and core procedures (eligibility, unit lifecycle, alerts, allocation, lookback, audit) |
| `sql/03_procedures.sql` | Registration, screening, recording donations, testing, donation calls, login lockout |
| `sql/04_indexes.sql` | Performance indexes |
| `sql/05_reference_data.sql` | Blood groups, compatibility, shelf lives, donation intervals, deferral reasons |
| `sql/06_security.sql` | Application and reporting roles (least privilege) |
| `sql/07_sample_data.sql` | Hand-written sample data for every table |
| `sql/queries.sql` | The 13 key queries |
| `demo.py` | Replays Step 2 Scenario 1 (emergency trauma request) through the backend |
| `vytal/` | Python backend (password hashing, sessions, operations used by the GUI) |
| `tests/` | pytest suite (unit, integration, concurrency, security, data integrity) |
| `bench/` | Performance benchmark and its bulk-data generator |
| `ops/` | Runnable nightly expiry and backup scripts, with a crontab example |
| `report/` | Script that builds the Step 4 report |

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
git clone https://github.com/COMP-4150-Project/Blood-Donation-Management-System.git
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
UPDATE donor SET blood_group = 'C+' WHERE user_id = 101;   -- refused: unknown blood group
DELETE FROM facility WHERE facility_id = 1;                -- refused: facility is still referenced
```

Type `\q` to exit.

Every sample account uses the password `Vytal-demo-2026` (stored as a salted scrypt hash). For example: `admin@vytal.example.ca` (Admin), `m.ross@riverside.example.ca` (Staff, Emergency nurse) and `liam.tremblay@mail.ca` (Donor).

---

## Run the tests

From the project folder, with the virtual environment active and the variables from step 3 set:

```bash
pytest -q
```

Expected: **148 passed, 3 skipped** (151 tests in total).

- The tests create and drop their own `vytal_test*` databases, so your `vytal` database is never touched.
- The 3 skipped tests in `tests/test_schema.py` compare the schema with the Step 3 design sources, which are not in this repository. This is expected.

---

## Run the demo

```bash
python demo.py
```

This builds a separate `vytal_demo` database and replays Scenario 1 from the Step 2 specification through the `vytal/` package: login, an Emergency request with an unknown group, a shortfall and low-stock alert, a donor search and call, then the lab confirming A+ and the last unit being reserved. It uses the same superuser connection as `setup.sh` and never touches your `vytal` database.

---

## Stored procedures, nightly job and backups

The stored procedures are written as PostgreSQL functions (`CREATE FUNCTION`) because most of them return a value to the caller, such as the number of units reserved by `allocate()`. PostgreSQL's `CREATE PROCEDURE` cannot return values in the same way.

`expire_units()` is the nightly expiry job (FR-20). The `ops/` folder makes the scheduled jobs runnable:

| Script | What it does |
|---|---|
| `ops/nightly_expiry.sh` | Runs `expire_units()` once and logs how many units expired |
| `ops/backup.sh [dir]` | Daily `pg_dump` (DI-05), then restores it into a scratch database and checks the row counts match |
| `ops/crontab.example` | Cron entries for both jobs; edit the paths and install with `crontab -e` |

```bash
PGDATABASE=vytal ./ops/nightly_expiry.sh
PGDATABASE=vytal ./ops/backup.sh backups
```

If the `pg_cron` extension is installed, the expiry job can be scheduled inside the database instead:

```sql
SELECT cron.schedule('vytal-expiry', '5 0 * * *', 'SELECT vytal.expire_units()');
```

---

## Benchmark (optional)

Measures query and procedure times with and without the indexes on the full-size dataset that Step 2's performance targets assume (10,000 donors, about 54,000 donations and 117,000 units), including PR-05 (expiring 50,000 units). Generating the data takes about 3 minutes; the benchmark takes longer, mostly for the PR-05 run without indexes.

```bash
createdb vytal_bench
PGDATABASE=vytal_bench ./setup.sh --no-data
PGDATABASE=vytal_bench python bench/bulk_data.py --donors 10000 --days 1460 --regular .60 --churn .04
PGDATABASE=vytal_bench python bench/bench.py      # writes bench/results.md
```

---

## Troubleshooting

| Error | Fix |
|---|---|
| `psql: command not found` | Add PostgreSQL's `bin` folder to your PATH (step 1) and open a new terminal. |
| `password authentication failed` | `PGPASSWORD` is wrong or not set in this terminal. |
| `permission denied to create role` | `PGUSER` is not a superuser; use `postgres`. |
| `role "vytal_app" is not permitted to log in` | Re-run `./setup.sh`; it updates the role. |
| `database "vytal" already exists` | Run `dropdb vytal` first, or skip `createdb`. |
| `ModuleNotFoundError: No module named 'psycopg'` | The virtual environment is not active; run the `activate` command from step 2. |
| `./setup.sh: Permission denied` | Run `chmod +x setup.sh` once. |
