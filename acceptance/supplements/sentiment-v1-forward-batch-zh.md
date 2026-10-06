# sentiment_v1 前向验证批次运行手册

更新时间：2026-10-06
关联代码：

- 批次规格模块：[app/services/stock_selection/sentiment_forward_batch.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/stock_selection/sentiment_forward_batch.py)
- 冻结脚本：[scripts/init_sentiment_forward_batch.py](/Volumes/STORAGE_Jackyhu/code/ana/scripts/init_sentiment_forward_batch.py)
- 运行脚本：[scripts/run_sentiment_forward_batch.py](/Volumes/STORAGE_Jackyhu/code/ana/scripts/run_sentiment_forward_batch.py)
- 冻结规格文件：[data/experiments/sentiment_v1_forward_batch.json](/Volumes/STORAGE_Jackyhu/code/ana/data/experiments/sentiment_v1_forward_batch.json)
- 复用：情绪特征构建 [app/services/stock_selection/sentiment_features.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/stock_selection/sentiment_features.py)、特征拉取 [scripts/sync_hithink_sentiment_features.py](/Volumes/STORAGE_Jackyhu/code/ana/scripts/sync_hithink_sentiment_features.py)、成熟门槛/不完整批次 block [app/services/stock_selection/forward_shadow_evaluation.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/stock_selection/forward_shadow_evaluation.py)、实验框架 [app/services/stock_selection/experiment_framework.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/stock_selection/experiment_framework.py)

## 1. 背景与为什么只能前向验证

`sentiment_v1`（hithink featured/auction 情绪族）是 **forward_only**：可追溯存储最多约一年，
无法像 CN 价格湖那样回溯长历史。因此任何"回测"都是自欺，只能：

1. 冻结一个不可变的前向批次（预注册假设、宇宙、成本、cutoff、验收判据）；
2. 每个交易日前向采集情绪特征并冻结当时的决策；
3. 等足够多的决策日成熟（label horizon 走完）后，才允许下结论。

## 2. 冻结批次规格

规格文件：[data/experiments/sentiment_v1_forward_batch.json](/Volumes/STORAGE_Jackyhu/code/ana/data/experiments/sentiment_v1_forward_batch.json)（由 `init_sentiment_forward_batch.py` 生成，已落盘）。

