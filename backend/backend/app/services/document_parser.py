import io
import re
from pathlib import Path

from app.core.errors import ValidationAppError

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}

SKILL_TAXONOMY: dict[str, list[str]] = {
    "languages": ["python", "java", "javascript", "typescript", "go", "golang", "rust", "c++", "c#", "ruby", "php", "kotlin", "swift", "scala", "sql", "bash", "r"],
    "frontend": ["react", "vue", "angular", "svelte", "next.js", "nuxt", "tailwind", "redux", "html", "css", "vite", "webpack"],
    "backend": ["fastapi", "django", "flask", "spring boot", "express", "nestjs", "graphql", "rest", "grpc", "microservices", "node.js", "rails"],
    "data": ["pandas", "numpy", "spark", "airflow", "dbt", "etl", "kafka", "hadoop", "snowflake", "bigquery", "tableau", "power bi"],
    "ml": ["pytorch", "tensorflow", "scikit-learn", "llm", "nlp", "machine learning", "deep learning", "rag", "langchain", "transformers", "mlops", "computer vision"],
    "cloud": ["aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible", "jenkins", "github actions", "ci/cd", "lambda", "serverless"],
    "datastores": ["postgresql", "postgres", "mysql", "mongodb", "redis", "elasticsearch", "dynamodb", "sqlite", "cassandra", "neo4j"],
    "practices": ["agile", "scrum", "tdd", "code review", "system design", "observability", "security", "performance tuning", "unit testing"],
    "soft": ["leadership", "mentoring", "communication", "stakeholder management", "project management", "cross-functional", "problem solving", "collaboration"],
}

ACTION_VERB_PATTERN = re.compile(
    r"\b(built|designed|led|implemented|migrated|launched|scaled|optimized|automated|reduced|increased|owned|architected|delivered|mentored)\b",
    re.IGNORECASE,
)
METRIC_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(%|percent|x|k|m|ms|s|seconds|minutes|hours|users|requests|customers)", re.IGNORECASE)
YEARS_PATTERN = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_PATTERN = re.compile(r"https?://[^\s)]+")
TITLE_PATTERN = re.compile(
    r"(senior|staff|principal|lead|junior|associate)?\s*([a-z]+(?:\s[a-z]+)?)\s*(engineer|developer|manager|scientist|analyst|architect|designer|intern)",
    re.IGNORECASE,
)


def extract_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValidationAppError(
            f"unsupported file type '{suffix}'",
            details={"supported": sorted(SUPPORTED_SUFFIXES)},
        )
    if suffix == ".pdf":
        text = _extract_pdf(content)
    elif suffix == ".docx":
        text = _extract_docx(content)
    else:
        text = content.decode("utf-8", errors="ignore")

    cleaned = normalize_text(text)
    if len(cleaned) < 30:
        raise ValidationAppError(
            "could not extract readable text from document (it may be a scanned image)",
            details={"filename": filename},
        )
    return cleaned


def _extract_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValidationAppError("pypdf is not installed; cannot parse PDF") from exc
    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(content: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise ValidationAppError("python-docx is not installed; cannot parse DOCX") from exc
    document = Document(io.BytesIO(content))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_skills(text: str) -> dict[str, list[str]]:
    lowered = text.lower()
    found: dict[str, list[str]] = {}
    for category, skills in SKILL_TAXONOMY.items():
        hits = [skill for skill in skills if _contains_skill(lowered, skill)]
        if hits:
            found[category] = hits
    return found


def _contains_skill(lowered_text: str, skill: str) -> bool:
    if skill in {"c++", "c#", "r"}:
        return bool(re.search(rf"(?<![\w]){re.escape(skill)}(?![\w])", lowered_text))
    return skill in lowered_text


def flat_skills(skills_by_category: dict[str, list[str]]) -> list[str]:
    return sorted({skill for skills in skills_by_category.values() for skill in skills})


def extract_metrics(text: str) -> list[str]:
    return [f"{value} {unit}" for value, unit in METRIC_PATTERN.findall(text)][:15]


def extract_years_experience(text: str) -> float | None:
    matches = [float(value) for value in YEARS_PATTERN.findall(text)]
    candidate = max(matches) if matches else None
    if candidate and candidate <= 45:
        return candidate
    return None


def guess_titles(text: str, limit: int = 3) -> list[str]:
    titles: list[str] = []
    for prefix, core, suffix in TITLE_PATTERN.findall(text):
        title = " ".join(part for part in (prefix, core, suffix) if part).strip()
        title = re.sub(r"\s+", " ", title).title()
        if title not in titles:
            titles.append(title)
    return titles[:limit]


def parse_resume(text: str) -> dict:
    skills = detect_skills(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return {
        "skills": skills,
        "skill_list": flat_skills(skills),
        "titles": guess_titles(text),
        "years_experience": extract_years_experience(text),
        "achievements": extract_metrics(text),
        "action_verbs": sorted({match.lower() for match in ACTION_VERB_PATTERN.findall(text)}),
        "emails": list(dict.fromkeys(EMAIL_PATTERN.findall(text)))[:3],
        "links": list(dict.fromkeys(URL_PATTERN.findall(text)))[:5],
        "line_count": len(lines),
        "char_count": len(text),
    }


def parse_job_description(text: str) -> dict:
    skills = detect_skills(text)
    requirement_lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip() and re.search(r"(require|must have|responsib|qualif|experience with|proficien|familiar)", line, re.IGNORECASE)
    ]
    return {
        "skills": skills,
        "skill_list": flat_skills(skills),
        "titles": guess_titles(text, limit=2),
        "min_years": extract_years_experience(text),
        "requirements": requirement_lines[:12],
        "char_count": len(text),
    }


def match_resume_to_jd(resume: dict, jd: dict) -> dict:
    resume_skills = set(resume.get("skill_list", []))
    jd_skills = set(jd.get("skill_list", []))
    if not jd_skills:
        return {"match_score": None, "matched": [], "missing": [], "coverage": None}
    matched = sorted(resume_skills & jd_skills)
    missing = sorted(jd_skills - resume_skills)
    coverage = round(len(matched) / len(jd_skills) * 100, 1)
    return {
        "match_score": coverage,
        "coverage": coverage,
        "matched": matched,
        "missing": missing,
        "extra": sorted(resume_skills - jd_skills)[:20],
    }
