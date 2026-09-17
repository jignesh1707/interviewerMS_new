from typing import Any

from app.llm.router import LLMResult, UsageTotals


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.overrides: dict[str, dict[str, Any]] = {}

    def _payload(self, task: str) -> dict[str, Any]:
        if task in self.overrides:
            return self.overrides[task]
        defaults = {
            "resume_summary": {
                "headline": "Backend engineer",
                "seniority": "mid",
                "domains": ["fintech"],
                "top_strengths": ["python"],
                "possible_weaknesses": ["limited kubernetes"],
            },
            "question_generation": {
                "role": "Backend Engineer",
                "questions": [
                    {
                        "category": "technical_depth",
                        "question": "Tell me about a time you scaled a Python API.",
                        "star_focus": "measurable latency result",
                        "difficulty": "medium",
                        "what_to_listen_for": "concrete metrics",
                    },
                    {
                        "category": "gap_probe",
                        "question": "Describe a project where you used Kubernetes.",
                        "star_focus": "learning speed",
                        "difficulty": "hard",
                        "what_to_listen_for": "honest gap handling",
                    },
                ],
            },
            "answer_analysis": {
                "scores": {"clarity": 80, "structure": 75, "depth": 78, "impact": 70, "overall": 76},
                "star_breakdown": {"situation": True, "task": True, "action": True, "result": True},
                "strengths": ["clear action description"],
                "improvements": ["quantify the result"],
                "missing_evidence": ["no latency numbers"],
                "suggested_rewrite": "Add the p95 latency improvement.",
            },
            "followup_generation": {"followup": "What was the p95 latency before and after?", "reason": "no metric"},
            "final_scoring": {
                "overall_score": 76,
                "dimension_scores": {
                    "communication": 80,
                    "structure": 75,
                    "technical_depth": 78,
                    "impact": 70,
                    "role_fit": 72,
                },
                "readiness_level": "almost_ready",
                "summary": "Solid structure, needs quantified impact.",
                "top_strengths": ["clear storytelling"],
                "critical_gaps": ["quantified impact"],
                "story_bank_to_prepare": ["kubernetes migration"],
                "likely_followup_topics": ["observability"],
            },
            "tips_generation": {
                "quick_wins": ["Add numbers to every result"],
                "one_week_plan": ["Write five STAR stories"],
                "thirty_day_plan": ["Do two mock interviews"],
                "practice_prompts": ["Describe a latency win"],
            },
            "report_narrative": {"narrative": "Candidate shows solid structure.", "recommendation": "hire"},
        }
        return defaults.get(task, {})

    async def complete(self, task: str, messages: list[Any], **kwargs: Any) -> LLMResult:
        self.calls.append({"task": task, "tier": kwargs.get("tier")})
        return LLMResult(
            text="{}",
            provider="fake",
            model="fake-model",
            tier=kwargs.get("tier") or "standard",
            task=task,
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.0,
            latency_ms=1,
        )

    async def complete_json(self, task: str, messages: list[Any], **kwargs: Any) -> tuple[dict[str, Any], LLMResult]:
        result = await self.complete(task, messages, **kwargs)
        return self._payload(str(task)), result

    def status(self) -> dict[str, Any]:
        usage = UsageTotals()
        return {
            "providers": {
                "openai": {"configured": True, "available": True},
                "deepseek": {"configured": True, "available": True},
                "anthropic": {"configured": True, "available": True},
            },
            "tiers": {},
            "tasks": {},
            "usage": {
                "calls": usage.calls,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
                "by_model": {},
            },
        }
