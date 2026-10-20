"""Plans, packs and interview-length configuration, loaded from ``plans.yaml``.

A plan is something a student buys: a pack of ``pack_minutes`` that expires after ``pack_days``. Booking an interview
debits its length from the pack. Each allowed length has a profile that fixes how many questions and follow-ups it
gets, so a 15 minute interview cannot ask 15 questions. Each plan also names the model profile it is served with
(see models.yaml) and, optionally, the only AI providers it may ever use.
"""

import json
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
    # False keeps the plan out of GET /plans (grants such as referral or trial wallets are not for sale).
    listed: bool = True

    @model_validator(mode="after")
    def default_is_allowed(self) -> "Plan":
        if self.default_duration not in self.durations:
            raise ValueError(f"default_duration {self.default_duration} is not in durations {self.durations}")
        return self


class Sku(BaseModel):
    """A product the main app sells: it adds ``minutes`` valid for ``days`` to the student's wallet for ``plan``.

    Several products can feed one wallet with different sizes (a $5, a $10 and a $20 add-on all fill the Economy
    wallet). A plan used without a sku grants its own ``pack_minutes`` and ``pack_days``.
    """

    plan: str
    minutes: int = Field(ge=1)
    days: int = Field(ge=1)
    listed: bool = True  # False: a grant the main app makes (referral, trial), not shown in GET /plans


class PlansConfig(BaseModel):
    default_plan: str
    grace_seconds: int = Field(default=60, ge=0)
    interview_minutes: int = Field(default=15, ge=1)  # the unit for the `pack_interviews` / `interviews` shorthand
    profiles: dict[int, DurationProfile]
    plans: dict[str, Plan]
    skus: dict[str, Sku] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_references(self) -> "PlansConfig":
        if self.default_plan not in self.plans:
            raise ValueError(f"default_plan '{self.default_plan}' is not defined under plans")
        for name, plan in self.plans.items():
            for minutes in plan.durations:
                if minutes not in self.profiles:
                    raise ValueError(f"plan '{name}' allows {minutes} minutes but profiles has no entry for {minutes}")
        for name, sku in self.skus.items():
            if sku.plan not in self.plans:
                raise ValueError(f"sku '{name}' is for plan '{sku.plan}', which is not defined under plans")
            shortest = min(self.plans[sku.plan].durations)
            if sku.minutes < shortest:
                raise ValueError(f"sku '{name}' grants {sku.minutes} minutes, less than one {shortest} minute interview")
        return self

    def plan(self, name: str | None) -> Plan:
        key = name or self.default_plan
        if key not in self.plans:
            raise ValidationAppError("unknown plan", details={"plan": key, "allowed": sorted(self.plans)})
        return self.plans[key]

    def sku(self, name: str) -> Sku:
        if name not in self.skus:
            raise ValidationAppError("unknown sku", details={"sku": name, "allowed": sorted(self.skus)})
        return self.skus[name]

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


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expand_interviews(data: dict, unit: int) -> dict:
    """Turn the ``pack_interviews`` / ``interviews`` shorthand into minutes (interviews x ``interview_minutes``)."""
    for kind, shorthand, minutes_key in (("plans", "pack_interviews", "pack_minutes"), ("skus", "interviews", "minutes")):
        for name, entry in (data.get(kind) or {}).items():
            if shorthand not in entry:
                continue
            if minutes_key in entry:
                raise ValueError(f"{kind} '{name}' sets both {shorthand} and {minutes_key}; use one")
            entry = dict(entry)
            entry[minutes_key] = entry.pop(shorthand) * unit
            data[kind][name] = entry
    return data


def load_plans(path: Path, overrides: str = "") -> PlansConfig:
    """Read plans.yaml, lay the optional ``PLANS_OVERRIDES`` JSON over it, then validate the result."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    patch: dict = {}
    if overrides.strip():
        try:
            patch = json.loads(overrides)
        except json.JSONDecodeError as error:
            raise ValueError(f"PLANS_OVERRIDES is not valid JSON: {error}") from error
        if not isinstance(patch, dict):
            raise ValueError("PLANS_OVERRIDES must be a JSON object")
    unit = int(patch.get("interview_minutes", data.get("interview_minutes", 15)))
    # Expand each layer on its own, so an override written in interviews replaces a size the file wrote in minutes.
    merged = _deep_merge(_expand_interviews(data, unit), _expand_interviews(patch, unit))
    return PlansConfig.model_validate(merged)


@lru_cache
def _cached(path: str, overrides: str) -> PlansConfig:
    return load_plans(Path(path), overrides)


def get_plans() -> PlansConfig:
    settings = get_settings()
    return _cached(str(settings.plans_config_path), settings.plans_overrides)


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
