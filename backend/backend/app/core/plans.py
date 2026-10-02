"""Plans, packs and interview-length configuration, loaded from ``plans.yaml``.

A plan is something a student buys: a pack of ``pack_minutes`` that expires after ``pack_days``. Booking an interview
debits its length from the pack. Each allowed length has a profile that fixes how many questions and follow-ups it
gets, so a 15 minute interview cannot ask 15 questions. Each plan also names the model profile it is served with
(see models.yaml) and, optionally, the only AI providers it may ever use.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.core.errors import ValidationAppError


class DurationProfile(BaseModel):
    question_count: int = Field(ge=1, le=15)
    max_followups: int = Field(ge=0, le=15)


class RefundRule(BaseModel):
    """A pack can be refunded only while the student has barely used it."""

    max_interviews_started: int = Field(default=1, ge=0)  # "before the 2nd interview"
    max_minutes_used: int = Field(default=15, ge=0)  # "up to 15 minutes"
    rule: Literal["any", "all"] = "any"  # refundable if ANY condition still holds, or only if ALL do


class Plan(BaseModel):
    pack_minutes: int = Field(ge=1)
    pack_days: int = Field(ge=1)
    durations: list[int] = Field(min_length=1)
    default_duration: int
    llm_profile: str = "economy"  # a profile in models.yaml
    # A hard promise about where student data may go. When set, the router refuses any provider outside it,
    # whatever models.yaml says. Leave unset for "any configured provider".
    llm_allowed_providers: list[str] | None = None
    refund: RefundRule = Field(default_factory=RefundRule)

    @model_validator(mode="after")
    def default_is_allowed(self) -> "Plan":
        if self.default_duration not in self.durations:
            raise ValueError(f"default_duration {self.default_duration} is not in durations {self.durations}")
        return self


class PlansConfig(BaseModel):
    default_plan: str
    grace_seconds: int = Field(default=60, ge=0)
    profiles: dict[int, DurationProfile]
    plans: dict[str, Plan]

    @model_validator(mode="after")
    def check_references(self) -> "PlansConfig":
        if self.default_plan not in self.plans:
            raise ValueError(f"default_plan '{self.default_plan}' is not defined under plans")
        for name, plan in self.plans.items():
            for minutes in plan.durations:
                if minutes not in self.profiles:
                    raise ValueError(f"plan '{name}' allows {minutes} minutes but profiles has no entry for {minutes}")
        return self

    def plan(self, name: str | None) -> Plan:
        key = name or self.default_plan
        if key not in self.plans:
            raise ValidationAppError("unknown plan", details={"plan": key, "allowed": sorted(self.plans)})
        return self.plans[key]

    def profile(self, minutes: int) -> DurationProfile:
        return self.profiles[minutes]

    @staticmethod
    def resolve_duration(plan: Plan, requested: int | None) -> int:
        minutes = plan.default_duration if requested is None else requested
        if minutes not in plan.durations:
            raise ValidationAppError(
                "interview length is not available on this plan",
                details={"requested": minutes, "allowed": plan.durations},
            )
        return minutes


def load_plans(path: Path) -> PlansConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return PlansConfig.model_validate(data)


@lru_cache
def _cached(path: str) -> PlansConfig:
    return load_plans(Path(path))


def get_plans() -> PlansConfig:
    return _cached(str(get_settings().plans_config_path))


def validate_against_router(plans: PlansConfig, router_config) -> None:
    """Fail at startup if plans.yaml and models.yaml disagree about where a plan's data may go.

    Every plan's `llm_profile` must exist in models.yaml, and when a plan lists `llm_allowed_providers` its profile may
    not mention any other provider (a Premium profile listing DeepSeek would otherwise sit there waiting to be used).
    """
    known = router_config.profile_names()
    for name, plan in plans.plans.items():
        if plan.llm_profile not in known:
            raise ValueError(f"plan '{name}' uses model profile '{plan.llm_profile}', which models.yaml does not define")
        if plan.llm_allowed_providers is not None:
            extra = sorted(router_config.providers_in(plan.llm_profile) - set(plan.llm_allowed_providers))
            if extra:
                raise ValueError(
                    f"plan '{name}' only allows {sorted(plan.llm_allowed_providers)} but its model profile "
                    f"'{plan.llm_profile}' also lists {extra}"
                )
