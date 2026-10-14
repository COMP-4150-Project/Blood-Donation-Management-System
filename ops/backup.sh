#!/usr/bin/env bash
# DI-05: daily backup with a restore test.  Usage: ./ops/backup.sh [backup_dir]
# Dumps $PGDATABASE, restores the dump into a scratch database, compares row counts, then drops the scratch copy.
set -euo pipefail
export PGOPTIONS="-c client_min_messages=warning"
db="${PGDATABASE:?set PGDATABASE}"
dir="${1:-backups}"
mkdir -p "$dir"
file="$dir/${db}_$(date -u +%F).dump"
pg_dump -Fc -d "$db" -f "$file"

check="${db}_restore_check"
dropdb --if-exists "$check"
createdb "$check"
pg_restore -d "$check" "$file"
count="SELECT (SELECT count(*) FROM vytal.donation) || '/' || (SELECT count(*) FROM vytal.blood_unit)"
live=$(psql -X -At -d "$db" -c "$count")
restored=$(psql -X -At -d "$check" -c "$count")
dropdb "$check"
if [ "$live" != "$restored" ]; then
    echo "restore check FAILED: live $live, restored $restored (donations/units)" >&2
    exit 1
fi
echo "backup $file OK; restore check matched $live donations/units"
