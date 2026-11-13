#!/usr/bin/env bash
# Starts the Vytal web GUI (Step 5) on http://127.0.0.1:5050
# Needs the database from ./setup.sh. Connection comes from the standard libpq variables; the app logs in as vytal_app.
#   PGDATABASE=vytal VYTAL_APP_PW=... ./run_gui.sh
set -euo pipefail
cd "$(dirname "$0")"
export PGUSER=vytal_app PGPASSWORD="${VYTAL_APP_PW:-vytal_app_dev}" PGDATABASE="${PGDATABASE:-vytal}"
exec "${PYTHON:-python3}" -m gui.app
