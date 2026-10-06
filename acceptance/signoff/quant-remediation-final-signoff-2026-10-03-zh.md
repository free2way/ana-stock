# 量化口径整改：最终验收签署（2026-10-03）

- 结论：**整改通过（附范围豁免与残余风险登记）**
- 签署人：**Jacky Hu**（项目/业务）｜独立复核：**Vincent**（2026-10-03 已签，见[独立复核报告](quant-remediation-independent-review-2026-10-03-zh.md) §8）

## 1. 签署依据（已闭合项）

- 五项阻断全部闭合，经 3 名独立复核人证伪式复核 + Vincent 签署
- PG 测试库全量回归 batch9：`Ran 1235 / FAIL 62 / ERROR 9`，**新增失败 0**、净修好 23 项存量失败
- C-5/C-6 已由 Jacky Hu 签署（12/12 恒等式独立复算一致）
- S 项 13 项（含补登记的 S-13/S-14）代码与 57 个新测试完成；跨轨道 66 例交叉验证通过
- D-4：pilot（60 只）+ 全宇宙宽度审计（5,172 只、5 folds、`leakage_violation_count` 全 0，scope 已如实标注）
- FX：接入 SAFE 官方中间价（8,059 行 / 1994→2026-09-30），S-12 多币种解除 fail-closed；缺表仍 fail-closed
- 退市标的库内清理 16 只（`is_active` 1→0，剩余交集 0，备份可回滚）
- 代码冻结基准 **v28（当前基准）**（commit `e462598`、patch `7d27268e…`、untracked 20 文件 manifest `03f2b523…`、docs 95 文件 `bb58da6f…`、快照 `5df29765…`；产物 `data/artifacts/freeze-20261005-v28/`）——并入**"提升筛选成功率"整批补录**（批次 1 + 批次 2 + 接线与真实激活，见 §3.7）；**前序冻结基准 v21（批次 1 后）**（patch `8d7f621e…`、untracked 6 文件 manifest `6389fa85…`、docs 93 文件 `019a3364…`、快照 `e7c55aab…`；产物 `data/artifacts/freeze-20261005-v21/`）——并入批次 1 明细（统一成本口径 / 按日聚类 CI 下界 / 主评估基准与分层 / 可执行性硬门槛 / 实验框架，见 §3.6）
- **前序冻结基准 v17**（patch `b277c1d6…`、untracked 232 文件 manifest `701dbca2…`、docs 92 文件 `0fdaae18…`、快照 `305d971e…`；产物 `data/artifacts/freeze-20261003-v17/`）——并入**本轮补录**（opt-in 严格度统一 / OBSERVE 完整性证据开关 / screener 与 publication 两处不可晋级接入 / 最终账本，见 §3.5）；**前序冻结证据 v16**（patch `1a2e8c07…`、untracked 231 文件 manifest `5e2aea13…`、docs 92 文件 `5b877e92…`、快照 `954186e8…`，并入下一阶段先行工作阶段①②与门禁/审计补录（见 §3、§3.4））；前序冻结证据 **v14**（patch `93fb53c1…`、untracked 223 文件 manifest `4e9aaeb9…`、docs 92 文件 `fe3d27aa…`、快照 `f52a01e4…`，并入阶段①②）、**v10**（patch `40470f55…`、untracked 219 文件 manifest `16f57603…`、docs 91 文件 `5870e36c…`、快照 `41048291…`，含本题两处代码风险修复：训练标签口径、回测未建模公司行为）、**v9**（patch `1855b0e0…`、untracked 218 文件 manifest `deb2bd2e…`、**docs 91 文件** manifest `4ecb4195…`、快照 `bab529cc…`）；**本签署件已纳入 v9 docs 清单**（v8 的 docs 清单不含签署文件，版本链以 v9 闭合）
- 末次全量回归 batch10：`Ran 1266 / FAIL 63 / ERROR 9`，对比基线**新增失败 0**（S 项与两处修复并入后仍零回归）。
- 回归计数口径澄清：基线 94 个失败/错误名 → batch8 93（1 项已知顺序敏感 flake 消失）→ batch9 71；**相对基线净修 23、新增 0**（batch8→batch9 相差 22 = 23 减 1 项 flake）
- **仓库提交状态变化**：`HEAD` 现为 `e462598`（此前冻结基准为 `34c80d9`）；自其前 `df04f02`（`feat: modularize dashboards and harden quantum pipelines`）起，本轮整改的相当一部分已被提交，**非本流程（文档 agent）所为，待确认归属**；对照 v21 冻结时工作树剩 12 个已修改 + 7 个未跟踪。

