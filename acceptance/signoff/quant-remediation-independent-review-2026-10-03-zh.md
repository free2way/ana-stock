# 量化口径整改：独立复核报告（2026-10-03）

- 复核对象：当前工作树（未提交）`HEAD 34c80d9d81648e176de5455097f41c5f3ecb50df` + 脏树改动
- 复核方式：**3 名独立复核人（全新上下文、只读、互不通气、以证伪为目标）**，分别覆盖【意见1+2】【意见3+4】【意见5+A6】；明确要求**不采信任何文档/总结结论**，一切以代码、可复现命令与原始产物为准
- 复核边界：未修改仓库任何文件；未跑 DB 端到端集成（除日志复算）；三位复核人均声明了各自未能验证的点（见 §5）

---

## 1. 判定总览

| 审核意见 | 独立复核判定 | 关键依据 |
|---|---|---|
| ①调整后视图未接入训练/回测 | **确认闭合**（默认生产路径） | `_attach_adjusted_basis`/`_label_price` 全链路 + 真实数据实验（5 标的 2020/2020 行挂载 adjusted）；runner 传 `corporate_actions=` 且引擎真实执行拆股/分红 |
| ②默认引擎未统一 / manifest 未绑定 | **确认闭合** | 默认 `event_driven_daily_v2`；app/scripts 四处入口显式传参；manifest 绑定 `actions_snapshot_sha256`+`adjusted_view_sha256`（CN `903f341445a1`/`b9afa131e409`，US `1833353558f2`/`eadf2437f1fa`） |
| ③provenance 默认值不可追溯 | **确认闭合**（附 2 条绕过路径，已处置 1 条、登记 1 条） | `_with_provenance` 缺来源抛错且早于落盘；8 个调用点显式来源；历史覆盖率复算 CN/US 均真实 0.0% |
| ④公司行为存储覆盖同键修订 | **确认闭合** | 修订追加（`keep="last"` 仅按全内容列做幂等折叠）；`load_actions(as_of)` 可回放旧值 |
| ⑤冻结 patch hash 不含未跟踪文件内容 | **确认闭合** | freeze-v3 内容级清单独立复算：抽 30 条 0 不一致、tar 全 201 成员 0 不一致、遗漏审计 0；`shasum -c` 5/5 OK |
| 附：测试/文档可靠性 | **部分确认 → 已修** | PG 全量 `Ran 1154 / FAIL 44 / ERROR 49 / env errors 0`，名集对比新增失败 0；文档 5 处过期/矛盾表述已更正；存量 93 项如实标为「不计为通过」 |

> 结论：**五项阻断经独立复核确认闭合**；复核同时给出「夸大/证伪点」与「残余风险登记」，见 §3、§5。整体验收判定仍以人工签署为准（本报告不替代责任人与业务签字）。

---

## 2. 复核执行的必做验证（原始命令）

```bash
.venv/bin/python -m unittest tests.test_trainer_label_basis tests.test_runner_corporate_actions \
  tests.test_engine_default_and_manifest_binding tests.test_legacy_metric_display_guard   # 15 tests OK
.venv/bin/python -m unittest tests.test_provenance_contract tests.test_market_lake_writes \
  tests.test_lake_v2_shadow tests.test_market_data_quality tests.test_corporate_actions   # 38 tests OK
# 冻结：抽 30 条重算 sha256 → 0 不一致；tar 201 成员重算 → 0 不一致；shasum -a 256 -c SHA256SUMS → 5/5 OK
# 回归：FAIL/ERROR 名集差集（final.log → batch6b.log）→ 新增 0、移除 1（已知顺序敏感 flake）
# C-5/C-6：12 单元格 × 2 恒等式 = 24/24 吻合；6 个证据文件 SHA256 与签署记录逐一 MATCH
```

---

## 3. 复核发现的「夸大/证伪点」（均已在文档中更正）

