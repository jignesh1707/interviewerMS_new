from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.errors import ValidationAppError
from app.core.plans import PlansConfig, load_plans, period_key

PLANS_YAML = """
default_plan: standard
grace_seconds: 45
profiles:
  15: {question_count: 7, max_followups: 2}
  20: {question_count: 9, max_followups: 3}
plans:
  standard:
    included_minutes: 150
    period: monthly
    durations: [15, 20]
    default_duration: 15
  pack:
    included_minutes: 0
    period: none
    durations: [15]
    default_duration: 15
"""


def _write(tmp_path: Path, text: str = PLANS_YAML) -> Path:
    path = tmp_path / "plans.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_plans_profiles_and_grace(tmp_path):
    config = load_plans(_write(tmp_path))
    assert config.grace_seconds == 45
    assert config.plan(None).included_minutes == 150  # None means the default plan
    assert config.plan("pack").period == "none"
    assert config.profile(20).question_count == 9
    assert config.profile(20).max_followups == 3


def test_unknown_plan_is_a_validation_error(tmp_path):
    config = load_plans(_write(tmp_path))
    with pytest.raises(ValidationAppError):
        config.plan("platinum")


def test_duration_defaults_and_must_be_allowed(tmp_path):
    config = load_plans(_write(tmp_path))
    plan = config.plan("standard")
    assert config.resolve_duration(plan, None) == 15
    assert config.resolve_duration(plan, 20) == 20
    with pytest.raises(ValidationAppError) as exc:
        config.resolve_duration(plan, 25)
    assert exc.value.details["allowed"] == [15, 20]


def test_plan_duration_without_a_profile_is_rejected_at_load(tmp_path):
    bad = PLANS_YAML.replace("durations: [15, 20]", "durations: [15, 30]")
    with pytest.raises(ValueError, match="30"):
        load_plans(_write(tmp_path, bad))


def test_default_plan_must_exist(tmp_path):
    bad = PLANS_YAML.replace("default_plan: standard", "default_plan: missing")
    with pytest.raises(ValueError, match="missing"):
        load_plans(_write(tmp_path, bad))


def test_default_duration_must_be_allowed(tmp_path):
    bad = PLANS_YAML.replace("default_duration: 15\n  pack", "default_duration: 20\n  pack").replace(
        "durations: [15, 20]\n    default_duration: 20", "durations: [15]\n    default_duration: 20"
    )
    with pytest.raises(ValueError, match="default_duration"):
        load_plans(_write(tmp_path, bad))


def test_period_keys():
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    assert period_key("monthly", now) == "2026-10"
    assert period_key("none", now) == "lifetime"


def test_shipped_plans_file_matches_the_10_dollar_offer():
    """plans.yaml in the repo: 150 minutes = 10 x 15, 8 x 20 or 6 x 25."""
    from app.config import BASE_DIR

    config = load_plans(BASE_DIR / "plans.yaml")
    plan = config.plan(None)
    assert plan.included_minutes == 150
    assert plan.durations == [15, 20, 25]
    assert isinstance(config, PlansConfig)
