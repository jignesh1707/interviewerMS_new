from app.llm.providers.base import LLMMessage

SYSTEM = (
    "You are a strict but constructive interview answer evaluator. "
    "You judge answers against the STAR framework and score them honestly. "
    "You always return strict JSON and never wrap it in prose."
)


def build_answer_analysis_messages(
    *,
    question: str,
    transcript: str,
    metrics: dict,
    heuristic_scores: dict,
) -> list[LLMMessage]:
    user = f"""Evaluate this interview answer.

Question: {question}

Answer transcript:
{transcript[:6000]}

Deterministic text metrics (already computed):
- word_count: {metrics.get("word_count")}
- words_per_minute: {metrics.get("words_per_minute")}
- filler_total: {metrics.get("filler_total")} (ratio {metrics.get("filler_ratio")})
- hedge_total: {metrics.get("hedge_total")}
- star_coverage: {metrics.get("star_coverage")} (0-1)
- star_hits: {metrics.get("star_hits")}
- action_verbs: {metrics.get("action_verbs")}

Baseline heuristic scores (0-100): {heuristic_scores}

Rules:
- Anchor your scores to the metrics, but adjust for semantic quality the metrics cannot see.
- Do not reward verbosity. Penalise filler, hedging and missing results.
- Scores must be integers 0-100.

Return JSON exactly:
{{
  "scores": {{"clarity": int, "structure": int, "depth": int, "impact": int, "overall": int}},
  "star_breakdown": {{"situation": bool, "task": bool, "action": bool, "result": bool}},
  "strengths": [string],
  "improvements": [string],
  "missing_evidence": [string],
  "suggested_rewrite": string
}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]


def build_coaching_messages(*, question: str, transcript: str) -> list[LLMMessage]:
    user = f"""Give ONE actionable coaching tip (max 30 words) to improve the next answer to this question.

Question: {question}
Answer: {transcript[:2000]}

Return JSON exactly: {{"tip": string}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]
