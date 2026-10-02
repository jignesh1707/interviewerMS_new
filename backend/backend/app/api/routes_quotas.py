from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from app.api.deps import require_api_key
from app.config import get_settings
from app.core.errors import NotFoundError
from app.services.interview_service import get_interview_service

Tenant = Annotated[str, Depends(require_api_key)]
Student = Annotated[str, Path(min_length=1, max_length=200)]
PlanName = Annotated[str | None, Query(max_length=64)]

router = APIRouter(prefix="/quotas", tags=["quotas"], dependencies=[Depends(require_api_key)])


class GrantRequest(BaseModel):
    minutes: int = Field(ge=1, le=100_000)


def _quota():
    if not get_settings().plans_enabled:
        raise NotFoundError("plans are not enabled on this service")
    return get_interview_service().quota


@router.get("/{external_ref}")
async def get_quota(external_ref: Student, tenant: Tenant, plan: PlanName = None) -> dict:
    """A student's balance for the current period."""
    return await _quota().status(tenant, external_ref, plan)


@router.post("/{external_ref}/grant")
async def grant_minutes(external_ref: Student, payload: GrantRequest, tenant: Tenant, plan: PlanName = None) -> dict:
    """Add minutes to a student's balance for the current period (for example after a top-up purchase)."""
    return await _quota().grant(tenant, external_ref, payload.minutes, plan)
