"""Central configuration, loaded from environment variables / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    openai_api_key: str = ""
    llm_model: str = "gpt-6.2-sol"
    llm_reasoning_effort: str = "medium"

    # API protection
    app_access_code: str = "change-me"
    admin_token: str = "change-me-too"

    # Alerts
    alert_webhook_url: str = ""

    # Storage
    db_path: str = "data/risk.db"

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def data_dir(self) -> Path:
        return REPO_ROOT / "data"

    @property
    def raw_dir(self) -> Path:
        return REPO_ROOT / "data" / "raw"


@lru_cache
def get_settings() -> Settings:
    return Settings()
