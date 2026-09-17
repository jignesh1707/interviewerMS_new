from app.llm.providers.base import LLMMessage

SYSTEM = (
    "You are a senior technical interviewer and hiring manager. "
    "You design behavioural interviews using the STAR method (Situation, Task, Action, Result). "
    "You always return strict JSON and never wrap it in prose."
)


def build_question_generation_messages(
    *,
    role: str,
    resume_summary: dict,
    jd_summary: dict,
    match: dict,
    question_count: int,
    focus_areas: list[str] | None = None,
) -> list[LLMMessage]:
    user = f"""Create {question_count} behavioural interview questions for the role "{role}".

Candidate resume signals:
- Skills: {", ".join(resume_summary.get("skill_list", [])[:30]) or "none detected"}
- Titles: {", ".join(resume_summary.get("titles", [])) or "unknown"}
- Years of experience: {resume_summary.get("years_experience")}
- Quantified achievements: {"; ".join(resume_summary.get("achievements", [])[:8]) or "none detected"}

Job description signals:
- Required skills: {", ".join(jd_summary.get("skill_list", [])[:30]) or "none detected"}
- Requirements: {"; ".join(jd_summary.get("requirements", [])[:8]) or "none detected"}
- Minimum years: {jd_summary.get("min_years")}

Skill match analysis:
- Coverage: {match.get("coverage")}%
- Matched skills: {", ".join(match.get("matched", [])[:20]) or "none"}
- Missing skills: {", ".join(match.get("missing", [])[:20]) or "none"}

Priority focus areas: {", ".join(focus_areas) if focus_areas else "infer from the gaps above"}

Requirements:
- Every question must require a STAR-structured answer and invite a concrete, quantified result.
- Include a mix of: gap-probing questions (missing skills), depth questions on matched strength areas, and collaboration/ownership questions.
- Ask one question at a time; no compound questions.

Return JSON exactly in this shape:
{{
  "role": string,
  "questions": [
    {{
      "category": "gap_probe" | "technical_depth" | "ownership" | "collaboration" | "problem_solving",
      "question": string,
      "star_focus": string,
      "difficulty": "easy" | "medium" | "hard",
      "what_to_listen_for": string
    }}
  ]
}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]


def build_followup_messages(*, question: str, transcript: str, gaps: list[str]) -> list[LLMMessage]:
    user = f"""Interview question: {question}

Candidate answer transcript:
{transcript[:4000]}

Detected weaknesses: {", ".join(gaps) if gaps else "none"}

Write ONE short follow-up question (max 25 words) that probes the weakest part of the answer.
Return JSON exactly: {{"followup": string, "reason": string}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]


def build_resume_summary_messages(resume_text: str) -> list[LLMMessage]:
    user = f"""Summarise this resume into JSON for interview planning.

Resume:
{resume_text[:6000]}

Return JSON exactly:
{{"headline": string, "seniority": string, "domains": [string], "top_strengths": [string], "possible_weaknesses": [string]}}"""
    return [LLMMessage(role="system", content=SYSTEM), LLMMessage(role="user", content=user)]
