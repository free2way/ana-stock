from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.market_lake import (
    list_lake_symbols,
    load_lake_latest_metrics,
    load_lake_rows,
)
from app.services.repository import FundamentalSnapshotRepository, SymbolRepository
from app.services.stock_selection.artifacts import (
    ArtifactWriteResult,
    persist_sample_artifact,
    persist_universe_artifact,
)
from app.services.stock_selection.data_readiness import (
    DataReadinessConfig,
    DataReadinessEvidenceWriteResult,
    DataReadinessReport,
    assess_data_readiness,
    persist_data_readiness_report,
)
from app.services.stock_selection.factor_pipeline import CrossSectionalFactorPipeline
from app.services.stock_selection.factor_sets import (
    ResearchFactorSet,
    factor_pipeline_for_factor_set,
    get_research_factor_set,
)
from app.services.stock_selection.historical_universe_contract import (
    load_historical_universe_contract_for_market,
)
from app.services.stock_selection.factor_diagnostics import (
    FactorDiagnosticConfig,
    FactorDiagnosticReport,
    diagnose_factors,
)
from app.services.stock_selection.production_data import (
    ProductionResearchDataset,
    build_production_research_dataset,
    default_price_factor_specs,
)
from app.services.stock_selection.p1_factors import (
    P1PriceVolumeFeatureConfig,
    P1_PRICE_VOLUME_FACTOR_NAMES,
)
from app.services.stock_selection.p1_sampling import P1SamplingConfig
from app.services.stock_selection.ranker import RankerConfig
from app.services.stock_selection.research_artifacts import (
    ResearchEvidenceWriteResult,
    persist_factor_diagnostic_evidence,
    persist_walk_forward_evidence,
)
from app.services.stock_selection.research_runner import (
    WalkForwardComparisonConfig,
    WalkForwardComparisonResult,
    run_walk_forward_model_comparison,
)
from app.services.stock_selection.sample_builder import SampleBuildConfig
from app.services.execution_costs import FillCostModel
from app.services.stock_selection.executable_outcomes import FILL_COST_OUTCOME_VERSION
from app.services.stock_selection.execution_evidence import ResearchExecutionEvidence
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.sentiment_features import SENTIMENT_FACTOR_SET_KEY
from app.services.stock_selection.sentiment_research import SentimentResearchFeatureConfig
from app.services.stock_selection.universe import (
    SecurityMetadata,
    default_universe_rules,
)


@dataclass(frozen=True, slots=True)
class MarketResearchInputs:
    market: str
    rows: tuple[dict, ...]
    metadata: dict[str, SecurityMetadata]
    industries: dict[str, str | None]
    source_version: str
    selection_mode: str
    selected_tickers: tuple[str, ...]
    metadata_coverage_count: int
    industry_coverage_count: int