## 2. 范围豁免（本次签署的边界）

| # | 豁免项 | 替代证据 | 后续 |
|---|---|---|---|
| E1 | D-4 **正式全市场选型口径**（`membership` 无免费时点源；`universe_revision_history` 只能向前积累；`security_master` 受 Tushare 免费配额限制） | pilot + 全宇宙宽度泄漏审计（violation=0） | 商业源立项或自建 append-only 存储积累；Tushare 配额恢复后重跑契约脚本 |
| E2 | S 项 **6 项运行环境证据**（S-6/S-7 预计算重跑；S-4/S-8/S-12 前端截图；S-7 TradingView 凭据） | 代码 + 单测（57 例） | 环境就绪后补取证，不改判定 |
| E3 | **73 项预先存在测试失败**（全部为本次整改前既有，集中在 `test_app`：UI 文案/路由、concept 404、CAT-6 夹具过短、`reversal` 信号缺口） | 最新账本 vs 整改前基线名集差集 = **新增 0**（证明非本次引入；见 §3.5） | 另立批次排期修复 |

> E3 后续：阶段②已产出 `docs/acceptance-debt-registry-zh.md`（对齐最新账本后为 73 项 / 12 类）承接 E3 的登记与排期，跟踪口径见 §3。

## 3. 下一阶段先行工作补录

> 本节补录签署后启动的**下一阶段先行工作**（阶段①②，以及门禁/审计补录见 §3.4）成果；相关代码/文档已并入代码冻结基准 **v16**（前序，见 §1），本轮补录并入 **v17**（见 §3.5）；更早前序 v14，见 §1。**E1–E3 豁免保持不变**。

### 3.1 阶段①：共享价格口径合同

- 成果：新增共享合同 `app/services/price_basis_contract.py`——统一**三态探测** + **按入口决策**（train / inference / backtest）+ **审计字段**（视图哈希、覆盖率、缺失、回退、授权）。
- 一致性：`tests/test_price_basis_contract.py` 一致性测试 **21 例 OK**；相关回归 **90 例 OK**（含一致性 21 例）。证据：日志 `data/artifacts/acceptance-20261002/phase1-price-basis-contract-tests.log`（SHA256 `8ee1bb8e…`）、`data/artifacts/acceptance-20261002/phase1-price-basis-contract-evidence.json`。
- 有意行为变更：视图重建后，**旧模型回测被显式拒绝**（basis 版本不匹配 fail-closed，不再静默按旧口径出数）。
- 该变更不扩大也不缩小 E1–E3 豁免范围。

### 3.2 阶段②：验收流水线

- 固定测试库：`pqw_test`（建库/迁移由 `scripts/setup_test_database.py` 负责）。
- 标准入口：`scripts/run_acceptance_suite.py`——产出**逐测试账本**（pass / fail / error / skip / not_collected）。
- 首版全量账本：`Ran 1293 / pass 1222 / fail 62 / error 9 / skip 0 / not_collected 0`；对比基线 `added 0 / removed 23 / changed 23（ERROR→FAIL）`。
- 复跑一致性：复跑账本**剥离时间字段后 JSON 逐字节一致**。
- 债务承接：存量失败登记于 `docs/acceptance-debt-registry-zh.md`（首版 71 项 / 11 类；对齐最新账本后为 **73 项 / 12 类**，见 §3.5；负责人 **Jacky Hu**、期限 **下一批次待排期**），**承接原 E3** 的跟踪与排期。
- 验收证据路径（均在 `data/artifacts/acceptance-20261002/`）：
  - 首版账本：`acceptance-ledger-20261003T182703-r12-pqwtest.json` / `.md`
  - 复跑账本：`acceptance-ledger-20261003T183354-r12b-pqwtest.json` / `.md`
  - 回归日志：`regression-with-postgres-acceptance-20261003T182703-r12-pqwtest.log`
  - 冻结产物：`data/artifacts/freeze-20261003-v14/`（阶段②时点；后续冻结见 `data/artifacts/freeze-20261003-v17/`，当前基准见 `data/artifacts/freeze-20261005-v28/`）