| # | 来源 | 原表述 | 事实 | 处置 |
|---|---|---|---|---|
| 1 | 意见④ | “存储层不再 `keep='last'` 去重” | `corporate_actions.py:201` 仍有 `keep="last"`，但 subset 为**全内容列**，只折叠字节相同行；同自然键不同内容保留 | 文档改为精确表述（已改） |
| 2 | 意见④ | 测试覆盖“两来源两条修订” | 实为**同源**（`akshare_fhps`）不同 `source_reference`/金额 | 文档改为精确表述（已改） |
| 3 | 意见① | “样本元数据写入 `adjustment_version` 绑定” | 训练侧原只写 `label_price_basis` + 两个分量哈希，未写合成串 | **代码补写合成绑定**（`trainer.py` run config）+ 文档更正 |
| 4 | 意见② | “`test_runner_corporate_actions.py` 覆盖跨拆股/分红断言调整生效” | 该文件只断言映射与源码字符串；行为断言在 `tests/test_event_driven_backtest.py:274` | 文档更正并指向真实行为测试 |
| 5 | 意见② | “全仓不存在 `BacktestRunner().run(` 裸调用” | `tests/test_app.py:343` 有 1 处（依赖新默认值，风险低；守护测试只扫 app/、scripts/） | 文档限定为“app/ 与 scripts/ 内无裸调用” |
| 6 | 文档一致性 | — | 完成汇总/行动方案 5 处过期或自相矛盾（A-1 仍指 v2、D-4 标“未执行”、1097/1102 旧数字、M3 状态、C-5/C-6 自相矛盾） | 全部更正（见 §4） |

---

## 4. 复核发现的问题与处置清单

| # | 问题（来源） | 严重度 | 处置 | 证据 |
|---|---|---|---|---|
| P1 | `LAKE_V2_SHADOW_ENABLED=false` 时 provenance 校验被完全跳过，缺来源行静默写入（意见③复核） | 高 | 校验前置为**无条件执行**，开关仅控制是否落 shadow | `market_lake.py` `write_lake_v2_partition`；新增测试 `test_provenance_is_validated_even_when_shadow_is_disabled` |
| P2 | `provider="auto"`（选择器非来源）可通过闸门（意见③复核） | 中 | `LAKE_V2_PLACEHOLDER_PROVIDERS={"","unknown","auto"}` 拒绝；`market_sync.py`、`cn_market_universe.py` 去除 `or "auto"`/`or "eastmoney_push2"` 弱回退 | 新增测试 `test_auto_provider_selector_is_rejected_as_placeholder` |
| P3 | 可追溯判据未显式排除 `legacy_v1_lake:` 前缀（意见③复核） | 中 | 判据加 `NOT starts_with(source_reference,'legacy_v1_lake:')` 并同步 criterion 文案 | `scripts/audit_provenance_traceability.py` |
| P4 | `load_actions(as_of)`/最新修订按**字符串**比较 `ingested_at`，混用时区偏移会排序错误（意见④复核） | 中 | 改为解析为绝对时刻（UTC）比较与排序（null 最小） | `corporate_actions.py` `load_actions`；新增测试 `test_as_of_and_latest_use_absolute_time_not_string_order` |
| P5 | `reconciled` 标签档在标准 `_load_rows` 下**静默**产出 `UNVERIFIED`/空标签（意见①复核） | 高 | 切档前 fail-fast 校验 `price_basis`/`execution_source_reference`/`corporate_action_status`，缺失即 `RuntimeError` | `trainer.py` reconciled 分支 |
| P6 | `adjusted_view_manifest` 读 `version` 恒为 None（schema 无 `version` 字段）（意见②复核） | 低 | reader 回落到 `schema_version` | `adjustment_snapshot.py:43` |
| P7 | 文档 5 处过期/矛盾（意见⑤复核） | 中 | 全部更正；旧数字标注作废 | 完成汇总、行动方案 |

