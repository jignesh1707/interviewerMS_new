import json
from typing import Annotated

from pydantic import ValidationError

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile

from app.api.deps import expensive_call, require_api_key
from app.config import get_settings
from app.core.errors import ValidationAppError
from app.core.limits import read_capped
from app.schemas.interview import (
    AnswerResponse,
    AnswerTextRequest,
    CreateInterviewRequest,
    CreateInterviewResponse,
    InterviewStatus,
    ReportResponse,
)
from app.services.interview_service import get_interview_service
from app.services.storage import get_async_store
from app.voice import stt

Tenant = Annotated[str, Depends(require_api_key)]

router = APIRouter(prefix="/interviews", tags=["interviews"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=CreateInterviewResponse, status_code=201, dependencies=[Depends(expensive_call)])
async def create_interview(payload: CreateInterviewRequest, tenant: Tenant) -> CreateInterviewResponse:
    service = get_interview_service()
    result = await service.create_interview(payload, tenant_id=tenant)
    return CreateInterviewResponse(
        interview=await service.build_status(result["interview"]),
        questions=result["questions"],
    )


@router.post("/upload", response_model=CreateInterviewResponse, status_code=201, dependencies=[Depends(expensive_call)])
async def create_interview_with_files(
    role: Annotated[str, Form()],
    tenant: Tenant,
    candidate_name: Annotated[str | None, Form()] = None,
    resume_text: Annotated[str | None, Form()] = None,
    jd_text: Annotated[str | None, Form()] = None,
    callback_url: Annotated[str | None, Form()] = None,
    consent_to_ai_processing: Annotated[bool | None, Form()] = None,
    external_ref: Annotated[str | None, Form(max_length=200)] = None,
    config_json: Annotated[str | None, Form()] = None,
    metadata_json: Annotated[str | None, Form()] = None,
    resume_file: Annotated[UploadFile | None, File()] = None,
    jd_file: Annotated[UploadFile | None, File()] = None,
) -> CreateInterviewResponse:
    config = _parse_json_form(config_json, "config_json")
    metadata = _parse_json_form(metadata_json, "metadata_json")
    try:
        payload = CreateInterviewRequest(
            role=role,
            candidate_name=candidate_name,
            resume_text=resume_text,
            jd_text=jd_text,
            callback_url=callback_url,
            consent_to_ai_processing=consent_to_ai_processing,
            external_ref=external_ref,
            metadata=metadata or {},
            config=config or {},
        )
    except ValidationError as exc:
        raise ValidationAppError("invalid interview payload", details={"errors": exc.errors(include_url=False, include_context=False, include_input=False)}) from exc
    doc_limit = get_settings().max_doc_upload_mb * 1024 * 1024
    resume_bytes = None
    jd_bytes = None
    if resume_file and resume_file.filename:
        resume_bytes = (resume_file.filename, await read_capped(resume_file, doc_limit, "resume_file"))
    if jd_file and jd_file.filename:
        jd_bytes = (jd_file.filename, await read_capped(jd_file, doc_limit, "jd_file"))

    service = get_interview_service()
    result = await service.create_interview(
        payload, resume_bytes=resume_bytes, jd_bytes=jd_bytes, tenant_id=tenant
    )
    return CreateInterviewResponse(
        interview=await service.build_status(result["interview"]),
        questions=result["questions"],
    )


@router.get("")
async def list_interviews(
    tenant: Tenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    external_ref: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    store = get_async_store()
    interviews = await store.list_interviews(tenant, limit=limit, offset=offset, external_ref=external_ref)
    service = get_interview_service()
    counts = await store.answer_counts([item["id"] for item in interviews])
    return {
        "items": [await service.build_status(item, counts.get(item["id"], 0)) for item in interviews],
        "limit": limit,
        "offset": offset,
    }


@router.get("/{interview_id}")
async def get_interview(interview_id: str, tenant: Tenant) -> dict:
    service = get_interview_service()
    store = get_async_store()
    interview = await store.get_interview_for_tenant(interview_id, tenant)
    return {
        "interview": await service.build_status(interview),
        "questions": interview.get("questions") or [],
        "match": interview.get("match_analysis"),
    }


@router.get("/{interview_id}/questions")
async def get_questions(interview_id: str, tenant: Tenant) -> dict:
    interview = await get_async_store().get_interview_for_tenant(interview_id, tenant)
    return {"items": interview.get("questions") or []}


@router.get("/{interview_id}/answers")
async def get_answers(interview_id: str, tenant: Tenant) -> dict:
    store = get_async_store()
    await store.get_interview_for_tenant(interview_id, tenant)
    return {"items": await store.list_answers(interview_id)}


@router.post("/{interview_id}/answers", response_model=AnswerResponse, dependencies=[Depends(expensive_call)])
async def submit_text_answer(interview_id: str, tenant: Tenant, payload: AnswerTextRequest) -> AnswerResponse:
    await get_async_store().get_interview_for_tenant(interview_id, tenant)
    service = get_interview_service()
    result = await service.submit_answer(
        interview_id,
        transcript=payload.transcript,
        question_id=payload.question_id,
        question_index=payload.question_index,
        duration_seconds=payload.duration_seconds,
    )
    return AnswerResponse(**result)


@router.post("/{interview_id}/answers/audio", response_model=AnswerResponse, dependencies=[Depends(expensive_call)])
async def submit_audio_answer(
    interview_id: str,
    tenant: Tenant,
    question_index: Annotated[int, Form()],
    audio: Annotated[UploadFile, File()],
    duration_seconds: Annotated[float | None, Form()] = None,
) -> AnswerResponse:
    await get_async_store().get_interview_for_tenant(interview_id, tenant)
    content = await read_capped(audio, get_settings().stt_max_upload_mb * 1024 * 1024, "audio")
    filename = audio.filename or "answer.webm"
    transcription = await stt.transcribe_bytes(content, filename)

    service = get_interview_service()
    audio_path = service.save_audio(interview_id, filename, content)
    duration = duration_seconds or transcription.get("duration_seconds")
    result = await service.submit_answer(
        interview_id,
        transcript=transcription["text"],
        question_index=question_index,
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
async def get_transcript(interview_id: str, tenant: Tenant) -> dict:
    store = get_async_store()
    await store.get_interview_for_tenant(interview_id, tenant)
    answers = await store.list_answers(interview_id)
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


@router.post("/{interview_id}/finish", response_model=ReportResponse, dependencies=[Depends(expensive_call)])
async def finish_interview(interview_id: str, tenant: Tenant) -> ReportResponse:
    service = get_interview_service()
    store = get_async_store()
    await store.get_interview_for_tenant(interview_id, tenant)
    report = await service.finish_interview(interview_id)
    saved = await store.get_report(interview_id)
    return ReportResponse(
        interview_id=interview_id,
        status="completed",
        report=report,
        created_at=saved["created_at"] if saved else None,
    )


@router.get("/{interview_id}/report", response_model=ReportResponse)
async def get_report(interview_id: str, tenant: Tenant) -> ReportResponse:
    store = get_async_store()
    interview = await store.get_interview_for_tenant(interview_id, tenant)
    saved = await store.get_report(interview_id)
    return ReportResponse(
        interview_id=interview_id,
        status=interview["status"],
        report=saved["payload"] if saved else None,
        created_at=saved["created_at"] if saved else None,
    )


@router.get("/{interview_id}/events")
async def get_events(interview_id: str, tenant: Tenant) -> dict:
    store = get_async_store()
    await store.get_interview_for_tenant(interview_id, tenant)
    return {"items": await store.list_events(interview_id)}


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



BY_REF_BATCH = 200


@router.delete("/by-ref/{external_ref}")
async def delete_interviews_by_ref(external_ref: str, tenant: Tenant) -> dict:
    """Erase every interview this tenant created for one end user (right-to-erasure requests)."""
    store = get_async_store()
    service = get_interview_service()
    deleted = 0
    while True:
        ids = await store.list_interview_ids_by_ref(tenant, external_ref, limit=BY_REF_BATCH)
        if not ids:
            return {"deleted": deleted}
        for interview_id in ids:
            await service.delete_interview(interview_id)
        deleted += len(ids)


@router.delete("/{interview_id}", status_code=204)
async def delete_interview(interview_id: str, tenant: Tenant) -> Response:
    """Erase an interview and its transcripts, report, events and any stored audio."""
    await get_async_store().get_interview_for_tenant(interview_id, tenant)
    await get_interview_service().delete_interview(interview_id)
    return Response(status_code=204)


@router.get("/{interview_id}/status", response_model=InterviewStatus)
async def get_status(interview_id: str, tenant: Tenant) -> InterviewStatus:
    service = get_interview_service()
    interview = await get_async_store().get_interview_for_tenant(interview_id, tenant)
    return InterviewStatus(**(await service.build_status(interview)))