### 3.3 与豁免/冻结的关系

- **日后复跑标准入口**：阶段②的 `scripts/run_acceptance_suite.py` + `pqw_test` 作为后续验收复跑的标准入口；账本差异口径沿用（`added > 0` 必须另行处置，不得并入债务表）。
- **E3 承接**：E3 的存量失败由 `docs/acceptance-debt-registry-zh.md` 承接跟踪（首版 71 项，对齐最新账本后 73 项，见 §3.5），豁免边界与验收结论不变。
- **E1 / E2 不变**：豁免内容与后续动作保持原样（见 §2）。

### 3.4 下一阶段补录（第 3 / 4 / 5 项）

> 本节补录签署后、冻结 v16 前的下一阶段工作成果；相关代码已并入代码冻结基准 **v16**（前序，见 §1）。**E1–E3 豁免与 §2 结论不变。**

#### 第 3 项：统一晋级门禁（已接入服务路径）

- **9 项检查 / `promotable` 标记**：门禁 v2（`app/services/stock_selection/promotion_gate_v2.py`）对每个候选 run 输出 9 项检查——`run_status_completed`、`run_scope_promotable`、`price_basis_contract`、`corporate_action_coverage`、`data_readiness`、`training_sample_size`、`oos_evaluation`、`purge_embargo_audit`、`statistical_evidence`——并按检查结果给出 `promotable` 标记与 `non_promotable_reasons`（非晋级运行**保留可达**，仅显式标注"非晋级/研究口径"）。
- **审批追溯 `approval.json`**：`persist_promotion_approval_record` 生成**内容寻址、不可覆盖**的审批记录（`approval.json` + `manifest.json`，含 market / run_id / 决策 / 原因 / code_version / data_version / decided_by / decided_at）；同证据幂等复用，异内容拒绝覆盖。
- **真实 readiness 拦截证据**：正式全市场训练被 PIT 就绪门槛 **fail-closed**（`historical_security_master / membership / delisting / industry / universe_revision` 未验证），如实标注为预先存在缺口，未以"通过"呈现（见 §2 E1）。
- **接入 `PredictionRepository` 服务路径**：服务期评估桥接 `app/services/stock_selection/promotion_enforcement.py` 已接入生产信号 run 选择 `app/services/repositories/predictions.py`（`assess_run_for_serving` / `annotate_rows_with_promotion`）；`PQW_PROMOTION_GATE_ENFORCE` **默认 True（fail-closed）**：门禁判定为 `REJECT`（显式失败：研究口径、readiness/PIT 阻断、价基被拒、未建模公司行为未 opt-in、样本不足、OOS 为负、purge 违规、统计子门禁被拒等）的 run 不得成为服务冠军/推荐基准。
- **OBSERVE 默认不拦截**：`OBSERVE`（证据缺失、非显式失败）仅标注 `promotable=False` 并记 WARNING，不拦截——因为大量历史 run 的晋级证据从未持久化，拦截会误伤全部遗留 run。
- **`PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE` 开关**：对应配置 `promotion_gate_require_complete_evidence`，**默认 False**（observe-only，即历史行为）。置 True 后收紧为"证据必须完整"，`OBSERVE` 运行与 `REJECT` 一样被拦截；该开关**不覆盖** `PQW_PROMOTION_GATE_ENFORCE`——仅当 `PQW_PROMOTION_GATE_ENFORCE` 开启时才发生拦截。

