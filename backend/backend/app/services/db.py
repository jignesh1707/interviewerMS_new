"""Database access for the interview store.

Two backends share one tiny interface:

* ``SqliteDatabase``: a single local file, used for development, tests and single-machine deploys.
* ``PostgresDatabase``: a psycopg connection pool, used in production (for example Supabase through its
  transaction pooler). Tables live in a dedicated schema so they are never exposed by Supabase's
  automatic REST API.

SQL is written once with ``?`` placeholders and unqualified table names; the Postgres backend rewrites
both. Timestamps stay ISO-8601 TEXT and JSON stays TEXT in both backends so the store logic is identical.
"""

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

TABLES = ("interviews", "answers", "reports", "events")
_TABLE_RE = re.compile(r"\b(" + "|".join(TABLES) + r")\b")
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    external_ref TEXT,
    role TEXT NOT NULL,
    candidate_name TEXT,
    status TEXT NOT NULL,
    resume_text TEXT,
    jd_text TEXT,
    resume_summary TEXT,
    jd_summary TEXT,
    match_analysis TEXT,
    questions TEXT,
    config TEXT,
    callback_url TEXT,
    metadata TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS answers (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL,
    question_id TEXT,
    question_index INTEGER NOT NULL,
    question TEXT NOT NULL,
    audio_path TEXT,
    transcript TEXT,
    metrics TEXT,
    heuristic_scores TEXT,
    analysis TEXT,
    router TEXT,
    duration_seconds REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (interview_id) REFERENCES interviews (id)
);

CREATE TABLE IF NOT EXISTS reports (
    interview_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (interview_id) REFERENCES interviews (id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answers_interview ON answers (interview_id, question_index);
CREATE INDEX IF NOT EXISTS idx_answers_question ON answers (interview_id, question_id);
CREATE INDEX IF NOT EXISTS idx_events_interview ON events (interview_id, id);
CREATE INDEX IF NOT EXISTS idx_interviews_tenant ON interviews (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_interviews_external_ref ON interviews (tenant_id, external_ref);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    external_ref TEXT,
    role TEXT NOT NULL,
    candidate_name TEXT,
    status TEXT NOT NULL,
    resume_text TEXT,
    jd_text TEXT,
    resume_summary TEXT,
    jd_summary TEXT,
    match_analysis TEXT,
    questions TEXT,
    config TEXT,
    callback_url TEXT,
    metadata TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS answers (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews (id) ON DELETE CASCADE,
    question_id TEXT,
    question_index INTEGER NOT NULL,
    question TEXT NOT NULL,
    audio_path TEXT,
    transcript TEXT,
    metrics TEXT,
    heuristic_scores TEXT,
    analysis TEXT,
    router TEXT,
    duration_seconds DOUBLE PRECISION,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    interview_id TEXT PRIMARY KEY REFERENCES interviews (id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    interview_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answers_interview ON answers (interview_id, question_index);
CREATE INDEX IF NOT EXISTS idx_answers_question ON answers (interview_id, question_id);
CREATE INDEX IF NOT EXISTS idx_events_interview ON events (interview_id, id);
CREATE INDEX IF NOT EXISTS idx_interviews_tenant ON interviews (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_interviews_external_ref ON interviews (tenant_id, external_ref);

-- Candidate data must never be reachable through Supabase's REST API.
ALTER TABLE interviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
"""


class Tx(Protocol):
    def execute(self, sql: str, params: tuple = ()) -> None: ...
    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]: ...


class Database(Protocol):
    dialect: str

    def ensure_schema(self) -> None: ...
    def execute(self, sql: str, params: tuple = ()) -> None: ...
    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None: ...
    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]: ...
    def transaction(self) -> Any: ...
    def close(self) -> None: ...


class SqliteDatabase:
    dialect = "sqlite"

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")

    def ensure_schema(self) -> None:
        with self._lock:
            self._connection.executescript(SQLITE_SCHEMA)
            for table, column, definition in (
                ("answers", "question_id", "TEXT"),
                ("interviews", "tenant_id", "TEXT"),
                ("interviews", "external_ref", "TEXT"),
            ):
                existing = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._connection.executescript(SQLITE_SCHEMA)  # indexes on columns added above
            self._connection.commit()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._connection.execute(sql, params)
            self._connection.commit()

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, params).fetchall()]

    @contextmanager
    def transaction(self) -> Iterator["_SqliteTx"]:
        with self._lock:
            try:
                yield _SqliteTx(self._connection)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class _SqliteTx:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._connection.execute(sql, params)

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(sql, params).fetchall()]


class PostgresDatabase:
    dialect = "postgres"

    def __init__(self, url: str, *, schema: str = "interviewer", pool_size: int = 5, timeout: float = 10.0) -> None:
        if not _IDENT_RE.match(schema):
            raise ValueError("DB_SCHEMA must be a lowercase identifier")
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.schema = schema
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=max(1, pool_size),
            timeout=timeout,
            max_lifetime=1800,
            max_idle=300,
            check=ConnectionPool.check_connection,
            # prepare_threshold=None: Supabase's transaction pooler (Supavisor) does not support
            # server-side prepared statements.
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 10},
            open=True,
        )

    def _qualify(self, sql: str) -> str:
        return _TABLE_RE.sub(lambda m: f'"{self.schema}".{m.group(1)}', sql.replace("?", "%s"))

    def ensure_schema(self) -> None:
        with self._pool.connection() as conn:
            # CREATE SCHEMA IF NOT EXISTS still demands database-level CREATE privilege, which a locked-down
            # app role does not have, so only create the schema when it is genuinely missing.
            exists = conn.execute(
                "SELECT 1 AS ok FROM information_schema.schemata WHERE schema_name = %s", (self.schema,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE SCHEMA "{self.schema}"')
            for statement in _split_statements(POSTGRES_SCHEMA):
                conn.execute(self._qualify(statement))

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._pool.connection() as conn:
            conn.execute(self._qualify(sql), params)

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            return conn.execute(self._qualify(sql), params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            return list(conn.execute(self._qualify(sql), params).fetchall())

    @contextmanager
    def transaction(self) -> Iterator["_PostgresTx"]:
        with self._pool.connection() as conn:  # commits on success, rolls back on error
            yield _PostgresTx(conn, self._qualify)

    def close(self) -> None:
        self._pool.close()


class _PostgresTx:
    def __init__(self, conn: Any, qualify: Any) -> None:
        self._conn = conn
        self._qualify = qualify

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.execute(self._qualify(sql), params)

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return list(self._conn.execute(self._qualify(sql), params).fetchall())


def _split_statements(script: str) -> list[str]:
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def postgres_schema_sql(schema: str) -> str:
    """Return the DDL an administrator can run once, so the app role needs no CREATE privilege."""
    if not _IDENT_RE.match(schema):
        raise ValueError("DB_SCHEMA must be a lowercase identifier")
    body = ";\n".join(
        _TABLE_RE.sub(lambda m: f'"{schema}".{m.group(1)}', s) for s in _split_statements(POSTGRES_SCHEMA)
    )
    return f'CREATE SCHEMA IF NOT EXISTS "{schema}";\n{body};\n'
