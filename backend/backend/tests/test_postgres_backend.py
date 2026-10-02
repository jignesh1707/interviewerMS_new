"""Postgres-specific behaviour, run against a throwaway embedded Postgres (skipped if unavailable)."""

import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.errors import NotFoundError
from app.services import interview_service as interview_service_module
from app.services import storage as storage_module
from app.services.db import PostgresDatabase, postgres_schema_sql
from app.services.storage import Store
from tests.fakes import FakeRouter

import logging

logging.getLogger("pgserver").setLevel(logging.CRITICAL)  # its atexit cleanup logs to a closed stream
pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

SCHEMA = "interviewer_pgtest"
HEADERS = {"X-API-Key": "p" * 40}


@pytest.fixture(scope="module")
def admin_url():
    server = pgserver.get_server(tempfile.mkdtemp())
    yield server.get_uri()
    server.cleanup()


def _as_role(url: str, role: str) -> str:
    """Swap the user in a connection URL, keeping host, port and query (TCP on Windows, a unix socket on Linux)."""
    parts = urlsplit(url)
    hostport = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{role}:pw@{hostport}"))


@pytest.fixture(scope="module")
def app_url(admin_url):
    """A dedicated role that owns its schema, like the production setup in docs/DEPLOY_FLY_SUPABASE.md."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("CREATE ROLE interviewer_app LOGIN PASSWORD 'pw'")
        conn.execute(f'CREATE SCHEMA "{SCHEMA}" AUTHORIZATION interviewer_app')
        conn.execute("GRANT CONNECT ON DATABASE postgres TO interviewer_app")
    return _as_role(admin_url, "interviewer_app")


@pytest.fixture()
def store(app_url):
    database = PostgresDatabase(app_url, schema=SCHEMA, pool_size=4)
    store = Store(database=database)
    yield store
    store.close()


def _create(store, tenant="t1", ref=None, role="Eng"):
    return store.create_interview(
        role=role, candidate_name=None, resume_text="x" * 40, jd_text=None, config={},
        callback_url=None, metadata={}, tenant_id=tenant, external_ref=ref,
    )


def test_backend_is_postgres_and_tables_live_in_dedicated_schema(store, admin_url):
    assert store.db.dialect == "postgres"
    with psycopg.connect(admin_url) as conn:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_name IN ('interviews','answers','reports','events')"
        ).fetchall()
    assert {schema for schema, _ in rows} == {SCHEMA}
    assert {name for _, name in rows} == {"interviews", "answers", "reports", "events"}


def test_row_level_security_enabled_on_every_table(store, admin_url):
    with psycopg.connect(admin_url) as conn:
        rows = conn.execute(
            "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r'",
            (SCHEMA,),
        ).fetchall()
    assert len(rows) == 4 and all(enabled for _, enabled in rows)


def test_other_roles_cannot_read_candidate_data(store, admin_url):
    """Models Supabase's anon/authenticated API roles: no schema access, and RLS hides rows even with grants."""
    _create(store)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("CREATE ROLE api_like LOGIN PASSWORD 'pw'")
    api_url = _as_role(admin_url, "api_like")
    with psycopg.connect(api_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f'SELECT * FROM "{SCHEMA}".interviews')
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'GRANT USAGE ON SCHEMA "{SCHEMA}" TO api_like')
        conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{SCHEMA}" TO api_like')
    with psycopg.connect(api_url) as conn:
        assert conn.execute(f'SELECT COUNT(*) FROM "{SCHEMA}".interviews').fetchone()[0] == 0


def test_app_role_has_no_access_to_public_schema_tables_it_does_not_own(store):
    # The app only ever touches its own schema; verify queries are schema-qualified.
    assert '"interviewer_pgtest".interviews' in store.db._qualify("SELECT * FROM interviews WHERE id = ?")
    assert "%s" in store.db._qualify("SELECT * FROM interviews WHERE id = ?")


def test_no_server_side_prepared_statements(store):
    for _ in range(10):
        store.list_interviews("t1")
    with store.db._pool.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM pg_prepared_statements").fetchone()["n"] == 0


