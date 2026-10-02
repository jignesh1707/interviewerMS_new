import asyncio
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.errors import AllProvidersFailedError, ConflictError, TimeLimitError, ValidationAppError
from app.core.logging import get_logger
from app.core.url_safety import validate_callback_url
from app.llm.providers.base import LLMMessage
from app.llm.router import ModelRouter, get_router
from app.llm.safety import redact_secrets
from app.llm.tasks import LLMTask
from app.prompts.guard import clamp_scores, detect_injection
from app.voice.audio import safe_audio_suffix
from app.prompts.analysis import build_answer_analysis_messages, build_coaching_messages
from app.prompts.questions import build_followup_messages, build_question_generation_messages, build_resume_summary_messages
from app.prompts.report import (
    build_final_scoring_messages,
    build_report_narrative_messages,
    build_tips_messages,
)
from app.schemas.interview import CreateInterviewRequest
from app.services import document_parser as parser
from app.services.quota import QuotaService, Reservation
from app.services.storage import AsyncStore, Store, get_async_store
from app.services.text_analysis import aggregate_heuristic_scores, analyze_transcript, heuristic_score

logger = get_logger(__name__)


def _restrict(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class InterviewService:
    def __init__(self, store: Store | AsyncStore | None = None, router: ModelRouter | None = None) -> None:
        if isinstance(store, Store):
            store = AsyncStore(store)
        self.store = store or get_async_store()
        self.router = router or get_router()
        self.settings = get_settings()
        self.quota = QuotaService(self.store)

    async def create_interview(
        self,
        payload: CreateInterviewRequest,
        *,
        resume_bytes: tuple[str, bytes] | None = None,
        jd_bytes: tuple[str, bytes] | None = None,
        resume_summary_text: str | None = None,
        jd_summary_text: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an interview. With plans enabled, book the student's minutes first and give them back on failure."""
        reservation: Reservation | None = None
        if self.settings.plans_enabled:
            reservation = await self.quota.reserve(
                tenant_id or "default", payload.external_ref, payload.plan, payload.config.duration_minutes
            )
            payload = payload.model_copy(
                update={"config": payload.config.model_copy(update=reservation.config_overrides())}
            )
        try:
            return await self._create_interview(
                payload,
                resume_bytes=resume_bytes,
                jd_bytes=jd_bytes,
                tenant_id=tenant_id,
                reservation=reservation,
            )
        except BaseException:
            if reservation is not None:
                await asyncio.shield(self.quota.refund(reservation))
            raise

    async def _create_interview(
        self,
        payload: CreateInterviewRequest,
        *,
        resume_bytes: tuple[str, bytes] | None,
        jd_bytes: tuple[str, bytes] | None,
        tenant_id: str | None,
        reservation: Reservation | None,
    ) -> dict[str, Any]:
        consent = getattr(payload, "consent_to_ai_processing", None)
        if self.settings.require_consent and consent is not True:
            raise ValidationAppError(
                "consent_to_ai_processing must be true: candidate data is sent to external AI providers"
            )
        if payload.callback_url:
            await asyncio.to_thread(validate_callback_url, payload.callback_url)
        resume_text = payload.resume_text
        jd_text = payload.jd_text
        if resume_bytes:
            resume_text = await asyncio.to_thread(parser.extract_text, resume_bytes[0], resume_bytes[1])
        if jd_bytes:
            jd_text = await asyncio.to_thread(parser.extract_text, jd_bytes[0], jd_bytes[1])

        if not resume_text and not jd_text:
            raise ValidationAppError("provide at least a resume or a job description (text or file)")

        resume_summary = parser.parse_resume(resume_text or "")
        jd_summary = parser.parse_job_description(jd_text or "")
        match = parser.match_resume_to_jd(resume_summary, jd_summary)

        config = payload.config.model_dump()
        interview = await self.store.create_interview(
            role=payload.role,
            candidate_name=payload.candidate_name,
            resume_text=resume_text,
            jd_text=jd_text,
            config=config,
            callback_url=payload.callback_url,
            metadata=payload.metadata,
            tenant_id=tenant_id,
            external_ref=getattr(payload, "external_ref", None),
        )
        interview_id = interview["id"]
        await self.store.add_event(
            interview_id,
            "interview.created",
            {"role": payload.role, "consent_to_ai_processing": consent},
        )
        for source, text in (("resume", resume_text), ("jd", jd_text)):
            flags = detect_injection(text or "")
            if flags:
                await self.store.add_event(
                    interview_id, "integrity.possible_prompt_injection", {"source": source, "phrases": flags}
                )

        resume_narrative = None
        if resume_text:
            narrative, _ = await self._try_llm_json(
                LLMTask.RESUME_SUMMARY,
                build_resume_summary_messages(resume_text),
                interview_id,
            )
            resume_narrative = narrative

        try:
            questions, routing = await self._generate_questions(
                role=payload.role,
                resume_summary=resume_summary,
                jd_summary=jd_summary,
                match=match,
                config=config,
                resume_narrative=resume_narrative,
            )
        except AllProvidersFailedError as exc:
            await self.store.update_interview(interview_id, status="failed", error=str(exc))
            await self.store.add_event(interview_id, "interview.failed", {"stage": "question_generation"})
            raise

        if reservation is not None:
            # The clock starts once the questions are ready, so slow generation never eats the student's time.
            deadline = datetime.now(timezone.utc) + timedelta(
                minutes=reservation.minutes, seconds=reservation.grace_seconds
            )
            config.update(
                plan=reservation.plan,
                charged_minutes=reservation.minutes,
                deadline_at=deadline.isoformat(),
            )
        interview = await self.store.update_interview(
            interview_id,
            status="questions_ready",
            resume_summary=resume_summary,
            jd_summary=jd_summary,
            match_analysis=match,
            questions=questions,
            config=config,
        )
        await self.store.add_event(
            interview_id,
            "interview.questions_ready",
            {"count": len(questions), "routing": routing},
        )

        from app.services import webhook

        webhook.fire_and_forget(
            interview.get("callback_url"),
            "interview.created",
            {"interview_id": interview_id, "status": "questions_ready", "question_count": len(questions)},
        )
        return {"interview": interview, "questions": questions, "match": match}

    async def _generate_questions(
        self,
        *,
        role: str,
        resume_summary: dict[str, Any],
        jd_summary: dict[str, Any],
        match: dict[str, Any],
        config: dict[str, Any],
        resume_narrative: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        focus = list(config.get("focus_areas") or [])
        if resume_narrative:
            focus += resume_narrative.get("possible_weaknesses", [])[:3]
        if match.get("missing"):
            focus += [f"missing skill: {skill}" for skill in match["missing"][:5]]

        payload, result = await self.router.complete_json(
            LLMTask.QUESTION_GENERATION,
            build_question_generation_messages(
                role=role,
                resume_summary=resume_summary,
                jd_summary=jd_summary,
                match=match,
                question_count=int(config.get("question_count", self.settings.default_question_count)),
                focus_areas=focus,
            ),
            max_tokens=self.settings.llm_max_output_tokens,
        )

        raw_questions = payload.get("questions") or []
        if not isinstance(raw_questions, list) or not raw_questions:
            raise ValidationAppError("model returned no interview questions")

        questions = []
        for index, item in enumerate(raw_questions):
            if not isinstance(item, dict) or not item.get("question"):
                continue
            questions.append(
                {
                    "id": f"q{index}",
                    "index": index,
                    "category": str(item.get("category", "general")),
                    "question": str(item["question"]).strip(),
                    "star_focus": str(item.get("star_focus", "")),
                    "difficulty": str(item.get("difficulty", "medium")),
                    "what_to_listen_for": str(item.get("what_to_listen_for", "")),
                    "is_followup": False,
                    "parent_id": None,
                    "depth": 0,
                }
            )
        if not questions:
            raise ValidationAppError("model returned malformed interview questions")

        routing = {
            "task": result.task,
            "tier": result.tier,
            "provider": result.provider,
            "model": result.model,
            "fallbacks": max(0, len(result.attempts) - 1),
            "cost_usd": result.estimated_cost_usd,
        }
        return questions, routing

    async def submit_answer(
        self,
        interview_id: str,
        *,
        transcript: str,
        question_id: str | None = None,
        question_index: int | None = None,
        duration_seconds: float | None = None,
        audio_path: str | None = None,
    ) -> dict[str, Any]:
        interview = await self.store.get_interview(interview_id)
        questions = list(interview.get("questions") or [])
        if not questions:
            raise ValidationAppError("interview has no questions yet")
        if not transcript.strip():
            raise ValidationAppError("transcript is empty; no speech detected")
        if interview.get("status") == "completed":
            raise ConflictError("interview is already completed; no more answers can be submitted")
        self.ensure_time_remaining(interview)
        self.enforce_answer_caps(transcript=transcript, duration_seconds=duration_seconds)

        resolved_index, target = self._resolve_question(questions, question_id, question_index)
        if any(item.get("question_id") == target["id"] for item in await self.store.list_answers(interview_id)):
            raise ConflictError(
                "this question has already been answered", details={"question_id": target["id"]}
            )
        question_text = target["question"]
        metrics = analyze_transcript(transcript, duration_seconds)
        heuristics = heuristic_score(metrics)

        analysis = None
        injection_flags: list[str] = []
        router_trace: dict[str, Any] = {}
        config = interview.get("config") or {}

        if config.get("analyze_per_answer", True):
            analysis, routing = await self._try_llm_json(
                LLMTask.ANSWER_ANALYSIS,
                build_answer_analysis_messages(
                    question=question_text,
                    transcript=transcript,
                    metrics=metrics,
                    heuristic_scores=heuristics,
                ),
                interview_id,
            )
            if routing:
                router_trace["analysis"] = routing
            injection_flags = detect_injection(transcript)
            analysis = clamp_scores(
                analysis, heuristics, 10 if injection_flags else self.settings.score_clamp_delta
            )
        if injection_flags:
            metrics["integrity_flags"] = ["possible_prompt_injection"]
            await self.store.add_event(
                interview_id,
                "integrity.possible_prompt_injection",
                {"source": "answer", "question_index": resolved_index, "phrases": injection_flags},
            )

        overall = float(
            (analysis or {}).get("scores", {}).get("overall", heuristics.get("overall", 0))
        )
        existing_answers = await self.store.list_answers(interview_id)
        followup_question = None
        if self._should_generate_followup(
            config=config,
            questions=questions,
            target=target,
            existing_answers=existing_answers,
            overall=overall,
            analysis=analysis,
        ):
            payload, routing = await self._try_llm_json(
                LLMTask.FOLLOWUP_GENERATION,
                build_followup_messages(
                    question=question_text,
                    transcript=transcript,
                    gaps=list((analysis or {}).get("missing_evidence") or []),
                ),
                interview_id,
            )
            if routing:
                router_trace["followup"] = routing
            text = (payload or {}).get("followup")
            if text:
                followup_question = self._build_followup_question(questions, resolved_index, target, text)
                questions = self._insert_question(questions, resolved_index + 1, followup_question)

        answer = await self.store.add_answer(
            interview_id=interview_id,
            question_id=target["id"],
            question_index=resolved_index,
            question=question_text,
            transcript=transcript.strip(),
            audio_path=audio_path,
            metrics=metrics,
            heuristic_scores=heuristics,
            analysis=analysis,
            router=router_trace or None,
            duration_seconds=duration_seconds,
        )
        if followup_question:
            await self.store.update_interview(interview_id, status="in_progress", questions=questions)
        else:
            await self.store.update_interview(interview_id, status="in_progress")

        answered_ids = {item["question_id"] for item in existing_answers if item.get("question_id")}
        answered_ids.add(target["id"])
        next_question = self._next_pending_question(questions, answered_ids)

        await self.store.add_event(
            interview_id,
            "interview.answer_recorded",
            {
                "question_id": target["id"],
                "word_count": metrics["word_count"],
                "overall": overall,
                "followup_created": bool(followup_question),
            },
        )
        return {
            "answer_id": answer["id"],
            "question_id": target["id"],
            "question_index": resolved_index,
            "transcript": answer["transcript"],
            "metrics": metrics,
            "heuristic_scores": heuristics,
            "analysis": analysis,
            "followup": followup_question["question"] if followup_question else None,
            "followup_question": followup_question,
            "questions": questions if followup_question else None,
            "next_question_id": next_question["id"] if next_question else None,
            "all_answered": next_question is None,
            "router": router_trace or None,
        }

    @staticmethod
    def ensure_time_remaining(interview: dict[str, Any]) -> None:
        """Refuse new answers once the interview's server-side deadline has passed (finish is still allowed)."""
        raw = (interview.get("config") or {}).get("deadline_at")
        if not raw:
            return
        if datetime.now(timezone.utc) > datetime.fromisoformat(raw):
            raise TimeLimitError(
                "the time limit for this interview has been reached; call finish to get the report",
                details={"deadline_at": raw},
            )

    def enforce_answer_caps(self, *, transcript: str, duration_seconds: float | None) -> None:
        """Bound what one answer can cost: speech length (STT) and text length (LLM tokens)."""
        max_seconds = self.settings.max_answer_seconds
        if duration_seconds is not None and duration_seconds > max_seconds:
            raise ValidationAppError(
                f"an answer can be at most {max_seconds} seconds long",
                details={"max_answer_seconds": max_seconds},
            )
        max_chars = self.settings.max_transcript_chars
        if len(transcript) > max_chars:
            raise ValidationAppError(
                f"transcript exceeds {max_chars} characters", details={"max_transcript_chars": max_chars}
            )

    @staticmethod
    def _resolve_question(
        questions: list[dict[str, Any]],
        question_id: str | None,
        question_index: int | None,
    ) -> tuple[int, dict[str, Any]]:
        if question_id is not None:
            for index, item in enumerate(questions):
                if item.get("id") == question_id:
                    return index, item
            raise ValidationAppError(
                "question_id not found in this interview",
                details={"question_id": question_id},
            )
        assert question_index is not None
        if question_index < 0 or question_index >= len(questions):
            raise ValidationAppError(
                "question_index out of range", details={"max_index": len(questions) - 1}
            )
        return question_index, questions[question_index]

    @staticmethod
    def _should_generate_followup(
        *,
        config: dict[str, Any],
        questions: list[dict[str, Any]],
        target: dict[str, Any],
        existing_answers: list[dict[str, Any]],
        overall: float,
        analysis: dict[str, Any] | None,
    ) -> bool:
        if not config.get("ask_followups", True):
            return False
        if target.get("is_followup") or int(target.get("depth", 0)) > 0:
            return False
        max_followups = int(config.get("max_followups", 3))
        if max_followups <= 0:
            return False
        current_followups = sum(1 for item in questions if item.get("is_followup"))
        if current_followups >= max_followups:
            return False
        answered_followups = sum(1 for item in existing_answers if item.get("question_id", "").endswith("-f"))
        if answered_followups >= max_followups:
            return False
        threshold = float(config.get("followup_score_threshold", 75))
        has_gaps = bool((analysis or {}).get("missing_evidence"))
        return overall < threshold or has_gaps

    @staticmethod
    def _build_followup_question(
        questions: list[dict[str, Any]],
        parent_index: int,
        parent: dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        parent_id = parent["id"]
        existing = sum(1 for item in questions if item.get("parent_id") == parent_id)
        return {
            "id": f"{parent_id}-f{existing + 1}",
            "index": parent_index + 1,
            "category": "followup",
            "question": text.strip(),
            "star_focus": parent.get("star_focus", ""),
            "difficulty": parent.get("difficulty", "medium"),
            "what_to_listen_for": "a direct, quantified answer to the follow-up",
            "is_followup": True,
            "parent_id": parent_id,
            "depth": int(parent.get("depth", 0)) + 1,
        }

    @staticmethod
    def _insert_question(
        questions: list[dict[str, Any]], position: int, new_question: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if any(item.get("id") == new_question["id"] for item in questions):
            return questions
        updated = list(questions)
        updated.insert(position, new_question)
        for index, item in enumerate(updated):
            item["index"] = index
        return updated

    @staticmethod
    def _next_pending_question(
        questions: list[dict[str, Any]], answered_ids: set[str]
    ) -> dict[str, Any] | None:
        for item in questions:
            if item.get("id") not in answered_ids:
                return item
        return None

    async def finish_interview(self, interview_id: str, *, callback_url: str | None = None) -> dict[str, Any]:
        interview = await self.store.get_interview(interview_id)
        if interview.get("status") == "completed":
            saved = await self.store.get_report(interview_id)
            if saved:  # idempotent: do not pay for a second scoring run
                return saved["payload"]
        answers = await self.store.list_answers(interview_id)
        if not answers:
            raise ValidationAppError("cannot finish an interview with no recorded answers")

        await self.store.update_interview(interview_id, status="processing")
        await self.store.add_event(interview_id, "interview.processing", {"answers": len(answers)})

        questions = interview.get("questions") or []
        index_by_id = {item.get("id"): position for position, item in enumerate(questions)}

        def order_key(answer: dict[str, Any]) -> tuple[int, str]:
            question_ref = answer.get("question_id")
            if question_ref in index_by_id:
                return index_by_id[question_ref], answer.get("created_at") or ""
            return int(answer.get("question_index") or 0), answer.get("created_at") or ""

        per_answer = []
        for position, answer in enumerate(sorted(answers, key=order_key)):
            analysis = answer.get("analysis") or {}
            scores = analysis.get("scores") or answer.get("heuristic_scores") or {}
            metrics = answer.get("metrics") or {}
            per_answer.append(
                {
                    "index": position,
                    "question_id": answer.get("question_id"),
                    "is_followup": "-f" in (answer.get("question_id") or ""),
                    "question": answer["question"],
                    "overall": float(scores.get("overall", answer.get("heuristic_scores", {}).get("overall", 0))),
                    "words_per_minute": metrics.get("words_per_minute"),
                    "filler_total": metrics.get("filler_total"),
                    "star_coverage": metrics.get("star_coverage"),
                    "strengths": analysis.get("strengths", []),
                    "improvements": analysis.get("improvements", []),
                }
            )

        aggregate = aggregate_heuristic_scores(
            [answer.get("heuristic_scores") or {} for answer in answers]
        )
        match = interview.get("match_analysis") or {}
        trace: dict[str, Any] = {}

        scorecard, routing = await self._try_llm_json(
            LLMTask.FINAL_SCORING,
            build_final_scoring_messages(
                role=interview["role"],
                answer_digest=per_answer,
                aggregate=aggregate,
                match=match,
            ),
            interview_id,
        )
        if routing:
            trace["final_scoring"] = routing
        if not scorecard:
            scorecard = self._fallback_scorecard(per_answer, aggregate, match)

        tips = None
        tips_payload, routing = await self._try_llm_json(
            LLMTask.TIPS_GENERATION,
            build_tips_messages(
                role=interview["role"],
                dimension_scores=scorecard.get("dimension_scores", {}),
                critical_gaps=scorecard.get("critical_gaps", []),
                per_question=per_answer,
            ),
            interview_id,
        )
        if routing:
            trace["tips"] = routing
        if tips_payload:
            tips = tips_payload

        narrative = None
        narrative_payload, routing = await self._try_llm_json(
            LLMTask.REPORT_NARRATIVE,
            build_report_narrative_messages(
                role=interview["role"],
                scorecard=scorecard,
                aggregate=aggregate,
            ),
            interview_id,
        )
        if routing:
            trace["narrative"] = routing
        if narrative_payload:
            narrative = narrative_payload

        coaching = [answer.get("analysis", {}).get("suggested_rewrite") for answer in answers if answer.get("analysis")]

        report = {
            "interview_id": interview_id,
            "role": interview["role"],
            "candidate_name": interview.get("candidate_name"),
            "answer_count": len(answers),
            "overall_score": scorecard.get("overall_score"),
            "readiness_level": scorecard.get("readiness_level"),
            "dimension_scores": scorecard.get("dimension_scores", {}),
            "summary": scorecard.get("summary"),
            "top_strengths": scorecard.get("top_strengths", []),
            "critical_gaps": scorecard.get("critical_gaps", []),
            "story_bank_to_prepare": scorecard.get("story_bank_to_prepare", []),
            "likely_followup_topics": scorecard.get("likely_followup_topics", []),
            "improvement_plan": tips,
            "narrative": narrative,
            "aggregate_metrics": aggregate,
            "skill_match": match,
            "per_question": per_answer,
            "coaching_notes": [note for note in coaching if note],
            "routing_trace": trace,
            "generated_at": None,
        }

        from app.services.storage import utc_now

        report["generated_at"] = utc_now()
        await self.store.save_report(interview_id, report)
        await self.store.update_interview(interview_id, status="completed", finished_at=utc_now())
        await self.store.add_event(interview_id, "interview.completed", {"overall_score": report["overall_score"]})

        from app.services import webhook

        target = callback_url or interview.get("callback_url")
        await webhook.deliver(
            target,
            "interview.completed",
            {
                "interview_id": interview_id,
                "status": "completed",
                "overall_score": report["overall_score"],
                "readiness_level": report["readiness_level"],
            },
        )
        return report

    async def _try_llm_json(
        self,
        task: LLMTask,
        messages: list[LLMMessage],
        interview_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            payload, result = await self.router.complete_json(task, messages)
        except Exception as exc:  # noqa: BLE001
            safe_error = redact_secrets(str(exc)[:300])
            logger.warning("llm_task_degraded task=%s interview=%s error=%s", task, interview_id, safe_error)
            await self.store.add_event(
                interview_id,
                "interview.llm_degraded",
                {"task": str(task), "error": safe_error},
            )
            return None, None
        routing = {
            "task": result.task,
            "tier": result.tier,
            "provider": result.provider,
            "model": result.model,
            "fallbacks": max(0, len(result.attempts) - 1),
            "cost_usd": round(result.estimated_cost_usd, 6),
            "latency_ms": result.latency_ms,
        }
        return payload, routing

    @staticmethod
    def _fallback_scorecard(
        per_answer: list[dict[str, Any]], aggregate: dict[str, Any], match: dict[str, Any]
    ) -> dict[str, Any]:
        coverage = match.get("coverage") or 0.0
        overall = int(round(aggregate.get("overall", 0.0)))
        weak = [item["question"] for item in per_answer if item["overall"] < 60]
        if overall >= 80:
            readiness = "interview_ready"
        elif overall >= 65:
            readiness = "almost_ready"
        elif overall >= 45:
            readiness = "needs_practice"
        else:
            readiness = "not_ready"
        return {
            "overall_score": overall,
            "dimension_scores": {
                "communication": int(round(aggregate.get("clarity", 0.0))),
                "structure": int(round(aggregate.get("structure", 0.0))),
                "technical_depth": int(round(aggregate.get("depth", 0.0))),
                "impact": int(round(aggregate.get("impact", 0.0))),
                "role_fit": int(round(coverage)),
            },
            "readiness_level": readiness,
            "summary": "Heuristic scorecard generated without LLM assistance because all providers were unavailable.",
            "top_strengths": [f"Completed {len(per_answer)} STAR answers"],
            "critical_gaps": weak[:5],
            "story_bank_to_prepare": match.get("missing", [])[:5],
            "likely_followup_topics": match.get("missing", [])[:5],
        }

    def save_audio(self, interview_id: str, filename: str, content: bytes) -> str | None:
        """Persist uploaded audio only when RETAIN_AUDIO is on; by default only the transcript is kept."""
        if not self.settings.retain_audio:
            return None
        suffix = safe_audio_suffix(filename)
        directory = self.settings.storage_dir / "audio" / interview_id
        directory.mkdir(parents=True, exist_ok=True)
        _restrict(directory, 0o700)
        target = directory / f"{uuid.uuid4().hex}{suffix}"
        target.write_bytes(content)
        _restrict(target, 0o600)
        return str(target)

    async def delete_interview(self, interview_id: str) -> None:
        """Erase an interview: database rows and any stored audio."""
        await self.store.delete_interview(interview_id)
        await asyncio.to_thread(
            shutil.rmtree, self.settings.storage_dir / "audio" / interview_id, ignore_errors=True
        )
        logger.info("interview_deleted id=%s", interview_id)

    async def purge_expired(self) -> int:
        days = self.settings.retention_days
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        expired = await self.store.list_expired_interview_ids(cutoff)
        for interview_id in expired:
            await self.delete_interview(interview_id)
        if expired:
            logger.info("retention_purge deleted=%d older_than_days=%d", len(expired), days)
        return len(expired)

    async def build_status(self, interview: dict[str, Any], answered_count: int | None = None) -> dict[str, Any]:
        if answered_count is None:
            answered_count = await self.store.count_answers(interview["id"])
        questions = interview.get("questions") or []
        config = interview.get("config") or {}
        deadline_at = config.get("deadline_at")  # only set when plans are enabled
        return {
            "id": interview["id"],
            "status": interview["status"],
            "role": interview["role"],
            "candidate_name": interview.get("candidate_name"),
            "question_count": len(questions),
            "answered_count": answered_count,
            "created_at": interview["created_at"],
            "updated_at": interview["updated_at"],
            "finished_at": interview.get("finished_at"),
            "error": interview.get("error"),
            "duration_minutes": config.get("duration_minutes") if deadline_at else None,
            "deadline_at": deadline_at,
        }


_service: InterviewService | None = None


def get_interview_service() -> InterviewService:
    global _service
    if _service is None:
        _service = InterviewService()
    return _service
