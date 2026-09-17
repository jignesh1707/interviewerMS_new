from fastapi import APIRouter, Depends

from app.api.deps import require_api_key
from app.config import get_settings
from app.core.logging import get_logger
from app.llm.router import get_router
from app.voice import stt, tts

logger = get_logger(__name__)
router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": get_settings().app_name}


@router.get("/ready")
async def ready() -> dict:
    settings = get_settings()
    model_router = get_router()
    return {
        "status": "ok",
        "llm_providers": {
            name: model_router.provider_configured(name)
            for name in ("openai", "deepseek", "anthropic")
        },
        "voice": {
            "stt_model_loaded": stt.model_ready(),
            "stt_model": settings.whisper_model,
            "tts_binary_available": tts.binary_available(),
        },
        "storage": str(settings.database_path),
    }


@router.get("/models", dependencies=[Depends(require_api_key)])
async def models() -> dict:
    return get_router().status()