@dataclass(frozen=True, slots=True)
class ProductionResearchRunConfig:
    market: str
    horizons: tuple[int, ...] = (1, 3, 5)
    pilot_ticker_limit: int | None = None
    prediction_date_count: int = 20
    minimum_training_dates: int = 120
    minimum_training_samples: int = 5_000
    ranker_estimators: int = 120
    top_tail_n: int = 5
    two_stage_validation_dates: int = 20
    two_stage_minimum_validation_dates: int = 10
    round_trip_cost_bps: float = 20.0
    drawdown_penalty: float = 0.25
    target_mode: str = "risk_adjusted_return"
    factor_set_key: str = "original_v1"
    history_limit_per_symbol: int = 320
    model_keys: tuple[str, ...] = ("equal_weight", "ridge", "lambdarank")
    fill_cost_model: FillCostModel | None = None
    regime_policy_mode: str = "historical_required"
    training_sampling: P1SamplingConfig | None = None
    # Optional, default-off HiThink sentiment family. The pre-open auction path
    # is only reachable through an explicit SentimentResearchFeatureConfig.
    sentiment_research: SentimentResearchFeatureConfig | None = None

    def __post_init__(self) -> None:
        if str(self.market or "").strip().upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if not self.horizons or any(value <= 0 for value in self.horizons):
            raise ValueError("horizons must contain positive values")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons must not contain duplicates")
        if self.pilot_ticker_limit is not None and self.pilot_ticker_limit < 20:
            raise ValueError("pilot_ticker_limit must be at least 20")
        if self.prediction_date_count <= 0:
            raise ValueError("prediction_date_count must be positive")
        if self.minimum_training_dates <= 0 or self.minimum_training_samples < 2:
            raise ValueError("minimum training requirements are invalid")
        if self.ranker_estimators <= 0 or self.top_tail_n <= 0:
            raise ValueError("ranker_estimators and top_tail_n must be positive")
        if (
            self.two_stage_minimum_validation_dates < 2
            or self.two_stage_validation_dates < self.two_stage_minimum_validation_dates
        ):
            raise ValueError("two-stage validation date requirements are invalid")
        if self.round_trip_cost_bps < 0 or self.drawdown_penalty < 0:
            raise ValueError("cost and drawdown penalty must not be negative")
        if self.target_mode not in {"risk_adjusted_return", "net_return"}:
            raise ValueError("unsupported research target_mode")
        if self.fill_cost_model:
            if self.target_mode != "net_return" or self.round_trip_cost_bps != 0 or self.drawdown_penalty != 0:
                raise ValueError("fill-cost research currently requires net_return and zero legacy cost/penalty")
            if self.market.upper() == "CN" and 1 in self.horizons:
                raise ValueError("CN fill-cost research cannot use same-session exit")
        minimum_required_history = self.minimum_required_history_sessions
        if self.history_limit_per_symbol < minimum_required_history:
            raise ValueError(
                "history_limit_per_symbol must cover feature warm-up, training, OOS dates, and labels"
            )
        get_research_factor_set(self.factor_set_key)
        if self.factor_set_key == SENTIMENT_FACTOR_SET_KEY and (
            self.sentiment_research is None or not self.sentiment_research.enabled
        ):
            raise ValueError(
                "sentiment_v1 factor set requires an enabled sentiment_research "
                "config; it is never enabled silently"
            )
        allowed_models = {"equal_weight", "ridge", "lambdarank", "top_tail", "two_stage"}
        if not self.model_keys or any(item not in allowed_models for item in self.model_keys):
            raise ValueError("model_keys contains an unsupported model")
        if len(set(self.model_keys)) != len(self.model_keys):
            raise ValueError("model_keys must not contain duplicates")
        if self.regime_policy_mode != "historical_required":
            raise ValueError("production research requires historical regime evidence mode")
        if (
            self.training_sampling is not None
            and {"top_tail", "two_stage"} & set(self.model_keys)
        ):
            raise ValueError("P1 weighted sampling currently supports Ridge/LambdaRank challengers only")

    @property
    def minimum_required_history_sessions(self) -> int:
        # Universe eligibility starts after 120 observations. D+1 execution,
        # horizon labels and purge then precede the requested OOS panel.
        return 120 + self.minimum_training_dates + self.prediction_date_count + max(self.horizons) + 2


@dataclass(frozen=True, slots=True)
class ProductionResearchRunResult:
    config: ProductionResearchRunConfig
    inputs: MarketResearchInputs
    dataset: ProductionResearchDataset
    universe_artifact: ArtifactWriteResult
    sample_artifact: ArtifactWriteResult
    comparisons: dict[int, WalkForwardComparisonResult]
    evidence_artifacts: dict[int, ResearchEvidenceWriteResult]
    readiness_audit: MarketReadinessAuditResult | None = None
    factor_set: ResearchFactorSet | None = None


@dataclass(frozen=True, slots=True)
class MarketReadinessAuditResult:
    report: DataReadinessReport
    evidence: DataReadinessEvidenceWriteResult
    source_version: str


@dataclass(frozen=True, slots=True)
class ProductionFactorDiagnosticConfig:
    market: str
    horizon_days: int = 3
    pilot_ticker_limit: int | None = 80
    diagnostic_date_count: int = 60
    history_limit_per_symbol: int = 200
    target_components: tuple[str, ...] = (
        "net_return",
        "market_excess_return",
        "industry_excess_return",
        "risk_adjusted_return",
    )
    round_trip_cost_bps: float = 20.0
    drawdown_penalty: float = 0.25

    def __post_init__(self) -> None:
        if str(self.market or "").strip().upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.pilot_ticker_limit is not None and self.pilot_ticker_limit < 20:
            raise ValueError("pilot_ticker_limit must be at least 20")
        if self.diagnostic_date_count < 20:
            raise ValueError("diagnostic_date_count must be at least 20")
        minimum_required_history = 120 + self.diagnostic_date_count + self.horizon_days
        if self.history_limit_per_symbol < minimum_required_history:
            raise ValueError(
                "history_limit_per_symbol must cover universe warm-up, diagnostic dates, and labels"
            )
        if not self.target_components or any(not str(value or "").strip() for value in self.target_components):
            raise ValueError("target_components must not be empty")


