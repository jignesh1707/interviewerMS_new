import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DEV_ENVIRONMENTS = {"development", "dev", "local", "test", "testing"}
DEFAULT_API_KEY = "dev-key-change-me"
MIN_API_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "voice-interviewer"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"

    api_keys: str = DEFAULT_API_KEY
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Callback (webhook) target policy. Empty allowlist = any public https host.
    callback_allowed_hosts: str = ""
    callback_allow_insecure: bool = False

    # Rate limits (per tenant, in-process: use a gateway as well when running several workers).
    rate_limit_per_minute: int = 120
    rate_limit_expensive_per_minute: int = 20
    daily_expensive_budget: int = 2000  # LLM/STT/TTS-backed calls per tenant per UTC day; 0 = unlimited
    auth_fail_limit_per_minute: int = 10  # failed key attempts per client address
    # Per student (tenant + the interview's external_ref), for the same LLM/STT-backed calls. 0 = unlimited.
    # Calls without an external_ref are only covered by the tenant limits above.
    student_rate_limit_per_minute: int = 30
    student_daily_budget: int = 200

    # Untrusted document parsing.
    max_pdf_pages: int = 30
    max_docx_uncompressed_mb: int = 20
    doc_parse_timeout_seconds: float = 20.0

    # Prompt-injection hardening: LLM scores may differ from the heuristic baseline by at most this.
    score_clamp_delta: int = 25

    # Candidate data handling.
    retain_audio: bool = False  # keep uploaded audio after transcription
    retention_days: int = 0  # purge interviews older than this many days; 0 = keep forever
    retention_sweep_minutes: int = 60
    llm_disabled_providers: str = ""  # e.g. "deepseek" to keep candidate data away from a vendor
    # Strip names, contact details, links, addresses and work-authorization lines from the copy of a resume or
    # answer that is sent to an AI provider (stored data is not changed). Turn off only for debugging.
    redact_pii_for_llm: bool = True
    require_consent: bool = False  # require consent_to_ai_processing=true when creating interviews

    # Request size limits.
    max_doc_upload_mb: int = 5
    max_text_chars: int = 100_000
    max_transcript_chars: int = 4_000  # one answer; about three minutes of speech
    max_metadata_bytes: int = 16_384

    storage_dir: Path = BASE_DIR / "data"
    database_path: Path = BASE_DIR / "data" / "interviews.db"

    # Production database. When DATABASE_URL is set the service uses Postgres (for example Supabase's
    # transaction pooler) and ignores DATABASE_PATH. Tables live in DB_SCHEMA, never in `public`.
    database_url: str = ""
    db_schema: str = "interviewer"
    db_pool_size: int = 5
    db_auto_migrate: bool = True  # create tables on startup; set false when an admin runs the SQL

    # Shared rate-limit state (for example Upstash). Empty = per-process memory.
    redis_url: str = ""
    redis_key_prefix: str = "interviewer"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # OpenRouter may route to several upstream hosts. "deny" asks it to use only endpoints that do
    # not retain or train on prompts; "allow" removes the restriction. Empty sends no preference.
    openrouter_data_collection: str = "deny"

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    llm_timeout_seconds: float = 60.0
    llm_max_attempts_per_provider: int = 1
    provider_cooldown_seconds: float = 90.0
    llm_max_output_tokens: int = 2048
    llm_temperature: float = 0.4

    models_config_path: Path = BASE_DIR / "models.yaml"

    # Plans, per-student quotas and interview length limits (see plans.yaml). Off by default: existing
    # callers keep today's behaviour. When on, every interview needs an external_ref (the student id).
    plans_enabled: bool = False
    plans_config_path: Path = BASE_DIR / "plans.yaml"
    # Per-answer caps, always applied.
    max_answer_seconds: int = 180

    default_question_count: int = 8
    max_question_count: int = 15

    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 3

    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = ""
    stt_max_upload_mb: int = 25
    # Transcriptions running at once per machine (CPU bound). Others wait up to stt_queue_timeout_seconds, then get a
    # 503 with Retry-After so the client can retry instead of every request slowing down together.
    stt_max_concurrent: int = 2
    stt_queue_timeout_seconds: float = 20.0
    # Load the Whisper model when the service starts instead of on the first answer.
    stt_preload: bool = True

    piper_binary: str = "piper"
    piper_model_path: str = ""
    piper_default_voice: str = "en_US-lessac-medium"
    piper_allowed_voices: str = ""  # extra voice names callers may request, comma separated

    @property
    def is_development(self) -> bool:
        return self.environment.strip().lower() in DEV_ENVIRONMENTS

    @property
    def api_key_entries(self) -> dict[str, str]:
        """Map API key -> tenant id.

        ``API_KEYS`` is a comma list of ``key`` or ``tenant:key`` entries. Keys without
        an explicit tenant get a stable id derived from the key hash.
        """
        entries: dict[str, str] = {}
        for raw in self.api_keys.split(","):
            raw = raw.strip()
            if not raw:
                continue
            tenant, sep, key = raw.partition(":")
            if not sep:
                key, tenant = raw, ""
            key = key.strip()
            tenant = tenant.strip() or "key-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            if key:
                entries[key] = tenant
        return entries

    @property
    def api_key_set(self) -> set[str]:
        return set(self.api_key_entries)

    @property
    def disabled_provider_set(self) -> set[str]:
        return {p.strip().lower() for p in self.llm_disabled_providers.split(",") if p.strip()}

    @property
    def piper_voice_set(self) -> set[str]:
        extra = {v.strip() for v in self.piper_allowed_voices.split(",") if v.strip()}
        return {self.piper_default_voice} | extra

    @property
    def callback_host_set(self) -> set[str]:
        return {h.strip().lower() for h in self.callback_allowed_hosts.split(",") if h.strip()}

    def validate_for_startup(self) -> None:
        """Refuse to run outside development with weak or default credentials."""
        if self.is_development:
            return
        keys = self.api_key_set
        if not keys:
            raise RuntimeError("API_KEYS is empty; refusing to start outside development")
        for key in keys:
            if key == DEFAULT_API_KEY or len(key) < MIN_API_KEY_LENGTH:
                raise RuntimeError(
                    f"API_KEYS contains the default or a short key; use random keys of at least "
                    f"{MIN_API_KEY_LENGTH} characters when ENVIRONMENT={self.environment!r}"
                )
        if self.cors_origins.strip() == "*":
            raise RuntimeError("CORS_ORIGINS must be explicit outside development")

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_for_startup()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
