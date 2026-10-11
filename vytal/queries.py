"""Named queries from sql/queries.sql, shared by the backend, the tests and the benchmark."""
import re
from dataclasses import dataclass
from pathlib import Path

QUERIES_SQL = Path(__file__).resolve().parents[1] / "sql" / "queries.sql"


@dataclass(frozen=True)
class Query:
    name: str
    title: str
    serves: str
    why: str
    sql: str


def load(path=QUERIES_SQL):
    blocks = re.split(r"^-- name: ", path.read_text(), flags=re.M)[1:]
    out = {}
    for b in blocks:
        name, rest = b.split("\n", 1)
        meta = dict(re.findall(r"^-- (title|serves|why): (.*)$", rest, flags=re.M))
        sql = "\n".join(l for l in rest.splitlines() if not re.match(r"^-- (title|serves|why): ", l)).strip()
        out[name.strip()] = Query(name.strip(), meta["title"], meta["serves"], meta["why"], sql)
    return out