@dataclass(frozen=True, slots=True)
class ProductionFactorDiagnosticResult:
    config: ProductionFactorDiagnosticConfig
    inputs: MarketResearchInputs
    dataset: ProductionResearchDataset
    reports: dict[str, FactorDiagnosticReport]
    evidence_artifacts: dict[str, ResearchEvidenceWriteResult]
    run_scope: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def market_lake_source_version(market: str) -> str:
    market_code = str(market or "").strip().upper()
    lake_dir = get_settings().data_dir / "lake" / f"{market_code.lower()}_daily"
    files = sorted(lake_dir.glob("date=*/*.parquet"))
    if not files:
        raise ValueError(f"no {market_code} market lake files found")
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(lake_dir)).encode("utf-8"))
        digest.update(_sha256_file(path).encode("ascii"))
    return f"market_lake_v1:{market_code}:{digest.hexdigest()[:20]}"


def _chunks(items: list[str], size: int = 800) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _parse_optional_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _security_type(market: str, ticker: str) -> str:
    if market == "CN":
        code = ticker.split(".", 1)[0]
        return "equity" if code.startswith(("0", "3", "4", "6", "8")) else "unknown"
    return "equity"


def research_run_scope(selection_mode: str) -> str:
    if selection_mode == "full_market_lake":
        return "engineering_full_market_survivor_biased_not_for_model_selection"
    if selection_mode == "latest_liquidity_pilot_not_point_in_time":
        return "engineering_pilot_not_for_model_selection"
    return "engineering_subset_not_for_model_selection"


def p1_feature_config_for_factor_set(
    factor_set: ResearchFactorSet,
) -> P1PriceVolumeFeatureConfig | None:
    """Enable P1 feature construction only for an explicitly selected P1 family."""
    return (
        P1PriceVolumeFeatureConfig()
        if set(factor_set.feature_names) & set(P1_PRICE_VOLUME_FACTOR_NAMES)
        else None
    )


def audit_market_research_readiness(
    *,
    market: str,
    artifact_root: Path | None = None,
    required_history_sessions: int = 252,
    historical_universe_contract_path: Path | None = None,
) -> MarketReadinessAuditResult:
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    settings = get_settings()
    historical_contract = load_historical_universe_contract_for_market(
        market=market_code,
        artifacts_dir=settings.artifacts_dir,
        explicit_path=historical_universe_contract_path,
        settings_path=settings.historical_universe_contract_path,
    )
    symbols = sorted(list_lake_symbols(market=market_code))
    if not symbols:
        raise ValueError(f"no {market_code} market lake symbols found")
    recent_metrics = load_lake_latest_metrics(
        market=market_code,
        tickers=symbols,
        lookback_days=max(320, required_history_sessions + 10),
    )
    metrics = {ticker: recent_metrics.get(ticker, {}) for ticker in symbols}
    overview_by_ticker: dict[str, dict] = {}
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        for ticker_chunk in _chunks(symbols):
            overview_by_ticker.update(symbol_repo.list_overviews_for_tickers(ticker_chunk))
    industries = {
        ticker: (
            (overview_by_ticker.get(ticker) or {}).get("industry")
            or (overview_by_ticker.get(ticker) or {}).get("sector")
        )
        for ticker in symbols
    }
    report = assess_data_readiness(
        metrics,
        security_types={ticker: _security_type(market_code, ticker) for ticker in symbols},
        metadata_present={ticker: ticker in overview_by_ticker for ticker in symbols},
        industries=industries,
        config=DataReadinessConfig(
            market=market_code,
            required_history_sessions=required_history_sessions,
            require_historical_universe_contract=True,
        ),
        historical_universe_contract=historical_contract,
    )
    source_version = market_lake_source_version(market_code)
    root = artifact_root or (settings.artifacts_dir / "stock_selection_research")
    evidence = persist_data_readiness_report(
        report,
        source_version=source_version,
        root=root / "readiness",
    )
    return MarketReadinessAuditResult(
        report=report,
        evidence=evidence,
        source_version=source_version,
    )


