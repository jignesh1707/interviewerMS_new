from pathlib import Path

ALLOWED_AUDIO_SUFFIXES = frozenset({".webm", ".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".mp4"})


def safe_audio_suffix(filename: str | None, default: str = ".webm") -> str:
    """Return a whitelisted audio extension for a client-supplied filename."""
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_AUDIO_SUFFIXES else default
