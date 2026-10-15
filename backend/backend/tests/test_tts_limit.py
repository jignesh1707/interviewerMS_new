import asyncio
import subprocess
import threading

import pytest

from app.config import get_settings
from app.core.errors import ServiceUnavailableError, SpeechUnavailableError
from app.voice import tts


@pytest.fixture(autouse=True)
def _fresh_slots():
    tts._slots = None
    yield
    tts._slots = None


async def test_only_the_configured_number_of_syntheses_run_at_once(monkeypatch):
    monkeypatch.setattr(get_settings(), "tts_max_concurrent", 2)
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def fake(_text, _model, _voice):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        threading.Event().wait(0.05)
        with lock:
            state["now"] -= 1
        return b"RIFFwav"

    monkeypatch.setattr(tts, "_synthesize_sync", fake)
    results = await asyncio.gather(*(tts.synthesize("hello") for _ in range(6)))

    assert results == [b"RIFFwav"] * 6
    assert state["max"] == 2


async def test_busy_machine_answers_503_with_retry_after(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "tts_max_concurrent", 1)
    monkeypatch.setattr(settings, "tts_queue_timeout_seconds", 0.1)
    release = threading.Event()

    def blocking(_text, _model, _voice):
        release.wait(5)
        return b"RIFFwav"

    monkeypatch.setattr(tts, "_synthesize_sync", blocking)
    first = asyncio.create_task(tts.synthesize("one"))
    await asyncio.sleep(0.05)
    with pytest.raises(ServiceUnavailableError) as busy:
        await tts.synthesize("two")
    assert busy.value.status_code == 503
    assert busy.value.headers == {"Retry-After": "5"}

    release.set()
    assert await first == b"RIFFwav"
    assert await tts.synthesize("three") == b"RIFFwav"  # the slot was released


def test_a_piper_timeout_is_a_503_not_a_500(monkeypatch):
    monkeypatch.setattr(tts, "_resolve_binary", lambda: "piper")

    def hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="piper", timeout=60)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(SpeechUnavailableError) as failure:
        tts._synthesize_sync("hello", "", "en_US-lessac-medium")
    assert failure.value.status_code == 503
