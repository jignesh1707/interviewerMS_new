import asyncio
import tempfile
import time
from pathlib import Path

from app.config import get_settings
from app.core.errors import SpeechUnavailableError, ValidationAppError
from app.core.logging import get_logger

logger = get_logger(__name__)

_model = None
_model_lock = asyncio.Lock()


def _load_model():
    global _model
    settings = get_settings()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SpeechUnavailableError(
            "faster-whisper is not installed. Install voice extras: pip install -r requirements-voice.txt"
        ) from exc
    logger.info(
        "loading whisper model=%s device=%s compute_type=%s",
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
    )
    return WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )


async def _get_model():
    global _model
    if _model is None:
        async with _model_lock:
            if _model is None:
                _model = await asyncio.to_thread(_load_model)
    return _model


def _transcribe_sync(audio_path: str) -> dict:
    settings = get_settings()
    model = _model
    segments, info = model.transcribe(
        audio_path,
        beam_size=5,
        vad_filter=True,
        language=settings.whisper_language or None,
    )
    collected = []
    for segment in segments:
        collected.append(
            {
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "text": segment.text.strip(),
            }
        )
    text = " ".join(item["text"] for item in collected).strip()
    duration = round(info.duration, 2) if getattr(info, "duration", None) else None
    return {
        "text": text,
        "language": getattr(info, "language", None),
        "language_probability": round(getattr(info, "language_probability", 0.0) or 0.0, 3),
        "duration_seconds": duration,
        "segments": collected,
    }


async def transcribe_bytes(content: bytes, filename: str = "answer.webm") -> dict:
    settings = get_settings()
    if not content:
        raise ValidationAppError("empty audio payload")
    max_bytes = settings.stt_max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise ValidationAppError(
            f"audio exceeds {settings.stt_max_upload_mb}MB limit",
            details={"bytes": len(content)},
        )

    await _get_model()
    suffix = Path(filename).suffix or ".webm"
    tmp_path = None
    started = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(content)
            tmp_path = handle.name
        result = await asyncio.to_thread(_transcribe_sync, tmp_path)
    except SpeechUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcription_failed filename=%s", filename)
        raise ValidationAppError(f"failed to transcribe audio: {exc}") from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    result["processing_ms"] = int((time.monotonic() - started) * 1000)
    return result


def model_ready() -> bool:
    return _model is not None