修复后验证：`tests.test_lake_v2_shadow / test_corporate_actions / test_market_lake_writes / test_provenance_contract / test_market_data_quality` **41 例 OK**；`测试核心 7 模块` **31 例 OK**；随后 PG 全量重跑（§6）。

---

## 5. 残余风险登记（未整改 / 需跟踪）

| # | 残余风险 | 理由与影响 | 后续动作 |
|---|---|---|---|
| R1 | 研究挑战者管线 `stock_selection`（`labels.py::build_executable_label`）仍用 raw 价 + `corporate_action_jump_threshold=0.80` 启发式剔除，`validate_prices` 显式要求 `price_basis=="raw"` | **设计口径**：该管线是"可执行/成交约束"研究，不用于生产标签；启发式对未登记动作有漏判风险 | 在研究文档显式标注"研究口径 ≠ 生产口径"；如需生产化需接入 actions |
| R2（**已闭合**） | 评估/洞察路径曾用未复权 close | — | 新增共享助手 `app/services/price_basis.py`（`preferred_price`/`preferred_close`：`adjusted_*` → `adj_close`(legacy) → raw）并接线 4 处（`model_evaluation`、`selection_quality`、`recommendation_regression`、`factor_experiments`）；gap/intraday/drawdown 等一端为 raw open/low 的比较**刻意保留 raw**（避免混基）；新增 `tests/test_price_basis.py` 9 例（含 adjusted 生效断言） |
| R3（**已闭合，有边界**） | 修复脚本直写 v1 绕过 provenance | — | `clean_us_non_common_lake.py`、`repair_lake_date_dtype.py` 已在 v1 重写后按分区重建 v2 shadow（`provider="manual_repair"`）；`repair_us_lake_from_alpaca.py` 经复核确认只写 `_us_alpaca` 暂存区、**不写 canonical v1**，其 shadow 块已**撤销**（否则造成 shadow ⊅ v1 反向漂移）；三个脚本 docstring 均写入运维要求（修复后必须 `verify_lake_manifest.py --check`）。边界：basis 冲突或分区清空场景仍可能半完成/残留 shadow 行，属 fail-closed 设计代价，登记跟踪 |
| R4（**已闭合**） | `stock_dividend` 复用 `split` 因子语义 | — | 已由 B-7 官方口径对照验证（CN 603040 送转 `factor=1.45` 与东财一致，即 1→factor 乘数语义正确）＋引擎拆股守恒测试（`tests/test_event_driven_backtest.py:274`）；C-7 100/100 抽检亦覆盖 |
| R5（**已闭合**） | `tests/test_app.py:343` 裸调用 | — | 已改为显式 `engine_version="event_driven_daily_v2"` |
| R6（**已闭合**） | freeze 原仅覆盖 git 可见源码；`docs/` 被 `.gitignore:26` 忽略，**验收文档此前完全不在冻结范围** | 审计文档无法被内容校验 | freeze 脚本增加 `collect_docs_manifest()`：v5 起对 `docs/` 下 **85 个文件**做内容 SHA256（`docs_manifest.json` + `baseline.docs.content_sha256`）；`data/` 仍按理由排除（数据版本由 lake/eval manifest 负责） |
| R7 | PG 回归日志为自报输出；`test_watchlist_uses_batched_prediction_queries` 的顺序敏感性未独立复跑证明 | 证据边界 | 如需强证明可复跑该 flake 多次 |
| R8（**已闭合**） | `schema_version` 命名滞后 | 已在新增 `docs` 段时同步升为 `quant_remediation_freeze_v3`（v5 起） | — |
| R9 | 存量 93 个失败/错误（全部落在 `test_app`：39×LightGBM 历史长度不足、5×watchlist 属性缺失、2×legacy report、其余断言不符） | 预先存在，**不计为通过** | 另行排期修复；本轮只保证"新增失败 0"；最新账本重算为 **73 项**（2026-10-03T203040，见 `docs/acceptance-debt-registry-zh.md`） |