def load_market_research_inputs(
    *,
    market: str,
    tickers: Iterable[str] | None = None,
    pilot_ticker_limit: int | None = None,
    limit_per_symbol: int | None = None,
) -> MarketResearchInputs:
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    explicit_tickers = sorted(
        {str(item or "").strip().upper() for item in (tickers or []) if str(item or "").strip()}
    )
    if explicit_tickers:
        selected = explicit_tickers
        selection_mode = "explicit_tickers"
    elif pilot_ticker_limit:
        metrics = load_lake_latest_metrics(market=market_code, lookback_days=180)
        eligible = [
            (ticker, values)
            for ticker, values in metrics.items()
            if int(values.get("history_days") or 0) >= 120
            and int(values.get("duplicate_conflict_days") or 0) == 0
            and _security_type(market_code, ticker) == "equity"
        ]
        eligible.sort(
            key=lambda item: (
                -float(item[1].get("avg_dollar_volume") or 0.0),
                item[0],
            )
        )
        selected = [ticker for ticker, _ in eligible[:pilot_ticker_limit]]
        selection_mode = "latest_liquidity_pilot_not_point_in_time"
    else:
        selected = sorted(list_lake_symbols(market=market_code))
        selection_mode = "full_market_lake"
    if not selected:
        raise ValueError("market research input selection is empty")

    rows = load_lake_rows(
        markets=[market_code],
        tickers=set(selected),
        limit_per_symbol=limit_per_symbol,
    )
    overview_by_ticker: dict[str, dict] = {}
    fundamental_by_ticker: dict[str, dict] = {}
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        fundamental_repo = FundamentalSnapshotRepository(db)
        for ticker_chunk in _chunks(selected):
            overview_by_ticker.update(symbol_repo.list_overviews_for_tickers(ticker_chunk))
            for item in fundamental_repo.list_latest_for_market(market_code, tickers=ticker_chunk):
                ticker = str(item.get("ticker") or "").strip().upper()
                if ticker:
                    fundamental_by_ticker[ticker] = item
    metadata = {
        ticker: SecurityMetadata(
            ticker=ticker,
            security_type=_security_type(market_code, ticker),
            listing_date=_parse_optional_date((fundamental_by_ticker.get(ticker) or {}).get("listing_date")),
            name=(overview_by_ticker.get(ticker) or {}).get("name"),
        )
        for ticker in selected
    }
    industries = {
        ticker: (
            (overview_by_ticker.get(ticker) or {}).get("industry")
            or (overview_by_ticker.get(ticker) or {}).get("sector")
        )
        for ticker in selected
    }
    base_source_version = market_lake_source_version(market_code)
    selection_hash = hashlib.sha256("\n".join(selected).encode("utf-8")).hexdigest()[:12]
    source_version = f"{base_source_version}:{selection_mode}:{selection_hash}"
    if limit_per_symbol is not None:
        source_version = f"{source_version}:recent_{int(limit_per_symbol)}_sessions"
    return MarketResearchInputs(
        market=market_code,
        rows=tuple(rows),
        metadata=metadata,
        industries=industries,
        source_version=source_version,
        selection_mode=selection_mode,
        selected_tickers=tuple(selected),
        metadata_coverage_count=len(overview_by_ticker),
        industry_coverage_count=sum(bool(value) for value in industries.values()),
    )


