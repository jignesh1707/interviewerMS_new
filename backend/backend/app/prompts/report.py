from app.llm.providers.base import LLMMessage

SYSTEM = (
    "You are a hiring panel lead producing a final interview scorecard. "
    "You are calibrated, evidence-based and specific. "
    "You always return strict JSON and never wrap it in prose."
)


def build_final_scoring_messages(*, role: str, answer_digest: list[dict], aggregate: dict, match: dict) -> list[LLMMessage]:
    digest_lines = []
    for item in answer_digest:
        digest_lines.append(
            f"- Q{item.get('index')}: {item.get('question')}\n"
            f"  score={item.get('overall')} metrics_wpm={item.get('words_per_minute')} "
            f"fillers={item.get('filler_total')} star={item.get('star_coverage')}\n"
            f"  strengths={item.get('strengths')}\n"
            f"  improvements={item.get('improvements')}"
        )
    user = f"""Produce the final interview scorecard for the role "{role}".

Per-answer digest:
{chr(10).join(digest_lines) if digest_lines else "no answers recorded"}

Aggregate heuristic scores (0-100): {aggregate}
Resume/JD skill coverage: {match.get("coverage")}%

Return JSON exactly:
{{
  "overall_score": int,
  "dimension_scores": {{"communication": int, "structure": int, "technical_depth": int, "impact": int, "role_fit": int}},
  "readiness_level": "not_ready" | "needs_practice" | "almost_ready" | "interview_ready",
  "summary": string,
  "top_strengths": [string],
  "critical_gaps": [string],
  "story_bank_to_prepare": [string],
  "likely_followup_topics": [string]
}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]


def build_tips_messages(*, role: str, dimension_scores: dict, critical_gaps: list[str], per_question: list[dict]) -> list[LLMMessage]:
    weak = [item.get("question") for item in per_question if float(item.get("overall", 100)) < 60]
    user = f"""Create a prioritised improvement plan for a candidate interviewing for "{role}".

Dimension scores: {dimension_scores}
Critical gaps: {", ".join(critical_gaps) if critical_gaps else "none"}
Weakest questions: {"; ".join(str(q) for q in weak[:5]) or "none"}

Return JSON exactly:
{{"quick_wins": [string], "one_week_plan": [string], "thirty_day_plan": [string], "practice_prompts": [string]}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]


def build_report_narrative_messages(*, role: str, scorecard: dict, aggregate: dict) -> list[LLMMessage]:
    user = f"""Write a concise recruiter-facing narrative for the interview conducted for "{role}".

Scorecard: {scorecard}
Aggregate metrics: {aggregate}

Rules:
- 120-180 words, plain text, no markdown headings.
- Reference concrete evidence from the scorecard.

Return JSON exactly: {{"narrative": string, "recommendation": "strong_hire" | "hire" | "borderline" | "no_hire"}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]
