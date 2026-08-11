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
    postgres_pool_size: int = Field(default=20)
    postgres_max_overflow: int = Field(default=20)
    postgres_pool_timeout_seconds: int = Field(default=30)
    postgres_pool_recycle_seconds: int = Field(default=1800)
    postgres_connect_timeout_seconds: int = Field(default=10)
    postgres_statement_timeout_ms: int = Field(default=60000)
    postgres_idle_transaction_timeout_ms: int = Field(default=60000)
    postgres_application_name: str = Field(default="pqw-app")
    tushare_token: str | None = Field(default=None)
    alpaca_api_key: str | None = Field(default=None)
    alpaca_api_secret: str | None = Field(default=None)
    alpaca_endpoint: str = Field(default="https://paper-api.alpaca.markets/v2")
    alpaca_data_endpoint: str = Field(default="https://data.alpaca.markets/v2")
    alpaca_data_feed: str = Field(default="iex")
    polygon_api_key: str | None = Field(default=None)
    polygon_endpoint: str = Field(default="https://api.polygon.io")
    # SEC asks automated clients to identify a real contact.  Keep the
    # official EDGAR integration disabled until this is explicitly provided.
    sec_user_agent: str | None = Field(default=None)
    sec_data_endpoint: str = Field(default="https://data.sec.gov")
    sec_company_tickers_endpoint: str = Field(default="https://www.sec.gov/files/company_tickers.json")
    sec_timeout_seconds: float = Field(default=15.0)
    a_stock_data_max_symbols: int = Field(default=50)
    us_trade_universe_min_price: float = Field(default=3.0)
    us_trade_universe_min_avg_dollar_volume: float = Field(default=2_000_000.0)
    us_trade_universe_min_avg_volume: float = Field(default=200_000.0)
    us_trade_universe_min_history_days: int = Field(default=10)
    x_bearer_token: str | None = Field(default=None)
    x_api_endpoint: str = Field(default="https://api.x.com/2")
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
    backtest_commission_bps: float = Field(default=8.0)
    backtest_slippage_bps: float = Field(default=12.0)
    backtest_max_position_weight: float = Field(default=0.2)
    backtest_min_signal_score: float = Field(default=0.05)
    backtest_default_holding_days: int = Field(default=3)
    backtest_benchmark_symbol: str | None = Field(default=None)
    backtest_max_sector_weight: float = Field(default=0.35)
    backtest_min_adv: float = Field(default=50000000.0)
    backtest_max_gap_pct: float = Field(default=0.08)
    backtest_rebalance_threshold: float = Field(default=0.02)
    kronos_enabled: bool = Field(default=True)
    kronos_model_name: str = Field(default="NeoQuasar/Kronos-mini")
    kronos_runner_command: str | None = Field(default=None)
    kronos_repo_path: str | None = Field(default=None)
    kronos_device: str = Field(default="cpu")
    kronos_candidate_limit: int = Field(default=60)
    kronos_history_limit: int = Field(default=180)
    kronos_min_history: int = Field(default=60)
    kronos_prediction_horizon_days: int = Field(default=3)
    kronos_timeout_seconds: float = Field(default=180.0)
    kronos_temperature: float = Field(default=0.8)
    kronos_top_p: float = Field(default=0.9)
    kronos_sample_count: int = Field(default=3)
    kronos_seed: int = Field(default=42)

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
        for path in required_paths:
            path.mkdir(parents=True, exist_ok=True)

    @property
    def resolved_database_url(self) -> str:
        if self.database_url and str(self.database_url).strip():
            return str(self.database_url).strip()
        raise RuntimeError("PQW_DATABASE_URL is required. This application is PostgreSQL-only at runtime.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