def run_production_research_challenger(
    *,
    config: ProductionResearchRunConfig,
    artifact_root: Path | None = None,
    tickers: Iterable[str] | None = None,
    execution_evidence: ResearchExecutionEvidence | None = None,
    regime_snapshots_by_date: Mapping[date, Mapping[str, object]] | None = None,
    regime_decision_cutoffs_by_date: Mapping[date, str] | None = None,
    historical_training_regime_by_date: Mapping[date, str] | None = None,
    historical_universe_contract_path: Path | None = None,
) -> ProductionResearchRunResult:
    market_code = config.market.strip().upper()
    if bool(config.fill_cost_model) != (execution_evidence is not None):
        raise ValueError("fill-cost research and execution evidence must be enabled together")
    explicit_tickers = tuple(
        sorted({str(item or "").strip().upper() for item in (tickers or ()) if str(item or "").strip()})
    )
    root = artifact_root or (get_settings().artifacts_dir / "stock_selection_research")
    readiness_audit = None
    if config.pilot_ticker_limit is None and not explicit_tickers:
        readiness_audit = audit_market_research_readiness(
            market=market_code,
            artifact_root=root,
            required_history_sessions=config.minimum_required_history_sessions,
            historical_universe_contract_path=historical_universe_contract_path,
        )
        if not readiness_audit.report.passed:
            blockers = ", ".join(readiness_audit.report.blockers)
            raise RuntimeError(
                "formal full-market research blocked by data readiness: " + blockers
            )
    inputs = load_market_research_inputs(
        market=market_code,
        tickers=explicit_tickers,
        pilot_ticker_limit=config.pilot_ticker_limit,
        limit_per_symbol=config.history_limit_per_symbol,
    )
    factor_set = get_research_factor_set(config.factor_set_key)
    p1_feature_config = p1_feature_config_for_factor_set(factor_set)
    dataset = build_production_research_dataset(
        inputs.rows,
        market=market_code,
        metadata=inputs.metadata,
        industries=inputs.industries,
        universe_rules=default_universe_rules(market_code),
        sample_config=SampleBuildConfig(
            market=market_code,
            horizons=config.horizons,
            round_trip_cost_bps=config.round_trip_cost_bps,
            drawdown_penalty=config.drawdown_penalty,
            target_mode=config.target_mode,
            require_relative_returns=config.target_mode != "net_return",
            fill_cost_model=config.fill_cost_model,
            label_version=(FILL_COST_OUTCOME_VERSION if config.fill_cost_model else "next_open_fixed_horizon_net_profit_v1"
                           if config.target_mode == "net_return" else "next_open_industry_excess_dd_v1"),
        ),
        source_version=inputs.source_version,
        execution_evidence=execution_evidence,
        p1_feature_config=p1_feature_config,
        sentiment_research_config=config.sentiment_research,
    )
    if execution_evidence is not None:
        store = JsonPayloadArtifactStore(root / "execution_evidence")
        reference = store.write(
            execution_evidence.payload(), namespace="research_execution_evidence")
        if store.read(reference) != execution_evidence.payload():
            raise RuntimeError("execution evidence cold artifact verification failed")
        dataset = replace(dataset, sample_result=replace(dataset.sample_result, label_contract={
            **dataset.sample_result.label_contract, "execution_evidence_artifact": reference,
            "execution_evidence_artifact_root": "execution_evidence",
        }))
    universe_artifact = persist_universe_artifact(dataset.universe_result, root=root / "universes")
    sample_artifact = persist_sample_artifact(dataset.sample_result, root=root / "datasets")
    factor_pipeline = factor_pipeline_for_factor_set(factor_set)
    feature_names = factor_set.feature_names
    comparisons: dict[int, WalkForwardComparisonResult] = {}
    evidence: dict[int, ResearchEvidenceWriteResult] = {}
    run_scope = research_run_scope(inputs.selection_mode)
    for horizon in config.horizons:
        horizon_dates = sorted(
            {
                item.feature_date
                for item in dataset.sample_result.samples
                if item.horizon_days == horizon and item.tradable
            }
        )
        prediction_dates = horizon_dates[-config.prediction_date_count :]
        comparison = run_walk_forward_model_comparison(
            dataset.sample_result.samples,
            trading_dates=dataset.trading_dates,
            prediction_dates=prediction_dates,
            factor_pipeline=factor_pipeline,
            ranker_config=RankerConfig(
                feature_names=feature_names,
                horizon_days=horizon,
                n_estimators=config.ranker_estimators,
            ),
            config=WalkForwardComparisonConfig(
                horizon_days=horizon,
                purge_sessions=horizon,
                minimum_training_dates=config.minimum_training_dates,
                minimum_training_samples=config.minimum_training_samples,
                top_tail_n=config.top_tail_n,
                top_tail_estimators=config.ranker_estimators,
                two_stage_validation_dates=config.two_stage_validation_dates,
                two_stage_minimum_validation_dates=config.two_stage_minimum_validation_dates,
                require_all_prediction_dates=True,
                model_keys=config.model_keys,
                regime_policy_mode=config.regime_policy_mode,
                training_sampling=config.training_sampling,
            ),
            regime_snapshots_by_date=regime_snapshots_by_date,
            regime_decision_cutoffs_by_date=regime_decision_cutoffs_by_date,
            historical_training_regime_by_date=historical_training_regime_by_date,
        )
        comparisons[horizon] = comparison
        evidence[horizon] = persist_walk_forward_evidence(
            comparison,
            root=root / "evidence",
            market=market_code,
            source_version=inputs.source_version,
            universe_version=dataset.universe_result.universe_version,
            dataset_version=dataset.sample_result.dataset_version,
            run_scope=run_scope,
            factor_set_key=factor_set.key,
            factor_set_version=factor_set.version(),
        )
    return ProductionResearchRunResult(
        config=config,
        inputs=inputs,
        dataset=dataset,
        universe_artifact=universe_artifact,
        sample_artifact=sample_artifact,
        comparisons=comparisons,
        evidence_artifacts=evidence,
        readiness_audit=readiness_audit,
        factor_set=factor_set,
    )