---

## 6. 复核后修复的再验证

- **PG 全量重跑（batch7，P1–P6 修复后）**：`Ran 1157 / FAIL 44 / ERROR 49 / env errors 0`；名集差集 vs 基线 = **新增 0**。
- **PG 全量重跑（batch8，R2/R3/R5 修复后）**：`Ran 1166 / FAIL 44 / ERROR 49 / env errors 0`；名集差集 vs 基线 = **新增 0**（+9 为 `test_price_basis` 新增测试）。
- **修复后定向测试**：`test_lake_v2_shadow / test_corporate_actions / test_market_lake_writes / test_provenance_contract / test_market_data_quality` **41 例 OK**；`trainer_label_basis / runner_corporate_actions / engine_default_and_manifest_binding / legacy_metric_display_guard / stock_selection_history_backfill / us_market_scheduler / community_data_sources` **31 例 OK**。
- **历史冻结 v6**（`data/artifacts/freeze-20261003-v9/`，schema `quant_remediation_freeze_v3`）：patch `1855b0e0…`、untracked 218 文件 / manifest `b1bb96b7…`、docs manifest `c27d401e…`（85 文件）、快照 `1e051580…`；v5 为 P1–P6 后、v6 为 R2–R5 后状态，A-1 证据以 v6 为准。
- **PG 全量重跑（batch9，S 项 A/B/C 轨道 + R9 CAT-1/CAT-5 修复后）**：`Ran 1235 / FAIL 62 / ERROR 9`；名集差集 vs 基线 = **新增 0**、**移除 23**（净修好 23 项存量失败，无回归）。
- **计数口径**：基线 94 名 → batch8 93（flaky 消失 1）→ batch9 71；净修 23、新增 0。
- **历史冻结 v9**：patch `1855b0e0…`、untracked 218 文件 / manifest `af59b5a6…`、docs manifest `c27d401e…`、快照 `1e051580…`。
- **冻结 v17（v17 时点基准）**：patch `b277c1d6…`、untracked 232 文件 / manifest `701dbca2…`、docs 92 文件 manifest `0fdaae18…`、快照 `305d971e…`；产物 `data/artifacts/freeze-20261003-v17/`（并入本轮补录：opt-in 严格度统一、`PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE` 开关、screener champion 与 publication_guard 两处不可晋级接入、最终账本 73 项，见下文本轮补录）。
- **前序冻结 v16**：patch `1a2e8c07…`、untracked 231 文件 / manifest `5e2aea13…`、docs 92 文件 manifest `5b877e92…`、快照 `954186e8…`；产物 `data/artifacts/freeze-20261003-v16/`（含阶段①②与门禁/审计补录：`app/services/price_basis_contract.py`、`app/services/stock_selection/promotion_gate_v2.py`、`app/services/stock_selection/promotion_enforcement.py`、`app/services/optin_audit.py`、`scripts/run_acceptance_suite.py`、`scripts/setup_test_database.py`、`scripts/monitor_quant_risk.py`、`docs/acceptance-debt-registry-zh.md` 等）。
- **前序冻结 v14**：patch `93fb53c1…`、untracked 223 文件 / manifest `4e9aaeb9…`、docs 92 文件 manifest `fe3d27aa…`、快照 `f52a01e4…`；产物 `data/artifacts/freeze-20261003-v14/`（阶段①②后状态）。
- **一处已知的时间差**：v6 冻结完成后，本节与 §5/§8 才追加/更新文本；因此 v6 的 `docs_manifest` 与本报告末段存在文本级差异（源码/脚本/测试内容不受影响）。如需零差异证据，可在定稿后再冻结一次。
- 复核时点记录（修复前）：batch6b `Ran 1154 / FAIL 44 / ERROR 49 / env errors 0`（新增失败 0）。

### 阶段①②并入（下一阶段先行工作）

