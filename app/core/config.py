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
    # Optional override for the historical-universe evidence contract artifact
    # consumed by the formal full-market readiness gate.  When unset, the
    # conventional path
    # ``artifacts_dir/stock_selection_research/historical_universe_contract.json``
    # is used.  A missing file keeps the gate fail-closed (unchanged behaviour).
    historical_universe_contract_path: Path | None = Field(default=None)
    # P0B shadow write: canonical OHLCV plus provenance to data/lake_v2.
    # v1 readers are unaffected; disable only for a documented incident.
    lake_v2_shadow_enabled: bool = Field(default=True)
    database_url: str | None = Field(default=None)
    postgres_pool_size: int = Field(default=20)
    postgres_max_overflow: int = Field(default=20)
    postgres_pool_timeout_seconds: int = Field(default=30)
    postgres_pool_recycle_seconds: int = Field(default=1800)
    postgres_connect_timeout_seconds: int = Field(default=10)
    postgres_statement_timeout_ms: int = Field(default=60000)
    postgres_idle_transaction_timeout_ms: int = Field(default=60000)
    postgres_application_name: str = Field(default="pqw-app")
    storage_capacity_monitor_enabled: bool = Field(default=True)
    storage_capacity_monitor_interval_seconds: int = Field(default=3600)
    prediction_artifacts_enabled: bool = Field(default=True)
    prediction_cold_reads_enabled: bool = Field(default=True)
    prediction_artifact_schema_version: str = Field(default="prediction-artifact-v1")
    prediction_hot_write_mode: str = Field(default="compact")
    prediction_hot_full_trade_days: int = Field(default=5)
    prediction_hot_top_k: int = Field(default=100)
    prediction_hot_explanation_limit: int = Field(default=50)
    prediction_hot_explanation_boundary_radius: int = Field(default=5)
    prediction_publication_staging_threshold_rows: int = Field(default=1_000_000)
    prediction_publication_max_rows: int = Field(default=5_000_000)
    prediction_publication_max_estimated_bytes: int = Field(default=4 * 1024 * 1024 * 1024)
    # Complete CN date windows fail explicitly on short history/resource limits.
    # US remains on its separately named legacy policy in this CN-first rollout.
    trainer_cn_window_mode: str = Field(default="complete_dates_v1")
    trainer_cn_window_dates: int = Field(default=252, ge=1, le=1260)
    trainer_cn_window_max_rows: int = Field(default=1_500_000, ge=1)
    trainer_cn_window_max_estimated_fit_bytes: int = Field(default=2 * 1024**3, ge=1)
    # P0 execution-aware label switch (2026-09, single canonical definition).
    # The scheduled CN trainer must label samples with the net return of a
    # tradable next-open entry held for a fixed horizon under explicit
    # per-fill costs; blank settings resolve to the executable profile in the
    # trainer. The legacy short-horizon momentum composite stays available
    # only as an explicitly opted-in research rollback lever
    # (`PQW_TRAINER_CN_LABEL_PROFILE=legacy_short_horizon_composite_v1`).
    # Signal-day limit-up opens cannot actually be bought next morning under
    # T+1; excluded samples keep an auditable exclusion_reason in their
    # profile. Consumption lives in trainer.py via FillCostModel. Entry
    # unreachability uses the band-relative limit threshold -- a fixed 9.7%
    # cutoff would be wrong on 20%-band boards.
    trainer_cn_label_profile: str = Field(default="executable_net_return_v1")
    trainer_cn_entry_not_executable_policy: str = Field(default="exclude")
    # Fail-closed label price basis (A1 follow-up). Adjusted-view labels are only
    # trustworthy when every price point of every label window comes from the
    # rebuilt view. When a view is present for the market but only partially
    # covers the label windows, the run must not silently mix raw fallbacks into
    # "adjusted" labels: by default the trainer raises before persisting a run.
    # Operators who knowingly want a mixed run may set
    # PQW_TRAINER_REQUIRE_FULL_ADJUSTED_COVERAGE=false, in which case the run
    # config records the real basis (`mixed:<share>`) plus the adjusted/raw/
    # dropped sample counts. A market with no view at all is trained and
    # reported as `mixed:0` rather than `adjusted_view`, but is not blocked:
    # there is no adjusted basis for the coverage to be inconsistent with.
    trainer_require_full_adjusted_coverage: bool = Field(default=True)
    # Raw-label explicit opt-in (A1 fail-closed follow-up). A CN/US training run
    # whose adjusted view file is entirely absent must not silently fall back to
    # raw prices: by default the trainer refuses to start. Operators who
    # knowingly want a raw-basis run set
    # PQW_TRAINER_ALLOW_RAW_FALLBACK=true, in which case the run is persisted as
    # `mixed:0.00000000` with `raw_fallback_allowed=true`. A view that exists but
    # cannot be read (`unreadable`) is never covered by this opt-in: it always
    # raises, so a corrupt view cannot masquerade as "no view".
    trainer_allow_raw_fallback: bool = Field(default=False)
    # Structured opt-in audit (operator / reason). Both fail-closed opt-ins
    # (raw-label fallback, unmodeled corporate-action acceptance) record who
    # waived the gate and why. A missing reason refuses the opt-in; the operator
    # defaults to `unknown` but is always recorded. See app/services/optin_audit.py.
    optin_operator: str | None = Field(default=None)
    optin_reason: str | None = Field(default=None)
    # Serve-time enforcement of the unified promotion gate (v2). When True
    # (fail-closed default) a serving/recommendation path must not adopt a run
    # whose gate decision is REJECT (an explicit FAIL: research scope, a data
    # -readiness/point-in-time blocker, a rejected price basis, unmodeled
    # corporate actions without opt-in, too few samples, negative OOS, a purge
    # violation or a rejected statistical sub-gate). Non-promotable evidence is
    # still surfaced on the product payloads. Set
    # PQW_PROMOTION_GATE_ENFORCE=false to observe only: runs stay served, are
    # still labelled "非晋级/研究口径", and a WARNING is logged for each.
    promotion_gate_enforce: bool = Field(default=True)
    # Tighten the serve-time gate from "REJECT only" to "REJECT or OBSERVE".
    # When True, a run whose unified gate decision is OBSERVE (missing evidence
    # rather than an explicit failure) is *also* withheld from serving, so an
    # evidence-less legacy run can no longer be adopted as a champion. Default
    # False preserves historical serving for runs that never persisted
    # promotion evidence: the trainer does not yet persist data_readiness /
    # statistical_gate / OOS-mean evidence, so blocking OBSERVE today would
    # withhold every current artifact. Once that upstream evidence is actually
    # written for new runs, set this to True (fail-closed). Interception is
    # always subordinate to ``promotion_gate_enforce``: with enforcement
    # disabled a REJECT/OBSERVE run is still labelled and is not withheld.
    promotion_gate_require_complete_evidence: bool = Field(default=False)
    # Unified gate (v2) evidence thresholds. The defaults are aligned with what
    # the production trainer can actually produce: a 60-session walk-forward
    # prediction window yields at most ~54 matured OOS evaluation dates, so the
    # historical 120-date requirement would turn every fresh trainer run into an
    # explicit REJECT under the fail-closed serve-time enforcement default. The
    # formal promotion protocol may raise these; lowering them requires approval.
    promotion_gate_minimum_oos_dates: int = Field(default=40, ge=1)
    promotion_gate_minimum_training_samples: int = Field(default=1000, ge=1)
    trainer_cn_execution_commission_bps: float = Field(default=2.5, ge=0.0, le=200.0)
    trainer_cn_execution_slippage_bps: float = Field(default=15.0, ge=0.0, le=200.0)
    trainer_us_window_mode: str = Field(default="legacy_row_budget_v1")
    trainer_us_window_dates: int = Field(default=252, ge=1, le=1260)
    trainer_us_window_max_rows: int = Field(default=120_000, ge=1)
    trainer_us_window_max_estimated_fit_bytes: int = Field(default=2 * 1024**3, ge=1)
    # ---- Training-side robustness knobs (2026-10) ----
    # (1) Point-in-time tradable-universe filter applied to the lake rows fed
    # into sample construction. Semantics are reused from
    # app.services.stock_selection.universe.default_universe_rules (min_price,
    # min_adv20=50M, min_history_sessions=120, and -- for CN -- the signal-day
    # limit-up lock). The lake row schema does not carry ST / suspension state,
    # so those two rules are recorded as unapplied in the run's
    # `universe_filter_stats` rather than silently assumed to have run.
    # Disable for a documented research rollback.
    trainer_universe_filter_enabled: bool = Field(default=True)
    # (1a) Minimum point-in-time history (sessions) enforced by the *training-side*
    # PIT universe filter. 0 (default) disables ONLY the insufficient-history
    # rule for the trainer while keeping the liquidity and CN signal-day
    # limit-up rules active. Rationale (2026-10-08): the lake row slice handed
    # to the trainer is a short window (CN: ~425 trade dates, full-market
    # coverage only from 2025-02-14), so the 120-session warm-up prunes the head
    # of the window and starves the mature-feature-date gate with the observed
    # `insufficient_mature_feature_dates; dates=233/252`. The effective value is
    # recorded in the run's `universe_filter_stats` for auditability.
    trainer_universe_min_history_sessions: int = Field(default=0, ge=0)
    # (2) Net-return target winsorization plus a robust regression objective.
    # Quantiles are resolved per market so CN and US may carry different tail
    # behaviour. `trainer_objective="huber"` is the robust default;
    # "regression"/"l2" restores the legacy least-squares fit.
    trainer_label_winsorize_enabled: bool = Field(default=True)
    trainer_cn_label_winsorize_lower: float = Field(default=0.025, ge=0.0, lt=0.5)
    trainer_cn_label_winsorize_upper: float = Field(default=0.975, gt=0.5, le=1.0)
    trainer_us_label_winsorize_lower: float = Field(default=0.025, ge=0.0, lt=0.5)
    trainer_us_label_winsorize_upper: float = Field(default=0.975, gt=0.5, le=1.0)
    trainer_objective: str = Field(default="huber")
    trainer_huber_alpha: float = Field(default=0.9, gt=0.0)
    # (3) Drawdown penalty lambda applied to the executable label's
    # risk_adjusted_return (net - lambda*|path_drawdown|) and reused as the OOS
    # metric. `net_return` itself is preserved unchanged on every sample.
    trainer_drawdown_penalty: float = Field(default=0.25, ge=0.0)
    # Fit-target switch (2026-10). False (default) keeps the historical GBDT
    # target -- the executable `net_return` -- so the drawdown penalty only
    # moves the OOS metric, not the fit. True makes the penalty actually change
    # training: the fit target becomes the same label's `risk_adjusted_return`
    # (net - lambda*|path_drawdown|). Either way `net_return` is preserved on
    # every sample and the OOS evidence always reports both
    # `mean_risk_adjusted_return` and `mean_net_return`, so the two variants
    # stay directly comparable. The run config records the effective
    # `fit_target` (`net_return` / `risk_adjusted_return`) plus the penalty.
    trainer_fit_on_risk_adjusted: bool = Field(default=False)
    # Deterministic seed for the GBDT estimator. Exposed so a multi-seed
    # ablation can separate protocol effects from estimator variance; 42 is the
    # historical hard-coded value, so the default keeps single-seed runs
    # byte-identical.
    trainer_random_seed: int = Field(default=42, ge=0)
    # (4) Embargo gap in sessions between the last training feature date and
    # the prediction date. Unset -> `horizon_days` (purge already covers the
    # label window; the embargo adds an equal forward gap). Set 0 to restore
    # the legacy no-embargo protocol.
    trainer_embargo_sessions: int | None = Field(default=None, ge=0)
    # Per-trade-date cross-sectional feature transform: winsorize then MAD
    # robust z-score using only the same date's cross-section (point in time).
    # Mirrors CrossSectionalFactorPipeline; a degenerate cross-section (fewer
    # than two values or zero MAD) is left untransformed instead of collapsed.
    trainer_feature_transform_enabled: bool = Field(default=True)
    trainer_feature_transform_winsor_lower: float = Field(default=0.025, ge=0.0, lt=0.5)
    trainer_feature_transform_winsor_upper: float = Field(default=0.975, gt=0.5, le=1.0)
    trainer_feature_transform_zscore_clip: float = Field(default=3.0, gt=0.0)
    # Executable-label execution assumptions for the CN production trainer.
    # The confirmed next-open label prices a T+1 entry at the next open and
    # excludes signal-day limit-up opens as unbuyable unless policy="keep".
    # (Single canonical definition; consumption lives in trainer.py via
    # FillCostModel. Entry unreachability uses the band-relative limit
    # threshold -- a fixed 9.7% cutoff would be wrong on 20%-band boards.)
    # CN/US market facts are physically isolated.  These lists remain for
    # read-rollout compatibility, but both markets are enabled by default and
    # write routing must never use a shared legacy table as the only target.
    market_physical_live_markets: str = Field(default="CN,HK,US")
    market_physical_live_reads_enabled: bool = Field(default=True)
    market_physical_live_dual_write_legacy: bool = Field(default=True)
    market_physical_hot_markets: str = Field(default="CN,HK,US")
    market_physical_hot_dual_write_legacy: bool = Field(default=True)
    market_physical_snapshot_markets: str = Field(default="CN,HK,US")
    market_physical_snapshot_dual_write_legacy: bool = Field(default=True)
    workspace_snapshot_inline_payload_max_bytes: int = Field(default=100 * 1024)
    job_inline_result_max_bytes: int = Field(default=64 * 1024)
    json_payload_artifact_schema_version: str = Field(default="json-payload-artifact-v1")
    app_setting_inline_value_max_bytes: int = Field(default=32 * 1024)
    # Screener query fallback: when both the wide precompute snapshot and the
    # exact-parameter snapshot are missing (e.g. the background precompute job
    # has not run yet for this parameter set), the page/CSV/bulk paths run the
    # live ``ScreenerService.screen`` synchronously instead of rendering an empty
    # result set.  Live screening touches the market lake and can take seconds on
    # a full-market universe, so it is memoised for a short TTL and its result is
    # persisted as the exact-parameter snapshot for later requests.  Set
    # PQW_SCREENER_LIVE_FALLBACK_ENABLED=false to restore the snapshot-only
    # behaviour (empty page + "snapshot still being prepared" notice).
    screener_live_fallback_enabled: bool = Field(default=True)
    screener_live_fallback_ttl_seconds: float = Field(default=90.0, gt=0.0)
    # Upper bound (seconds) on a single synchronous live-fallback computation.
    # 0 keeps the historic blocking behaviour; a positive budget lets a slow
    # full-market screen fall back to the snapshot-pending rendering instead of
    # holding the request open.
    screener_live_fallback_timeout_seconds: float = Field(default=0.0, ge=0.0)
    tushare_token: str | None = Field(default=None)
    hithink_finance_api_key: str | None = Field(default=None)
    hithink_finance_base_url: str = Field(default="https://fuyao.aicubes.cn")
    hithink_finance_timeout_seconds: float = Field(default=25.0)
    hithink_finance_max_retries: int = Field(default=2)
    hithink_finance_min_request_interval_seconds: float = Field(default=0.20)
    hithink_finance_daily_dump_enabled: bool = Field(default=True)
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
    # Read-only EDGAR daily-filings (US event ledger) source.  SEC allows at
    # most 10 requests/second across all clients; the default interval is a
    # deliberately conservative ~5 req/s so a busy day stays well inside it.
    sec_efts_endpoint: str = Field(default="https://efts.sec.gov/LATEST/search-index")
    sec_filings_archive_endpoint: str = Field(default="https://www.sec.gov/Archives/edgar/data")
    # Official EDGAR daily index (``form.YYYYMMDD.idx``), used only as a
    # read-only completeness cross-check against the full-text fetch.  Schedule
    # the daily fetch after 23:00 ET, when this index has settled.
    sec_daily_index_endpoint: str = Field(
        default="https://www.sec.gov/Archives/edgar/daily-index"
    )
    sec_min_request_interval_seconds: float = Field(default=0.2)
    # Hard cap on EFTS result pages per (date, form) query; 1 page = 100 hits.
    # EDGAR only serves a 10,000-hit window (~100 pages), so this doubles as a
    # runaway guard and as the window ceiling for a single form on one day.
    sec_filings_max_pages: int = Field(default=100)
    # EDGAR's full-text search occasionally answers HTTP 5xx under sustained
    # pagination; retry those (and 429) a few times before failing closed.
    sec_filings_max_retries: int = Field(default=3)
    sec_filings_retry_backoff_seconds: float = Field(default=0.75)
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
    feishu_app_id: str | None = Field(default=None)
    feishu_app_secret: str | None = Field(default=None)
    feishu_chat_id: str | None = Field(default=None)
    feishu_api_base_url: str = Field(default="https://open.feishu.cn/open-apis")
    telegram_bot_token: str | None = Field(default=None)
    telegram_chat_id: str | None = Field(default=None)
    auth_username: str = Field(default="admin")
    auth_password: str | None = Field(default=None)
    auth_secret: str | None = Field(default=None)
    auth_cookie_max_age_seconds: int = Field(default=60 * 60 * 24 * 7)
    # None = derive the Secure flag from the request scheme (https -> Secure).
    # Set explicitly via PQW_AUTH_COOKIE_SECURE=true/false when the app sits
    # behind a TLS-terminating proxy whose forwarded scheme is not visible.
    auth_cookie_secure: bool | None = Field(default=None)
    backtest_commission_bps: float = Field(default=8.0)
    backtest_slippage_bps: float = Field(default=12.0)
    # P0 #1: the scheduled production chain labels and evaluates on next-open
    # executable outcomes with realistic per-leg costs, never the legacy
    # short-horizon composite that rewards unfillable momentum chasers.
    # The label family knob is `trainer_cn_label_profile`; the horizon stays
    # derived from the run's lookback so label maturity cannot drift apart
    # between the trainer and its evaluation.
    # P0 #3: realistic CN round-trip cost ladder (commission ~2.5bps per leg
    # plus stamp/transfer load and next-open market-order slippage).  The
    # previous 20bps flat assumption flattered every headline metric.
    trainer_round_trip_cost_bps: float = Field(default=50.0, ge=0.0, le=200.0)
    # Single canonical round-trip cost basis shared by every selection /
    # evaluation path that previously measured gross returns at 0 cost.  The
    # 50bps default is the existing majority convention: the P0 #3 trainer
    # ladder nominal (`trainer_round_trip_cost_bps`) and the model-evaluation
    # sensitivity ladder's scheduled nominal both centre on 50bps.  Every
    # consumer must surface the value it actually used plus this source string,
    # so a summary can never silently imply cost-free returns.
    selection_canonical_round_trip_cost_bps: float = Field(default=50.0, ge=0.0, le=200.0)
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

    model_config = SettingsConfigDict(env_prefix="PQW_", env_file=(".env", ".env.local"), extra="ignore")

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