def run_production_factor_diagnostics(
    *,
    config: ProductionFactorDiagnosticConfig,
    artifact_root: Path | None = None,
    tickers: Iterable[str] | None = None,
    historical_universe_contract_path: Path | None = None,
) -> ProductionFactorDiagnosticResult:
    market_code = config.market.strip().upper()
    explicit_tickers = tuple(
        sorted({str(item or "").strip().upper() for item in (tickers or ()) if str(item or "").strip()})
    )
    root = artifact_root or (get_settings().artifacts_dir / "stock_selection_research")
    if config.pilot_ticker_limit is None and not explicit_tickers:
        readiness = audit_market_research_readiness(
            market=market_code,
            artifact_root=root,
            historical_universe_contract_path=historical_universe_contract_path,
        )
        if not readiness.report.passed:
            raise RuntimeError(
                "formal full-market factor diagnostics blocked by data readiness: "
                + ", ".join(readiness.report.blockers)
            )
    inputs = load_market_research_inputs(
        market=market_code,
        tickers=explicit_tickers,
        pilot_ticker_limit=config.pilot_ticker_limit,
        limit_per_symbol=config.history_limit_per_symbol,
    )
    dataset = build_production_research_dataset(
        inputs.rows,
        market=market_code,
        metadata=inputs.metadata,
        industries=inputs.industries,
        universe_rules=default_universe_rules(market_code),
        sample_config=SampleBuildConfig(
            market=market_code,
            horizons=(config.horizon_days,),
            round_trip_cost_bps=config.round_trip_cost_bps,
            drawdown_penalty=config.drawdown_penalty,
        ),
        source_version=inputs.source_version,
    )
    persist_universe_artifact(dataset.universe_result, root=root / "universes")
    persist_sample_artifact(dataset.sample_result, root=root / "datasets")
    horizon_samples = [
        item
        for item in dataset.sample_result.samples
        if item.horizon_days == config.horizon_days and item.tradable
    ]
    diagnostic_dates = sorted({item.feature_date for item in horizon_samples})[
        -config.diagnostic_date_count :
    ]
    if not diagnostic_dates:
        raise ValueError("factor diagnostics found no eligible sample dates")
    diagnostic_date_set = set(diagnostic_dates)
    diagnostic_samples = [
        item for item in horizon_samples if item.feature_date in diagnostic_date_set
    ]
    scores = CrossSectionalFactorPipeline(default_price_factor_specs()).transform(
        diagnostic_samples
    )
    run_scope = research_run_scope(inputs.selection_mode)
    reports: dict[str, FactorDiagnosticReport] = {}
    evidence: dict[str, ResearchEvidenceWriteResult] = {}
    for target_component in config.target_components:
        report = diagnose_factors(
            scores,
            config=FactorDiagnosticConfig(
                minimum_cross_section_size=20,
                minimum_date_count=20,
                target_component=target_component,
            ),
        )
        reports[target_component] = report
        evidence[target_component] = persist_factor_diagnostic_evidence(
            report,
            root=root / "factor_diagnostics",
            market=market_code,
            source_version=inputs.source_version,
            universe_version=dataset.universe_result.universe_version,
            dataset_version=dataset.sample_result.dataset_version,
            run_scope=run_scope,
        )
    return ProductionFactorDiagnosticResult(
        config=config,
        inputs=inputs,
        dataset=dataset,
        reports=reports,
        evidence_artifacts=evidence,
        run_scope=run_scope,
    )
