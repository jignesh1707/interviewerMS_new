from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field, model_validator

from app.api.deps import require_api_key
from app.config import get_settings
from app.core.errors import NotFoundError
from app.services.interview_service import get_interview_service

Tenant = Annotated[str, Depends(require_api_key)]
Student = Annotated[str, Path(min_length=1, max_length=200)]

router = APIRouter(prefix="/packs", tags=["packs"], dependencies=[Depends(require_api_key)])


class ActivateRequest(BaseModel):
    external_ref: str = Field(min_length=1, max_length=200)
    # Send exactly one: a `sku` (a product from plans.yaml with its own size) or a `plan` (that plan's default pack).
    plan: str | None = Field(default=None, min_length=1, max_length=64)
    sku: str | None = Field(default=None, min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=200)  # your payment/event id: a replay is applied once
    purchased_at: datetime  # when the student paid (include a timezone); expiry is counted from here

    @model_validator(mode="after")
    def one_of_plan_or_sku(self) -> "ActivateRequest":
        if (self.plan is None) == (self.sku is None):
            raise ValueError("send exactly one of plan or sku")
        return self


class RevokeRequest(BaseModel):
    external_ref: str = Field(min_length=1, max_length=200)
    plan: str = Field(min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=200)


class ExpireRequest(BaseModel):
    external_ref: str = Field(min_length=1, max_length=200)
    plan: str = Field(min_length=1, max_length=64)


def _packs():
    if not get_settings().plans_enabled:
        raise NotFoundError("plans are not enabled on this service")
    return get_interview_service().packs


@router.post("/activate")
async def activate_pack(payload: ActivateRequest, tenant: Tenant) -> dict:
    """Record a purchase. Call it from the main app's backend once a payment has succeeded, never from a browser."""
    return await _packs().activate(
        tenant,
        payload.external_ref,
        payload.plan,
        sku=payload.sku,
        payment_id=payload.payment_id,
        purchased_at=payload.purchased_at,
    )


@router.post("/revoke")
async def revoke_pack(payload: RevokeRequest, tenant: Tenant) -> dict:
    """Take a purchase back (a refund). Refused with 409 `refund_not_allowed` once the pack is used too much."""
    return await _packs().revoke(tenant, payload.external_ref, payload.plan, payment_id=payload.payment_id)


@router.post("/expire")
async def expire_pack(payload: ExpireRequest, tenant: Tenant) -> dict:
    """End a student's pack now (the subscription that included it ended). Not a refund; safe to repeat.

    The minutes left are gone for good: a later purchase starts a fresh pack. A student with no pack gets a harmless
    `expired: false`, so you can call this on every cancellation without checking first.
    """
    return await _packs().expire(tenant, payload.external_ref, payload.plan)


@router.delete("/by-ref/{external_ref}")
async def erase_packs(external_ref: Student, tenant: Tenant) -> dict:
    """Delete a student's wallets and purchase records (call it when the account is deleted). Safe to repeat.

    This is not a refund and cannot be undone: the balance is gone. Interviews are erased separately with
    `DELETE /interviews/by-ref/{external_ref}`.
    """
    return await _packs().erase(tenant, external_ref)


@router.get("/{external_ref}")
async def get_packs(external_ref: Student, tenant: Tenant) -> dict:
    """A student's balance on every plan: minutes left, days left, whether a refund is still possible."""
    return {"external_ref": external_ref, "packs": await _packs().status_all(tenant, external_ref)}


plans_router = APIRouter(prefix="/plans", tags=["plans"], dependencies=[Depends(require_api_key)])


@plans_router.get("")
async def list_plans(tenant: Tenant) -> dict:
    """The plans on sale: pack size, validity, interview lengths, refund rule and which AI providers may see data.

    `llm_providers` is what a consent screen and privacy policy must name for that plan. It is computed from
    models.yaml and plans.yaml, minus anything switched off with LLM_DISABLED_PROVIDERS.
    """
    if not get_settings().plans_enabled:
        raise NotFoundError("plans are not enabled on this service")
    from app.core.plans import get_plans
    from app.llm.router import get_router

    settings = get_settings()
    plans = get_plans()
    router_config = get_router().config
    items = []
    for name, plan in plans.plans.items():
        providers = router_config.providers_in(plan.llm_profile)
        if plan.llm_allowed_providers is not None:
            providers &= set(plan.llm_allowed_providers)
        providers -= settings.disabled_provider_set
        items.append(
            {
                "name": name,
                "pack_minutes": plan.pack_minutes,
                "pack_days": plan.pack_days,
                "durations": plan.durations,
                "default_duration": plan.default_duration,
                "refund": plan.refund.model_dump(),
                "llm_profile": plan.llm_profile,
                "llm_providers": sorted(providers),
            }
        )
    skus = [
        {"name": name, "plan": sku.plan, "minutes": sku.minutes, "days": sku.days}
        for name, sku in plans.skus.items()
    ]
    return {"default_plan": plans.default_plan, "plans": items, "skus": skus}
