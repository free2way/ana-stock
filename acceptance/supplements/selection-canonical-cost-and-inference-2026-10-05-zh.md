# 统一成本口径、按日聚类 CI 与主评估基准/分层（2026-10-05）

本文件记录一次性口径统一的实现位置与默认值来源，供复核。

## 1. Canonical 往返成本

- 设置项：`PQW_SELECTION_CANONICAL_ROUND_TRIP_COST_BPS`（`app/core/config.py`，字段
  `selection_canonical_round_trip_cost_bps`），默认 **50 bps**，范围 [0, 200]。
- 解析入口：`app/services/cost_basis.py::canonical_round_trip_cost_bps(market)`；摘要统一附带
  `cost_bps` / `cost_basis="round_trip"` / `cost_source`（`CANONICAL_COST_SOURCE`）。
- 50bps 来源（仓库既有主流口径）：
  - `Settings.trainer_round_trip_cost_bps` 默认 50，注释为 P0 #3「commission ~2.5bps per leg +
    stamp/transfer + next-open 滑点」。
  - `model_evaluation.DEFAULT_COST_SENSITIVITY_LADDER_BPS = (20, 50, 80)` 的 scheduled nominal 为 50。
  - `docs/regime-aware-hit-rate-p0-cost-bridge-execution-2026-09-18-zh.md` 的旧固定费率口径在 40–50bps 区间。
- 单值按市场唯一：同一 canonical 值应用于每个市场；`market` 参数仅为未来按市场覆盖预留，当前
  不改变取值。

## 2. 0 成本路径补扣费

- `app/services/selection_quality.py`
  - AI 日报路径：`return_1d_pct` 仍为 gross；新增 `net_return_1d_pct = close_1d - cost_pct`，
    `hit_1d` 改为按净收益取符号，`execution_hit` 要求 `open_to_high - cost_pct >= 2.0` 且
    `open_to_low > -4.0`。
  - 因子实验路径：优先读取 outcome 的 `net_return_1d_pct`，缺失时按 canonical 成本回算。
  - `_aggregate` 新增 `cost_bps`/`cost_basis`/`cost_source`/`avg_net_return_1d_pct` 及聚类 CI 字段。
- `app/services/factor_experiments.py`
  - `compute_forward_outcome` 新增 `cost_bps`/`cost_basis`/`cost_source` 与
    `net_return_{1,3,5}d_pct`（gross `return_*` 字段保持不变）。
  - `summarize_factor_outcomes` 的 `hit_rate_{h}d_pct` 改按净收益计算，新增 `cost_*` 与
    `avg_net_return_{h}d_pct`。
- 既有字段不删除；命中标志的取值口径改为净收益。

## 3. hit_rate 按日聚类 CI 与下界门槛

- `app/services/statistical_inference.py::day_clustered_hit_rate_ci`：按日聚类的 moving-block
  bootstrap（block 长度 = 评估 horizon），同日多票作为同一 cluster。
- `app/services/model_evaluation.py::_summarize_return_vectors`：
  - 保留 `confidence_low/high`（iid 正态，标注 `confidence_method=iid_normal_diagnostic_not_promotion_evidence`，
    列语义不变）。
  - 新增 `hit_rate_ci95_iid`、`hit_rate_ci95_clustered`、`hit_rate_ci_lower_bound_clustered`、
    `hit_rate_ci_cluster_method` 等。
- `app/services/stock_selection/selective_policy.py`：`active_mean_ci95` 改为按日顺序 moving-block
  bootstrap（block=horizon）的区间；iid 区间保留为 `active_mean_ci95_iid` 诊断字段。
- 门槛消费方：
  - `app/services/stock_selection/promotion_gate.py` 的 `active_mean_ci95_lower_bound` 使用聚类下界
    （`active_mean_ci95[0]`），iid 不得用于过闸。
  - `app/services/selection_quality.py::_guidance_from_summary`：来源择优要求按日聚类命中率 CI
    下界 > `SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT`（50，即高于抛硬币），否则不入选
    `preferred_sources`（记录于 `rejected_sources`）。

## 4. 主评估基准与分层

- `app/services/model_evaluation.py`
  - 基准：`_benchmark_universe_tickers` 取该 run 在同一批交易日的预测宇宙（按最优 rank 取值，
    上限 `BENCHMARK_UNIVERSE_MAX_TICKERS=300`）；`_equal_weight_benchmark_by_date` 复刻
    `backtesting/runner.py::_benchmark_returns` 的等权日收益口径；`_benchmark_section` 输出
    `benchmark_status` / `benchmark_avg_return_pct` / `excess_avg_return_pct` /
    `excess_positive_rate_pct`，并可叠加指数（`benchmark_symbol`，run config 或
    `backtest_benchmark_symbol`，best-effort）。
  - `benchmark_status` 由 `not_available_in_v1` 改为真实状态：
    `available_same_day_universe_equal_weight_v1` 或
    `available_equal_weight_plus_index_overlay_v1` 或 `not_available_no_benchmark_universe`。
  - 分层：在 market/risk regime、buy_gate 之外新增 `industry:<值>`、
    `market_cap_bucket:<small|mid|large|unknown>`、`liquidity_bucket:<low|mid|high|unknown>`；
    市值来自最新 `fundamental_snapshots`，流动性为近 20 日 `close*volume` ADV，桶阈值为评估内
    三分位（样本不足时为 `unknown`）。摘要 `stratification` 记录口径与形状。