- **阶段① 共享价格口径合同**：新增 `app/services/price_basis_contract.py`——三态探测 + 按入口决策（train / inference / backtest）+ 审计字段（视图哈希、覆盖率、缺失、回退、授权）；一致性测试 `tests/test_price_basis_contract.py` **21 例 OK**，相关回归 **90 例 OK**（含一致性 21 例；证据：`data/artifacts/acceptance-20261002/phase1-price-basis-contract-tests.log`、`data/artifacts/acceptance-20261002/phase1-price-basis-contract-evidence.json`）；有意行为变更 = **视图重建后旧模型回测被拒绝**（fail-closed）。
- **阶段② 验收流水线**：固定测试库 `pqw_test`；标准入口 `scripts/run_acceptance_suite.py`（逐测试账本 pass/fail/error/skip/not_collected，建库/迁移见 `scripts/setup_test_database.py`）；首版全量账本 `Ran 1293 / pass 1222 / fail 62 / error 9 / skip 0 / not_collected 0`，对比基线 `added 0 / removed 23 / changed 23（ERROR→FAIL）`；复跑账本剥离时间字段后 **JSON 逐字节一致**；债务表 `docs/acceptance-debt-registry-zh.md`（71 项 / 11 类，负责人 **Jacky Hu**、期限 **下一批次待排期**）。
- **验收证据路径**（均在 `data/artifacts/acceptance-20261002/`）：首版账本 `acceptance-ledger-20261003T182703-r12-pqwtest.json` / `.md`；复跑账本 `acceptance-ledger-20261003T183354-r12b-pqwtest.json` / `.md`；回归日志 `regression-with-postgres-acceptance-20261003T182703-r12-pqwtest.log`；冻结产物 `data/artifacts/freeze-20261003-v14/`（阶段②时点）；后续冻结见 `data/artifacts/freeze-20261003-v17/`，当前基准见 `data/artifacts/freeze-20261005-v28/`。
- **与豁免关系**：E1–E3 豁免保持不变；阶段②流水线为**日后复跑标准入口**，债务表**承接 E3** 的跟踪与排期。

### 下一阶段补录（第 3 / 4 / 5 项，冻结 v16；最终口径见 v17）

> 补录签署后、冻结 **v16** 前的下一阶段工作；该时点流水线账本 `data/artifacts/acceptance-20261002/acceptance-ledger-20261003T200843.json` / `.md`（`Ran 1339 / fail 61 / error 9`；对比基线 `added=0 / removed=24 / changed=22 / unchanged_failing=48`）。**E1–E3 豁免不变**。逐项内容与签署件 §3.4 一致。最新口径（v17）见下节。

- **第 3 项：统一晋级门禁（已接入服务路径）**。门禁 v2 输出 **9 项检查**（`run_status_completed`、`run_scope_promotable`、`price_basis_contract`、`corporate_action_coverage`、`data_readiness`、`training_sample_size`、`oos_evaluation`、`purge_embargo_audit`、`statistical_evidence`）与 `promotable` 标记；每次评估生成内容寻址、不可覆盖的审批追溯 `approval.json`（`persist_promotion_approval_record`）；正式全市场训练被 PIT 就绪门槛 **fail-closed**（真实 readiness 拦截证据，未以"通过"呈现）。桥接模块 `promotion_enforcement.py` 已接入 `PredictionRepository` 服务路径（`app/services/repositories/predictions.py`），`PQW_PROMOTION_GATE_ENFORCE` **默认 True**：`REJECT` 的 run 不得成为服务冠军/推荐基准。`OBSERVE`（证据缺失）**默认不拦截**，仅标注"非晋级/研究口径"+WARNING；开关 `PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE`（配置 `promotion_gate_require_complete_evidence`，**默认 False**）置 True 后 `OBSERVE` 同 `REJECT` 一并拦截，且**不覆盖** `PQW_PROMOTION_GATE_ENFORCE`。
- **第 5 项：opt-in 审计与风险监控**。`app/services/optin_audit.py` 将两处 opt-in 升级为结构化记录（`operator` / `decided_at` / `scope` / `reason`），缺 `reason` 时 `MissingOptinReasonError` **fail-closed** 拒绝，`operator` 默认 `unknown` 但始终记录。`scripts/monitor_quant_risk.py` 首跑产出 `data/artifacts/acceptance-20261002/quant-risk-monitor.json` / `.md`：数据陈旧度 CN/US 均 `fresh`（lag 0）；复权覆盖 CN 1.0 / US 0.9617（unsupported 1,906），delta 均 0；6 类 fail-closed 事件全 0（runs 80 / 日志 2）；模型漂移 `cn_close_2026-09-03` run 311→312 `drift=False`。
- **第 4 项：评估加固（明确缓做）**。PIT 成分/修订历史**无免费数据源**（免费源仅当前快照 + 当前成分纳入日），`historical_membership_verified` 如实不可用；自建规则宇宙语义不同、不得据此置 `True`；**幸存者偏差无法根治**。待付费/学术源或按日快照向前积累 vintage 后再评估（详见《[数据源可行性报告](quant-remediation-data-source-feasibility-2026-10-03-zh.md)》§2 与签署件 §2 E1）。

