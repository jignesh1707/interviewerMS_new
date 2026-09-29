"""Static audit: external model HTTP/SDK calls must live behind the safety boundary."""

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

ALLOWED_HTTP_FILES = {
    APP_ROOT / "llm" / "providers" / "openai_compatible.py",
    APP_ROOT / "llm" / "providers" / "anthropic.py",
    APP_ROOT / "services" / "webhook.py",
}

ALLOWED_SDK_NAMES = {
    "openai",
    "anthropic",
    "deepseek",
}


def _iter_py_files():
    for path in APP_ROOT.rglob("*.py"):
        if path.name == "__pycache__":
            continue
        yield path


def test_httpx_model_posts_are_only_in_approved_adapters():
    offenders = []
    for path in _iter_py_files():
        text = path.read_text(encoding="utf-8")
        if "httpx" not in text:
            continue
        if path.resolve() in {p.resolve() for p in ALLOWED_HTTP_FILES}:
            continue
        if "AsyncClient" in text or "httpx.post" in text or "client.post" in text:
            offenders.append(str(path.relative_to(APP_ROOT.parent)))
    assert offenders == [], f"unexpected HTTP clients outside boundary: {offenders}"


def test_adapters_require_boundary_permit():
    for path in (
        APP_ROOT / "llm" / "providers" / "openai_compatible.py",
        APP_ROOT / "llm" / "providers" / "anthropic.py",
    ):
        text = path.read_text(encoding="utf-8")
        assert "require_boundary_permit" in text, f"{path.name} missing require_boundary_permit"


def test_no_official_sdk_imports_outside_adapters():
    forbidden = ("import openai", "from openai", "import anthropic", "from anthropic")
    offenders = []
    for path in _iter_py_files():
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path}: {needle}")
    assert offenders == [], f"direct SDK imports found: {offenders}"


def test_router_does_not_call_httpx_directly():
    text = (APP_ROOT / "llm" / "router.py").read_text(encoding="utf-8")
    assert "httpx" not in text


def test_interview_service_does_not_call_providers():
    text = (APP_ROOT / "services" / "interview_service.py").read_text(encoding="utf-8")
    assert "OpenAICompatibleClient" not in text
    assert "AnthropicClient" not in text
    assert "httpx" not in text
    assert "Authorization" not in text
