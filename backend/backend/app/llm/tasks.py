from enum import StrEnum


class LLMTask(StrEnum):
    RESUME_SUMMARY = "resume_summary"
    JD_METADATA = "jd_metadata"
    FOLLOWUP_GENERATION = "followup_generation"
    ANSWER_COACHING = "answer_coaching"
    QUESTION_GENERATION = "question_generation"
    TIPS_GENERATION = "tips_generation"
    ANSWER_ANALYSIS = "answer_analysis"
    FINAL_SCORING = "final_scoring"
    REPORT_NARRATIVE = "report_narrative"


TIER_INTENT: dict[str, str] = {
    "cheap": "high-volume, low-reasoning work such as tagging, short follow-ups and coaching hints",
    "standard": "core structured generation such as STAR question sets and improvement tips",
    "premium": "high-stakes reasoning such as final scoring and report narratives",
}