#### 第 5 项：opt-in 审计与风险监控（已落地）

- **opt-in 结构化审计**：`app/services/optin_audit.py` 将训练 raw 回退（`PQW_TRAINER_ALLOW_RAW_FALLBACK`）与回测接受未建模公司行为（`allow_unmodeled_corporate_actions`）两处豁免，从裸布尔升级为结构化记录，字段含 **`operator` / `decided_at` / `scope` / `reason`**；`operator` 默认 `unknown` 但始终显式记录（可与"字段缺失"区分）。
- **缺 reason fail-closed**：opt-in 未携带可审计原因时抛 `MissingOptinReasonError` **拒绝放行**；原因可取 run 参数或 `PQW_OPTIN_REASON`，操作人取 run 参数或 `PQW_OPTIN_OPERATOR`。旧布尔键保留以兼容历史消费方。
- **`monitor_quant_risk.py` 首跑指标**：`scripts/monitor_quant_risk.py` 首跑产出 `data/artifacts/acceptance-20261002/quant-risk-monitor.json` / `.md`（schema `quant-risk-monitor-v1`，2026-10-03T12:04:37Z）：
  - 数据陈旧度：CN / US 均 `fresh`（lag 0 天）；
  - 复权覆盖：CN 覆盖率 1.0、US 0.9617（unsupported 1,906），相对上次 delta 均 0；
  - fail-closed 事件：`price_basis_reject / coverage_block / raw_fallback_gap_block / unreadable_view_block / unmodeled_block / missing_optin_reason` 全 0（扫描 runs 80、日志 2）；
  - 模型漂移：`cn_close_2026-09-03` run 311→312，`drift=False`（各 horizon hit_rate/avg_return delta 全 0）。

#### 第 4 项：评估加固（明确缓做）

- **PIT 成分/修订历史无免费数据源**：免费源（AKShare 中证/新浪/东财板块等）只提供**当前成分快照 + 当前成分的纳入日**，无法回放"某历史时点指数/板块成分"；`historical_membership_verified` 如实保持不可用（见《[数据源可行性报告](quant-remediation-data-source-feasibility-2026-10-03-zh.md)》§2）。
- **自建宇宙不能替代**：仓库现有 `UniverseRuleConfig` / `SecurityMetadata` 只能重建"可交易全域"，语义不同于"指数历史成分"，且退市历史仅 361 条（早期退市/转三板/B 股未必齐），不得据此置 `True`（§2 E1 同源）。
- **幸存者偏差无法根治**：免费源均为当前快照，无法回放历史 vintage，故本项**明确缓做**；待引入付费/学术源（CSMAR / Wind / 聚宽 / RiceQuant 成分历史）或按日快照**向前积累** append-only vintage 后再评估。

### 3.5 本轮补录（opt-in 严格度统一 / 门禁两处接入 / 最终账本；冻结 **v17**）

> 本节补录冻结 **v17** 前收口的本轮工作；相关代码已并入代码冻结基准 **v17**（见 §1）。**E1–E3 豁免与 §2 结论不变。**

