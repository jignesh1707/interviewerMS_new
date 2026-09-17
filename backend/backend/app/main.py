from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes_interviews, routes_speech, routes_system
from app.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)
settings = get_settings()

app = FastAPI(
    title="Voice Interviewer Microservice",
    version="0.1.0",
    description=(
        "Voice-driven STAR interview microservice with a cost-aware multi-provider LLM router, "
        "local speech-to-text/text-to-speech, deterministic text analytics and webhook callbacks."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("app_error path=%s code=%s message=%s", request.url.path, exc.code, exc.message)
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "internal server error", "details": {}}},
    )


app.include_router(routes_system.router, prefix="/api/v1")
app.include_router(routes_interviews.router, prefix="/api/v1")
app.include_router(routes_speech.router, prefix="/api/v1")


@app.get("/")
async def root() -> dict:
    return {
        "service": settings.app_name,
        "version": "0.1.0",
        "docs": "/docs",
        "api_prefix": "/api/v1",
    }
