from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile

from app.api.deps import expensive_call, require_api_key
from app.config import get_settings
from app.core.limits import read_capped
from app.voice import stt, tts

router = APIRouter(prefix="/speech", tags=["speech"], dependencies=[Depends(require_api_key)])


@router.post("/transcribe", dependencies=[Depends(expensive_call)])
async def transcribe(audio: Annotated[UploadFile, File()]) -> dict:
    content = await read_capped(audio, get_settings().stt_max_upload_mb * 1024 * 1024, "audio")
    result = await stt.transcribe_bytes(content, audio.filename or "answer.webm")
    return result


@router.post("/synthesize", dependencies=[Depends(expensive_call)])
async def synthesize(
    text: Annotated[str, Form(max_length=3000)],
    voice: Annotated[str | None, Form(max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")] = None,
) -> Response:
    audio = await tts.synthesize(text, voice)
    return Response(content=audio, media_type="audio/wav")
