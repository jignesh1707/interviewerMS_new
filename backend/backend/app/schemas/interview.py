from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class InterviewConfig(BaseModel):
    question_count: int = Field(default=8, ge=3, le=15)
    focus_areas: list[str] = Field(default_factory=list)
    language: str = "en"
    ask_followups: bool = True
    analyze_per_answer: bool = True
    max_followups: int = Field(default=3, ge=0, le=15)
    followup_score_threshold: int = Field(default=75, ge=0, le=100)


class CreateInterviewRequest(BaseModel):
    role: str = Field(min_length=2, max_length=200)
    candidate_name: str | None = Field(default=None, max_length=200)
    resume_text: str | None = None
    jd_text: str | None = None
    callback_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    config: InterviewConfig = Field(default_factory=InterviewConfig)


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
    transcript: str = Field(min_length=1)
    duration_seconds: float | None = Field(default=None, ge=0)

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
