"""Plan, quota and interview-length configuration, loaded from ``plans.yaml``.

A plan gives a student ``included_minutes`` per period. Booking an interview debits its length from that
balance. Each allowed length has a profile that fixes how many questions and follow-ups it gets, so a
15 minute interview cannot ask 15 questions.
"""

from datetime import datetime, timezone
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


class Plan(BaseModel):
    included_minutes: int = Field(ge=0)
    period: Literal["monthly", "none"] = "monthly"  # "none": one balance for the student's lifetime
    durations: list[int] = Field(min_length=1)
    default_duration: int

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


def period_key(period: str, now: datetime | None = None) -> str:
    if period == "none":
        return "lifetime"
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def load_plans(path: Path) -> PlansConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return PlansConfig.model_validate(data)


@lru_cache
def _cached(path: str) -> PlansConfig:
    return load_plans(Path(path))


def get_plans() -> PlansConfig:
    return _cached(str(get_settings().plans_config_path))
