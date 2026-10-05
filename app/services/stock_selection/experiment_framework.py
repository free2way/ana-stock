"""胜率提升实验框架（最小可用版本）。

这个模块把一次"改动前后"的选股假设检验固化成可复算、可审计的流程：

1. **冻结 dataset_hash**：把 ``market_lake`` 来源版本、``factor_set``、
   ``label_version``、``universe_version`` 组合成一个稳定哈希，复算时任何
   输入漂移都会改变哈希。
2. **同面板 treated/control**：两条臂共用同一宇宙、同一 OOS 日期集合、
   同一 Top-N、同一成本与标签版本（禁止各自窗口）。
3. **分层评估**：按 regime / 行业 / 市值桶 / 流动性桶输出
   n / hit_rate / net_avg / positive_date_rate。
4. **显著性**：主指标按日期聚类的 block bootstrap（复用
   :mod:`app.services.statistical_inference`）+ treated−control 配对 t 检验
   + 多桶 BH-FDR；随机对照用行内 shuffle × 5 seeds 产出 null 分布，四分类
   ``confirmed_alive`` / ``train_only`` / ``reversed_strict`` / ``noise``。
5. **判定**：CI95 下界 > 0 且效应 ≥ 收紧后的 ``min_effect_pp``（经 FDR）
   且独立日期数 ≥ ``min_independent_dates``。
6. **attempts 收紧**：接入 :mod:`app.services.stock_selection.experiment_registry`
   的哈希链 attempts，阈值随历史尝试次数收紧
   ``min_effect_pp × sqrt(1 + ln(attempts))``（attempts=1 时因子为 1）。

模块本身对 DB 无依赖：核心的 :func:`evaluate_experiment` 只吃合成/已加载的
收益向量，因此测试可以完全离线运行；只有在真正冻结市场来源版本时才会懒加载
:func:`app.services.stock_selection.production_research.market_lake_source_version`。

7. **排序字段敏感性**：``spec.ranking_fields`` 可声明多个候选排序字段（默认
   单字段 ``raw_score``）。每个字段分别重算 effect/分类；若各字段效应**符号
   不一致**，报告标 ``ranking_sensitivity.stable = false``，并把分类降级为
   ``noise``/``reversed_strict``（禁止 ``confirmed_alive``），判定理由注明
   "低功效/排序不稳定"。

逐票可成交门槛 vs regime 层（语义分离）：

- **逐票可成交门槛**（:class:`PerTicketTradabilityConfig` + :func:`apply_per_ticket_gate`）
  只用**单票自身**的可成交特征（``limit_up_today`` 涨停封板买不到、
  ``volume<=0`` 停牌/零成交代理、``dollar_volume`` 低流动性分位），
  **不读取 regime / 市场状态 / trade_readiness_score**，因此 treated 与 regime
  不再共线。
- **regime 层**（:data:`REGIME_LAYER_FIELDS`）：``regime``、``tradability_status``、
  ``readiness_bucket``、``trade_readiness_score`` 属于 regime/市场状态与模型
  置信层，只用于**单独分层报告**（``spec.strata``），禁止用来定义 treated/control。
  这样"门槛语义"与"市场状态"不再互相混淆。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.services.statistical_inference import block_bootstrap_mean_ci

SCHEMA_VERSION = "stock_selection_experiment_framework_v1"

# 与 promotion_gate_v2 的统计子门对齐：PASS 的实验对应
# ELIGIBLE_FOR_MANUAL_REVIEW，其余为 REJECT。常量本地声明以避免在核心
# 模块导入期耦合 promotion_gate_v2；测试会断言它们与 gate 常量一致。
PROMOTION_DECISION_ELIGIBLE = "ELIGIBLE_FOR_MANUAL_REVIEW"
PROMOTION_DECISION_REJECT = "REJECT"

PRIMARY_METRICS = ("net_return", "gross_return", "market_excess_return", "risk_adjusted_return")

CLASSIFICATIONS = ("confirmed_alive", "train_only", "reversed_strict", "noise")

DEFAULT_STRICT_T_THRESHOLD = 3.5
DEFAULT_RANDOM_CONTROL_SEEDS = 5
DEFAULT_RANKING_FIELDS = ("raw_score",)

# 逐票可成交性特征：只允许单票自身字段，禁止 regime / readiness。
PER_TICKET_TRADABILITY_FIELDS = ("limit_up_today", "volume", "dollar_volume")

# regime / 模型置信层字段：仅用于单独分层报告，禁止参与 treated/control 定义。
REGIME_LAYER_FIELDS = ("regime", "tradability_status", "readiness_bucket", "trade_readiness_score")


# ---------------------------------------------------------------------------
# 假设规格（YAML / JSON）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangeSpec:
    kind: str
    patch: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PanelSpec:
    oos_start: date
    oos_end: date
    universe_version: str
    label_version: str
    cost_bps: float
    horizons: tuple[int, ...]
    top_n: int


@dataclass(frozen=True, slots=True)
class DecisionSpec:
    primary_metric: str = "net_return"
    min_effect_pp: float = 0.0
    alpha: float = 0.05
    fdr: float = 0.05
    min_independent_dates: int = 20
    strict_t_threshold: float = DEFAULT_STRICT_T_THRESHOLD
    bootstrap_iterations: int = 1000


@dataclass(frozen=True, slots=True)
class SelectionExperimentSpec:
    experiment_id: str
    market: str
    hypothesis: str
    change: ChangeSpec
    panel: PanelSpec
    strata: tuple[str, ...]
    decision: DecisionSpec
    ranking_fields: tuple[str, ...] = DEFAULT_RANKING_FIELDS


def load_experiment_spec(path: Path | str) -> SelectionExperimentSpec:
    """读取并校验假设规格（``.json`` / ``.yaml`` / ``.yml``）。

    YAML 优先使用环境中已安装的 PyYAML；若不可用则回退到本模块自带的
    极简 YAML 子集解析器（映射、标量列表、内联列表、注释），以保持"零新增
    外部依赖"。规格结构见 :class:`SelectionExperimentSpec`。
    """

    spec_path = Path(path)
    text = spec_path.read_text(encoding="utf-8")
    if spec_path.suffix.lower() in {".yaml", ".yml"}:
        raw = _load_yaml(text)
    else:
        raw = json.loads(text)
    if not isinstance(raw, Mapping):
        raise ValueError("experiment spec must be a mapping")
    return spec_from_mapping(raw)


def spec_from_mapping(raw: Mapping[str, Any]) -> SelectionExperimentSpec:
    experiment_id = _require_str(raw, "experiment_id")
    market = _require_str(raw, "market").upper()
    if market not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    hypothesis = _require_str(raw, "hypothesis")

    change_raw = raw.get("change")
    if not isinstance(change_raw, Mapping):
        raise ValueError("change must be a mapping with kind/patch")
    change = ChangeSpec(
        kind=_require_str(change_raw, "kind"),
        patch=dict(change_raw.get("patch") or {}),
    )

    panel_raw = raw.get("panel")
    if not isinstance(panel_raw, Mapping):
        raise ValueError("panel must be a mapping")
    panel = PanelSpec(
        oos_start=_require_date(panel_raw, "oos_start"),
        oos_end=_require_date(panel_raw, "oos_end"),
        universe_version=_require_str(panel_raw, "universe_version"),
        label_version=_require_str(panel_raw, "label_version"),
        cost_bps=float(_require_number(panel_raw, "cost_bps")),
        horizons=tuple(int(value) for value in _require_sequence(panel_raw, "horizons")),
        top_n=int(_require_number(panel_raw, "top_n")),
    )
    if panel.oos_end < panel.oos_start:
        raise ValueError("panel.oos_end must not be before panel.oos_start")
    if not panel.horizons or any(value <= 0 for value in panel.horizons):
        raise ValueError("panel.horizons must contain positive values")
    if len(set(panel.horizons)) != len(panel.horizons):
        raise ValueError("panel.horizons must not contain duplicates")
    if panel.top_n <= 0:
        raise ValueError("panel.top_n must be positive")
    if panel.cost_bps < 0:
        raise ValueError("panel.cost_bps must not be negative")

    strata_raw = raw.get("strata") or []
    if isinstance(strata_raw, str):
        strata_raw = [strata_raw]
    if not isinstance(strata_raw, Sequence):
        raise ValueError("strata must be a list of dimension names")
    strata = tuple(str(item) for item in strata_raw if str(item).strip())

    decision_raw = raw.get("decision") or {}
    if not isinstance(decision_raw, Mapping):
        raise ValueError("decision must be a mapping")
    decision = DecisionSpec(
        primary_metric=str(decision_raw.get("primary_metric", "net_return")),
        min_effect_pp=float(decision_raw.get("min_effect_pp", 0.0)),
        alpha=float(decision_raw.get("alpha", 0.05)),
        fdr=float(decision_raw.get("fdr", 0.05)),
        min_independent_dates=int(decision_raw.get("min_independent_dates", 20)),
        strict_t_threshold=float(
            decision_raw.get("strict_t_threshold", decision_raw.get("t_threshold", DEFAULT_STRICT_T_THRESHOLD))
        ),
        bootstrap_iterations=int(decision_raw.get("bootstrap_iterations", 1000)),
    )
    if decision.primary_metric not in PRIMARY_METRICS:
        raise ValueError(f"decision.primary_metric must be one of {PRIMARY_METRICS}")
    if not 0.0 < decision.alpha < 1.0:
        raise ValueError("decision.alpha must be in (0, 1)")
    if not 0.0 < decision.fdr < 1.0:
        raise ValueError("decision.fdr must be in (0, 1)")
    if decision.min_effect_pp < 0:
        raise ValueError("decision.min_effect_pp must not be negative")
    if decision.min_independent_dates < 1:
        raise ValueError("decision.min_independent_dates must be positive")
    if decision.strict_t_threshold <= 0:
        raise ValueError("decision.strict_t_threshold must be positive")
    if decision.bootstrap_iterations < 50:
        raise ValueError("decision.bootstrap_iterations must be at least 50")

    ranking_raw = raw.get("ranking_fields")
    if ranking_raw is None:
        ranking_fields = DEFAULT_RANKING_FIELDS
    else:
        if isinstance(ranking_raw, str):
            ranking_raw = [ranking_raw]
        if not isinstance(ranking_raw, Sequence):
            raise ValueError("ranking_fields must be a list of field names")
        ranking_fields = tuple(str(item).strip() for item in ranking_raw if str(item).strip())
        if not ranking_fields:
            raise ValueError("ranking_fields must not be empty")
        if len(set(ranking_fields)) != len(ranking_fields):
            raise ValueError("ranking_fields must not contain duplicates")

    return SelectionExperimentSpec(
        experiment_id=experiment_id,
        market=market,
        hypothesis=hypothesis,
        change=change,
        panel=panel,
        strata=strata,
        decision=decision,
        ranking_fields=ranking_fields,
    )


# ---------------------------------------------------------------------------
# 面板行与冻结哈希
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentRow:
    feature_date: date
    ticker: str
    raw_score: float
    net_return: float
    gross_return: float | None = None
    market_excess_return: float | None = None
    label_value: float | None = None
    strata: Mapping[str, str] = field(default_factory=dict)
    # 多排序字段：字段名 -> 取值（如 trend_score / momentum_20）。缺省时回退 raw_score。
    ranking_values: Mapping[str, float] = field(default_factory=dict)
    # 逐票可成交特征：limit_up_today(0/1) / volume / dollar_volume。禁止放 regime/readiness。
    tradability_values: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DatasetHashResult:
    dataset_hash: str
    source_version: str
    components: Mapping[str, str]


def freeze_dataset_hash(
    *,
    market: str,
    factor_set_key: str,
    label_version: str,
    universe_version: str,
    source_version: str | None = None,
    sentiment: Mapping[str, object] | None = None,
) -> DatasetHashResult:
    """冻结 dataset_hash：market_lake 来源版本 + factor_set + label + universe。

    ``source_version`` 可显式传入（测试/复算用）；省略时懒加载
    ``market_lake_source_version``，因此模块导入本身不触碰 DB/文件系统。

    ``sentiment`` 可选：传入时把情绪覆盖窗口与决定 cutoff 语义并入哈希（情绪
    因子集不得在未声明覆盖/cutoff 的情况下复用同一 dataset_hash）；缺省不改变
    既有价格/P1 数据集的哈希。
    """

    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    resolved_source = source_version
    if resolved_source is None:
        from app.services.stock_selection.production_research import market_lake_source_version

        resolved_source = market_lake_source_version(market_code)
    components = {
        "market": market_code,
        "source_version": str(resolved_source),
        "factor_set_key": str(factor_set_key),
        "label_version": str(label_version),
        "universe_version": str(universe_version),
    }
    if sentiment is not None:
        from app.services.stock_selection.sentiment_research import (
            validate_sentiment_hash_components,
        )

        resolved_sentiment = validate_sentiment_hash_components(sentiment)
        for key in (
            "factor_set_key",
            "source_version",
            "coverage_start",
            "coverage_end",
            "missing_policy",
            "decision_cutoff",
            "cutoff_time_local",
            "auction_path_enabled",
        ):
            components[f"sentiment_{key}"] = resolved_sentiment.get(key)
    canonical = json.dumps(components, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return DatasetHashResult(
        dataset_hash=f"selection_dataset_v1:{market_code}:{digest}",
        source_version=str(resolved_source),
        components=components,
    )


# ---------------------------------------------------------------------------
# 确定性工具
# ---------------------------------------------------------------------------


def tightened_min_effect_pp(min_effect_pp: float, attempts: int) -> tuple[float, float]:
    """按历史尝试次数收紧最小效应阈值，返回 ``(effective, factor)``。

    因子为 ``sqrt(1 + ln(max(attempts, 1)))``：首次尝试（attempts<=1）为 1，
    之后随 attempts 单调增大，等价于题设的 ``min_effect × sqrt(log(attempts))``
    但避免了 attempts=1 时 log(1)=0 导致阈值归零。
    """

    base = float(min_effect_pp)
    if base < 0:
        raise ValueError("min_effect_pp must not be negative")
    factor = math.sqrt(1.0 + math.log(max(int(attempts), 1)))
    return base * factor, factor


def independent_date_count(dates: Iterable[date], *, horizon_days: int) -> int:
    """近似独立日期数：按 horizon 对日期去重叠（贪心取最早可用日期）。"""

    unique = sorted(set(dates))
    if not unique:
        return 0
    if horizon_days <= 1:
        return len(unique)
    count = 0
    last: date | None = None
    for current in unique:
        if last is None or (current - last).days >= horizon_days:
            count += 1
            last = current
    return count


def _deterministic_seed(*parts: object) -> int:
    canonical = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _two_sided_p_value(t_stat: float) -> float:
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """Benjamini–Hochberg step-up，返回与输入同序的 q 值。"""

    count = len(p_values)
    if count == 0:
        return []
    order = sorted(range(count), key=lambda index: p_values[index])
    q_values: list[float] = [1.0] * count
    previous = 1.0
    for rank in range(count, 0, -1):
        index = order[rank - 1]
        candidate = min(previous, p_values[index] * count / rank)
        q_values[index] = candidate
        previous = candidate
    return q_values


# ---------------------------------------------------------------------------
# 分层与显著性
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArmPanelSummary:
    arm: str
    evaluated_date_count: int
    observation_count: int
    mean_metric: float
    mean_net_return: float
    positive_date_rate: float


@dataclass(frozen=True, slots=True)
class EffectStatistics:
    date_count: int
    independent_date_count: int
    mean_diff: float
    effect_pp: float
    ci95: tuple[float, float]
    std_error: float
    t_stat: float
    p_value: float
    q_value: float
    block_length: int
    bootstrap_iterations: int


@dataclass(frozen=True, slots=True)
class StrataBucketMetrics:
    dimension: str
    bucket: str
    observation_count: int
    date_count: int
    hit_rate: float
    net_avg: float
    positive_date_rate: float
    control_net_avg: float | None
    effect_pp: float | None
    effect_ci95: tuple[float, float] | None
    t_stat: float | None
    p_value: float | None
    q_value: float | None


@dataclass(frozen=True, slots=True)
class RandomControlResult:
    seeds: tuple[int, ...]
    shuffle_count: int
    null_mean: float
    null_std: float
    null_min: float
    null_max: float
    observed_effect: float
    p_value: float
    beats_null: bool


@dataclass(frozen=True, slots=True)
class DecisionCheck:
    key: str
    passed: bool
    observed: float | int | str | None
    threshold: str
    detail: str


@dataclass(frozen=True, slots=True)
class SelectionExperimentReport:
    schema_version: str
    experiment_id: str
    market: str
    hypothesis: str
    change: Mapping[str, Any]
    panel: Mapping[str, Any]
    dataset_hash: str
    dataset_hash_components: Mapping[str, str]
    attempts: int
    tightening_factor: float
    effective_min_effect_pp: float
    treated: ArmPanelSummary
    control: ArmPanelSummary
    strata: tuple[StrataBucketMetrics, ...]
    overall: EffectStatistics
    random_control: RandomControlResult
    classification: str
    decision: str
    checks: tuple[DecisionCheck, ...]
    recompute_command: str
    report_digest: str
    ranking_fields: tuple[str, ...] = DEFAULT_RANKING_FIELDS
    ranked_by: str = "raw_score"
    ranking_sensitivity: Mapping[str, Any] = field(default_factory=dict)
    layers: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return _to_jsonable(_asdict(self))


# `dataclasses.asdict` 在这里不方便处理 date，用自带递归转换。
def _asdict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {name: _asdict(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return {key: _asdict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_asdict(item) for item in value]
    return value


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


def _metric_value(row: ExperimentRow, primary_metric: str) -> float:
    if primary_metric == "net_return":
        return float(row.net_return)
    if primary_metric == "gross_return":
        if row.gross_return is None:
            raise ValueError("row is missing gross_return required by primary_metric")
        return float(row.gross_return)
    if primary_metric == "market_excess_return":
        if row.market_excess_return is None:
            raise ValueError("row is missing market_excess_return required by primary_metric")
        return float(row.market_excess_return)
    if primary_metric == "risk_adjusted_return":
        if row.label_value is None:
            raise ValueError("row is missing label_value/risk_adjusted_return required by primary_metric")
        return float(row.label_value)
    raise ValueError(f"unsupported primary_metric: {primary_metric}")


def _validate_costs(rows: Sequence[ExperimentRow], *, cost_bps: float) -> None:
    expected = cost_bps / 10_000.0
    for row in rows:
        if row.gross_return is None:
            continue
        observed = float(row.gross_return) - float(row.net_return)
        if abs(observed - expected) > 1e-9:
            raise ValueError(
                "configured cost_bps does not match row label components "
                f"(expected {expected}, observed {observed})"
            )


def _ranking_value(row: ExperimentRow, ranking_field: str) -> float:
    if ranking_field in {"raw_score", "score"}:
        return float(row.raw_score)
    if ranking_field in (row.ranking_values or {}):
        return float(row.ranking_values[ranking_field])
    raise ValueError(
        f"ranking field {ranking_field!r} missing on row {row.ticker}@{row.feature_date.isoformat()}"
    )


def _select_top_n(
    rows: Sequence[ExperimentRow],
    *,
    top_n: int,
    oos_start: date,
    oos_end: date,
    ranking_field: str = "raw_score",
) -> tuple[dict[date, list[ExperimentRow]], tuple[date, ...]]:
    by_date: dict[date, list[ExperimentRow]] = {}
    for row in rows:
        if not (oos_start <= row.feature_date <= oos_end):
            continue
        by_date.setdefault(row.feature_date, []).append(row)
    selected: dict[date, list[ExperimentRow]] = {}
    for feature_date in sorted(by_date):
        eligible = by_date[feature_date]
        if len(eligible) < top_n:
            raise ValueError(f"fewer than top_n eligible rows on {feature_date.isoformat()}")
        selected[feature_date] = sorted(
            eligible,
            key=lambda item: (-_ranking_value(item, ranking_field), item.ticker),
        )[:top_n]
    if not selected:
        raise ValueError("no rows fall inside the OOS panel window")
    return selected, tuple(sorted(selected))


def _enforce_same_panel(treated_dates: tuple[date, ...], control_dates: tuple[date, ...]) -> tuple[date, ...]:
    treated_set = set(treated_dates)
    control_set = set(control_dates)
    if treated_set != control_set:
        missing = sorted(treated_set.symmetric_difference(control_set))
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(
            "treated/control must share the same OOS date set; "
            f"mismatched dates ({len(missing)}): {preview}"
        )
    return treated_dates


def _daily_series(
    selected: Mapping[date, Sequence[ExperimentRow]],
    *,
    primary_metric: str,
    predicate: Any = None,
) -> dict[date, float]:
    series: dict[date, float] = {}
    for feature_date in sorted(selected):
        values = [
            _metric_value(row, primary_metric)
            for row in selected[feature_date]
            if predicate is None or predicate(row)
        ]
        if values:
            series[feature_date] = statistics.fmean(values)
    return series


def _daily_net_series(
    selected: Mapping[date, Sequence[ExperimentRow]],
    *,
    predicate: Any = None,
) -> dict[date, float]:
    series: dict[date, float] = {}
    for feature_date in sorted(selected):
        values = [
            float(row.net_return)
            for row in selected[feature_date]
            if predicate is None or predicate(row)
        ]
        if values:
            series[feature_date] = statistics.fmean(values)
    return series


def _arm_summary(
    arm: str,
    selected: Mapping[date, Sequence[ExperimentRow]],
    daily_metric: Mapping[date, float],
) -> ArmPanelSummary:
    observations = [row for rows in selected.values() for row in rows]
    net_values = [float(row.net_return) for row in observations]
    metric_values = list(daily_metric.values())
    return ArmPanelSummary(
        arm=arm,
        evaluated_date_count=len(daily_metric),
        observation_count=len(observations),
        mean_metric=statistics.fmean(metric_values) if metric_values else 0.0,
        mean_net_return=statistics.fmean(net_values) if net_values else 0.0,
        positive_date_rate=(
            sum(value > 0 for value in daily_metric.values()) / len(daily_metric)
            if daily_metric
            else 0.0
        ),
    )


def _paired_effect(
    treated_series: Mapping[date, float],
    control_series: Mapping[date, float],
    *,
    block_length: int,
    bootstrap_iterations: int,
    seed: int,
    alpha: float,
) -> tuple[float, tuple[float, float], float, float, int]:
    common = sorted(set(treated_series) & set(control_series))
    if len(common) < 2:
        raise ValueError("paired effect requires at least two common dates")
    diffs = [treated_series[day] - control_series[day] for day in common]
    mean = statistics.fmean(diffs)
    std = statistics.stdev(diffs)
    std_error = std / math.sqrt(len(diffs)) if std > 0 else 0.0
    t_stat = mean / std_error if std_error > 0 else 0.0
    ci = block_bootstrap_mean_ci(
        diffs,
        block_length=max(1, int(block_length)),
        iterations=max(50, int(bootstrap_iterations)),
        seed=seed,
        alpha=alpha,
    )
    if ci is None:
        ci = (mean, mean)
    return mean, (float(ci[0]), float(ci[1])), std_error, t_stat, len(common)


def _random_control_null(
    treated_selected: Mapping[date, Sequence[ExperimentRow]],
    control_selected: Mapping[date, Sequence[ExperimentRow]],
    *,
    primary_metric: str,
    seeds: Sequence[int],
) -> tuple[list[float], float]:
    """行内 shuffle：在每天内把 treated/control 的样本重新分配，保留臂大小。"""

    pooled: dict[date, list[tuple[float, str]]] = {}
    for feature_date in sorted(set(treated_selected) | set(control_selected)):
        items: list[tuple[float, str]] = []
        for row in treated_selected.get(feature_date, ()):
            items.append((_metric_value(row, primary_metric), "treated"))
        for row in control_selected.get(feature_date, ()):
            items.append((_metric_value(row, primary_metric), "control"))
        pooled[feature_date] = items

    null_effects: list[float] = []
    all_diffs: list[float] = []
    for seed in seeds:
        rng = random.Random(seed)
        daily_diffs: list[float] = []
        for feature_date in sorted(pooled):
            items = list(pooled[feature_date])
            treated_count = sum(1 for _, arm in items if arm == "treated")
            rng.shuffle(items)
            treated_values = [value for value, _ in items[:treated_count]]
            control_values = [value for value, _ in items[treated_count:]]
            if not treated_values or not control_values:
                continue
            daily_diffs.append(statistics.fmean(treated_values) - statistics.fmean(control_values))
        if daily_diffs:
            null_effects.append(statistics.fmean(daily_diffs))
            all_diffs.extend(daily_diffs)
    null_std = statistics.stdev(null_effects) if len(null_effects) >= 2 else 0.0
    return null_effects, null_std


# ---------------------------------------------------------------------------
# 逐票可成交门槛 / regime 层分离
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerTicketTradabilityConfig:
    """逐票可成交门槛：只用单票字段，禁止 regime/市场状态/readiness。

    - ``exclude_limit_up_today``：信号日涨停封板（下一开盘大概率买不到）。
    - ``exclude_zero_volume``：``volume<=0`` 作为停牌/零成交代理。
    - ``min_dollar_volume_percentile``：按当日截面成交额分位剔除低流动性，
      ``None`` 关闭；缺失 ``dollar_volume`` 的行按 ``require_known_fields`` 处理。
    - ``require_known_fields``：字段缺失时是否 fail-closed（默认否，缺失即放行）。
    """

    exclude_limit_up_today: bool = True
    exclude_zero_volume: bool = True
    min_dollar_volume_percentile: float | None = 20.0
    require_known_fields: bool = False

    def __post_init__(self) -> None:
        if self.min_dollar_volume_percentile is not None and not (
            0.0 < float(self.min_dollar_volume_percentile) < 100.0
        ):
            raise ValueError("min_dollar_volume_percentile must be in (0, 100) or None")

    def as_dict(self) -> dict[str, Any]:
        return {
            "exclude_limit_up_today": bool(self.exclude_limit_up_today),
            "exclude_zero_volume": bool(self.exclude_zero_volume),
            "min_dollar_volume_percentile": self.min_dollar_volume_percentile,
            "require_known_fields": bool(self.require_known_fields),
        }


def _percentile_threshold(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = int(len(ordered) * (float(percentile) / 100.0))
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def per_ticket_dollar_volume_thresholds(
    rows: Iterable[ExperimentRow],
    config: PerTicketTradabilityConfig,
) -> dict[date, float | None]:
    """按 feature_date 计算 ``dollar_volume`` 截面分位阈值（实数口径）。"""

    if config.min_dollar_volume_percentile is None:
        return {}
    by_date: dict[date, list[float]] = {}
    for row in rows:
        value = (row.tradability_values or {}).get("dollar_volume")
        if value is not None:
            by_date.setdefault(row.feature_date, []).append(float(value))
    return {
        feature_date: _percentile_threshold(values, config.min_dollar_volume_percentile)
        for feature_date, values in by_date.items()
    }


def per_ticket_tradable(
    row: ExperimentRow,
    config: PerTicketTradabilityConfig,
    *,
    dollar_volume_threshold: float | None = None,
) -> bool:
    values = row.tradability_values or {}
    if config.exclude_limit_up_today:
        limit_up = values.get("limit_up_today")
        if limit_up is not None and float(limit_up) > 0.0:
            return False
        if limit_up is None and config.require_known_fields:
            return False
    if config.exclude_zero_volume:
        volume = values.get("volume")
        if volume is not None and float(volume) <= 0.0:
            return False
        if volume is None and config.require_known_fields:
            return False
    if config.min_dollar_volume_percentile is not None:
        dollar_volume = values.get("dollar_volume")
        if dollar_volume is None:
            if config.require_known_fields:
                return False
        elif dollar_volume_threshold is not None and float(dollar_volume) < dollar_volume_threshold:
            return False
    return True


def apply_per_ticket_gate(
    rows: Iterable[ExperimentRow],
    config: PerTicketTradabilityConfig | None = None,
) -> list[ExperimentRow]:
    """返回逐票可成交子集（treated 池）。

    门槛阈值按整池 ``feature_date`` 截面计算，因此不依赖 regime/市场状态；
    同一 (date,ticker) 的逐票特征在任何 regime 下判定一致。
    """

    materialized = list(rows)
    rules = config or PerTicketTradabilityConfig()
    thresholds = per_ticket_dollar_volume_thresholds(materialized, rules)
    return [
        row
        for row in materialized
        if per_ticket_tradable(
            row,
            rules,
            dollar_volume_threshold=thresholds.get(row.feature_date),
        )
    ]


def per_ticket_gate_summary(
    rows: Iterable[ExperimentRow],
    config: PerTicketTradabilityConfig | None = None,
) -> dict[str, Any]:
    """报告门槛的逐条排除计数，便于如实说明缺口/覆盖。"""

    materialized = list(rows)
    rules = config or PerTicketTradabilityConfig()
    thresholds = per_ticket_dollar_volume_thresholds(materialized, rules)
    excluded_limit_up = 0
    excluded_zero_volume = 0
    excluded_low_liquidity = 0
    missing_fields: dict[str, int] = {name: 0 for name in PER_TICKET_TRADABILITY_FIELDS}
    kept = 0
    for row in materialized:
        values = row.tradability_values or {}
        for name in PER_TICKET_TRADABILITY_FIELDS:
            if values.get(name) is None:
                missing_fields[name] += 1
        if per_ticket_tradable(row, rules, dollar_volume_threshold=thresholds.get(row.feature_date)):
            kept += 1
            continue
        if rules.exclude_limit_up_today and float(values.get("limit_up_today") or 0.0) > 0.0:
            excluded_limit_up += 1
        elif rules.exclude_zero_volume and values.get("volume") is not None and float(values["volume"]) <= 0.0:
            excluded_zero_volume += 1
        elif rules.min_dollar_volume_percentile is not None and values.get("dollar_volume") is not None:
            excluded_low_liquidity += 1
    return {
        "config": rules.as_dict(),
        "input_count": len(materialized),
        "kept_count": kept,
        "excluded_count": len(materialized) - kept,
        "excluded_limit_up_today": excluded_limit_up,
        "excluded_zero_volume": excluded_zero_volume,
        "excluded_low_liquidity": excluded_low_liquidity,
        "missing_field_counts": missing_fields,
    }


def layer_partition(strata: Iterable[str]) -> dict[str, list[str]]:
    """把分层维度划分到 per_ticket / regime_layer 两层。"""

    per_ticket: list[str] = []
    regime_layer: list[str] = []
    for dimension in strata:
        name = str(dimension)
        if name in REGIME_LAYER_FIELDS:
            regime_layer.append(name)
        else:
            per_ticket.append(name)
    return {
        "per_ticket": per_ticket,
        "regime_layer": regime_layer,
        "regime_layer_fields": list(REGIME_LAYER_FIELDS),
        "semantics": (
            "per_ticket 用于 treated/control 定义与分层；regime_layer "
            "(regime/tradability_status/readiness) 仅单独分层报告，禁止参与门槛定义"
        ),
    }


# ---------------------------------------------------------------------------
# 排序字段敏感性
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FieldEvaluation:
    field: str
    selected: bool
    treated_selected: Mapping[date, list[ExperimentRow]]
    control_selected: Mapping[date, list[ExperimentRow]]
    treated_daily: Mapping[date, float]
    control_daily: Mapping[date, float]
    panel_dates: tuple[date, ...]
    mean_diff: float
    effect_pp: float
    ci95: tuple[float, float]
    std_error: float
    t_stat: float
    p_value: float
    date_count: int
    independent_date_count: int
    null_effects: list[float]
    observed_effect: float
    beats_null: bool
    permutation_p: float
    classification: str


def _evaluate_field(
    spec: SelectionExperimentSpec,
    *,
    ranking_field: str,
    treated_list: Sequence[ExperimentRow],
    control_list: Sequence[ExperimentRow],
    in_sample_rows: Iterable[ExperimentRow] | None,
    base_seed: int,
    horizon_days: int,
    primary_metric: str,
    random_control_seeds: int,
) -> _FieldEvaluation:
    """按单个排序字段重算 treated−control 效应、随机对照与四分类。"""

    treated_selected, treated_dates = _select_top_n(
        treated_list,
        top_n=spec.panel.top_n,
        oos_start=spec.panel.oos_start,
        oos_end=spec.panel.oos_end,
        ranking_field=ranking_field,
    )
    control_selected, control_dates = _select_top_n(
        control_list,
        top_n=spec.panel.top_n,
        oos_start=spec.panel.oos_start,
        oos_end=spec.panel.oos_end,
        ranking_field=ranking_field,
    )
    panel_dates = _enforce_same_panel(treated_dates, control_dates)
    treated_daily = _daily_series(treated_selected, primary_metric=primary_metric)
    control_daily = _daily_series(control_selected, primary_metric=primary_metric)

    if ranking_field == spec.ranking_fields[0]:
        field_seed = base_seed
    else:
        field_seed = _deterministic_seed(spec.experiment_id, "ranking", ranking_field)
    mean_diff, ci95, std_error, t_stat, date_count = _paired_effect(
        treated_daily,
        control_daily,
        block_length=horizon_days,
        bootstrap_iterations=spec.decision.bootstrap_iterations,
        seed=field_seed,
        alpha=spec.decision.alpha,
    )
    p_value = _two_sided_p_value(t_stat)
    seeds = tuple(field_seed + 1000 + index for index in range(random_control_seeds))
    null_effects, _null_std = _random_control_null(
        treated_selected, control_selected, primary_metric=primary_metric, seeds=seeds
    )
    observed_effect = mean_diff
    beats_null = bool(null_effects) and observed_effect > max(null_effects)
    permutation_p = (
        (1 + sum(1 for value in null_effects if value >= observed_effect)) / (len(null_effects) + 1)
        if null_effects
        else 1.0
    )
    in_sample_t = _in_sample_t(
        spec,
        in_sample_rows=in_sample_rows,
        primary_metric=primary_metric,
        ranking_field=ranking_field,
    )
    classification = _classify(
        t_stat=t_stat,
        threshold=spec.decision.strict_t_threshold,
        beats_null=beats_null,
        in_sample_t=in_sample_t,
    )
    return _FieldEvaluation(
        field=ranking_field,
        selected=True,
        treated_selected=treated_selected,
        control_selected=control_selected,
        treated_daily=treated_daily,
        control_daily=control_daily,
        panel_dates=panel_dates,
        mean_diff=mean_diff,
        effect_pp=mean_diff * 100.0,
        ci95=(float(ci95[0]), float(ci95[1])),
        std_error=std_error,
        t_stat=t_stat,
        p_value=p_value,
        date_count=date_count,
        independent_date_count=independent_date_count(panel_dates, horizon_days=horizon_days),
        null_effects=null_effects,
        observed_effect=observed_effect,
        beats_null=beats_null,
        permutation_p=permutation_p,
        classification=classification,
    )


def _ranking_sensitivity(
    ranking_fields: Sequence[str],
    evaluations: Mapping[str, _FieldEvaluation],
    *,
    threshold: float,
) -> dict[str, Any]:
    per_field: dict[str, Any] = {}
    signs: set[int] = set()
    for ranking_field in ranking_fields:
        result = evaluations[ranking_field]
        sign = 1 if result.effect_pp > 0 else -1 if result.effect_pp < 0 else 0
        signs.add(sign)
        per_field[ranking_field] = {
            "effect_pp": result.effect_pp,
            "ci95": list(result.ci95),
            "t_stat": result.t_stat,
            "p_value": result.p_value,
            "date_count": result.date_count,
            "classification": result.classification,
            "sign": sign,
        }
    nonzero_signs = {sign for sign in signs if sign != 0}
    stable = len(nonzero_signs) <= 1 and len(ranking_fields) > 0
    flipped = sorted(name for name in ranking_fields if per_field[name]["sign"] == -1)
    confirmed = sorted(name for name in ranking_fields if per_field[name]["classification"] == "confirmed_alive")
    detail = (
        "排序字段效应同号，方向稳定"
        if stable
        else (
            "低功效/排序不稳定：不同 ranking_fields 效应符号不一致"
            f"（负向字段={flipped or 'n/a'}；strict_t={threshold}）"
        )
    )
    return {
        "stable": stable,
        "per_field": per_field,
        "ranking_fields": list(ranking_fields),
        "effect_signs": sorted(signs),
        "flipped_fields": flipped,
        "confirmed_fields": confirmed,
        "detail": detail,
    }


def _cap_ranking_unstable(classification: str, *, t_stat: float, threshold: float) -> str:
    """排序不稳定时禁止 confirmed_alive/train_only，最高只能 noise/reversed_strict。"""

    if classification == "reversed_strict":
        return "reversed_strict"
    if t_stat <= -threshold:
        return "reversed_strict"
    return "noise"


# ---------------------------------------------------------------------------
# 主评估入口
# ---------------------------------------------------------------------------


def evaluate_experiment(
    spec: SelectionExperimentSpec,
    *,
    treated_rows: Iterable[ExperimentRow],
    control_rows: Iterable[ExperimentRow],
    dataset_hash: str | DatasetHashResult | None = None,
    attempts: int = 0,
    in_sample_rows: Iterable[ExperimentRow] | None = None,
    recompute_command: str | None = None,
    random_control_seeds: int = DEFAULT_RANDOM_CONTROL_SEEDS,
) -> SelectionExperimentReport:
    """在同一面板上评估 treated−control，并给出显著性、四分类与判定。

    输入为已加载的收益向量，不依赖 DB，因此可用于离线复算与单元测试。
    """

    if random_control_seeds < 2:
        raise ValueError("random_control_seeds must be at least 2")

    horizon_days = max(spec.panel.horizons)
    primary_metric = spec.decision.primary_metric

    treated_list = list(treated_rows)
    control_list = list(control_rows)
    if not treated_list or not control_list:
        raise ValueError("both treated and control rows are required")
    _validate_costs(treated_list, cost_bps=spec.panel.cost_bps)
    _validate_costs(control_list, cost_bps=spec.panel.cost_bps)

    base_seed = _deterministic_seed(spec.experiment_id, "overall")
    evaluations: dict[str, _FieldEvaluation] = {}
    for ranking_field in spec.ranking_fields:
        evaluations[ranking_field] = _evaluate_field(
            spec,
            ranking_field=ranking_field,
            treated_list=treated_list,
            control_list=control_list,
            in_sample_rows=in_sample_rows,
            base_seed=base_seed,
            horizon_days=horizon_days,
            primary_metric=primary_metric,
            random_control_seeds=random_control_seeds,
        )
    primary_field = spec.ranking_fields[0]
    primary = evaluations[primary_field]
    if not primary.selected:
        raise ValueError("no rows fall inside the OOS panel window")

    treated_selected = primary.treated_selected
    control_selected = primary.control_selected
    treated_daily = primary.treated_daily
    control_daily = primary.control_daily
    panel_dates = primary.panel_dates
    mean_diff = primary.mean_diff
    ci95 = primary.ci95
    std_error = primary.std_error
    t_stat = primary.t_stat
    p_value = primary.p_value
    date_count = primary.date_count
    effect_pp = primary.effect_pp
    indep_dates = primary.independent_date_count
    beats_null = primary.beats_null

    strata_buckets = _strata_buckets(
        spec,
        treated_selected=treated_selected,
        control_selected=control_selected,
        base_seed=base_seed,
        horizon_days=horizon_days,
    )

    # BH-FDR 家族 = 总体效应 + 各分层桶（多桶校正）。
    family_p = [p_value] + [bucket.p_value for bucket in strata_buckets if bucket.p_value is not None]
    family_q = benjamini_hochberg(family_p)
    overall_q = family_q[0]
    cursor = 1
    adjusted_buckets: list[StrataBucketMetrics] = []
    for bucket in strata_buckets:
        if bucket.p_value is None:
            adjusted_buckets.append(bucket)
            continue
        adjusted_buckets.append(_replace_bucket_q(bucket, family_q[cursor]))
        cursor += 1
    strata_buckets = tuple(adjusted_buckets)

    overall = EffectStatistics(
        date_count=date_count,
        independent_date_count=indep_dates,
        mean_diff=mean_diff,
        effect_pp=effect_pp,
        ci95=ci95,
        std_error=std_error,
        t_stat=t_stat,
        p_value=p_value,
        q_value=overall_q,
        block_length=max(1, horizon_days),
        bootstrap_iterations=spec.decision.bootstrap_iterations,
    )

    seeds = tuple(base_seed + 1000 + index for index in range(random_control_seeds))
    random_control = RandomControlResult(
        seeds=seeds,
        shuffle_count=len(primary.null_effects),
        null_mean=statistics.fmean(primary.null_effects) if primary.null_effects else 0.0,
        null_std=statistics.stdev(primary.null_effects) if len(primary.null_effects) >= 2 else 0.0,
        null_min=min(primary.null_effects) if primary.null_effects else 0.0,
        null_max=max(primary.null_effects) if primary.null_effects else 0.0,
        observed_effect=primary.observed_effect,
        p_value=primary.permutation_p,
        beats_null=beats_null,
    )

    classification = primary.classification
    ranking_sensitivity = _ranking_sensitivity(
        spec.ranking_fields,
        evaluations,
        threshold=spec.decision.strict_t_threshold,
    )
    if not ranking_sensitivity["stable"]:
        classification = _cap_ranking_unstable(
            classification, t_stat=t_stat, threshold=spec.decision.strict_t_threshold
        )

    effective_min_effect_pp, tightening_factor = tightened_min_effect_pp(
        spec.decision.min_effect_pp, attempts
    )
    checks = _decision_checks(
        spec,
        classification=classification,
        ci95=ci95,
        effect_pp=effect_pp,
        effective_min_effect_pp=effective_min_effect_pp,
        overall_q=overall_q,
        indep_dates=indep_dates,
        ranking_stable=ranking_sensitivity["stable"],
        ranking_detail=ranking_sensitivity["detail"],
    )
    decision = "PASS" if all(check.passed for check in checks) else "REJECT"


    if isinstance(dataset_hash, DatasetHashResult):
        dataset_hash_value = dataset_hash.dataset_hash
        dataset_hash_components = dict(dataset_hash.components)
    else:
        dataset_hash_value = str(dataset_hash or "unfrozen")
        dataset_hash_components = {}

    treated_summary = _arm_summary("treated", treated_selected, treated_daily)
    control_summary = _arm_summary("control", control_selected, control_daily)

    panel_payload = {
        "oos_start": spec.panel.oos_start.isoformat(),
        "oos_end": spec.panel.oos_end.isoformat(),
        "universe_version": spec.panel.universe_version,
        "label_version": spec.panel.label_version,
        "cost_bps": spec.panel.cost_bps,
        "horizons": list(spec.panel.horizons),
        "top_n": spec.panel.top_n,
        "evaluated_dates": len(panel_dates),
        "horizon_days_used": horizon_days,
    }
    change_payload = {"kind": spec.change.kind, "patch": dict(spec.change.patch)}
    layers_payload = layer_partition(spec.strata)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": spec.experiment_id,
        "market": spec.market,
        "hypothesis": spec.hypothesis,
        "change": change_payload,
        "panel": panel_payload,
        "dataset_hash": dataset_hash_value,
        "dataset_hash_components": dataset_hash_components,
        "attempts": int(attempts),
        "tightening_factor": tightening_factor,
        "effective_min_effect_pp": effective_min_effect_pp,
        "treated": _asdict(treated_summary),
        "control": _asdict(control_summary),
        "strata": [_asdict(bucket) for bucket in strata_buckets],
        "overall": _asdict(overall),
        "random_control": _asdict(random_control),
        "classification": classification,
        "decision": decision,
        "checks": [_asdict(check) for check in checks],
        "ranking_fields": list(spec.ranking_fields),
        "ranked_by": primary_field,
        "ranking_sensitivity": ranking_sensitivity,
        "layers": layers_payload,
        "recompute_command": recompute_command or "",
    }
    digest = hashlib.sha256(
        json.dumps(_to_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    return SelectionExperimentReport(
        schema_version=SCHEMA_VERSION,
        experiment_id=spec.experiment_id,
        market=spec.market,
        hypothesis=spec.hypothesis,
        change=change_payload,
        panel=panel_payload,
        dataset_hash=dataset_hash_value,
        dataset_hash_components=dataset_hash_components,
        attempts=int(attempts),
        tightening_factor=tightening_factor,
        effective_min_effect_pp=effective_min_effect_pp,
        treated=treated_summary,
        control=control_summary,
        strata=strata_buckets,
        overall=overall,
        random_control=random_control,
        classification=classification,
        decision=decision,
        checks=tuple(checks),
        recompute_command=recompute_command or "",
        report_digest=digest,
        ranking_fields=tuple(spec.ranking_fields),
        ranked_by=primary_field,
        ranking_sensitivity=ranking_sensitivity,
        layers=layers_payload,
    )


def _replace_bucket_q(bucket: StrataBucketMetrics, q_value: float) -> StrataBucketMetrics:
    return StrataBucketMetrics(
        dimension=bucket.dimension,
        bucket=bucket.bucket,
        observation_count=bucket.observation_count,
        date_count=bucket.date_count,
        hit_rate=bucket.hit_rate,
        net_avg=bucket.net_avg,
        positive_date_rate=bucket.positive_date_rate,
        control_net_avg=bucket.control_net_avg,
        effect_pp=bucket.effect_pp,
        effect_ci95=bucket.effect_ci95,
        t_stat=bucket.t_stat,
        p_value=bucket.p_value,
        q_value=q_value,
    )


def _strata_buckets(
    spec: SelectionExperimentSpec,
    *,
    treated_selected: Mapping[date, Sequence[ExperimentRow]],
    control_selected: Mapping[date, Sequence[ExperimentRow]],
    base_seed: int,
    horizon_days: int,
) -> tuple[StrataBucketMetrics, ...]:
    buckets: list[StrataBucketMetrics] = []
    for dimension_index, dimension in enumerate(spec.strata):
        bucket_names: set[str] = set()
        for rows in treated_selected.values():
            for row in rows:
                value = (row.strata or {}).get(dimension)
                if value:
                    bucket_names.add(str(value))
        for bucket in sorted(bucket_names):
            predicate = _bucket_predicate(dimension, bucket)
            treated_metric = _daily_series(
                treated_selected, primary_metric=spec.decision.primary_metric, predicate=predicate
            )
            control_metric = _daily_series(
                control_selected, primary_metric=spec.decision.primary_metric, predicate=predicate
            )
            treated_net = _daily_net_series(treated_selected, predicate=predicate)
            control_net = _daily_net_series(control_selected, predicate=predicate)
            observations = [
                row for rows in treated_selected.values() for row in rows if predicate(row)
            ]
            net_values = [float(row.net_return) for row in observations]
            bucket_effect_pp: float | None = None
            bucket_ci: tuple[float, float] | None = None
            bucket_t: float | None = None
            bucket_p: float | None = None
            common_dates = sorted(set(treated_metric) & set(control_metric))
            if len(common_dates) >= 2:
                mean_diff, ci, _se, t_stat, _n = _paired_effect(
                    treated_metric,
                    control_metric,
                    block_length=horizon_days,
                    bootstrap_iterations=spec.decision.bootstrap_iterations,
                    seed=base_seed + 100 + dimension_index * 1000 + len(buckets),
                    alpha=spec.decision.alpha,
                )
                bucket_effect_pp = mean_diff * 100.0
                bucket_ci = ci
                bucket_t = t_stat
                bucket_p = _two_sided_p_value(t_stat)
            buckets.append(
                StrataBucketMetrics(
                    dimension=dimension,
                    bucket=bucket,
                    observation_count=len(observations),
                    date_count=len(treated_net),
                    hit_rate=(
                        sum(value > 0 for value in net_values) / len(net_values) if net_values else 0.0
                    ),
                    net_avg=statistics.fmean(net_values) if net_values else 0.0,
                    positive_date_rate=(
                        sum(value > 0 for value in treated_net.values()) / len(treated_net)
                        if treated_net
                        else 0.0
                    ),
                    control_net_avg=(
                        statistics.fmean(control_net.values()) if control_net else None
                    ),
                    effect_pp=bucket_effect_pp,
                    effect_ci95=bucket_ci,
                    t_stat=bucket_t,
                    p_value=bucket_p,
                    q_value=None,
                )
            )
    return tuple(buckets)


def _bucket_predicate(dimension: str, bucket: str) -> Any:
    def predicate(row: ExperimentRow) -> bool:
        return str((row.strata or {}).get(dimension)) == bucket

    return predicate


def _in_sample_t(
    spec: SelectionExperimentSpec,
    *,
    in_sample_rows: Iterable[ExperimentRow] | None,
    primary_metric: str,
    ranking_field: str = "raw_score",
) -> float | None:
    """OOS 之前（train 区）的同面板配对 t，用于区分 ``train_only`` 与 ``noise``。

    ``in_sample_rows`` 通过 :func:`build_in_sample_rows` 生成，行上带有
    ``strata["__arm__"]`` 臂标记。没有 train 区数据时返回 ``None``。
    """

    if in_sample_rows is None:
        return None
    cutoff = spec.panel.oos_start
    by_date: dict[date, dict[str, list[ExperimentRow]]] = {}
    for row in in_sample_rows:
        if row.feature_date >= cutoff:
            continue
        arm = str((row.strata or {}).get("__arm__") or "")
        if arm not in {"treated", "control"}:
            continue
        by_date.setdefault(row.feature_date, {"treated": [], "control": []})[arm].append(row)
    daily: dict[str, dict[date, float]] = {"treated": {}, "control": {}}
    for feature_date in sorted(by_date):
        for arm in ("treated", "control"):
            eligible = by_date[feature_date][arm]
            if len(eligible) < spec.panel.top_n:
                continue
            selected = sorted(
                eligible,
                key=lambda item: (-_ranking_value(item, ranking_field), item.ticker),
            )[: spec.panel.top_n]
            daily[arm][feature_date] = statistics.fmean(
                _metric_value(row, primary_metric) for row in selected
            )
    common = sorted(set(daily["treated"]) & set(daily["control"]))
    if len(common) < 2:
        return None
    diffs = [daily["treated"][day] - daily["control"][day] for day in common]
    std = statistics.stdev(diffs)
    std_error = std / math.sqrt(len(diffs)) if std > 0 else 0.0
    if std_error <= 0:
        return 0.0
    return statistics.fmean(diffs) / std_error


def build_in_sample_rows(
    *,
    treated_rows: Iterable[ExperimentRow],
    control_rows: Iterable[ExperimentRow],
    oos_start: date,
) -> list[ExperimentRow]:
    """把 OOS 之前的行打上 ``strata["__arm__"]`` 臂标记，供 ``train_only`` 判定。"""

    output: list[ExperimentRow] = []
    for arm, rows in (("treated", treated_rows), ("control", control_rows)):
        for row in rows:
            if row.feature_date < oos_start:
                strata = dict(row.strata or {})
                strata["__arm__"] = arm
                output.append(
                    ExperimentRow(
                        feature_date=row.feature_date,
                        ticker=row.ticker,
                        raw_score=row.raw_score,
                        net_return=row.net_return,
                        gross_return=row.gross_return,
                        market_excess_return=row.market_excess_return,
                        label_value=row.label_value,
                        strata=strata,
                        ranking_values=dict(row.ranking_values or {}),
                        tradability_values=dict(row.tradability_values or {}),
                    )
                )
    return output


def _classify(
    *,
    t_stat: float,
    threshold: float,
    beats_null: bool,
    in_sample_t: float | None,
) -> str:
    """四分类：strict t 门槛 + 随机对照 null 分布。

    ``confirmed_alive``：OOS 显著（t ≥ 门槛）且严格超过全部 5 个 shuffle null
    样本；``reversed_strict``：t ≤ −门槛（严格反向）；``train_only``：train 区
    显著但 OOS 未确认；其余为 ``noise``。5 seeds 下排列 p 的取值下限为 1/6，
    因此判定用"超过 null 最大值"而不是 p≤alpha（p 仅作报告）。
    """

    if t_stat <= -threshold:
        return "reversed_strict"
    if t_stat >= threshold and beats_null:
        return "confirmed_alive"
    if in_sample_t is not None and in_sample_t >= threshold and t_stat < threshold:
        return "train_only"
    return "noise"


def _decision_checks(
    spec: SelectionExperimentSpec,
    *,
    classification: str,
    ci95: tuple[float, float],
    effect_pp: float,
    effective_min_effect_pp: float,
    overall_q: float,
    indep_dates: int,
    ranking_stable: bool = True,
    ranking_detail: str = "",
) -> tuple[DecisionCheck, ...]:
    strict = spec.decision.strict_t_threshold
    return (
        DecisionCheck(
            key="random_control_confirmed",
            passed=classification == "confirmed_alive",
            observed=classification,
            threshold="classification == confirmed_alive",
            detail="随机对照 + 严格 t 门槛未确认存活",
        ),
        DecisionCheck(
            key="ci95_lower_bound_positive",
            passed=ci95[0] > 0,
            observed=ci95[0],
            threshold="ci95_low > 0",
            detail="日期聚类 block bootstrap 的 95% 下界须为正",
        ),
        DecisionCheck(
            key="min_effect_pp",
            passed=effect_pp >= effective_min_effect_pp,
            observed=effect_pp,
            threshold=f"effect_pp >= {effective_min_effect_pp:.4f} (attempts收紧后)",
            detail="效应须达到经 attempts 收紧后的最小百分点门槛",
        ),
        DecisionCheck(
            key="fdr_significant",
            passed=overall_q <= spec.decision.fdr,
            observed=overall_q,
            threshold=f"q_value <= {spec.decision.fdr} (BH-FDR, strict_t={strict})",
            detail="总体效应经 BH-FDR 校正后仍显著",
        ),
        DecisionCheck(
            key="independent_dates",
            passed=indep_dates >= spec.decision.min_independent_dates,
            observed=indep_dates,
            threshold=f"independent_dates >= {spec.decision.min_independent_dates}",
            detail="去重叠后的独立日期数须达标（horizon 去重叠）",
        ),
        DecisionCheck(
            key="ranking_stability",
            passed=ranking_stable,
            observed=ranking_stable,
            threshold="all ranking_fields share the same effect sign",
            detail=ranking_detail or "排序字段敏感性检查：低功效/排序不稳定时禁止 confirmed_alive",
        ),
    )


# ---------------------------------------------------------------------------
# 与既有 promotion_gate 的衔接
# ---------------------------------------------------------------------------


def experiment_promotion_evidence(report: SelectionExperimentReport) -> dict[str, Any]:
    """把实验报告映射成 ``promotion_gate_v2`` 统计子门可消费的 evidence。

    ``_statistical_gate_check`` 读取 ``decision`` 与 ``checks[*].status``；本函数
    只做格式转换，不修改也不绕过 promotion_gate 的任何阈值：
    - ``decision == PASS``  -> ``ELIGIBLE_FOR_MANUAL_REVIEW``（仍需人工复核）
    - 其余                   -> ``REJECT``
    """

    decision = PROMOTION_DECISION_ELIGIBLE if report.decision == "PASS" else PROMOTION_DECISION_REJECT
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "selection_experiment_framework",
        "decision": decision,
        "experiment_id": report.experiment_id,
        "market": report.market,
        "dataset_hash": report.dataset_hash,
        "report_digest": report.report_digest,
        "classification": report.classification,
        "checks": [
            {
                "key": check.key,
                "status": "PASS" if check.passed else "FAIL",
                "observed": check.observed,
                "threshold": check.threshold,
                "detail": check.detail,
            }
            for check in report.checks
        ],
    }


# ---------------------------------------------------------------------------
# 报告落盘（JSON + Markdown）
# ---------------------------------------------------------------------------


def report_markdown(report: SelectionExperimentReport, *, data_source: str | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# 选股实验报告：{report.experiment_id}")
    lines.append("")
    lines.append(f"- 市场：`{report.market}`")
    lines.append(f"- 假设：{report.hypothesis}")
    lines.append(f"- 改动：`{report.change.get('kind')}` patch=`{json.dumps(report.change.get('patch'), ensure_ascii=False, sort_keys=True)}`")
    lines.append(f"- dataset_hash：`{report.dataset_hash}`")
    lines.append(f"- attempts：{report.attempts}（收紧因子 {report.tightening_factor:.4f}，有效阈值 {report.effective_min_effect_pp:.4f} pp）")
    if data_source:
        lines.append(f"- 数据来源：`{data_source}`")
    lines.append(f"- 判定：**{report.decision}**，四分类：`{report.classification}`")
    lines.append(f"- 排序字段（primary）：`{report.ranked_by}`；候选：{list(report.ranking_fields)}")
    lines.append(f"- 复算命令：`{report.recompute_command}`")
    lines.append(f"- 报告摘要：`{report.report_digest}`")
    lines.append("")
    panel = report.panel
    lines.append("## 面板")
    lines.append("")
    lines.append(
        f"- OOS：{panel.get('oos_start')} ~ {panel.get('oos_end')}（{panel.get('evaluated_dates')} 个交易日）"
    )
    lines.append(
        f"- universe_version=`{panel.get('universe_version')}`，label_version=`{panel.get('label_version')}`，"
        f"top_n={panel.get('top_n')}，cost_bps={panel.get('cost_bps')}，horizons={panel.get('horizons')}"
    )
    lines.append("")
    lines.append("## 两臂概览")
    lines.append("")
    lines.append("| arm | dates | obs | mean_metric | mean_net_return | positive_date_rate |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for arm in (report.treated, report.control):
        lines.append(
            f"| {arm.arm} | {arm.evaluated_date_count} | {arm.observation_count} | "
            f"{arm.mean_metric:.6f} | {arm.mean_net_return:.6f} | {arm.positive_date_rate:.4f} |"
        )
    lines.append("")
    lines.append("## 显著性（treated − control，配对）")
    lines.append("")
    overall = report.overall
    lines.append(f"- effect_pp：{overall.effect_pp:.4f}")
    lines.append(f"- CI95：({overall.ci95[0]:.6f}, {overall.ci95[1]:.6f})，block_length={overall.block_length}")
    lines.append(f"- t={overall.t_stat:.4f}，p={overall.p_value:.6g}，q(BH-FDR)={overall.q_value:.6g}")
    lines.append(f"- 独立日期数：{overall.independent_date_count} / {overall.date_count}")
    rc = report.random_control
    lines.append(
        f"- 随机对照：seeds={list(rc.seeds)}，null∈[{rc.null_min:.6f}, {rc.null_max:.6f}]，"
        f"observed={rc.observed_effect:.6f}，beats_null={rc.beats_null}，p_perm={rc.p_value:.4f}"
    )
    lines.append("")
    sensitivity = dict(report.ranking_sensitivity or {})
    per_field = dict(sensitivity.get("per_field") or {})
    if per_field:
        lines.append("## 排序字段敏感性")
        lines.append("")
        lines.append(f"- stable：**{sensitivity.get('stable')}**（{sensitivity.get('detail', '')}）")
        lines.append("")
        lines.append("| ranking_field | effect_pp | t | classification | sign |")
        lines.append("| --- | --- | --- | --- | --- |")
        for field in report.ranking_fields:
            entry = dict(per_field.get(field) or {})
            effect = entry.get("effect_pp")
            t_value = entry.get("t_stat")
            lines.append(
                f"| {field} | {'' if effect is None else f'{effect:.4f}'} | "
                f"{'' if t_value is None else f'{t_value:.4f}'} | "
                f"{entry.get('classification', '')} | {entry.get('sign', '')} |"
            )
        lines.append("")
    layers = dict(report.layers or {})
    if layers:
        lines.append("## 门槛层语义")
        lines.append("")
        lines.append(f"- per_ticket 分层：{layers.get('per_ticket')}")
        lines.append(f"- regime_layer 分层（仅单独报告）：{layers.get('regime_layer')}")
        lines.append(f"- {layers.get('semantics', '')}")
        lines.append("")
    if report.strata:
        lines.append("## 分层表")
        lines.append("")
        lines.append(
            "| dimension | bucket | n | dates | hit_rate | net_avg | positive_date_rate | "
            "control_net_avg | effect_pp | t | p | q |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for bucket in report.strata:
            effect = "" if bucket.effect_pp is None else f"{bucket.effect_pp:.4f}"
            t_value = "" if bucket.t_stat is None else f"{bucket.t_stat:.4f}"
            p_value = "" if bucket.p_value is None else f"{bucket.p_value:.4g}"
            q_value = "" if bucket.q_value is None else f"{bucket.q_value:.4g}"
            control_net = "" if bucket.control_net_avg is None else f"{bucket.control_net_avg:.6f}"
            lines.append(
                f"| {bucket.dimension} | {bucket.bucket} | {bucket.observation_count} | "
                f"{bucket.date_count} | {bucket.hit_rate:.4f} | {bucket.net_avg:.6f} | "
                f"{bucket.positive_date_rate:.4f} | {control_net} | {effect} | {t_value} | "
                f"{p_value} | {q_value} |"
            )
        lines.append("")
    lines.append("## 判定检查")
    lines.append("")
    lines.append("| key | passed | observed | threshold |")
    lines.append("| --- | --- | --- | --- |")
    for check in report.checks:
        lines.append(f"| {check.key} | {check.passed} | {check.observed} | {check.threshold} |")
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ExperimentReportFiles:
    json_path: Path
    markdown_path: Path
    report_digest: str


def persist_experiment_report(
    report: SelectionExperimentReport,
    *,
    output_dir: Path,
    data_source: str | None = None,
) -> ExperimentReportFiles:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = report.as_dict()
    json_path = output_dir / f"{report.experiment_id}-experiment.json"
    markdown_path = output_dir / f"{report.experiment_id}-experiment.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        report_markdown(report, data_source=data_source).rstrip() + "\n",
        encoding="utf-8",
    )
    return ExperimentReportFiles(
        json_path=json_path,
        markdown_path=markdown_path,
        report_digest=report.report_digest,
    )


# ---------------------------------------------------------------------------
# 极简 YAML 子集解析（无 PyYAML 时的回退）
# ---------------------------------------------------------------------------


def _load_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return _parse_simple_yaml(text)
    return yaml.safe_load(text)


def _parse_simple_yaml(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        content = _strip_inline_comment(raw)
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        lines.append((indent, content.strip()))
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ValueError("unsupported YAML structure (trailing lines)")
    return value


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError("unsupported YAML indentation jump")
        if content.startswith("- "):
            break
        if ":" not in content:
            raise ValueError(f"unsupported YAML mapping line: {content!r}")
        key, _, remainder = content.partition(":")
        key = key.strip()
        remainder = remainder.strip()
        index += 1
        if remainder:
            result[key] = _parse_scalar(remainder)
            continue
        if index < len(lines) and lines[index][0] > indent:
            nested_indent = lines[index][0]
            nested, index = _parse_block(lines, index, nested_indent)
            result[key] = nested
        else:
            result[key] = None
    return result, index


def _parse_sequence(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent != indent or not content.startswith("- "):
            break
        item = content[2:].strip()
        index += 1
        if item:
            result.append(_parse_scalar(item))
        elif index < len(lines) and lines[index][0] > indent:
            nested, index = _parse_block(lines, index, lines[index][0])
            result.append(nested)
        else:
            result.append(None)
    return result, index


def _parse_scalar(token: str) -> Any:
    text = token.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline(inner)]
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _split_inline(inner: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            buffer.append(char)
            continue
        if char == ",":
            parts.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(char)
    if buffer:
        parts.append("".join(buffer).strip())
    return [part for part in parts if part]


def _strip_inline_comment(raw: str) -> str:
    quote: str | None = None
    for position, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "#":
            return raw[:position]
    return raw


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"{key} is required and must be a non-empty string")
    return str(value)


def _require_number(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric") from exc


def _require_date(raw: Mapping[str, Any], key: str) -> date:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"{key} must be an ISO date (YYYY-MM-DD)") from exc


def _require_sequence(raw: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{key} must be a list")
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value
