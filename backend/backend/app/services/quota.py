"""Per-student minute quota: plan lookup, booking, refunds and top-ups.

Booking an interview debits its full length from the student's balance for the current period. The debit is a
single conditional UPDATE in the store, so concurrent bookings cannot overspend.
"""

from dataclasses import dataclass
from typing import Any

from app.core.errors import QuotaExceededError, ValidationAppError
from app.core.plans import PlansConfig, get_plans, period_key
from app.services.storage import AsyncStore


@dataclass(frozen=True)
class Reservation:
    tenant_id: str
    external_ref: str
    plan: str
    period: str
    minutes: int
    question_count: int
    max_followups: int
    grace_seconds: int

    def config_overrides(self) -> dict[str, Any]:
        return {
            "duration_minutes": self.minutes,
            "question_count": self.question_count,
            "max_followups": self.max_followups,
        }


class QuotaService:
    def __init__(self, store: AsyncStore, plans: PlansConfig | None = None) -> None:
        self.store = store
        self._plans = plans

    @property
    def plans(self) -> PlansConfig:
        return self._plans or get_plans()

    async def reserve(
        self, tenant_id: str, external_ref: str | None, plan_name: str | None, requested_minutes: int | None
    ) -> Reservation:
        if not external_ref:
            raise ValidationAppError("external_ref (the student id) is required when plans are enabled")
        plans = self.plans
        plan_key = plan_name or plans.default_plan
        plan = plans.plan(plan_key)
        minutes = plans.resolve_duration(plan, requested_minutes)
        period = period_key(plan.period)
        if not await self.store.quota_debit(
            tenant_id, external_ref, period, minutes, allowance=plan.included_minutes
        ):
            state = await self.store.quota_get(tenant_id, external_ref, period)
            remaining = max(0, plan.included_minutes + state["bonus_minutes"] - state["used_minutes"])
            raise QuotaExceededError(
                "not enough interview minutes left for this period",
                details={
                    "requested_minutes": minutes,
                    "remaining_minutes": remaining,
                    "period": plan.period,
                    "period_key": period,
                },
            )
        profile = plans.profile(minutes)
        return Reservation(
            tenant_id=tenant_id,
            external_ref=external_ref,
            plan=plan_key,
            period=period,
            minutes=minutes,
            question_count=profile.question_count,
            max_followups=profile.max_followups,
            grace_seconds=plans.grace_seconds,
        )

    async def refund(self, reservation: Reservation) -> None:
        await self.store.quota_credit(
            reservation.tenant_id, reservation.external_ref, reservation.period, reservation.minutes
        )

    async def status(self, tenant_id: str, external_ref: str, plan_name: str | None = None) -> dict[str, Any]:
        plans = self.plans
        plan_key = plan_name or plans.default_plan
        plan = plans.plan(plan_key)
        period = period_key(plan.period)
        state = await self.store.quota_get(tenant_id, external_ref, period)
        return {
            "external_ref": external_ref,
            "plan": plan_key,
            "period": plan.period,
            "period_key": period,
            "included_minutes": plan.included_minutes,
            "bonus_minutes": state["bonus_minutes"],
            "used_minutes": state["used_minutes"],
            "remaining_minutes": max(
                0, plan.included_minutes + state["bonus_minutes"] - state["used_minutes"]
            ),
            "allowed_durations": plan.durations,
        }

    async def grant(
        self, tenant_id: str, external_ref: str, minutes: int, plan_name: str | None = None
    ) -> dict[str, Any]:
        plan = self.plans.plan(plan_name)
        await self.store.quota_add_bonus(tenant_id, external_ref, period_key(plan.period), minutes)
        return await self.status(tenant_id, external_ref, plan_name)
