#!/usr/bin/env bash
# Container start: private PostgreSQL 16 (unix socket only), schema + sample data, then the GUI on $PORT.
# The data is made up and is rebuilt on every start, so no disk is needed.
set -euo pipefail
cd "$(dirname "$0")/.."
PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
# VYTAL_PGDATA, not PGDATA: the postgres base image presets PGDATA to a protected volume path
export PGDATA="${VYTAL_PGDATA:-$HOME/pgdata}" PGSOCK="${PGSOCK:-/tmp/vytal-sock}"
rm -rf "$PGDATA"; mkdir -p "$PGSOCK"
"$PGBIN/initdb" -D "$PGDATA" -U postgres --auth=trust -E UTF8 > /dev/null
"$PGBIN/pg_ctl" -D "$PGDATA" -w -l "$PGDATA/pg.log" \
    -o "-c listen_addresses='' -c unix_socket_directories=$PGSOCK -c fsync=off" start > /dev/null
export PGHOST="$PGSOCK" PGUSER=postgres
"$PGBIN/createdb" vytal
PGDATABASE=vytal ./setup.sh
export PGDATABASE=vytal VYTAL_DEMO=1 VYTAL_SECRET="${VYTAL_SECRET:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
export PGUSER=vytal_app PGPASSWORD="${VYTAL_APP_PW:-vytal_app_dev}"
trap '"$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop > /dev/null' EXIT
"${PYTHON:-python3}" -m gunicorn -b "0.0.0.0:${PORT:-7860}" -w 1 --threads 8 --timeout 60 gui.app:app
