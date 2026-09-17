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

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def test_dir() -> Path:
    return TEST_DIR
