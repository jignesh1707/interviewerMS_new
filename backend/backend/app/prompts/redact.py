"""Strip personal identifiers from text before it is sent to an AI provider (data minimization).

The interview questions and scores need a candidate's skills, titles, achievements and answers, not their name or
contact details. This removes direct identifiers on a best-effort basis:

* emails, phone numbers, links (LinkedIn, GitHub, personal sites), ID numbers, street addresses and ZIP codes;
* for resumes, the name on the first line, any names the caller supplies, and lines about work authorization
  or immigration status (OPT, H-1B, visa sponsorship, citizenship...), which matter to F-1/OPT students.

It is NOT anonymization. A resume still names employers, schools and projects, and spoken answers can say anything.
Only a copy for the AI provider is redacted; the stored text and the analytics use the original.
"""

import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_SOCIAL_SITES = r"(?:linkedin|github|gitlab|twitter|facebook|instagram|medium|behance|dribbble|leetcode|kaggle|stackoverflow)"
LINK = re.compile(
    r"(?:https?://\S+|www\.\S+|\b" + _SOCIAL_SITES + r"\.com/\S+)",
    re.IGNORECASE,
)

PHONE = re.compile(
    r"""
    (?<![\w.])(?:
        \+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,5}){2,4}          # international, starts with +
      | (?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}              # US/Canada 415-555-0132, (415) 555 0132
      | \d{5}[\s.-]\d{5}                                              # 98765 43210
      | \d{10}                                                        # 4155550132
    )(?![\w])
    """,
    re.VERBOSE,
)

SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_STREET_WORDS = (
    r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Parkway|Pkwy|Highway|Hwy"
)
ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,4}(?:" + _STREET_WORDS + r")\b\.?(?:,?\s*(?:Apt|Suite|Ste|Unit|#)\s*\w+)?",
)
STATE_ZIP = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")

# Lines about work authorization or immigration status. Case-sensitive acronyms are kept separate so ordinary words
# ("opt", "cpt") and ML metrics ("F1 score") are not caught.
_AUTH_ACRONYMS = re.compile(r"\b(?:OPT|CPT|EAD|I-20|I-983|H-1B|H1B|H-4|L-1|F-1|J-1|TN)\b|\bF1\s+(?:visa|student|status)\b")
_AUTH_PHRASES = re.compile(
    r"work\s+(?:authori[sz]ation|permit|visa)|visa\s+(?:status|sponsorship|type)|(?:require|need)s?\s+(?:visa\s+)?sponsorship"
    r"|authori[sz]ed\s+to\s+work|green\s*card|permanent\s+resident|(?:U\.?S\.?|American)\s+citizen(?:ship)?"
    r"|STEM\s+OPT|curricular\s+practical\s+training|optional\s+practical\s+training",
    re.IGNORECASE,
)

# A first line is treated as a name only if it looks like one: 2-4 capitalised words, no digits, no job words.
_NOT_A_NAME = {
    "resume", "curriculum", "vitae", "cv", "summary", "profile", "objective", "experience", "education", "skills",
    "contact", "engineer", "developer", "manager", "analyst", "designer", "scientist", "intern", "consultant",
    "architect", "director", "specialist", "student", "senior", "junior", "lead", "software", "data", "backend",
    "frontend", "full", "stack", "machine", "learning", "product", "project", "technical", "associate", "graduate",
}
_NAME_TOKEN = re.compile(r"^(?:[A-Z][a-zA-Z'.-]*|[A-Z]{2,})$")


def redact_text(text: str, names: list[str] | tuple[str, ...] = ()) -> str:
    """Pattern-based redaction, safe for any text, including spoken answers."""
    if not text:
        return text
    out = EMAIL.sub("[email]", text)
    out = LINK.sub("[link]", out)
    out = SSN.sub("[id]", out)
    out = PHONE.sub("[phone]", out)
    out = ADDRESS.sub("[address]", out)
    out = STATE_ZIP.sub("[address]", out)
    return _redact_names(out, names)


def redact_resume(text: str, names: list[str] | tuple[str, ...] = ()) -> str:
    """Everything redact_text does, plus the first-line name and work-authorization lines (resumes only)."""
    if not text:
        return text
    kept_lines = [
        line for line in text.split("\n") if not (_AUTH_ACRONYMS.search(line) or _AUTH_PHRASES.search(line))
    ]
    out = "\n".join(_redact_first_line_name(kept_lines))
    return redact_text(out, names)


def _redact_first_line_name(lines: list[str]) -> list[str]:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        looks_like_name = (
            2 <= len(tokens) <= 4
            and len(stripped) < 40
            and not any(ch.isdigit() or ch in "@|/:" for ch in stripped)
            and all(_NAME_TOKEN.match(token) for token in tokens)
            and not any(token.lower().strip(".,") in _NOT_A_NAME for token in tokens)
        )
        if looks_like_name:
            lines = list(lines)
            lines[index] = "[name]"
        break  # only the first non-empty line is considered
    return lines


def _redact_names(text: str, names: list[str] | tuple[str, ...]) -> str:
    for name in names or ():
        name = (name or "").strip()
        if len(name) < 3:
            continue
        text = re.sub(re.escape(name), "[name]", text, flags=re.IGNORECASE)
        for part in name.split():
            if len(part) >= 3:
                # Exact capitalisation only, so a name like "Will" does not remove the word "will".
                text = re.sub(r"\b" + re.escape(part) + r"\b", "[name]", text)
    return text
