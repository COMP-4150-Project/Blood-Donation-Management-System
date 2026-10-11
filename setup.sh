#!/usr/bin/env bash
# Builds the Vytal database from scratch:  ./setup.sh             schema, logic and sample data
#                                          ./setup.sh --no-data   schema and logic only
# Connection comes from the standard libpq variables (PGHOST, PGPORT, PGUSER, PGDATABASE).
# Run as a superuser or as the database owner with CREATEROLE (06_security.sql creates the application roles).
set -euo pipefail
export PGOPTIONS="-c client_min_messages=warning"
cd "$(dirname "$0")"
# DI-06: every session works in UTC
psql -X -q -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN EXECUTE format('ALTER DATABASE %I SET timezone = %L', current_database(), 'UTC'); END \$\$"
for f in sql/0*.sql; do
    if [ "$f" = sql/07_sample_data.sql ] && [ "${1:-}" = --no-data ]; then continue; fi
    psql -X -q -v ON_ERROR_STOP=1 -f "$f" > /dev/null
done
