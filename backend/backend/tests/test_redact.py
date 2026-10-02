"""Strip personal identifiers from text before it goes to an AI provider (data minimization)."""

import pytest

from app.prompts.redact import redact_resume, redact_text

RESUME = """Jane Doe
jane.doe@example.com | +1 (415) 555-0132 | linkedin.com/in/janedoe | https://github.com/janedoe
123 Market Street, Apt 4B, San Francisco, CA 94105

Work authorization: F-1 OPT valid through June 2027, will require H-1B sponsorship
Senior backend engineer. Python, FastAPI, PostgreSQL.
- Reduced p95 latency by 40% and improved the F1 score of the ranking model from 0.71 to 0.83
- Led a team of 5 engineers, 2019-2023, shipping 12 releases
"""


# ----------------------------------------------------------------------------- direct identifiers


@pytest.mark.parametrize(
    "text",
    [
        "write to jane.doe+jobs@mail.example.co.uk today",
        "UPPER@EXAMPLE.COM",
    ],
)
def test_emails_are_removed(text):
    assert "@" not in redact_text(text)
    assert "[email]" in redact_text(text)


@pytest.mark.parametrize(
    "phone",
    [
        "+1 (415) 555-0132",
        "415-555-0132",
        "(415) 555 0132",
        "415.555.0132",
        "+91 98765 43210",
        "98765 43210",
        "+44 20 7946 0958",
        "4155550132",
    ],
)
def test_phone_numbers_are_removed(phone):
    cleaned = redact_text(f"call me on {phone} after five")
    assert "[phone]" in cleaned
    assert not any(ch.isdigit() for ch in cleaned.replace("[phone]", ""))


@pytest.mark.parametrize(
    "link, handle",
    [
        ("https://github.com/janedoe/project", "janedoe"),
        ("www.janedoe.dev/portfolio", "janedoe"),
        ("linkedin.com/in/jane-doe-123", "jane-doe"),
        ("github.com/janedoe", "janedoe"),
    ],
)
def test_links_are_removed(link, handle):
    cleaned = redact_text(f"see {link} for details")
    assert "[link]" in cleaned
    assert handle not in cleaned


def test_government_style_ids_are_removed():
    assert "[id]" in redact_text("SSN 123-45-6789 on file")
    assert "123-45-6789" not in redact_text("SSN 123-45-6789 on file")


def test_street_addresses_and_zip_codes_are_removed():
    cleaned = redact_text("I live at 123 Market Street, Apt 4B, San Francisco, CA 94105 now")
    assert "123 Market" not in cleaned and "94105" not in cleaned
    assert "[address]" in cleaned


# ----------------------------------------------------------------------------- what must survive


def test_metrics_dates_and_skills_are_left_alone():
    text = "Reduced p95 latency by 40%, cut cost 1,500,000 USD, 5+ years, 2019-2023, Python 3.11, 12 releases"
    assert redact_text(text) == text


def test_an_f1_score_is_not_mistaken_for_an_f1_visa():
    resume = "Improved the F1 score of the ranking model from 0.71 to 0.83"
    assert "F1 score" in redact_resume(resume)


def test_a_list_of_years_is_not_a_phone_number():
    text = "Revenue in 2020 2021 2022 grew steadily"
    assert redact_text(text) == text


# ----------------------------------------------------------------------------- resumes


def test_a_whole_resume_loses_identifiers_but_keeps_the_substance():
    cleaned = redact_resume(RESUME)
    for secret in ("Jane Doe", "jane.doe@example.com", "415", "janedoe", "Market Street", "94105"):
        assert secret not in cleaned, secret
    for kept in (
        "Senior backend engineer",
        "Python, FastAPI, PostgreSQL",
        "Reduced p95 latency by 40%",
        "F1 score",
        "Led a team of 5 engineers",
    ):
        assert kept in cleaned, kept


def test_work_authorization_lines_are_dropped():
    cleaned = redact_resume(RESUME)
    for word in ("F-1", "OPT", "H-1B", "sponsorship", "Work authorization"):
        assert word not in cleaned, word


@pytest.mark.parametrize(
    "line",
    [
        "Authorized to work in the US on STEM OPT",
        "Visa status: F-1 student",
        "Requires visa sponsorship",
        "EAD valid until 2027",
        "US citizen",
        "Green card holder",
    ],
)
def test_immigration_related_lines_are_dropped(line):
    cleaned = redact_resume(f"Backend engineer\n{line}\nPython")
    assert "Backend engineer" in cleaned and "Python" in cleaned
    assert line not in cleaned


def test_the_first_line_name_is_removed_but_a_job_title_is_not():
    assert "Jane Doe" not in redact_resume("Jane Doe\nPython engineer")
    assert "JANE DOE" not in redact_resume("JANE DOE\nPython engineer")
    assert redact_resume("Senior Backend Engineer\nPython").startswith("Senior Backend Engineer")
    assert redact_resume("Resume\nPython").startswith("Resume")


def test_a_known_candidate_name_is_removed_wherever_it_appears():
    cleaned = redact_resume("Objective: Priya Raman seeks a role.\nPriya built it.", names=["Priya Raman"])
    assert "Priya" not in cleaned and "Raman" not in cleaned
    assert cleaned.count("[name]") >= 2


def test_redaction_is_idempotent():
    once = redact_resume(RESUME, names=["Jane Doe"])
    assert redact_resume(once, names=["Jane Doe"]) == once


def test_empty_input_is_fine():
    assert redact_resume("") == "" and redact_text("") == ""


# ----------------------------------------------------------------------------- spoken answers


def test_answers_lose_contact_details_but_the_first_line_is_kept():
    answer = "Our team of three shipped it. You can reach me at 415-555-0132 or jane@example.com."
    cleaned = redact_text(answer)
    assert cleaned.startswith("Our team of three shipped it.")
    assert "415" not in cleaned and "@" not in cleaned