- **opt-in 严格度统一（启用即须 reason）**：`app/services/optin_audit.py` 将门禁收敛为统一规则——**只要 opt-in 被启用，就必须携带可审计 reason**（run 参数或 `PQW_OPTIN_REASON`），否则抛 `MissingOptinReasonError` **fail-closed 拒绝**；该要求**不因该豁免在具体 run 中是否被实际触发**而放宽（未启用的 opt-in 记 `reason=None` 且不报错）。`operator` 默认 `unknown` 但始终显式记录。
- **`PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE` 开关**：对应配置 `promotion_gate_require_complete_evidence`，**默认 False**（observe-only，即历史行为）。置 True 后收紧为"证据必须完整"，`OBSERVE` 运行与 `REJECT` 一样被拦截；该开关**不覆盖** `PQW_PROMOTION_GATE_ENFORCE`——仅当 `PQW_PROMOTION_GATE_ENFORCE` 开启时才发生拦截。
- **screener champion 接入不可晋级（REJECT 跳过）**：`app/services/screener.py` 的服务冠军选择接入统一门禁（`assess_run_for_serving`）——被 `REJECT`（或 `OBSERVE` 且开启完整性证据）的候选 run **跳过**，回退到更早的可服务 run，被拒 run 不再作为推荐基准；未拦截时仅标注 + WARNING。
- **publication_guard 接入不可晋级（REJECT 拒发）**：`app/services/stock_selection/publication_guard.py` 的发布路径接入门禁——`promotion_decision == REJECT` 时**拒绝发布**（`ValueError`）；`OBSERVE` 仅在 `PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE=true` 时同样拒发。**publication 的 `REJECT` 现服从 `PQW_PROMOTION_GATE_ENFORCE`**。
- **最终账本（债务表重算口径）**：`data/artifacts/acceptance-20261002/acceptance-ledger-20261003T203040.json` / `.md`——`Ran 1357 / pass 1284 / fail 64 / error 9 / skip 0 / not_collected 0`；对比整改前基线 `added=0 / removed=21 / changed=24 / unchanged_failing=49` → 仍失败 **73**；对应日志 `data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261003T203040.log`。
- **债务表对齐**：`docs/acceptance-debt-registry-zh.md` 已按最新账本重算为 **73 项 / 12 类**（新增 `CAT-15`，为 r9 triage 未收录项，按实测断言单列）；与旧 71 的差异 = **+2 复发项**（`test_dashboard_summary_is_cached_between_requests`、`test_watchlist_uses_batched_prediction_queries`，均落在整改前基线失败名集内，`added` 仍为 0），详见该表 §0。

### 3.6 批次 1（提升筛选成功率）补录（冻结 **v21**）

> 本节补录冻结 **v21** 前的批次 1 工作；代码并入代码冻结基准 **v21**（见 §1，产物 `data/artifacts/freeze-20261005-v21/`）。**E1–E3 豁免与 §2 结论不变。**

**五项成果摘要：**

- **①统一成本口径**：新增 `app/services/cost_basis.py`——`PQW_SELECTION_CANONICAL_ROUND_TRIP_COST_BPS`（配置字段 `selection_canonical_round_trip_cost_bps`，`app/core/config.py`）默认 **50 bps**、范围 [0, 200]；`canonical_round_trip_cost_bps(market)` / `canonical_cost_fields()` 统一输出 `cost_bps` / `cost_basis="round_trip"` / `cost_source`。**0 成本路径补扣费**：`selection_quality` 与 `factor_experiments` 的命中标志改按净收益（gross `return_*` 字段保留，新增 `net_return_*`），此前按毛收益计的 hit_rate 不再与净口径混用。
- **②hit_rate 按日聚类 CI 且门槛用下界**：新增 `statistical_inference.day_clustered_hit_rate_ci`（按日聚类的 moving-block bootstrap，block 长度 = 评估 horizon）；`selection_quality` 来源择优要求**聚类命中率 CI 下界 > 50**（`SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT`，低于阈值者记入 `rejected_sources`）；`promotion_gate` 的 `active_mean_ci95_lower_bound` 改用聚类下界，iid 区间仅作诊断字段（标注不用于过闸）。
- **③主评估接等权基准 + 行业/市值/流动性分层**：`model_evaluation._benchmark_universe_tickers` / `_equal_weight_benchmark_by_date` / `_benchmark_section`——`benchmark_status`、`benchmark_avg_return_pct`、`excess_avg_return_pct`、`excess_positive_rate_pct`（等权日收益，可叠加指数 overlay）；分层新增 `industry:<值>`、`market_cap_bucket:<small|mid|large|unknown>`、`liquidity_bucket:<low|mid|high|unknown>`，摘要 `stratification` 记录口径与形状。
- **④可执行性硬门槛 + CN 涨停降级 + 市值剔除修复**：screener 链路新增 **opt-in** 可交易/就绪硬门槛、CN 信号日涨停**降级/打标**（`limit_up_demoted`）、query-time 底部市值剔除（`exclude_bottom_market_cap_pct`）；覆盖测试 `tests/test_screener_execution_gates.py`。
- **⑤实验框架**：新增 `app/services/stock_selection/experiment_framework.py`（**冻结 `dataset_hash`**、同面板、分层、配对 t + 聚类 bootstrap + BH-FDR、随机对照 shuffle × 5 seeds 四分类、attempts 收紧 `min_effect_pp × sqrt(1+ln(attempts))`）与 `scripts/run_selection_experiment.py`；`experiment_promotion_evidence` 将实验报告映射为 **`promotion_gate_v2`** 统计子门可消费的 evidence（`PASS` → `ELIGIBLE_FOR_MANUAL_REVIEW`，其余 → `REJECT`，映射不改写也不绕过门禁阈值）。