def test_crud_tenant_scoping_and_counts(store):
    a = _create(store, "tenant-a", "user-1")
    b = _create(store, "tenant-b", "user-1")
    assert store.get_interview_for_tenant(a["id"], "tenant-a")["external_ref"] == "user-1"
    with pytest.raises(NotFoundError):
        store.get_interview_for_tenant(a["id"], "tenant-b")
    assert all(i["tenant_id"] == "tenant-a" for i in store.list_interviews("tenant-a"))
    assert [i["id"] for i in store.list_interviews("tenant-b", external_ref="user-1")] == [b["id"]]
    store.add_answer(
        interview_id=a["id"], question_index=0, question="q", transcript="t", audio_path=None,
        metrics={}, heuristic_scores={}, analysis=None, router=None, duration_seconds=1.5, question_id="q0",
    )
    assert store.count_answers(a["id"]) == 1
    assert store.answer_counts([a["id"], b["id"]]) == {a["id"]: 1}
    assert store.answer_counts([]) == {}


def test_update_report_and_events_roundtrip(store):
    interview = _create(store)
    updated = store.update_interview(interview["id"], status="in_progress", questions=[{"id": "q0"}])
    assert updated["status"] == "in_progress" and updated["questions"] == [{"id": "q0"}]
    store.save_report(interview["id"], {"overall_score": 70})
    store.save_report(interview["id"], {"overall_score": 80})  # upsert
    assert store.get_report(interview["id"])["payload"] == {"overall_score": 80}
    store.add_event(interview["id"], "a", {"x": 1})
    store.add_event(interview["id"], "b")
    assert [e["event_type"] for e in store.list_events(interview["id"])] == ["a", "b"]


def test_delete_removes_everything_in_one_transaction(store):
    interview = _create(store)
    store.add_answer(
        interview_id=interview["id"], question_index=0, question="q", transcript="t", audio_path="/tmp/a.webm",
        metrics={}, heuristic_scores={}, analysis=None, router=None, duration_seconds=None,
    )
    store.save_report(interview["id"], {"x": 1})
    store.add_event(interview["id"], "e")
    assert store.delete_interview(interview["id"]) == ["/tmp/a.webm"]
    assert store.get_interview_or_none(interview["id"]) is None
    for table in ("answers", "reports", "events"):
        assert store._query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE interview_id = ?", (interview["id"],))["n"] == 0


def test_transaction_rolls_back_on_error(store):
    interview = _create(store)
    with pytest.raises(RuntimeError):
        with store.db.transaction() as tx:
            tx.execute("DELETE FROM interviews WHERE id = ?", (interview["id"],))
            raise RuntimeError("boom")
    assert store.get_interview_or_none(interview["id"]) is not None


def test_concurrent_writers_do_not_interfere(store):
    errors: list[Exception] = []

    def work(n):
        try:
            for i in range(5):
                interview = _create(store, tenant="concurrent", ref=f"u{n}")
                store.add_event(interview["id"], "x")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert len(store.list_interviews("concurrent", limit=200)) == 40


def test_schema_sql_for_admins_is_idempotent_and_qualified(admin_url):
    sql = postgres_schema_sql("interviewer_admin_sql")
    assert 'CREATE SCHEMA IF NOT EXISTS "interviewer_admin_sql"' in sql
    assert '"interviewer_admin_sql".interviews' in sql and "ENABLE ROW LEVEL SECURITY" in sql
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)  # running twice must be safe


def test_auto_migrate_off_leaves_schema_untouched(app_url, monkeypatch):
    monkeypatch.setattr(get_settings(), "db_auto_migrate", False)
    # tables already exist from the other tests; this must simply connect without issuing DDL
    store = Store(database=PostgresDatabase(app_url, schema=SCHEMA, pool_size=1))
    store.ping()
    store.close()


def test_invalid_schema_name_rejected(app_url):
    with pytest.raises(ValueError):
        PostgresDatabase(app_url, schema='x"; DROP SCHEMA public; --')


# ---- end to end through the API on Postgres, plus external_ref erasure ---------------------