### 本轮补录（opt-in 严格度统一 / 门禁两处接入 / 最终账本，冻结 v17）

> 补录冻结 **v17** 前收口的本轮工作；代码并入基准 **v17**（`data/artifacts/freeze-20261003-v17/`）。逐项内容与签署件 §3.5 一致。**E1–E3 豁免不变**。

- **opt-in 严格度统一（启用即须 reason）**：`app/services/optin_audit.py` —— 只要 opt-in 启用即必须携带可审计 reason（run 参数或 `PQW_OPTIN_REASON`），否则 `MissingOptinReasonError` **fail-closed 拒绝**；不因豁免是否在具体 run 中被实际触发而放宽（未启用记 `reason=None` 且不报错）；`operator` 默认 `unknown` 但始终记录。
- **`PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE` 开关**：配置 `promotion_gate_require_complete_evidence` **默认 False**（observe-only）；置 True 后 `OBSERVE` 与 `REJECT` 一并拦截，且**不覆盖** `PQW_PROMOTION_GATE_ENFORCE`。
- **两处不可晋级接入**：screener champion 选择（`app/services/screener.py`，`assess_run_for_serving`）对 `REJECT`（或 `OBSERVE`+完整性证据）**跳过**并回退到更早可服务 run；publication 路径（`app/services/stock_selection/publication_guard.py`）对 `REJECT` **拒发**（`ValueError`）。**publication 的 `REJECT` 现服从 `PQW_PROMOTION_GATE_ENFORCE`**。
- **最终账本（v17 口径）**：`data/artifacts/acceptance-20261002/acceptance-ledger-20261003T203040.json` / `.md`——`Ran 1357 / pass 1284 / fail 64 / error 9 / skip 0 / not_collected 0`；对比整改前基线 `added=0 / removed=21 / changed=24 / unchanged_failing=49` → 仍失败 **73**；日志 `data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261003T203040.log`。债务表据此重算为 **73 项 / 12 类**（新增 `CAT-15`，r9 未收录，按实测断言单列）；与旧 71 差异 = +2 复发项（均属整改前基线失败名集，`added` 仍为 0，详见债务表 §0）。

### 批次 1 补录（冻结 v21）

> 本小节为**同口径补录**：批次 1（提升筛选成功率）工作与验收在冻结 **v21** 前收口；代码并入基准 **v21**（`data/artifacts/freeze-20261005-v21/`）。**§1–§8 的历史结论与 E1–E3 豁免不变。**

