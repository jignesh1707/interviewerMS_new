"""The Whisper beam size is a setting (default 5, unchanged) so it can be A/B tested without a code change."""

from types import SimpleNamespace

from app.config import Settings, get_settings
from app.voice import stt


def _fake_model(seen):
    class Model:
        def transcribe(self, path, **kwargs):
            seen.update(kwargs)
            return iter([SimpleNamespace(start=0.0, end=1.0, text=" hello ")]), SimpleNamespace(duration=1.0, language="en")

    return Model()


def test_the_default_beam_size_is_still_five():
    assert Settings().whisper_beam_size == 5


def test_the_beam_size_setting_reaches_the_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(stt, "_model", _fake_model(seen))
    monkeypatch.setattr(get_settings(), "whisper_beam_size", 1)
    assert stt._transcribe_sync("x.wav")["text"] == "hello"
    assert seen["beam_size"] == 1
