#!/usr/bin/env bash
# FR-20 / UC-14: run the nightly expiry job once. Schedule it with ops/crontab.example.
# Connection comes from the standard libpq variables (PGHOST, PGPORT, PGUSER, PGDATABASE).
set -euo pipefail
expired=$(psql -X -q -At -v ON_ERROR_STOP=1 -c "SELECT vytal.expire_units()")
echo "$(date -u +%FT%TZ) expired $expired unit(s)"