- **冻结 v21（前序）**：patch `8d7f621e…`、untracked 6 文件 manifest `6389fa85…`、docs 93 文件 `019a3364…`、快照 `e7c55aab…`；产物 `data/artifacts/freeze-20261005-v21/`（前序冻结 v17 见本节上段）。**仓库状态**：`HEAD` 现为 `e462598`（复核原文 HEAD `34c80d9` 之后的提交 `df04f02` 起，本轮整改相当一部分已被提交，非本流程所为，待确认归属）。
- **验收账本（批次 1）**：`data/artifacts/acceptance-20261002/acceptance-ledger-20261005T110203.json` / `.md`——`Ran 1415 / pass 1344 / fail 62 / error 9 / skip 0 / not_collected 0`；对比整改前基线 `added=0 / removed=23 / changed=23 / unchanged_failing=48`；日志 `data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261005T110203.log`（SHA256 `8aed28b8…`）。聚焦测试 **55 例 OK**（含 `test_selection_cost_and_cluster_ci` / `test_selection_experiment_framework` / `test_screener_execution_gates` / `test_statistical_inference`；`test_model_evaluation` 5 例依赖测试库）。
- **实验框架衔接点**：`app/services/stock_selection/experiment_framework.py::experiment_promotion_evidence` 将实验报告映射为 `promotion_gate_v2` 统计子门可消费的 evidence——`decision == PASS` → `ELIGIBLE_FOR_MANUAL_REVIEW`（仍需人工复核），其余 → `REJECT`；**只做格式转换，不改写也不绕过门禁任何阈值**（与门禁 v2 的 `_statistical_gate_check` 对齐）。入口脚本 `scripts/run_selection_experiment.py`；配套 `app/services/cost_basis.py`（`PQW_SELECTION_CANONICAL_ROUND_TRIP_COST_BPS` 默认 50bps）统一成本口径。
- **与豁免关系**：E1–E3 豁免与残余风险 R1–R9 登记保持不变；批次 1 的已知限制（基准宇宙上限 300 / 市值分层非 PIT / `evaluate_model_runs` 默认 20bps / reconciliation 未接基准分层 / CN 涨停回退 10cm / `limit_up_today` 仅 lake 动量路径 / 实验框架 5 seeds p 下限 1/6 等）见签署件 §3.6。

### "提升筛选成功率"整批补录（批次 1/2 + 接线 + 激活，冻结 v28）

> 本小节为**同口径补录**：整批工作在冻结 **v28** 前收口；代码并入基准 **v28**（`data/artifacts/freeze-20261005-v28/`）。**§1–§8 的历史结论与 E1–E3 豁免不变。**

