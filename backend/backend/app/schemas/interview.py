from typing import Any, Literal

import json

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import get_settings


class InterviewConfig(BaseModel):
    question_count: int = Field(default=8, ge=3, le=15)
    focus_areas: list[str] = Field(default_factory=list, max_length=20)
    language: str = "en"
    ask_followups: bool = True
    analyze_per_answer: bool = True
    max_followups: int = Field(default=3, ge=0, le=15)
    followup_score_threshold: int = Field(default=75, ge=0, le=100)
    # Interview length in minutes. Only used when plans are enabled: it must be a length the plan allows
    # (default: the plan's default) and it fixes question_count and max_followups from plans.yaml.
    duration_minutes: int | None = Field(default=None, ge=1, le=120)


class CreateInterviewRequest(BaseModel):
    role: str = Field(min_length=2, max_length=200)
    candidate_name: str | None = Field(default=None, max_length=200)
    resume_text: str | None = Field(default=None, max_length=200_000)
    jd_text: str | None = Field(default=None, max_length=200_000)
    callback_url: str | None = Field(default=None, max_length=2048)
    consent_to_ai_processing: bool | None = None
    external_ref: str | None = Field(default=None, min_length=1, max_length=200)
    plan: str | None = Field(default=None, min_length=1, max_length=64)  # a plan from plans.yaml; default plan if omitted
    metadata: dict[str, Any] = Field(default_factory=dict)
    config: InterviewConfig = Field(default_factory=InterviewConfig)

    @model_validator(mode="after")
    def enforce_size_limits(self) -> "CreateInterviewRequest":
        settings = get_settings()
        for name in ("resume_text", "jd_text"):
            value = getattr(self, name)
            if value is not None and len(value) > settings.max_text_chars:
                raise ValueError(f"{name} exceeds {settings.max_text_chars} characters")
        if len(json.dumps(self.metadata, default=str)) > settings.max_metadata_bytes:
            raise ValueError(f"metadata exceeds {settings.max_metadata_bytes} bytes")
        for area in self.config.focus_areas:
            if len(area) > 200:
                raise ValueError("focus_areas entries must be at most 200 characters")
        return self


class Question(BaseModel):
    id: str
    index: int
    category: str = "general"
    question: str
    star_focus: str = ""
    difficulty: str = "medium"
    what_to_listen_for: str = ""
    is_followup: bool = False
    parent_id: str | None = None
    depth: int = 0


class AnswerTextRequest(BaseModel):
    question_id: str | None = Field(default=None, min_length=1)
    question_index: int | None = Field(default=None, ge=0)
    transcript: str = Field(min_length=1, max_length=200_000)
    duration_seconds: float | None = Field(default=None, ge=0)

    @field_validator("transcript")
    @classmethod
    def limit_transcript(cls, value: str) -> str:
        limit = get_settings().max_transcript_chars
        if len(value) > limit:
            raise ValueError(f"transcript exceeds {limit} characters")
        return value

    @model_validator(mode="after")
    def require_question_reference(self) -> "AnswerTextRequest":
        if self.question_id is None and self.question_index is None:
            raise ValueError("provide either question_id or question_index")
        return self


class InterviewStatus(BaseModel):
    id: str
    status: Literal["created", "questions_ready", "in_progress", "processing", "completed", "failed"]
    role: str
    candidate_name: str | None = None
    question_count: int
    answered_count: int
    created_at: str
    updated_at: str
    finished_at: str | None = None
    error: str | None = None
    duration_minutes: int | None = None  # set when plans are enabled
    deadline_at: str | None = None  # answers are refused after this moment (UTC, ISO 8601)
    grace_seconds: int | None = None  # the last part of the time before deadline_at; the nominal end is deadline_at minus this
    seconds_remaining: int | None = None  # until deadline_at, measured by the server when it answered; 0 once passed


class CreateInterviewResponse(BaseModel):
    interview: InterviewStatus
    questions: list[Question]


class AnswerResponse(BaseModel):
    answer_id: str
    question_id: str | None = None
    question_index: int
    transcript: str
    metrics: dict[str, Any]
    heuristic_scores: dict[str, Any]
    analysis: dict[str, Any] | None = None
    followup: str | None = None
    followup_question: Question | None = None
    questions: list[Question] | None = None
    next_question_id: str | None = None
    all_answered: bool = False
    router: dict[str, Any] | None = None
    transcript_meta: dict[str, Any] | None = None


class ReportResponse(BaseModel):
    interview_id: str
    status: str
    report: dict[str, Any] | None = None
    created_at: str | None = None
