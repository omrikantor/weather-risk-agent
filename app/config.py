"""Central configuration, loaded from environment variables / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Every setting can come from an environment variable of the same name in capitals (for example
    OPENAI_API_KEY) or from the .env file. Environment variables win, which is how Render supplies secrets."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",          # unknown variables in .env are ignored instead of failing at start
    )

    # LLM
    openai_api_key: str = ""
    llm_model: str = "gpt-6.2-sol"
    llm_reasoning_effort: str = "medium"
    llm_max_output_tokens: int = 6000
    agent_max_steps: int = 8
    # USD per 1M tokens for cost tracking (OpenAI pricing page; update if prices change).
    price_input_per_m: float = 2.00
    price_cached_input_per_m: float = 0.10
    price_output_per_m: float = 10.00

    # API protection
    app_access_code: str = ""            # empty = open (local development only)
    admin_token: str = ""                # empty = admin endpoints disabled
    chat_rate_limit_per_min: int = 10    # per client IP
    daily_budget_usd: float = 3.0        # OpenAI spend cap per UTC day; adjustable at runtime via admin endpoint
    cors_origins: str = ""               # comma-separated; empty = same origin only

    # Live warning alerts (app/alerts/monitor.py): where each check posts new alerts, for example a Slack
    # incoming webhook or a webhook.site test URL. Empty = alerts are recorded but not sent.
    alert_webhook_url: str = ""
    # Run one live warning check in the background when the server starts, so live data is available within
    # about 20 seconds of a restart or deploy instead of waiting for the next scheduled check. On in the
    # Docker image (Dockerfile); off by default so local runs and tests never call the NWS API or write live
    # alert state into the committed database.
    alert_check_on_startup: bool = False

    # Storage
    db_path: str = "data/risk.db"

    @property
    def db_file(self) -> Path:
        """Absolute path of the SQLite database; a relative DB_PATH is taken from the repository root."""
        p = Path(self.db_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def data_dir(self) -> Path:
        return REPO_ROOT / "data"

    @property
    def raw_dir(self) -> Path:
        return REPO_ROOT / "data" / "raw"


@lru_cache   # settings are read once per process
def get_settings() -> Settings:
    return Settings()