| 字段 | 当前取值 | 语义 |
| --- | --- | --- |
| `schema_version` | `sentiment_v1_forward_batch_v1` | 批次规格模式 |
| `scope` | `sentiment_v1_forward_only` | 前向专用，禁止回填 |
| `batch_id` | `sentiment_v1_forward_2026-10-08` | 以首个可得交易日命名 |
| `factor_set` | `sentiment_v1` | 冻结的情绪因子集 |
| `factor_set_version` | `stock_selection_factor_set_v1:sentiment_v1:33f230ab30cde464` | 因子定义版本（方向/权重变更会改此值） |
| `missing_policy` | `exclude` | 截面缺失因子口径；与生产/实验共用 `factor_pipeline_for_factor_set` 工厂（`sentiment_v1` → `MissingFactorPolicy.EXCLUDE`），进哈希 |
| `universe_version` | `pit_universe_v1:CN:25815a348bebba7f` | 冻结时最新 PIT 宇宙（取自最新 universe 工件 manifest） |
| `label_version` | `label_net_return_v1` | 标签版本 |
| `cost_bps` | `50.0` | 单次往返成本（扣费净命中率口径） |
| `horizon_days` | `5` | 持有 horizon（下一个可得交易日起算） |
| `top_n` | `20` | 每次冻结决策的情绪 Top-N |
| `min_present_features` | `1` | 进入排名所需的最少非空情绪特征数 |
| `start_date` | `2026-10-08` | 今日（假期顺延）后的首个可得交易日 |
| `cutoff` | `eod` / `16:00` / `Asia/Shanghai` | **主路径**：同一交易日盘后 featured 数据，`semantics=same_trade_date_post_close_featured_data` |
| `cutoff.auction_path_included` | `false` | 竞价路径（`09:25`，`same_trade_date_call_auction_close`）**首批不纳入** |
| `coverage` | `trailing_one_year` / `start=2026-10-08` / `end=null` | 覆盖窗口；`end=null` 表示开放。窗口起点/终点进入哈希 |
| `dataset_hash` | `sentiment_forward_batch_v1:CN:f31ebe289588e2243433` | 覆盖窗口 + cutoff 语义 + 缺失因子口径 + factor/universe/label/成本/horizon/top_n 的规范哈希 |
| `created_at` | `2026-10-06T00:03:23+08:00` | 冻结时间 |
| `operator` | `unknown` | 操作者；读 `PQW_OPTIN_OPERATOR`，未设置则 `unknown` |
| `acceptance.min_matured_dates` | `60` | 成熟日数门槛（对齐现有 60 个未触碰决策日） |
| `acceptance.primary_metric` | `post_cost_net_hit_rate` | 主指标=扣费净命中率：成熟 Top-N 信号中净收益 > 0 的比例 |
| `acceptance.min_net_hit_rate` | `0.55` | 主指标的预注册下限（研究性假设，非已验证结论） |
| `acceptance.alpha` / `fdr` | `0.05` / `0.05` | 显著性口径：聚类 block bootstrap CI95 + BH-FDR |
| `acceptance.min_independent_dates` | `20` | 去重叠后的最小独立成熟日数 |
| `acceptance.strict_t_threshold` | `3.5` | 严格 t 门槛（shuffle×4 分类） |
| `acceptance.bootstrap_iterations` | `1000` | block bootstrap 次数 |
| `acceptance.random_control_seeds` | `5` | 行内 shuffle 随机对照种子数 |

### 2.1 创建/冻结命令

```bash
# 用环境变量记录操作者；默认读最新 PIT 宇宙、今日后的首个交易日
PQW_OPTIN_OPERATOR=your_name python scripts/init_sentiment_forward_batch.py

# 只预览不落盘
python scripts/init_sentiment_forward_batch.py --dry-run --print-spec

# 显式指定宇宙 / 起始日 / 覆盖截止
python scripts/init_sentiment_forward_batch.py \
  --universe-version pit_universe_v1:CN:25815a348bebba7f \
  --start-date 2026-10-08 --coverage-end 2027-10-08
```

**幂等与不可变**：相同 `dataset_hash` 重复运行返回 `reused_existing`；契约漂移（成本、horizon、
top_n、覆盖窗口、cutoff、宇宙、factor 版本、缺失因子口径）会得到不同 `dataset_hash`，脚本会 **fail closed**
并拒绝覆盖已冻结文件。要开新假设，请用新的 `--batch-path`（新的预注册），不要就地改。

> 2026-10-06 口径对齐：面板此前用默认 `NEUTRAL_ZERO` 管线构建，现已改为经
> `factor_sets.factor_pipeline_for_factor_set` 取得管线（`sentiment_v1` → `MissingFactorPolicy.EXCLUDE`），
> 与生产/实验口径一致；`missing_policy` 自此并入 `dataset_hash`，故哈希由
> `sentiment_forward_batch_v1:CN:cce369acaf892e29f56e` 变为 `sentiment_forward_batch_v1:CN:f31ebe289588e2243433`。
> 重算发生在首批 `start_date=2026-10-08` 之前、尚无任何 `collect` 快照落盘，因此是对同一预注册的合规更正
> 而非新假设。之后任何口径变更都必须走新 `--batch-path`。

> 注意：`operator`/`created_at` 不进入哈希（操作者或时间漂移不应 fork 批次），因此文件首次写入后
> 重复运行会保留首个 `operator`。若首个文件以 `unknown` 落盘、之后想补记操作者，需要删除该文件后
> 带 `PQW_OPTIN_OPERATOR` 重新生成（哈希不变，仅溯源字段更新）。