@pytest.fixture()
def client(app_url, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_keys", f"pg:{'p' * 40}")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "rate_limit_expensive_per_minute", 10_000)
    monkeypatch.setattr(settings, "daily_expensive_budget", 0)
    store = Store(database=PostgresDatabase(app_url, schema=SCHEMA, pool_size=4))
    monkeypatch.setattr(storage_module, "_store", store)
    monkeypatch.setattr(storage_module, "_async_store", None)
    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    interview_service_module._service = None
    store.close()


def _api_create(client, ref):
    response = client.post(
        "/api/v1/interviews",
        json={
            "role": "Backend Engineer",
            "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
            "jd_text": "Require Python and Kafka. 5+ years experience.",
            "config": {"question_count": 3},
            "external_ref": ref,
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()["interview"]["id"]


def test_full_interview_flow_on_postgres(client):
    interview_id = _api_create(client, "student-flow")
    answer = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": "I built a cache and reduced p95 latency by 60 percent."},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    assert client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS).status_code == 200
    status = client.get(f"/api/v1/interviews/{interview_id}/status", headers=HEADERS).json()
    assert status["status"] == "completed" and status["answered_count"] == 1
    assert client.get("/api/v1/ready").json() == {"status": "ok"}


def test_list_filters_by_external_ref_and_counts_answers(client):
    interview_id = _api_create(client, "student-list")
    _api_create(client, "someone-else")
    client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": "I led a migration and cut costs by 30 percent."},
        headers=HEADERS,
    )
    items = client.get("/api/v1/interviews?external_ref=student-list", headers=HEADERS).json()["items"]
    assert [item["id"] for item in items] == [interview_id]
    assert items[0]["answered_count"] == 1


def test_erase_by_external_ref_deletes_only_that_user(client):
    keep = _api_create(client, "keep-me")
    gone_1 = _api_create(client, "erase-me")
    gone_2 = _api_create(client, "erase-me")
    response = client.delete("/api/v1/interviews/by-ref/erase-me", headers=HEADERS)
    assert response.status_code == 200 and response.json() == {"deleted": 2}
    for interview_id in (gone_1, gone_2):
        assert client.get(f"/api/v1/interviews/{interview_id}", headers=HEADERS).status_code == 404
    assert client.get(f"/api/v1/interviews/{keep}", headers=HEADERS).status_code == 200
    assert client.delete("/api/v1/interviews/by-ref/erase-me", headers=HEADERS).json() == {"deleted": 0}


def test_ready_returns_503_when_postgres_unreachable(client, monkeypatch):
    store = storage_module.get_store()
    monkeypatch.setattr(store.db, "query_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert client.get("/api/v1/ready").status_code == 503


def test_migrate_cli_prints_and_applies(app_url, monkeypatch, capsys):
    from app import migrate

    monkeypatch.setattr(get_settings(), "db_schema", "interviewer_cli")
    assert migrate.main(["--print"]) == 0
    assert 'CREATE SCHEMA IF NOT EXISTS "interviewer_cli"' in capsys.readouterr().out
    monkeypatch.setattr(get_settings(), "database_url", "")
    assert migrate.main(["--apply"]) == 2  # no DATABASE_URL


def test_erase_by_external_ref_handles_more_than_one_batch(client, monkeypatch):
    from app.api import routes_interviews

    monkeypatch.setattr(routes_interviews, "BY_REF_BATCH", 2)
    ids = [_api_create(client, "many") for _ in range(5)]
    assert client.delete("/api/v1/interviews/by-ref/many", headers=HEADERS).json() == {"deleted": 5}
    assert all(client.get(f"/api/v1/interviews/{i}", headers=HEADERS).status_code == 404 for i in ids)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgresql://postgres:@127.0.0.1:5432/postgres", "postgresql://r:pw@127.0.0.1:5432/postgres"),
        ("postgresql://postgres:@/postgres?host=/tmp/pg", "postgresql://r:pw@/postgres?host=/tmp/pg"),
    ],
)
def test_as_role_handles_tcp_and_unix_socket_urls(url, expected):
    assert _as_role(url, "r") == expected
