import os
import tempfile
from pathlib import Path

TEST_DIR = Path(tempfile.mkdtemp(prefix="voice-interviewer-tests-"))

os.environ["API_KEYS"] = "test-key"
os.environ["DATABASE_PATH"] = str(TEST_DIR / "test.db")
os.environ["STORAGE_DIR"] = str(TEST_DIR / "storage")
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["WEBHOOK_URL"] = ""
# Run the whole suite against Postgres by setting TEST_DATABASE_URL (CI does this in a second job).
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    os.environ["DB_SCHEMA"] = "interviewer_test"

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def test_dir() -> Path:
    return TEST_DIR


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from app.core.ratelimit import budget, limiter

    limiter.reset()
    budget.reset()
    yield
    limiter.reset()
    budget.reset()
