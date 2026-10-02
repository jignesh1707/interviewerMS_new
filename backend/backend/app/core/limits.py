"""Request size limits: an ASGI body cap and a capped upload reader."""

from fastapi import UploadFile

from app.config import get_settings
from app.core.errors import PayloadTooLargeError

_MB = 1024 * 1024
_AUDIO_SUFFIXES = ("/answers/audio", "/speech/transcribe")


def body_limit_for(path: str) -> int:
    settings = get_settings()
    if path.endswith(_AUDIO_SUFFIXES):
        return (settings.stt_max_upload_mb + 1) * _MB
    # multipart create allows a resume and a JD file, plus form fields
    return (2 * settings.max_doc_upload_mb + 1) * _MB


class BodyLimitMiddleware:
    """Reject oversized request bodies by Content-Length and by bytes actually streamed."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = body_limit_for(scope.get("path", ""))
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send, limit)
            return

        received = 0
        exceeded = False
        started = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Stop feeding the app; whatever it answers is replaced with a 413 below.
                    exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message):
            nonlocal started
            if exceeded:
                if not started:
                    started = True
                    await self._reject(send, limit)
                return
            await send(message)
            if message["type"] == "http.response.start":
                started = True

        await self.app(scope, limited_receive, guarded_send)
        if exceeded and not started:
            await self._reject(send, limit)

    @staticmethod
    async def _reject(send, limit: int) -> None:
        body = (
            b'{"error":{"code":"payload_too_large","message":"request body exceeds '
            + str(limit // _MB).encode()
            + b' MB","details":{}}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def read_capped(upload: UploadFile, max_bytes: int, label: str) -> bytes:
    """Read an upload in chunks, failing as soon as it exceeds max_bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"{label} exceeds {max_bytes // _MB} MB limit", details={"limit_bytes": max_bytes}
            )
        chunks.append(chunk)
    return b"".join(chunks)
