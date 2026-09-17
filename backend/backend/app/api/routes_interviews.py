import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.api.deps import require_api_key
from app.core.errors import ValidationAppError
from app.schemas.interview import (
    AnswerResponse,
    AnswerTextRequest,
    CreateInterviewRequest,
    CreateInterviewResponse,
    InterviewStatus,
    ReportResponse,
)
from app.services.interview_service import get_interview_service
from app.services.storage import get_store
from app.voice import stt

router = APIRouter(prefix="/interviews", tags=["interviews"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=CreateInterviewResponse, status_code=201)
async def create_interview(payload: CreateInterviewRequest) -> CreateInterviewResponse:
    service = get_interview_service()
    result = await service.create_interview(payload)
    return CreateInterviewResponse(
        interview=service.build_status(result["interview"]),
        questions=result["questions"],
    )


@router.post("/upload", response_model=CreateInterviewResponse, status_code=201)
async def create_interview_with_files(
    role: Annotated[str, Form()],
    candidate_name: Annotated[str | None, Form()] = None,
    resume_text: Annotated[str | None, Form()] = None,
    jd_text: Annotated[str | None, Form()] = None,
    callback_url: Annotated[str | None, Form()] = None,
    config_json: Annotated[str | None, Form()] = None,
    metadata_json: Annotated[str | None, Form()] = None,
    resume_file: Annotated[UploadFile | None, File()] = None,
    jd_file: Annotated[UploadFile | None, File()] = None,
) -> CreateInterviewResponse:
    config = _parse_json_form(config_json, "config_json")
    metadata = _parse_json_form(metadata_json, "metadata_json")
    payload = CreateInterviewRequest(
        role=role,
        candidate_name=candidate_name,
        resume_text=resume_text,
        jd_text=jd_text,
        callback_url=callback_url,
        metadata=metadata or {},
        config=config or {},
    )
    resume_bytes = None
    jd_bytes = None
    if resume_file and resume_file.filename:
        resume_bytes = (resume_file.filename, await resume_file.read())
    if jd_file and jd_file.filename:
        jd_bytes = (jd_file.filename, await jd_file.read())

    service = get_interview_service()
    result = await service.create_interview(payload, resume_bytes=resume_bytes, jd_bytes=jd_bytes)
    return CreateInterviewResponse(
        interview=service.build_status(result["interview"]),
        questions=result["questions"],
    )


@router.get("")
async def list_interviews(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    store = get_store()
    interviews = store.list_interviews(limit=limit, offset=offset)
    service = get_interview_service()
    return {"items": [service.build_status(item) for item in interviews], "limit": limit, "offset": offset}


@router.get("/{interview_id}")
async def get_interview(interview_id: str) -> dict:
    service = get_interview_service()
    store = get_store()
    interview = store.get_interview(interview_id)
    return {
        "interview": service.build_status(interview),
        "questions": interview.get("questions") or [],
        "match": interview.get("match_analysis"),
    }


@router.get("/{interview_id}/questions")
async def get_questions(interview_id: str) -> dict:
    interview = get_store().get_interview(interview_id)
    return {"items": interview.get("questions") or []}


@router.get("/{interview_id}/answers")
async def get_answers(interview_id: str) -> dict:
    store = get_store()
    store.get_interview(interview_id)
    return {"items": store.list_answers(interview_id)}


@router.post("/{interview_id}/answers", response_model=AnswerResponse)
async def submit_text_answer(interview_id: str, payload: AnswerTextRequest) -> AnswerResponse:
    service = get_interview_service()
    result = await service.submit_answer(
        interview_id,
        payload.question_index,
        payload.transcript,
        duration_seconds=payload.duration_seconds,
    )
    return AnswerResponse(**result)


@router.post("/{interview_id}/answers/audio", response_model=AnswerResponse)
async def submit_audio_answer(
    interview_id: str,
    question_index: Annotated[int, Form()],
    audio: Annotated[UploadFile, File()],
    duration_seconds: Annotated[float | None, Form()] = None,
) -> AnswerResponse:
    content = await audio.read()
    filename = audio.filename or "answer.webm"
    transcription = await stt.transcribe_bytes(content, filename)

    service = get_interview_service()
    audio_path = service.save_audio(interview_id, filename, content)
    duration = duration_seconds or transcription.get("duration_seconds")
    result = await service.submit_answer(
        interview_id,
        question_index,
        transcription["text"],
        duration_seconds=duration,
        audio_path=audio_path,
    )
    result["transcript_meta"] = {
        "language": transcription.get("language"),
        "duration_seconds": transcription.get("duration_seconds"),
        "processing_ms": transcription.get("processing_ms"),
    }
    return AnswerResponse(**result)


@router.get("/{interview_id}/transcript")
async def get_transcript(interview_id: str) -> dict:
    store = get_store()
    store.get_interview(interview_id)
    answers = store.list_answers(interview_id)
    return {
        "items": [
            {
                "question_index": answer["question_index"],
                "question": answer["question"],
                "transcript": answer["transcript"],
                "duration_seconds": answer["duration_seconds"],
                "metrics": answer["metrics"],
            }
            for answer in answers
        ]
    }


@router.post("/{interview_id}/finish", response_model=ReportResponse)
async def finish_interview(interview_id: str) -> ReportResponse:
    service = get_interview_service()
    store = get_store()
    report = await service.finish_interview(interview_id)
    saved = store.get_report(interview_id)
    return ReportResponse(
        interview_id=interview_id,
        status="completed",
        report=report,
        created_at=saved["created_at"] if saved else None,
    )


@router.get("/{interview_id}/report", response_model=ReportResponse)
async def get_report(interview_id: str) -> ReportResponse:
    store = get_store()
    interview = store.get_interview(interview_id)
    saved = store.get_report(interview_id)
    return ReportResponse(
        interview_id=interview_id,
        status=interview["status"],
        report=saved["payload"] if saved else None,
        created_at=saved["created_at"] if saved else None,
    )


@router.get("/{interview_id}/events")
async def get_events(interview_id: str) -> dict:
    store = get_store()
    store.get_interview(interview_id)
    return {"items": store.list_events(interview_id)}


def _parse_json_form(raw: str | None, field: str) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationAppError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationAppError(f"{field} must be a JSON object")
    return value



@router.get("/{interview_id}/status", response_model=InterviewStatus)
async def get_status(interview_id: str) -> InterviewStatus:
    service = get_interview_service()
    interview = get_store().get_interview(interview_id)
    return InterviewStatus(**service.build_status(interview))
