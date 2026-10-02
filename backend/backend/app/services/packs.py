"""Purchased interview minutes: activation, booking, refunds and balances.

The main app tells this service when a student pays (``activate``); this service owns the balance, the days left and
the refund rule. Booking an interview debits its full length from the student's pack for the chosen plan in a single
conditional UPDATE in the store, so concurrent bookings cannot overspend.
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.errors import NotFoundError, QuotaExceededError, RefundNotAllowedError, ValidationAppError
from app.core.plans import Plan, PlansConfig, RefundRule, get_plans
from app.services.storage import AsyncStore

DAY = 86400


def _iso(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch else None


def _refund_allowed(rule: RefundRule, interviews_started: int, minutes_used: int) -> bool:
    by_interviews = interviews_started <= rule.max_interviews_started
    by_minutes = minutes_used <= rule.max_minutes_used
    return (by_interviews and by_minutes) if rule.rule == "all" else (by_interviews or by_minutes)


@dataclass(frozen=True)
class Reservation:
    tenant_id: str
    external_ref: str
    plan: str
    minutes: int
    question_count: int
    max_followups: int
    grace_seconds: int
    llm_profile: str
    llm_allowed_providers: tuple[str, ...] | None

    def config_overrides(self) -> dict[str, Any]:
        return {
            "duration_minutes": self.minutes,
            "question_count": self.question_count,
            "max_followups": self.max_followups,
        }


class PackService:
    def __init__(self, store: AsyncStore, plans: PlansConfig | None = None) -> None:
        self.store = store
        self._plans = plans

    @property
    def plans(self) -> PlansConfig:
        return self._plans or get_plans()

    # ------------------------------------------------------------------ booking

    async def reserve(
        self, tenant_id: str, external_ref: str | None, plan_name: str | None, requested_minutes: int | None
    ) -> Reservation:
        if not external_ref:
            raise ValidationAppError("external_ref (the student id) is required when plans are enabled")
        plans = self.plans
        plan_key = plan_name or plans.default_plan
        plan = plans.plan(plan_key)
        minutes = plans.resolve_duration(plan, requested_minutes)
        now = int(time.time())
        if not await self.store.pack_debit(tenant_id, external_ref, plan_key, minutes, now=now):
            raise QuotaExceededError(
                "no interview minutes available on this plan",
                details=await self._shortfall(tenant_id, external_ref, plan_key, minutes, now),
            )
        profile = plans.profile(minutes)
        return Reservation(
            tenant_id=tenant_id,
            external_ref=external_ref,
            plan=plan_key,
            minutes=minutes,
            question_count=profile.question_count,
            max_followups=profile.max_followups,
            grace_seconds=plans.grace_seconds,
            llm_profile=plan.llm_profile,
            llm_allowed_providers=tuple(plan.llm_allowed_providers) if plan.llm_allowed_providers else None,
        )

    async def _shortfall(self, tenant_id: str, ref: str, plan: str, requested: int, now: int) -> dict[str, Any]:
        row = await self.store.pack_get(tenant_id, ref, plan)
        remaining = max(0, row["minutes_total"] - row["minutes_used"]) if row else 0
        if row is None or row["minutes_total"] == 0:  # never bought, or the purchase was refunded
            reason = "no_active_pack"
        elif row["expires_at"] <= now:
            reason = "pack_expired"
        elif remaining == 0:
            reason = "no_minutes_left"
        else:
            reason = "insufficient_minutes"
        expired = row is None or row["expires_at"] <= now
        return {
            "reason": reason,
            "plan": plan,
            "requested_minutes": requested,
            "remaining_minutes": 0 if expired else remaining,
            "expires_at": _iso(row["expires_at"]) if row else None,
            "days_remaining": 0 if expired else math.ceil((row["expires_at"] - now) / DAY),
        }

    async def refund(self, reservation: Reservation) -> None:
        """Give the minutes back (the interview could not be created)."""
        await self.store.pack_credit(
            reservation.tenant_id, reservation.external_ref, reservation.plan, reservation.minutes
        )

    # ------------------------------------------------------------------ balances

    async def status(self, tenant_id: str, external_ref: str, plan_name: str) -> dict[str, Any]:
        plan = self.plans.plan(plan_name)
        row = await self.store.pack_get(tenant_id, external_ref, plan_name)
        now = int(time.time())
        total = row["minutes_total"] if row else 0
        used = row["minutes_used"] if row else 0
        started = row["interviews_started"] if row else 0
        expires = row["expires_at"] if row else 0
        unexpired = expires > now
        remaining = max(0, total - used) if unexpired else 0
        return {
            "external_ref": external_ref,
            "plan": plan_name,
            "active": unexpired and remaining > 0,
            "minutes_total": total,
            "minutes_used": used,
            "minutes_remaining": remaining,
            "expires_at": _iso(expires),
            "days_remaining": math.ceil((expires - now) / DAY) if unexpired else 0,
            "interviews_started": started,
            "refund_eligible": bool(row) and total > 0 and _refund_allowed(plan.refund, started, used),
            "allowed_durations": plan.durations,
        }

    async def status_all(self, tenant_id: str, external_ref: str) -> list[dict[str, Any]]:
        return [await self.status(tenant_id, external_ref, name) for name in sorted(self.plans.plans)]

    # ------------------------------------------------------------------ purchases and refunds

    async def activate(
        self, tenant_id: str, external_ref: str, plan_name: str, *, payment_id: str, purchased_at: datetime
    ) -> dict[str, Any]:
        plan = self.plans.plan(plan_name)
        if purchased_at.tzinfo is None:
            raise ValidationAppError("purchased_at must include a timezone (use UTC, e.g. 2026-10-02T12:00:00Z)")
        purchased = int(purchased_at.timestamp())
        if purchased > int(time.time()) + 3600:
            raise ValidationAppError("purchased_at is in the future")
        applied = await self.store.pack_activate(
            tenant_id,
            external_ref,
            plan_name,
            payment_id=payment_id,
            minutes=plan.pack_minutes,
            days=plan.pack_days,
            purchased_at=purchased,
        )
        return {"applied": applied, "pack": await self.status(tenant_id, external_ref, plan_name)}

    async def revoke(self, tenant_id: str, external_ref: str, plan_name: str, *, payment_id: str) -> dict[str, Any]:
        plan = self.plans.plan(plan_name)
        rule = plan.refund
        outcome = await self.store.pack_revoke(
            tenant_id,
            external_ref,
            plan_name,
            payment_id=payment_id,
            max_interviews_started=rule.max_interviews_started,
            max_minutes_used=rule.max_minutes_used,
            rule=rule.rule,
        )
        if outcome == "not_found":
            raise NotFoundError("no such payment for this student and plan")
        if outcome == "not_eligible":
            raise RefundNotAllowedError(
                "this pack has been used too much to be refunded",
                details={
                    "max_interviews_started": rule.max_interviews_started,
                    "max_minutes_used": rule.max_minutes_used,
                    "rule": rule.rule,
                },
            )
        return {"revoked": True, "already_revoked": outcome == "already_revoked",
                "pack": await self.status(tenant_id, external_ref, plan_name)}
