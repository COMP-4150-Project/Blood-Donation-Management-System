# Vytal GUI (Step 5)

A Flask web app on top of the Step 4 database and `vytal/services.py`. It has three kinds of account, each with its own screens.

| Role | Screens |
|---|---|
| Donor | My donations (eligibility ring, next eligible date, history, donation calls, body size with estimated blood volume), health screening, notifications, profile (contact details, height, weight) |
| Hospital staff | Dashboard (stock matrix, low-stock alerts, request queue), requests (create, reserve, call donors, issue, cancel), inventory (discard, minimums), donors (screen, verify group, record donation, defer), test results, find donors |
| Admin | Overview, accounts (create, deactivate, reset password), facilities (add, edit, deactivate, minimums), clinical rules (compatibility, shelf life, intervals, eligibility minimums for age, weight, hemoglobin and estimated blood volume, deferral reasons), audit log with CSV, lookback trace and quarantine, expiry job |

**Run:** build the database (`./setup.sh`), then `PGDATABASE=vytal ./run_gui.sh` and open http://127.0.0.1:5050. Demo accounts all use `Vytal-demo-2026`: `liam.tremblay@mail.ca` (donor), `m.ross@riverside.example.ca` (staff), `admin@vytal.example.ca` (admin).

**How it maps to the rubric:** Create, read, update and delete (nothing is destroyed: delete is cancel request, discard unit, deactivate account or facility, so the audit trail stays whole); three roles enforced by the database and the app; every screen reads and writes the real PostgreSQL database through stored procedures.

**Requirements covered (Step 2 US-01 to US-08):** an Emergency request takes dashboard, new request, submit (3 screens); donors see eligibility first; status and urgency use text with colour; errors show next to the field; keyboard and contrast friendly; works at 1366x768 and tablet widths; destructive actions ask for confirmation.

Tests: `pytest tests/test_gui.py`. Screenshots: `gui/screenshots/`.
