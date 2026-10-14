#!/usr/bin/env bash
# FR-20 / UC-14: run the nightly expiry job once. Schedule it with ops/crontab.example.
# Connection comes from the standard libpq variables (PGHOST, PGPORT, PGUSER, PGDATABASE).
set -euo pipefail
# the procedure also RAISEs a NOTICE; keep it out of the log line, but let errors through
expired=$(PGOPTIONS="${PGOPTIONS:-} -c client_min_messages=warning" psql -X -q -At -v ON_ERROR_STOP=1 -c "CALL vytal.run_nightly_expiry()")
echo "$(date -u +%FT%TZ) expired $expired unit(s)"
