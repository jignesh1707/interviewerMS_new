from pathlib import Path

import pytest

from app.core.errors import ValidationAppError
from app.core.plans import PlansConfig, load_plans

PLANS_YAML = """
default_plan: economy
grace_seconds: 45
profiles:
  15: {question_count: 7, max_followups: 2}
  20: {question_count: 9, max_followups: 3}
plans:
  economy:
    pack_minutes: 150
    pack_days: 30
    durations: [15, 20]
    default_duration: 15
    llm_profile: economy
  premium:
    pack_minutes: 250
    pack_days: 45
    durations: [15]
    default_duration: 15
    llm_profile: premium
    llm_allowed_providers: [anthropic, openai]
    refund: {max_interviews_started: 2, max_minutes_used: 30, rule: all}
"""


def _write(tmp_path: Path, text: str = PLANS_YAML) -> Path:
    path = tmp_path / "plans.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_plans_profiles_and_grace(tmp_path):
    config = load_plans(_write(tmp_path))
    assert config.grace_seconds == 45
    assert config.plan(None).pack_minutes == 150  # None means the default plan
    premium = config.plan("premium")
    assert (premium.pack_minutes, premium.pack_days) == (250, 45)
    assert config.profile(20).question_count == 9
    assert config.profile(20).max_followups == 3


def test_each_plan_names_its_model_profile_and_allowed_providers(tmp_path):
    config = load_plans(_write(tmp_path))
    assert config.plan("economy").llm_profile == "economy"
    assert config.plan("economy").llm_allowed_providers is None  # no extra restriction
    assert config.plan("premium").llm_profile == "premium"
    assert config.plan("premium").llm_allowed_providers == ["anthropic", "openai"]


def test_refund_rule_defaults_to_one_interview_or_15_minutes(tmp_path):
    config = load_plans(_write(tmp_path))
    default = config.plan("economy").refund
    assert (default.max_interviews_started, default.max_minutes_used, default.rule) == (1, 15, "any")
    custom = config.plan("premium").refund
    assert (custom.max_interviews_started, custom.max_minutes_used, custom.rule) == (2, 30, "all")


def test_unknown_plan_is_a_validation_error(tmp_path):
    config = load_plans(_write(tmp_path))
    with pytest.raises(ValidationAppError) as exc:
        config.plan("platinum")
    assert exc.value.details["allowed"] == ["economy", "premium"]


def test_duration_defaults_and_must_be_allowed(tmp_path):
    config = load_plans(_write(tmp_path))
    plan = config.plan("economy")
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
    bad = PLANS_YAML.replace("default_plan: economy", "default_plan: missing")
    with pytest.raises(ValueError, match="missing"):
        load_plans(_write(tmp_path, bad))


def test_default_duration_must_be_allowed(tmp_path):
    bad = PLANS_YAML.replace("default_duration: 15\n    llm_profile: economy", "default_duration: 25\n    llm_profile: economy")
    with pytest.raises(ValueError, match="default_duration"):
        load_plans(_write(tmp_path, bad))


def test_pack_minutes_and_days_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        load_plans(_write(tmp_path, PLANS_YAML.replace("pack_minutes: 150", "pack_minutes: 0")))
    with pytest.raises(ValueError):
        load_plans(_write(tmp_path, PLANS_YAML.replace("pack_days: 30", "pack_days: 0")))


def test_shipped_plans_file_matches_the_two_offers():
    """plans.yaml in the repo: Economy 150 minutes, Premium 250 minutes, both valid 30 days."""
    from app.config import BASE_DIR

    config = load_plans(BASE_DIR / "plans.yaml")
    economy, premium = config.plan("economy"), config.plan("premium")
    assert (economy.pack_minutes, economy.pack_days) == (150, 30)
    assert (premium.pack_minutes, premium.pack_days) == (250, 30)
    assert economy.durations == premium.durations == [15, 20, 25]
    assert premium.llm_profile == "premium"
    # Premium student data must never reach DeepSeek: the allow-list is the guarantee, the model lists are not.
    assert "deepseek" not in (premium.llm_allowed_providers or ["deepseek"])
    assert isinstance(config, PlansConfig)
