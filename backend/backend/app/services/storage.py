import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.errors import NotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
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
CREATE INDEX IF NOT EXISTS idx_events_interview ON events (interview_id, id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


class Store:
    def __init__(self, database_path: Path | None = None) -> None:
        settings = get_settings()
        self.database_path = database_path or settings.database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(self.database_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(SCHEMA)
        self._migrate()
        self._connection.commit()

    def _migrate(self) -> None:
        _ensure_column(self._connection, "answers", "question_id", "TEXT")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_answers_question ON answers (interview_id, question_id)"
        )

    def _execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(query, params)
            self._connection.commit()
            return cursor

    def _query_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def _query_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def create_interview(
        self,
        *,
        role: str,
        candidate_name: str | None,
        resume_text: str | None,
        jd_text: str | None,
        config: dict[str, Any],
        callback_url: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        interview_id = uuid.uuid4().hex
        now = utc_now()
        self._execute(
            """
            INSERT INTO interviews (
                id, role, candidate_name, status, resume_text, jd_text,
                config, callback_url, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interview_id,
                role,
                candidate_name,
                "created",
                resume_text,
                jd_text,
                json.dumps(config),
                callback_url,
                json.dumps(metadata),
                now,
                now,
            ),
        )
        return self.get_interview(interview_id)

    def get_interview(self, interview_id: str) -> dict[str, Any]:
        row = self._query_one("SELECT * FROM interviews WHERE id = ?", (interview_id,))
        if not row:
            raise NotFoundError(f"interview '{interview_id}' not found")
        return self._hydrate_interview(row)

    def get_interview_or_none(self, interview_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT * FROM interviews WHERE id = ?", (interview_id,))
        return self._hydrate_interview(row) if row else None

    @staticmethod
    def _hydrate_interview(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("resume_summary", "jd_summary", "match_analysis", "questions", "config", "metadata"):
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (TypeError, json.JSONDecodeError):
                    row[key] = None
        return row

    def update_interview(self, interview_id: str, **fields: Any) -> dict[str, Any]:
        if not fields:
            return self.get_interview(interview_id)
        allowed = {
            "role", "candidate_name", "status", "resume_text", "jd_text", "resume_summary",
            "jd_summary", "match_analysis", "questions", "config", "callback_url",
            "metadata", "error", "finished_at",
        }
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"resume_summary", "jd_summary", "match_analysis", "questions", "config", "metadata"} and not isinstance(value, str):
                value = json.dumps(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(interview_id)
        self._execute(f"UPDATE interviews SET {', '.join(assignments)} WHERE id = ?", tuple(values))
        return self.get_interview(interview_id)

    def list_interviews(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._query_all(
            "SELECT * FROM interviews ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [self._hydrate_interview(row) for row in rows]

    def add_answer(
        self,
        *,
        interview_id: str,
        question_index: int,
        question: str,
        transcript: str,
        audio_path: str | None,
        metrics: dict[str, Any],
        heuristic_scores: dict[str, Any],
        analysis: dict[str, Any] | None,
        router: dict[str, Any] | None,
        duration_seconds: float | None,
        question_id: str | None = None,
    ) -> dict[str, Any]:
        answer_id = uuid.uuid4().hex
        self._execute(
            """
            INSERT INTO answers (
                id, interview_id, question_id, question_index, question, audio_path, transcript,
                metrics, heuristic_scores, analysis, router, duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                answer_id,
                interview_id,
                question_id,
                question_index,
                question,
                audio_path,
                transcript,
                json.dumps(metrics),
                json.dumps(heuristic_scores),
                json.dumps(analysis) if analysis else None,
                json.dumps(router) if router else None,
                duration_seconds,
                utc_now(),
            ),
        )
        return self.get_answer(answer_id)

    def get_answer(self, answer_id: str) -> dict[str, Any]:
        row = self._query_one("SELECT * FROM answers WHERE id = ?", (answer_id,))
        if not row:
            raise NotFoundError(f"answer '{answer_id}' not found")
        return self._hydrate_answer(row)

    def list_answers(self, interview_id: str) -> list[dict[str, Any]]:
        rows = self._query_all(
            "SELECT * FROM answers WHERE interview_id = ? ORDER BY question_index ASC, created_at ASC",
            (interview_id,),
        )
        return [self._hydrate_answer(row) for row in rows]

    @staticmethod
    def _hydrate_answer(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("metrics", "heuristic_scores", "analysis", "router"):
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (TypeError, json.JSONDecodeError):
                    row[key] = None
        return row

    def save_report(self, interview_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        self._execute(
            """
            INSERT INTO reports (interview_id, payload, created_at) VALUES (?, ?, ?)
            ON CONFLICT(interview_id) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at
            """,
            (interview_id, json.dumps(payload), now),
        )
        return {"interview_id": interview_id, "payload": payload, "created_at": now}

    def get_report(self, interview_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT * FROM reports WHERE interview_id = ?", (interview_id,))
        if not row:
            return None
        try:
            row["payload"] = json.loads(row["payload"])
        except json.JSONDecodeError:
            row["payload"] = None
        return row

    def add_event(self, interview_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self._execute(
            "INSERT INTO events (interview_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (interview_id, event_type, json.dumps(payload or {}), utc_now()),
        )

    def list_events(self, interview_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query_all(
            "SELECT * FROM events WHERE interview_id = ? ORDER BY id ASC LIMIT ?", (interview_id, limit)
        )
        for row in rows:
            if row.get("payload"):
                try:
                    row["payload"] = json.loads(row["payload"])
                except json.JSONDecodeError:
                    row["payload"] = None
        return rows


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
