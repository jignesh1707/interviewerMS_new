import pytest

from app.llm.router import extract_json
from app.services import document_parser as parser
from app.services.text_analysis import aggregate_heuristic_scores, analyze_transcript, heuristic_score


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_code_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_prose():
    assert extract_json('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}


def test_extract_json_invalid():
    with pytest.raises(ValueError):
        extract_json("not json at all")


def test_analyze_transcript_star_detection():
    text = (
        "Situation: our API was slow during peak hours. My task was to reduce latency. "
        "I built a caching layer and optimized the database queries. As a result we reduced p95 latency by 60 percent."
    )
    metrics = analyze_transcript(text, duration_seconds=60)
    assert metrics["word_count"] > 20
    assert metrics["star_coverage"] == 1.0
    assert metrics["words_per_minute"] is not None
    assert any(verb in metrics["action_verbs"] for verb in ("built", "optimized"))


def test_filler_detection():
    metrics = analyze_transcript("Um, I basically just, like, worked on it, you know.", duration_seconds=10)
    assert metrics["filler_total"] >= 4


def test_heuristic_score_bounds():
    metrics = analyze_transcript("Short answer.")
    scores = heuristic_score(metrics)
    for value in scores.values():
        assert 0 <= value <= 100


def test_aggregate_scores():
    aggregate = aggregate_heuristic_scores(
        [
            {"clarity": 80, "structure": 60, "depth": 70, "impact": 50, "overall": 65},
            {"clarity": 60, "structure": 80, "depth": 50, "impact": 70, "overall": 65},
        ]
    )
    assert aggregate["clarity"] == 70.0
    assert aggregate["overall"] == 65.0


def test_parse_resume_and_jd_match():
    resume = """
    Jane Doe - Senior Backend Engineer
    Built a Python FastAPI service handling 2 million requests per day.
    Reduced p95 latency by 40 percent.
    Skills: Python, PostgreSQL, Docker, AWS, Kubernetes
    """
    jd = """
    We require a backend engineer.
    Must have Python and PostgreSQL experience.
    Familiar with Kafka and Terraform.
    5+ years of experience.
    """
    resume_data = parser.parse_resume(resume)
    jd_data = parser.parse_job_description(jd)
    match = parser.match_resume_to_jd(resume_data, jd_data)

    assert "python" in resume_data["skill_list"]
    assert "postgresql" in resume_data["skill_list"]
    assert resume_data["years_experience"] is None
    assert "python" in jd_data["skill_list"]
    assert "kafka" in match["missing"]
    assert "python" in match["matched"]
    assert match["coverage"] is not None