**验收数字（批次 1 后）：**

- acceptance 流水线：`Ran 1415 / pass 1344 / fail 62 / error 9 / skip 0 / not_collected 0`；对比整改前基线 `added=0 / removed=23 / changed=23 / unchanged_failing=48`。
  - 账本：`data/artifacts/acceptance-20261002/acceptance-ledger-20261005T110203.json` / `.md`
  - 日志：`data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261005T110203.log`（SHA256 `8aed28b8…`）
- 聚焦测试 **55 例 OK**：`tests.test_model_evaluation`（5）、`tests.test_stock_selection_promotion_gate`（11）、`tests.test_selection_cost_and_cluster_ci`（9）、`tests.test_selection_experiment_framework`（13）、`tests.test_screener_execution_gates`（12）、`tests.test_statistical_inference`（5）（`test_model_evaluation` 5 例依赖 `PQW_TEST_DATABASE_URL` 测试库）。

**已知限制（如实登记）：**

- 基准宇宙上限 **300 只**（`BENCHMARK_UNIVERSE_MAX_TICKERS`），excess 为近似。
- 市值分层取自**最新** `fundamental_snapshots`（**非 PIT**，存在潜在前视）。
- `model_evaluation.evaluate_model_runs` 默认仍为 **20bps**（`round_trip_cost_bps: float = 20.0`）；scheduled 评估链实传 **50bps**（`SCHEDULED_EVALUATION_ROUND_TRIP_COST_BPS = 50.0`），二者尚未收敛为同一默认值。
- **reconciliation 路径未接基准/分层**（`require_execution_reconciliation` 分支自带 summary，不含 benchmark / stratification）。
- CN 涨停 **profile 缺失时按代码前缀回退 10cm**（`_cn_limit_band_pct_by_code`）。
- `limit_up_today` 仅在 **lake 动量路径**标注（`screen_lake_momentum` → `_annotate_cn_limit_up`）。
- 实验框架 **5 seeds 下排列 p 下限 1/6**（判定用"超过 null 最大值"而非 p≤alpha）、**p 采用正态近似**（`erfc`）、独立日期按**日历日**去重叠（`independent_date_count`）。

### 3.7 提升筛选成功率：批次 1/2 + 接线 + 激活补录（冻结 **v28**）

> 本节补录冻结 **v28** 前的"提升筛选成功率"整批工作（批次 1 / 批次 2 / 接线与真实激活）；代码并入代码冻结基准 **v28**（见 §1，产物 `data/artifacts/freeze-20261005-v28/`；批次 1 明细见上节 §3.6）。**E1–E3 豁免与 §2 结论不变。**

**批次 2：训练侧口径与融合可靠性**