- **冻结 v28（当前基准）**：commit `e462598`、patch `7d27268e…`、untracked 20 文件 manifest `03f2b523…`、docs 95 文件 `bb58da6f…`、快照 `5df29765…`；产物 `data/artifacts/freeze-20261005-v28/`（前序冻结 v21 见本节上段；`HEAD` 仍为 `e462598`，此前审计确认的"相当一部分整改已被提交、非本流程所为"结论不变）。
- **验收账本（最新）**：`data/artifacts/acceptance-20261002/acceptance-ledger-20261005T215906-dimagent-fix2.json` / `.md`——`Ran 1561 / pass 1491 / fail 60 / error 10 / skip 0 / not_collected 0`；对比整改前基线 `added=0 / removed=24 / changed=21 / unchanged_failing=49`（当前仍失败 = 49 + 21 = **70**）。日志 `data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261005T215906-dimagent-fix2.log`（SHA256 `cb4885a7…`）。债务表按此账本重算**待排期**（v17 口径为 73 项）。
- **批次 2（训练侧 + 融合）**：训练侧 PIT 可交易池、标签 winsor + Huber、drawdown penalty（**默认仅指标**）、`embargo=horizon`、截面标准化；融合**可靠性加权**（缺可靠性回退等权）+ **校准概率弃权**；新增 `PQW_TRAINER_FIT_ON_RISK_ADJUSTED`（配置 `trainer_fit_on_risk_adjusted`，默认 False）与 `PQW_TRAINER_RANDOM_SEED`（配置 `trainer_random_seed`，默认 42）。
- **多 seed 消融产物**：`data/experiments/training_ablation_multiseed/training-ablation-20261005T120020Z.md` / `.json`（3 seeds 方向一致性判定）——drawdown 默认仅指标（3/3 逐位相同实证）；进拟合目标后 3/3 方向更优但幅度 < 跨 seed 离散度（**仅初步支持**）；四项全关在本 pilot 净收益更好（**反向信号，待生产口径复核**）。
- **接线与激活证据**：真实激活 **run 390 / eval 134** 成功——reliability `data/artifacts/stock_selection_research/reliability/oos_reliability_latest.json`（**107 样本（lookback 40 + horizon 6 过滤）/ hit_rate 0.3178 / IC −0.0328**）；calibration `data/artifacts/stock_selection_research/calibration/probability_calibration_latest.json`（**5 bins / 概率 0.1905–0.4091**）；`weight_source=row_metadata:oos_hit_rate`（**非等权**），概率有值。接线含：参数/白名单放行、可靠性元数据与校准产出闭环、调度落盘（失败仅告警）、US 物理表 JOIN、run 选择防遮蔽。
- **验证期副作用（用户授权删除）**：eval 131/132 已（或即将由另一 agent）按用户授权删除，保留 **133（隔离验证证据）** 与 **134（正式激活证据）**；备份 `data/artifacts/acceptance-20261002/eval-131-132-backup-2026-10-05.json`（含 `model_evaluations` 131/132 及其 metrics，可回滚；SHA-256 `2c7a9beb…2686`）。
- **未知归属的外部改动**：`app/services/stock_selection/__init__.py` 于 2026-10-05 18:54 被**外部改动**（相对冻结基线 +18 re-export，导出数 **200→218**），**非本流程所为**；其集合/身份断言成立，相关测试计数已同步更新。**待确认归属**。
- **已知限制（如实登记）**：252 会话生产窗口数据不足（`insufficient_mature_feature_dates`）；pilot 面板有幸存者偏差；US 市场未做消融；5 seeds 下排列 p 下限 1/6（用超 null 最大值判定）、p 正态近似；`execution_verified` 因除权日 `corporate_action_requires_account_replay` 仍为 False（既有定义）；指数/板块仅有当前成分，不可 PIT。
- **与豁免关系**：E1–E3 豁免与残余风险 R1–R9 登记保持不变；整批已知限制见签署件 §3.7。

---

## 7. 结论

1. 五项阻断经**三名独立复核人**分别验证，**全部确认闭合**（默认生产路径），未发现伪造或夸大性造假；
2. 复核发现的 6 处表述偏差与 6 项实质问题（P1–P6）**已全部处置**，并新增 3 个针对性测试；
3. 残余风险 R2/R3/R4/R5/R6/R8 **已闭合**；仍开放登记：R1（研究管线 raw 属设计口径）、R7（证据边界，含 `test_watchlist_uses_batched_prediction_queries` 顺序敏感项）、R9（预先存在失败，最新账本重算 **73 项**，不计通过）；
4. 本报告为**技术独立复核**；独立复核人 **Vincent 已于 2026-10-03 签署确认**（§8）。整体验收判定由责任人在接受 R1/R7/R9 的前提下另行确认。

---

## 8. 签署

```
独立复核范围：审核意见 ①–⑤ + 附（测试/文档可靠性）
复核方式：3 名独立复核人（全新上下文、只读、以证伪为目标）
判定：五项阻断全部确认闭合；复核发现 P1–P6 已全部修复并再验证
残余风险：R1–R9 登记在案（见 §5），其跟踪不阻塞本次复核结论
签署人：Vincent        日期：2026-10-03        结论：通过
```

> 说明：本签署确认**技术复核结论**；整体验收判定与残余风险接受由责任人另行决定。
