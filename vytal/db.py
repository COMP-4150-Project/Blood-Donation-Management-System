"""Database sessions: one transaction per unit of work, parameterized SQL only (SE-03), acting user stamped for the audit log."""
from contextlib import contextmanager

import psycopg


@contextmanager
def session(user_id=None):
    """Commits on success, rolls back on any exception. Connection settings come from libpq (PGHOST, PGDATABASE...)."""
    with psycopg.connect() as con:
        con.execute("SET search_path = vytal")
        if user_id is not None:
            con.execute("SELECT set_config('vytal.user_id', %s, true)", (str(user_id),))
        yield con
