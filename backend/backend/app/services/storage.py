import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.errors import NotFoundError
from app.services.db import Database, PostgresDatabase, SqliteDatabase

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Synchronous store. App code goes through ``AsyncStore`` so database waits never block the event loop."""

    def __init__(self, database: Database | None = None, database_path: Path | None = None) -> None:
        settings = get_settings()
        if database is None:
            if database_path is not None or not settings.database_url:
                database = SqliteDatabase(database_path or settings.database_path)
            else:
                database = PostgresDatabase(
                    settings.database_url, schema=settings.db_schema, pool_size=settings.db_pool_size
                )
        self.db = database
        self.database_path = getattr(database, "path", None)
        if database.dialect == "sqlite" or settings.db_auto_migrate:
            database.ensure_schema()

    def _execute(self, query: str, params: tuple = ()) -> None:
        self.db.execute(query, params)

    def _query_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        return self.db.query_one(query, params)

    def _query_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        return self.db.query_all(query, params)

    def ping(self) -> None:
        self.db.query_one("SELECT 1 AS ok")

    def close(self) -> None:
        self.db.close()

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
        tenant_id: str | None = None,
        external_ref: str | None = None,
    ) -> dict[str, Any]:
        interview_id = uuid.uuid4().hex
        now = utc_now()
        self._execute(
            """
            INSERT INTO interviews (
                id, role, candidate_name, status, resume_text, jd_text,
                config, callback_url, metadata, created_at, updated_at, tenant_id, external_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                tenant_id,
                external_ref,
            ),
        )
        return self.get_interview(interview_id)

    def get_interview(self, interview_id: str) -> dict[str, Any]:
        row = self._query_one("SELECT * FROM interviews WHERE id = ?", (interview_id,))
        if not row:
            raise NotFoundError(f"interview '{interview_id}' not found")
        return self._hydrate_interview(row)

    def get_interview_for_tenant(self, interview_id: str, tenant_id: str) -> dict[str, Any]:
        """Fetch an interview owned by tenant_id. Other tenants and legacy untagged rows give 404."""
        interview = self.get_interview(interview_id)
        if interview.get("tenant_id") != tenant_id:
            raise NotFoundError(f"interview '{interview_id}' not found")
        return interview

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

    def delete_interview(self, interview_id: str) -> list[str]:
        """Delete an interview and everything attached to it. Returns stored audio paths."""
        with self.db.transaction() as tx:
            paths = [
                row["audio_path"]
                for row in tx.query_all(
                    "SELECT audio_path FROM answers WHERE interview_id = ? AND audio_path IS NOT NULL",
                    (interview_id,),
                )
            ]
            for table in ("answers", "reports", "events"):
                tx.execute(f"DELETE FROM {table} WHERE interview_id = ?", (interview_id,))
            tx.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))
        return paths

    def list_expired_interview_ids(self, cutoff_iso: str, limit: int = 500) -> list[str]:
        rows = self._query_all(
            "SELECT id FROM interviews WHERE created_at < ? ORDER BY created_at LIMIT ?", (cutoff_iso, limit)
        )
        return [row["id"] for row in rows]

    def list_interviews(
        self, tenant_id: str, limit: int = 50, offset: int = 0, external_ref: str | None = None
    ) -> list[dict[str, Any]]:
        if external_ref is None:
            rows = self._query_all(
                "SELECT * FROM interviews WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            )
        else:
            rows = self._query_all(
                "SELECT * FROM interviews WHERE tenant_id = ? AND external_ref = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, external_ref, limit, offset),
            )
        return [self._hydrate_interview(row) for row in rows]

    def list_interview_ids_by_ref(self, tenant_id: str, external_ref: str, limit: int = 1000) -> list[str]:
        rows = self._query_all(
            "SELECT id FROM interviews WHERE tenant_id = ? AND external_ref = ? ORDER BY created_at LIMIT ?",
            (tenant_id, external_ref, limit),
        )
        return [row["id"] for row in rows]

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

    def count_answers(self, interview_id: str) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM answers WHERE interview_id = ?", (interview_id,))
        return int(row["n"]) if row else 0

    def answer_counts(self, interview_ids: list[str]) -> dict[str, int]:
        """Answer counts for many interviews in one query."""
        if not interview_ids:
            return {}
        marks = ", ".join("?" for _ in interview_ids)
        rows = self._query_all(
            f"SELECT interview_id, COUNT(*) AS n FROM answers WHERE interview_id IN ({marks}) GROUP BY interview_id",
            tuple(interview_ids),
        )
        return {row["interview_id"]: int(row["n"]) for row in rows}

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

    # ------------------------------------------------------------------ quotas

    def _ensure_quota_row(self, tenant_id: str, external_ref: str, period: str) -> None:
        self._execute(
            "INSERT INTO quotas (tenant_id, external_ref, period_key, used_minutes, bonus_minutes, updated_at) "
            "VALUES (?, ?, ?, 0, 0, ?) ON CONFLICT (tenant_id, external_ref, period_key) DO NOTHING",
            (tenant_id, external_ref, period, utc_now()),
        )

    def quota_get(self, tenant_id: str, external_ref: str, period: str) -> dict[str, int]:
        row = self._query_one(
            "SELECT used_minutes, bonus_minutes FROM quotas WHERE tenant_id = ? AND external_ref = ? AND period_key = ?",
            (tenant_id, external_ref, period),
        )
        return {"used_minutes": int(row["used_minutes"]), "bonus_minutes": int(row["bonus_minutes"])} if row else {
            "used_minutes": 0,
            "bonus_minutes": 0,
        }

    def quota_debit(self, tenant_id: str, external_ref: str, period: str, minutes: int, *, allowance: int) -> bool:
        """Book `minutes` if the student still has them. One conditional UPDATE, so concurrent bookings cannot overspend."""
        self._ensure_quota_row(tenant_id, external_ref, period)
        changed = self.db.execute_count(
            "UPDATE quotas SET used_minutes = used_minutes + ?, updated_at = ? "
            "WHERE tenant_id = ? AND external_ref = ? AND period_key = ? "
            "AND used_minutes + ? <= ? + bonus_minutes",
            (minutes, utc_now(), tenant_id, external_ref, period, minutes, allowance),
        )
        return changed == 1

    def quota_credit(self, tenant_id: str, external_ref: str, period: str, minutes: int) -> None:
        self._execute(
            "UPDATE quotas SET used_minutes = CASE WHEN used_minutes > ? THEN used_minutes - ? ELSE 0 END, "
            "updated_at = ? WHERE tenant_id = ? AND external_ref = ? AND period_key = ?",
            (minutes, minutes, utc_now(), tenant_id, external_ref, period),
        )

    def quota_add_bonus(self, tenant_id: str, external_ref: str, period: str, minutes: int) -> None:
        self._ensure_quota_row(tenant_id, external_ref, period)
        self._execute(
            "UPDATE quotas SET bonus_minutes = bonus_minutes + ?, updated_at = ? "
            "WHERE tenant_id = ? AND external_ref = ? AND period_key = ?",
            (minutes, utc_now(), tenant_id, external_ref, period),
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


class AsyncStore:
    """Runs every Store call on a worker thread so queries (possibly over the network) never block the loop."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def sync(self) -> Store:
        return self._store

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._store, name)
        if not callable(attribute):
            return attribute

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attribute, *args, **kwargs)

        return call


_store: Store | None = None
_async_store: AsyncStore | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def get_async_store() -> AsyncStore:
    global _async_store
    store = get_store()
    if _async_store is None or _async_store.sync is not store:
        _async_store = AsyncStore(store)
    return _async_store
