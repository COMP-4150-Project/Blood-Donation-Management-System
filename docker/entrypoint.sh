#!/usr/bin/env bash
# Container start: private PostgreSQL 16 (unix socket only), schema + sample data, then the GUI on $PORT.
# The data is made up and is rebuilt on every start, so no disk is needed (a restart is a full reseed, see deploy-vps.sh).
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
# The demo is served over HTTPS (Caddy in front, enable-https.sh), so the session cookie is Secure by default.
# A plain-HTTP run (no proxy yet) passes -e VYTAL_SECURE_COOKIE=0, as deploy-vps.sh does before HTTPS is set up.
export VYTAL_SECURE_COOKIE="${VYTAL_SECURE_COOKIE:-1}"
# Behind Caddy the sign-in throttle keys on the address Caddy appends to X-Forwarded-For. It is trusted only when the proxy
# is the sole way in, which is the HTTPS setup; a plain-HTTP run (published port 80) passes -e VYTAL_TRUST_PROXY=0.
export VYTAL_TRUST_PROXY="${VYTAL_TRUST_PROXY:-$VYTAL_SECURE_COOKIE}"
# Hourly expiry job (FR-20, INT-11), as the scheduler login like ops/crontab.example: past-date units turn Expired, the
# wastage is recorded and low-stock alerts and short requests follow. Every VYTAL_EXPIRY_EVERY seconds (default 3600).
(
    while sleep "${VYTAL_EXPIRY_EVERY:-3600}"; do
        VYTAL_SCHEDULER_PW="${VYTAL_SCHEDULER_PW:-vytal_scheduler_dev}" ./ops/nightly_expiry.sh || echo "expiry job failed" >&2
    done
) &
JOBPID=$!
export PGUSER=vytal_app PGPASSWORD="${VYTAL_APP_PW:-vytal_app_dev}"
trap 'kill "$JOBPID" 2>/dev/null || true; "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop > /dev/null' EXIT
# gunicorn runs as a child so `docker stop` / `docker restart` (SIGTERM to this PID 1) shuts it down at once
# instead of waiting for the kill timeout; the EXIT trap then stops PostgreSQL.
"${PYTHON:-python3}" -m gunicorn -b "0.0.0.0:${PORT:-7860}" -w 1 --threads 8 --timeout 60 gui.app:app &
GPID=$!
trap 'kill -TERM "$GPID" 2>/dev/null || true' TERM INT
set +e
wait "$GPID"; rc=$?
if [ "$rc" -gt 128 ]; then wait "$GPID"; rc=$?; fi   # interrupted by the trap: wait for gunicorn to finish
exit "$rc"
