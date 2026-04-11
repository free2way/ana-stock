from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "Personal Quant Workbench"
    root_dir: Path = ROOT_DIR
    storage_dir: Path = Field(default=ROOT_DIR / "storage")
    data_dir: Path = Field(default=ROOT_DIR / "data")
    raw_data_dir: Path = Field(default=ROOT_DIR / "data" / "raw")
    normalized_data_dir: Path = Field(default=ROOT_DIR / "data" / "normalized")
    qlib_data_dir: Path = Field(default=ROOT_DIR / "data" / "qlib")
    artifacts_dir: Path = Field(default=ROOT_DIR / "data" / "artifacts")
    database_url: str | None = Field(default=None)
    sqlite_path: Path = Field(default=ROOT_DIR / "storage" / "app.db")
    sqlite_timeout_seconds: float = Field(default=30.0)
    postgres_pool_size: int = Field(default=20)
    postgres_max_overflow: int = Field(default=20)
    postgres_pool_timeout_seconds: int = Field(default=30)
    postgres_pool_recycle_seconds: int = Field(default=1800)
    postgres_connect_timeout_seconds: int = Field(default=10)
    postgres_statement_timeout_ms: int = Field(default=60000)
    postgres_idle_transaction_timeout_ms: int = Field(default=60000)
    postgres_application_name: str = Field(default="pqw-app")
    tushare_token: str | None = Field(default=None)
    ai_api_key: str | None = Field(default=None)
    ai_base_url: str = Field(default="https://api.openai.com/v1")
    ai_model: str | None = Field(default=None)
    ai_provider_name: str = Field(default="OpenAI Compatible")
    ai_timeout_seconds: float = Field(default=20.0)
    wechat_webhook_url: str | None = Field(default=None)
    feishu_webhook_url: str | None = Field(default=None)
    telegram_bot_token: str | None = Field(default=None)
    telegram_chat_id: str | None = Field(default=None)
    auth_username: str = Field(default="admin")
    auth_password: str | None = Field(default=None)
    auth_secret: str | None = Field(default=None)
    auth_cookie_max_age_seconds: int = Field(default=60 * 60 * 24 * 7)

    model_config = SettingsConfigDict(env_prefix="PQW_", env_file=".env", extra="ignore")

    def ensure_directories(self) -> None:
        required_paths = [
            self.storage_dir,
            self.data_dir,
            self.raw_data_dir,
            self.normalized_data_dir,
            self.qlib_data_dir,
            self.artifacts_dir,
        ]
        if not self.database_url:
            required_paths.append(self.sqlite_path.parent)
        for path in required_paths:
            path.mkdir(parents=True, exist_ok=True)

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.sqlite_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
