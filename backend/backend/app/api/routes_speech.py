from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile

from app.api.deps import require_api_key
from app.voice import stt, tts

router = APIRouter(prefix="/speech", tags=["speech"], dependencies=[Depends(require_api_key)])


@router.post("/transcribe")
async def transcribe(audio: Annotated[UploadFile, File()]) -> dict:
    content = await audio.read()
    result = await stt.transcribe_bytes(content, audio.filename or "answer.webm")
    return result


@router.post("/synthesize")
async def synthesize(
    text: Annotated[str, Form()],
    voice: Annotated[str | None, Form()] = None,
) -> Response:
    audio = await tts.synthesize(text, voice)
    return Response(content=audio, media_type="audio/wav")
