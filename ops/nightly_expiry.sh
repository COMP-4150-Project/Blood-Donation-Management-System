#!/usr/bin/env bash
# FR-20 / UC-14: run the nightly expiry job once. Schedule it with ops/crontab.example.
# Connects as the scheduler role vytal_scheduler (SE-02), which may run only this job. Connection comes from the standard
# libpq variables (PGHOST, PGPORT, PGDATABASE); the login and password come from VYTAL_SCHEDULER_USER (default
# vytal_scheduler) and VYTAL_SCHEDULER_PW, the same password setup.sh was given. Without VYTAL_SCHEDULER_PW libpq falls
# back to ~/.pgpass, so the password need not sit in the crontab.
set -euo pipefail
export PGUSER="${VYTAL_SCHEDULER_USER:-vytal_scheduler}"
[ -n "${VYTAL_SCHEDULER_PW:-}" ] && export PGPASSWORD="$VYTAL_SCHEDULER_PW"
# the procedure also RAISEs a NOTICE; keep it out of the log line, but let errors through
expired=$(PGOPTIONS="${PGOPTIONS:-} -c client_min_messages=warning" psql -X -q -At -v ON_ERROR_STOP=1 -c "CALL vytal.run_nightly_expiry()")
echo "$(date -u +%FT%TZ) expired $expired unit(s)"
