from fastapi import APIRouter, Depends

from app.api.deps import require_api_key
from app.config import get_settings
from app.core.logging import get_logger
from app.core.errors import ServiceUnavailableError
from app.llm.router import get_router
from app.services.storage import get_store
from app.voice import stt, tts

logger = get_logger(__name__)
router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": get_settings().app_name}


@router.get("/ready")
async def ready() -> dict:
    """Unauthenticated probe for orchestrators: reports only whether the service is usable."""
    try:
        get_store().list_interviews("__ready__", limit=1)
    except Exception:  # noqa: BLE001
        logger.exception("readiness_check_failed")
        raise ServiceUnavailableError("storage unavailable") from None
    return {"status": "ok"}


@router.get("/ready/details", dependencies=[Depends(require_api_key)])
async def ready_details() -> dict:
    settings = get_settings()
    model_router = get_router()
    return {
        "status": "ok",
        "llm_providers": {
            name: model_router.provider_configured(name)
            for name in ("deepseek", "openrouter", "openai", "anthropic")
        },
        "voice": {
            "stt_model_loaded": stt.model_ready(),
            "stt_model": settings.whisper_model,
            "tts_binary_available": tts.binary_available(),
        },
    }


@router.get("/models", dependencies=[Depends(require_api_key)])
async def models() -> dict:
    return get_router().status()
