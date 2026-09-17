import asyncio
import shutil
import tempfile
from pathlib import Path

from app.config import get_settings
from app.core.errors import SpeechUnavailableError, ValidationAppError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _resolve_binary() -> str:
    settings = get_settings()
    binary = shutil.which(settings.piper_binary)
    if not binary:
        raise SpeechUnavailableError(
            f"piper binary '{settings.piper_binary}' not found on PATH. "
            "Install piper-tts or set PIPER_BINARY, then provide a voice model via PIPER_MODEL_PATH."
        )
    return binary


def _synthesize_sync(text: str, model_path: str, voice: str) -> bytes:
    binary = _resolve_binary()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        output_path = Path(handle.name)

    command = [binary, "--output_file", str(output_path)]
    if model_path:
        command += ["--model", model_path]
    elif voice:
        command += ["--model", voice]

    try:
        import subprocess

        process = subprocess.run(
            command,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if process.returncode != 0:
            raise SpeechUnavailableError(
                f"piper failed: {process.stderr.decode('utf-8', errors='ignore')[:400]}"
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise SpeechUnavailableError("piper produced no audio output")
        return output_path.read_bytes()
    finally:
        output_path.unlink(missing_ok=True)


async def synthesize(text: str, voice: str | None = None) -> bytes:
    settings = get_settings()
    if not text.strip():
        raise ValidationAppError("text for speech synthesis is empty")
    trimmed = text.strip()[:3000]
    model_path = settings.piper_model_path
    resolved_voice = voice or settings.piper_default_voice
    if not model_path:
        logger.warning("piper_model_path_not_set using voice_id=%s", resolved_voice)
    return await asyncio.to_thread(_synthesize_sync, trimmed, model_path, resolved_voice)


def binary_available() -> bool:
    try:
        _resolve_binary()
        return True
    except SpeechUnavailableError:
        return False
