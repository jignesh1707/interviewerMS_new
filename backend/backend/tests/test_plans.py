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
    # No plan's student data may reach DeepSeek: the allow-list is the guarantee, the model lists are not.
    assert economy.llm_allowed_providers is not None and premium.llm_allowed_providers is not None
    assert set(economy.llm_allowed_providers) == {"openai", "anthropic"}
    assert set(premium.llm_allowed_providers) == {"anthropic", "openai"}
    assert isinstance(config, PlansConfig)


SKU_YAML = PLANS_YAML + """
skus:
  small: {plan: economy, minutes: 30, days: 10}
  big: {plan: economy, minutes: 90, days: 45}
"""


def test_skus_load_with_their_wallet_size_and_validity(tmp_path):
    config = load_plans(_write(tmp_path, SKU_YAML))
    assert config.sku("big").plan == "economy"
    assert (config.sku("big").minutes, config.sku("big").days) == (90, 45)
    assert load_plans(_write(tmp_path)).skus == {}  # a file without skus still loads


def test_unknown_sku_is_a_validation_error_listing_the_choices(tmp_path):
    config = load_plans(_write(tmp_path, SKU_YAML))
    with pytest.raises(ValidationAppError) as error:
        config.sku("platinum")
    assert error.value.details["allowed"] == ["big", "small"]


def test_a_sku_for_an_undefined_plan_is_rejected_at_load(tmp_path):
    with pytest.raises(ValueError):
        load_plans(_write(tmp_path, PLANS_YAML + "skus:\n  x: {plan: gold, minutes: 30, days: 5}\n"))


def test_a_sku_smaller_than_one_interview_is_rejected_at_load(tmp_path):
    with pytest.raises(ValueError):
        load_plans(_write(tmp_path, PLANS_YAML + "skus:\n  x: {plan: economy, minutes: 10, days: 5}\n"))


def test_shipped_subscription_and_addon_offers():
    """The shipped plans.yaml: what each subscription includes and what each add-on sells."""
    from app.config import BASE_DIR

    config = load_plans(BASE_DIR / "plans.yaml")
    included = {name: (config.plan(name).pack_minutes // 15, config.plan(name).pack_days)
                for name in ("included_weekly", "included_monthly", "included_quarterly")}
    assert included == {"included_weekly": (2, 14), "included_monthly": (5, 35), "included_quarterly": (18, 100)}
    for name in included:
        assert config.plan(name).durations == [15]  # whole 15-minute interviews only
        assert set(config.plan(name).llm_allowed_providers) == {"openai", "anthropic"}
    assert {name: (sku.plan, sku.minutes, sku.days) for name, sku in config.skus.items()} == {
        "addon_5": ("economy", 150, 30),  # 10 x 15 minutes
        "addon_10_economy": ("economy", 300, 30),  # 20 x 15 minutes
        "addon_10_premium": ("premium", 75, 30),  # 5 x 15 minutes with Anthropic
        "addon_20_economy": ("economy", 650, 30),
        "addon_20_premium": ("premium", 250, 30),  # 10 x 25 minutes with Anthropic
    }