## 3. 前向运行程序

三个 stage，全部由冻结规格驱动。默认快照目录
`data/artifacts/stock_selection_research/sentiment_forward/<batch_id>/snapshots/<effective_date>.json`，
报告目录 `data/experiments/sentiment_v1_forward_reports/`。

### 3.1 `collect` — 每日采集并冻结当日决策

```bash
# 拉取 HiThink 情绪特征（复用 sync_hithink_sentiment_features.py）→ 落盘
python scripts/run_sentiment_forward_batch.py --stage collect

# 已手动同步过特征，跳过网络仅重建面板
python scripts/run_sentiment_forward_batch.py --stage collect --skip-fetch

# 按冻结 PIT 宇宙工件限定 universe（推荐）
python scripts/run_sentiment_forward_batch.py --stage collect \
  --universe-parquet data/artifacts/stock_selection_research/universes/pit_universe_v1_CN_25815a348bebba7f/universe.parquet
```

流程：(1) 复用同步脚本落盘情绪特征；(2) 在冻结 universe 与 **EOD 16:00 cutoff** 下构建 PIT 情绪矩阵；
(3) 用 `sentiment_v1` 因子管线做同日截面排名，取 Top-N 与完整打分面板——管线**经
`factor_sets.factor_pipeline_for_factor_set` 工厂取得**（`EXCLUDE` 缺失口径：缺失因子从 `factor_values`
中剔除并对现存因子重新归一，绝不以 0 参与合成），与生产/实验同一口径；(4) 以
`effective_trade_date`（下一交易日起算）为键，**幂等、不可变**落盘一个快照；覆盖窗口外的日期直接
`skipped_out_of_coverage`。

### 3.2 `status` — 查成熟度（只读，不产结论）

```bash
python scripts/run_sentiment_forward_batch.py --stage status
python scripts/run_sentiment_forward_batch.py --stage status --as-of-date 2026-10-30
```

输出成熟/待成熟/不完整批次数，以及是否有资格下结论（`conclusion_allowed`）。

### 3.3 `evaluate` — 到期评估（成熟前 block）

```bash
python scripts/run_sentiment_forward_batch.py --stage evaluate            # 预览
python scripts/run_sentiment_forward_batch.py --stage evaluate --execute  # 落盘报告
```

流程：(1) 复用 `forward_shadow_evaluation.build_cn_forward_shadow_evaluation` 计算逐日扣费净收益、
成熟门槛与"不完整冻结批次"判定；(2) 若成熟日数 < 60 或存在不完整批次 → 明确 `status=blocked`，
`conclusion=null`，**不产出任何结论**；(3) 只有成熟后才把冻结面板（Top-N=treated，次优 N=control，
同一面板）交给实验框架，产出聚类 CI95 / BH-FDR q 值 / shuffle 随机对照 / 四分类 / attempts 收紧后的报告。

### 3.4 频率建议

- `collect`：**每个 A 股交易日盘后（建议 16:30 之后）跑一次**。EOD 主路径的 cutoff 是 16:00，
  必须等上游 featured 数据发布后再采集，否则当日特征会缺。
- `status`：每日随 `collect` 后跑一次即可（廉价只读）。
- `evaluate`：**每周一次**（例如周五盘后）；成熟门槛以"成熟日数"为准而非自然周，所以频率只影响
  观察滞后，不影响判定口径。达到 60 个成熟决策日后再正式 `--execute` 出报告。

`cron` 示例（工作日盘后）：

```cron
30 16 * * 1-5 cd /Volumes/STORAGE_Jackyhu/code/ana && PQW_OPTIN_OPERATOR=ops .venv/bin/python scripts/run_sentiment_forward_batch.py --stage collect >> logs/sentiment_forward.log 2>&1
0 18 * * 5    cd /Volumes/STORAGE_Jackyhu/code/ana && .venv/bin/python scripts/run_sentiment_forward_batch.py --stage evaluate >> logs/sentiment_forward.log 2>&1
```