- **训练侧 PIT 可交易池**：训练样本按 PIT 可交易日过滤；标签 winsor + Huber 目标；drawdown penalty **默认仅用于指标**（不进拟合目标）；`embargo=horizon`；截面标准化（按交易日 winsor-MAD z-score）。
- **融合可靠性加权 + 校准概率弃权**：多模型融合按各模型 OOS 可靠性加权（缺可靠性时回退等权），叠加选择性校准的**概率弃权**（未校准行在开关开启时弃权）。
- **新增开关**：`PQW_TRAINER_FIT_ON_RISK_ADJUSTED`（配置 `trainer_fit_on_risk_adjusted`，**默认 False**）与 `PQW_TRAINER_RANDOM_SEED`（配置 `trainer_random_seed`，**默认 42**）。
- **多 seed 消融结论**（产物 `data/experiments/training_ablation_multiseed/training-ablation-20261005T120020Z.md` / `.json`，3 seeds 方向一致性判定）：drawdown 默认仅指标（3/3 逐位相同实证）；进拟合目标后 3/3 方向更优但幅度 < 跨 seed 离散度（**仅初步支持**）；四项全关在本 pilot 净收益更好（**反向信号，待生产口径复核**）。

**接线与激活**

- **接线**：参数/白名单放行、可靠性元数据与校准产出闭环、调度落盘（失败仅告警）、US 物理表 JOIN、run 选择防遮蔽。
- **真实激活（run 390 / eval 134）**：真实 artifact 落盘——reliability **107 样本（lookback 40 + horizon 6 过滤）/ hit_rate 0.3178 / IC −0.0328**（`data/artifacts/stock_selection_research/reliability/oos_reliability_latest.json`）；calibration **5 bins / 概率 0.1905–0.4091**（`data/artifacts/stock_selection_research/calibration/probability_calibration_latest.json`）；`weight_source=row_metadata:oos_hit_rate`（**非等权**），概率有值。
- **验证期副作用（用户授权删除）**：eval 131/132 已（或即将由另一 agent）按用户授权删除，保留 **133（隔离验证证据）** 与 **134（正式激活证据）**；删除前备份 `data/artifacts/acceptance-20261002/eval-131-132-backup-2026-10-05.json`（含 `model_evaluations` 131/132 及其 metrics，可回滚；SHA-256 `2c7a9beb…2686`）。

**外部改动（待确认归属）**

- `app/services/stock_selection/__init__.py` 于 **2026-10-05 18:54** 被**外部改动**（相对冻结基线 +18 re-export，导出数 **200→218**）；该改动**非本流程所为**，其**集合/身份断言均成立**，相关测试计数已同步更新。**待确认归属**。

**验收账本（最新）**

- `data/artifacts/acceptance-20261002/acceptance-ledger-20261005T215906-dimagent-fix2.json` / `.md`——`Ran 1561 / pass 1491 / fail 60 / error 10 / skip 0 / not_collected 0`；对比整改前基线 `added=0 / removed=24 / changed=21 / unchanged_failing=49`（当前仍失败 = unchanged_failing 49 + changed 21 = **70**；`added` 仍为 0）。日志 `data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261005T215906-dimagent-fix2.log`（SHA256 `cb4885a7…`）；债务表按此账本重算**待排期**。

**已知限制（如实登记）**

- 252 会话生产窗口数据不足（`insufficient_mature_feature_dates`）。
- pilot 面板有**幸存者偏差**。
- **US 市场未做消融**。
- 5 seeds 下排列 p 下限 **1/6**（用超 null 最大值判定）；p 为**正态近似**。
- `execution_verified` 因除权日 `corporate_action_requires_account_replay` 仍为 **False**（既有定义）。
- 指数/板块**仅有当前成分**，不可 PIT。

## 4. 残余风险登记

R1（研究挑战者管线 raw 属设计口径）、R7（flake 证据边界，含 `test_watchlist_uses_batched_prediction_queries` 顺序敏感项）、R9（存量失败，最新账本 `20261005T215906-dimagent-fix2` 重算为 **70 项** = unchanged_failing 49 + changed 21，债务表对齐待排期；此前 v17 口径为 73 项）——详见独立复核报告 §5；E1–E3 的边界自本签署生效。

## 5. 签署

```
验收结论：整改通过（附范围豁免 E1–E3 与残余风险登记）
项目签署：Jacky Hu      日期：2026-10-03
独立复核：Vincent       日期：2026-10-03（结论：通过）
```
