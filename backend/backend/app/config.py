from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "voice-interviewer"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    api_keys: str = "dev-key-change-me"
    cors_origins: str = "*"

    storage_dir: Path = BASE_DIR / "data"
    database_path: Path = BASE_DIR / "data" / "interviews.db"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    llm_timeout_seconds: float = 60.0
    llm_max_attempts_per_provider: int = 1
    provider_cooldown_seconds: float = 90.0
    llm_max_output_tokens: int = 2048
    llm_temperature: float = 0.4

    models_config_path: Path = BASE_DIR / "models.yaml"

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

    piper_binary: str = "piper"
    piper_model_path: str = ""
    piper_default_voice: str = "en_US-lessac-medium"

    @property
    def api_key_set(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
