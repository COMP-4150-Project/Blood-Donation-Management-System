"""Database sessions: one transaction per unit of work, parameterized SQL only (SE-03), acting user stamped for the audit log.

Inside request_scope() (one web request) every session() reuses one connection: each unit of work is still its own
transaction, with the acting user set for that transaction only. Outside a scope each session() opens its own connection.
"""
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg


class _Scope:
    __slots__ = ("con",)

    def __init__(self):
        self.con = None


_scope: ContextVar = ContextVar("vytal_request_scope", default=None)


def _connect(**kw):
    con = psycopg.connect(**kw)
    con.execute("SET search_path = vytal")
    return con


@contextmanager
def request_scope():
    """One connection for everything done until the block ends; it is closed then. A nested scope joins the outer one."""
    if _scope.get() is not None:
        yield
        return
    token = _scope.set(_Scope())
    try:
        yield
    finally:
        holder = _scope.get()
        _scope.reset(token)
        if holder.con is not None:
            holder.con.close()


def current_connection():
    """The request's connection (opened on first use, reopened if the server dropped it), or None outside request_scope()."""
    holder = _scope.get()
    if holder is None:
        return None
    if holder.con is None or holder.con.closed or holder.con.broken:
        if holder.con is not None:
            holder.con.close()
        holder.con = _connect(autocommit=True)          # transactions are opened per unit of work below
    return holder.con


@contextmanager
def session(user_id=None):
    """Commits on success, rolls back on any exception. Connection settings come from libpq (PGHOST, PGDATABASE...)."""
    con = current_connection()
    if con is None:
        with _connect() as con:
            if user_id is not None:
                con.execute("SELECT set_config('vytal.user_id', %s, true)", (str(user_id),))
            yield con
        return
    outer = con.info.transaction_status != psycopg.pq.TransactionStatus.IDLE
    with con.transaction():                             # a savepoint when nested in another session of this request
        before = con.execute("SELECT current_setting('vytal.user_id', true)").fetchone()[0] if outer else None
        con.execute("SELECT set_config('vytal.user_id', %s, true)", ("" if user_id is None else str(user_id),))
        yield con
        if outer:                                       # an error instead rolls the savepoint back, setting included
            con.execute("SELECT set_config('vytal.user_id', %s, true)", (before or "",))