## 4. 判定门槛（预注册）

| 判据 | 门槛 |
| --- | --- |
| 成熟日数 | ≥ 60 个成熟决策日，且无"不完整冻结批次" |
| 主指标 | 扣费净命中率（Top-N 成熟信号净收益 > 0 的比例）≥ 0.55 |
| 显著性 | treated−control 按日聚类的 block bootstrap CI95 下界 > 0 |
| 多重检验 | 总体 + 分层桶 BH-FDR q ≤ 0.05 |
| 独立日期 | 去重叠独立成熟日数 ≥ 20 |
| 随机对照 | 行内 shuffle×5 的 null 分布；四分类不得为 `noise`/`reversed_strict` |
| 尝试收紧 | attempts 经哈希链 registry 收紧阈值 `min_effect_pp × sqrt(1+ln(attempts))` |

全部满足记为 `acceptance_passed=true`；未成熟一律 `blocked`，不产生结论。

## 5. 测试

- 新增离线测试：[tests/test_sentiment_forward_batch.py](/Volumes/STORAGE_Jackyhu/code/ana/tests/test_sentiment_forward_batch.py)
  - 批次规格创建幂等 + 契约漂移 fail closed；
  - `dataset_hash` 稳定（operator/时间无关）且对成本/horizon/top_n/覆盖窗口/cutoff/宇宙/竞价路径/**缺失因子口径**敏感；
  - `missing_policy` 与生产/实验共用工厂一致（`sentiment_v1` → `EXCLUDE`），且进入 `dataset_hash`（口径切换即 fork 哈希）；
  - 面板打分使用 `EXCLUDE`：缺失情绪单元格从合成中剔除并重新归一，绝不按 0 稀释；
  - 未成熟批次 block（`conclusion_allowed=false`）+ 不完整批次 block；
  - 覆盖窗口外样本、非冻结宇宙成员不进面板；
  - 面板成熟日过滤 + 实验框架端到端跑通。
- 命令与结果（无网络、纯夹具）：

```bash
.venv/bin/python -m pytest tests/test_sentiment_forward_batch.py -q
# 12 passed, 9 subtests passed

.venv/bin/python -m pytest \
  tests/test_stock_selection_forward_shadow_evaluation.py \
  tests/test_selection_experiment_framework.py \
  tests/test_sentiment_factor_registration.py \
  tests/test_sentiment_forward_batch.py -q
# 52 passed, 85 subtests passed

.venv/bin/ruff check <新增/修改文件>
# All checks passed
```

- 离线集成冒烟：用一个临时批次 + 真实 lake 价格跑 `--stage status`，正确返回
  `status=blocked`、`conclusion=null`、`remaining_matured_dates=59`，并给出诊断命中率。

## 6. 未决问题

1. **竞价路径是否纳入首批？** 当前首批 `auction_path_included=false`，只走 EOD 16:00 主路径。
   理由：竞价快照（09:25）是**另一套 cutoff 语义与可用时点**，纳入会改变 `dataset_hash` 与
   样本构成；建议先把 EOD 主路径跑满 60 个成熟日，再用**独立批次**（新 `--batch-path`、
   `--include-auction-path`）验证竞价增量，避免混批。
2. `acceptance.min_net_hit_rate=0.55` 是研究性预注册下限，尚未经前向数据校准；首批成熟后应
   结合对照组分布复核该阈值。
3. `min_present_features=1` 的面板规则会让"仅 1 个非空特征"的票也进入排名；是否提高阈值
   （要求更完整的情绪向量）留待首批数据观察后再定，任何改动都会产生新 `dataset_hash`。
4. `universe_version` 依赖本地最新 PIT 宇宙工件（当前 `pit_universe_v1:CN:25815a348bebba7f`，
   `source_version` 含 `explicit_tickers`）。若生产宇宙切换到全市场工件，需要重新冻结批次。
