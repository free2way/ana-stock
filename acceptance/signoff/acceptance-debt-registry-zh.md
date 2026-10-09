# 验收债务登记表（存量测试失败，中文）

- 生成日期：2026-10-05（最新重算：**2026-10-09**，**CAT-7-1 收口后重跑 → E3 存量债务清零**）
- 负责人/期限：**已归口（Jacky Hu）**；E3 存量失败已清零，**无待排期项**。本表另登记 7 项「已知未决缺口」（见 §5），均为**产品/口径决策或外部数据缺口**，**不计入 E3 存量失败口径**，归口 Jacky Hu。
- 范围：**E3 范围豁免项**（`acceptance/signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §2 E3）——整改前既有、非本次引入的测试失败。
- 当前基线证据（**CAT-7-1 收口提交 `8b0af37` 之后的全量重跑，干净隔离库、无并发**）：
  - 最新全量账本：`tmp/acceptance-final-20261009/acceptance-ledger-20261009T182714-finalfresh.md` / `.json`
    （`Ran 1775 / pass 1773 / fail 0 / error 0 / skip 2 / expected_failure 0`，**失败名集为空**）
  - 运行日志：`tmp/acceptance-final-20261009/regression-with-postgres-acceptance-20261009T182714-finalfresh.log`
    （SHA256 `e210d40fee1b13095a8dd555904078e9c200d49437579e0bf50f643e90634142`）
  - 与基线逐项比较：**added=0 / removed=0 / changed=0 / unchanged_failing=0**（基线段为 0 失败名集）。
  - 上一版可比账本（**1 项口径**）：`tmp/acceptance-cat1c/acceptance-ledger-20261008T225900-cat1c.json` / `.md`
    （`Ran 1706 / pass 1703 / fail 1 / error 0 / skip 2`，唯一失败为 CAT-7-1）。
  - 更早口径：6 项 `tmp/acceptance-catfinal/acceptance-ledger-20261008T215857-catfinal.json`；7 项 `tmp/acceptance-cat12/…-cat12.json`；15 项 `tmp/acceptance-cat14/…-cat14.json`；28/30/32 项见 §0。
  - 分类来源：`data/artifacts/acceptance-20261002/r9-failure-triage-2026-10-03.json`（R9 93 项归因，14 类）。
- 记账口径：**不削弱断言、不删除测试、不改业务代码**。所有已修项逐条登记修复提交；未闭环项如实登记（本版为 0）。
- **记录口径（总量）**：签署件（`acceptance/signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §5 R9）登记的存量失败口径为 **70 项**（= unchanged_failing 49 + changed 21；上一版 v17 口径 73 项）；本表自身逐版口径为 **43 → 32 → 30 → 28 → 15 → 7 → 6 → 1 → 0**，**每批 `added=0`**（无新增失败，只做单向缩小，直至清零）。

## 0. 重算说明（43 → 32 → 30 → 28 → 15 → 7 → 6 → 1 → 0）

- **43 项口径**：以账本 `acceptance-ledger-20261007T132008.json`（`Ran 1705 / pass 1660 / fail 35 / error 8 / skip 2`）为准——失败 **43** 项。
- **CAT-4 收尾（32 项）**：账本 `tmp/acceptance-cat4/acceptance-ledger-20261007T164843-cat4.json`——相对 43 项口径 `added=0 / removed=11 / changed=0 / unchanged_failing=32`。
- **CAT-1 收尾（30 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T172735-cat1.json`——相对 43 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=30`。
- **CAT-1/CAT-4 余项收口（28 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T190140-cat1fix4.json`——相对上一版 30 项口径 `added=0 / removed=2 / changed=0 / unchanged_failing=28`。
- **CAT-2/CAT-14 收口（15 项）**：账本 `tmp/acceptance-cat14/acceptance-ledger-20261007T195733-cat14.json`——相对 28 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=15`（CAT-2 5 项 commit `c2de781`；CAT-14 8 项 commit `79cf8cb`）。
- **CAT-3/6/7-2/12/13 收口（7 项）**：账本 `tmp/acceptance-cat12/acceptance-ledger-20261008T192710-cat12.json`——相对 15 项口径 `added=0 / removed=8 / changed=0 / unchanged_failing=7`（CAT-3 `87fbde2`；CAT-6 `13a9342`/`d496d74`；CAT-12 `d496d74`；CAT-7 文案 `d61203f`；CAT-13 `5977640`）。
- **CAT-13 收尾（6 项）**：账本 `tmp/acceptance-catfinal/acceptance-ledger-20261008T215857-catfinal.json`——相对 7 项口径 `added=1（环境性）/ removed=1 / changed=0 / unchanged_failing=6`（`6b2aa1d`；`added` 的环境性失败已随 `d6bbdc5` 显式打桩消除）。
- **CAT-1/CAT-4 收口（1 项）**：账本 `tmp/acceptance-cat1c/acceptance-ledger-20261008T225900-cat1c.json`——相对 6 项口径 `added=0 / removed=7 / changed=0 / unchanged_failing=1`（CAT-1 `f14585b`/`fc04e9f`；CAT-4 余项 `5329982`）。
- **CAT-7-1 收口（0 项，本版）**：账本 `tmp/acceptance-final-20261009/acceptance-ledger-20261009T182714-finalfresh.json`——相对 1 项口径**清零**：唯一存量失败 `test_app.AppFlowTests.test_watchlist_add_and_sync_now_works` 随 **commit `8b0af37`** 转通过（用受支持市场 + provider 打桩覆盖 add+sync 成功路径），**失败名集为空**。至此 **E3 存量债务 = 0**。

### 环境性失败（已消除）

- 6 项口径另有 2 项概念页同步用例为**运行时刻环境**失败（美股盘中 bar 被「不写未完成交易日行」守卫拒绝），**commit `d6bbdc5`** 显式打桩 US 价格 provider 后消除；本表当前**不含任何环境性失败**。

### 三路审核修复闭环与样本集影响（2026-10-09）

- **闭环**：三路独立审核（A 砺行 `trainer.py` / C 秉直 执行与账本 / B 容之 校准·面板·健康）共 **8 条 P1/P2** 全部**复现属实**并闭环，对应 8 个修复提交：`e25e160`、`2eb469c`、`9a7bc4e`（A）；`015ef8d`、`36ca9fe`（C）；`4306b4e`、`3ef5f28`、`7a3a729`（B）。逐条复现/修复/回归明细见 `acceptance/supplements/audit-p1-closure-2026-10-09.md`。
- **合树干净全量**（隔离库，无并发 sibling）：首轮 `added=3`（经机制验证 + 单独复跑判定为 **DB 并发死锁 → `setUp` 中止 → `PQW_DATA_DIR`/`PQW_OPTIN_REASON` 泄漏**的测试编排伪失败），次轮同参复跑 **`added=0`**；**产品代码回归集为空**。明细见同一补充件。
- **样本集变化（重要，影响可比性）**：砺行 **P1-1**（`e25e160`，标的池过滤改为样本门控）修复后，**标注样本 204 → 208**（对照实验中 15 个共同信号日的入场/持有窗口位移）。**此前 v40/v41/v42 基准下的模型产出与样本门控后产出口径不同、不可直接比较**；在**重训产出新模型**之前，请勿用旧基准的模型指标与新版对比。

## 本轮修复登记（2026-10-09，commit 以 `git log --oneline` 实际核对为准）

> 本版 HEAD = `29c6744`；以下为本轮（v42 冻结之后）新增的修复/改动提交，按类别归并。

### A. 三路独立审核 P1/P2 闭环（8 条，详见 `acceptance/supplements/audit-p1-closure-2026-10-09.md`）

- **审核 A（选样 / 标签 / 评估口径，`app/services/trainer.py`）**
  - `e25e160` **P1-1** 标的池过滤改为**样本门控**（保留完整时间轴，不再移动入场/持有窗口）。
  - `2eb469c` **P1-2** 校准与训练**共享特征截面变换**（整窗 `_feature_matrix`，分批仅用于预测，同日截面不被切断）。
  - `9a7bc4e` **P1-3** OOS **先冻结候选名单后评估**，缺失/未成熟/被拒结果分别计数（`_freeze_oos_candidates` + `oos_candidate_audit`）。
- **审核 C（回测执行约束与账本一致性）**
  - `015ef8d` **P1-A** 期末强平纳入 **A 股 T+1 可卖校验**（唯一卖出校验点；被拦仓保留 open 并在 rejects / gate_stats / outcomes 可见）。
  - `36ca9fe` **P1-B** 卖出**原子化**（单事务）+ `pg_advisory_xact_lock` 串行化 + `idempotency_key`/nonce + NaN/Inf/负/零**数值有限性校验**。
- **审核 B（校准产物适用范围 / 情绪面板 / 健康探针）**
  - `4306b4e` **P1** 概率校准产物按 **市场/模型/持有期/分数口径** 分区（`calibration_artifact_path` / `resolve_calibration_artifact` / `inject_calibration_defaults`，拒绝未来产物，输出 `uncalibrated:<reason>`，thin refresh 失效标记）。
  - `7a3a729` **P2** `/health/ready` 依赖失败返回 **503** 且不再泄漏 DB 异常文本/产物绝对路径（详情移至鉴权端点 `/health/ready/diagnostics`）。
  - `3ef5f28` **P1** 情绪实验面板**先冻结 top-N 再测前向收益**，冻结名缺收益计入 `missing_returns`、不补位。

### B. 存量 E3 债务 CAT-7-1 归零

- `8b0af37` `test(watchlist): 用受支持市场+provider 打桩覆盖 add+sync 成功路径（CAT-7-1 收尾）` —— `test_watchlist_add_and_sync_now_works` **转通过**，E3 存量债务 **1 → 0**。

### C. CN / US 数据面与服务面修复

- `00db225` **热力图 CN 侧重建**：CN 收盘流水线重建 market workspace 快照（含 `market_heatmap_workspace`）。
- `6b964a0` **US 收盘视图时序**：保证 US 复权视图在信号训练前重建并按需跳过（`latest_date >= required_date` 时 skip），闭合「视图落后一天」复发路径。
- `01bb32b` CN fundamentals：`roe_avg_3y` 拥有**独立 content revision**（避免与其它字段共用版本号导致误判）。
- `9a43c19` 训练运行补齐**公司行为覆盖证据**，接通促销门 `corporate_action_coverage`（`corporate_action_coverage_audit`）。

### D. 标签与明细口径

- `cf36d4a` 信号文本标签对齐**横截面分位口径**（`build_signal_label` 类标签不再恒 Hold）。
- `af065b2` 标签管线补 **5 日路径指标** `next_5d_close_return`。
- `29c6744` **明细估计逐键来源解析**：`_build_detail_row` 估计按 key 从训练窗/OOS 桶择优回退，恢复 `expected_return_20d`。

### E. 许可与测试基础设施

- `df6ab4e` 采用 **AGPL-3.0** 许可（`LICENSE` 新增 + 两份 README 许可段落）。
- `c29873c` price-basis 与调整后视图用例**隔离改造**（真实临时 Parquet 视图替换 mock，断言增强）。

## 1. 汇总

| 类别（r9 triage id） | 数量 | 状态类型 | 负责人 | 期限 | 依据 |
|---|---|---|---|---|---|
| CAT-1-lightgbm-price-history | 0 | 已清零 | Jacky Hu | — | `f14585b` / `fc04e9f` |
| CAT-4-insight-model-output-404 | 0 | 已清零 | Jacky Hu | — | `5329982` |
| CAT-7-watchlist-add-message-and-ui（余 CAT-7-1） | 0 | **已清零** | Jacky Hu | — | 本批 `8b0af37` |
| CAT-3-legacy-report-freeze-conflict | 0 | 已清零 | Jacky Hu | — | `87fbde2` |
| CAT-6-job-pipeline-failed | 0 | 已清零 | Jacky Hu | — | `13a9342` / `d496d74` |
| CAT-12-openbb-provider-history | 0 | 已清零 | Jacky Hu | — | `d496d74` |
| CAT-13-ai-daily-report-scope | 0 | 已清零 | Jacky Hu | — | `5977640` / `6b2aa1d` |
| CAT-7-watchlist-add-message-and-ui（文案子因） | 0 | 已清零 | Jacky Hu | — | `d61203f` |
| CAT-2-watchlist-full-analysis-stub | 0 | 已清零 | Jacky Hu | — | `c2de781` |
| CAT-14-misc-regressions | 0 | 已清零 | Jacky Hu | — | `79cf8cb` |
| CAT-10-screener-cn-template-and-csv | 0 | 已清零 | Jacky Hu | — | `68276bc` |
| CAT-11-screener-watchlist-actions-and-tv | 0 | 已清零 | Jacky Hu | — | `68276bc` |
| **合计** | **0** | **FAIL 0 / ERROR 0** | — | — | — |

> 计数校验：全部为 0，合计 **0**；状态合计 FAIL 0 / ERROR 0，与最新账本（`acceptance-ledger-20261009T182714-finalfresh.json`）**空失败名集**逐一相符。
> **本批归零**：**CAT-7-1 1 项**（`8b0af37`）→ 至此 **E3 存量债务 = 0**。

## 2. 分类明细

### CAT-7-watchlist-add-message-and-ui（0 项，**已清零**）
- 本批清零：commit `8b0af37`——`test_watchlist_add_and_sync_now_works` 改以**受支持市场 + provider 打桩**覆盖 add+sync 成功路径（原先 `0700.HK` 触发「HK 数据不被 Parquet lake 支持」的同步语义分歧 S-13）。失败名集不再含该项。
- 历史与文案子因：`test_watchlist_page_adds_symbols_and_links_to_insight`（页面文案）已随 `d61203f` 通过。

### CAT-1-lightgbm-price-history（0 项，已清零）
- commit `f14585b`（首页三段断言对齐四步工作台契约）+ `fc04e9f`（`build_model_state` 在有横截面分位时按分位五分位渲染徽标，无分位时保持绝对阈值兜底）。

### CAT-4-insight-model-output-404（0 项，已清零）
- commit `5329982`——`SignalTrainer._future_path_metrics_20d` 新增 20 个交易日后验窗口标签并接入现役/legacy profile，OOS 校准生产者同步产出 20 日键。

### CAT-3 / CAT-6 / CAT-12 / CAT-13 / CAT-2 / CAT-14 / CAT-10 / CAT-11（各 0 项，均已清零）
- CAT-3 `87fbde2`（legacy 报告只读快照 `legacy_unverified`）；CAT-6 `13a9342`/`d496d74`（pipeline 夹具与市场日历）；CAT-12 `d496d74`（CN/OpenBB provider 回退桩）；CAT-13 `5977640`/`6b2aa1d`（AI 日报范围契约）；CAT-2 `c2de781`（watchlist 共享分析链 + Decision Console）；CAT-14 `79cf8cb`（八项回归还原）；CAT-10/CAT-11 `68276bc`（焦点池夹具放量 + 技术评级默认渲染）。

## 3. 附录：完整清单

> 闭合校验：本表 id 数为 **0**，与最新账本 `comparison` 的空失败名集（`acceptance-ledger-20261009T182714-finalfresh.json`）**逐一相等**。

| # | 状态 | 测试 id | 类别 |
|---|---|---|---|
| — | — | （空） | — |

## 4. 边界与未决（E3 口径）

- 本表登记的 E3 豁免边界内存量失败**已全部清零（0 项）**；任何后续新失败都必须另立批次并补充独立证据，**不得**并入本表。
- 分类沿用 R9 triage 既有归因（按测试方法名映射）。
- **代码冻结**：本版依据的收口提交为 `8b0af37`（CAT-7-1）；对应冻结基准 **v43**（`data/artifacts/freeze-20261009-v43`），随本次重算登记于 `acceptance/freeze-manifest-2026-10-05.md`；上一版基准为 v42（`data/artifacts/freeze-20261009-v42`）。
- **不确定项**：`test_watchlist_uses_batched_prediction_queries`（原 CAT-15）本版账本记为通过，但此前被独立复核报告 §5 R7 登记为**顺序/缓存敏感 flake**；单次通过不足以证明已根治，后续轮次若复发仍应归入 CAT-15。
- 本表数量与 **0**（E3）的对应关系由 `scripts/run_acceptance_suite.py` 产出的账本持续校验（最新：`acceptance-ledger-20261009T182714-finalfresh.json` / `.md`）；账本名集差集若出现 `added > 0`，应按新失败另行处理。

## 5. 已知未决缺口（不含在本轮修复内，待 owner 决策）

> 以下为**已知、已定位、但未在本轮收口范围内处理**的缺口。均为**产品/口径决策项或外部数据缺口**，**不计入上表 E3 存量失败口径**（测试集当前全绿，但这些缺口的证据/门禁/数据自洽性仍待处理）。逐项如实登记，未做推测性修复。

1. **CN `oos_evaluation` 仍 FAIL（服务面仍 withhold）**
   - 证据（run 395 CN 实测）：`mean_risk_adjusted_return = -0.01683`（门禁要求 >0，**FAIL**）；`evaluated_date_count = 54`（≥40 通过）；`mean_net_return = +0.00578`（为正，但**不在**门读取的 `_OOS_PERFORMANCE_KEYS` 内）。
   - 后果：`decision=REJECT` / `blocked=true` / `promotable=false`；**CN 服务面仍 withhold**（`list_latest_signal_decisions(market="CN")` = **0 行**；对照 US = 500 行）。
   - 关联：`non_promotable_reasons` 另含 `data_readiness`（NOT_ENOUGH_EVIDENCE）、`statistical_evidence`（NOT_ENOUGH_EVIDENCE）；`corporate_action_coverage` 已随 `9a43c19` 转 PASS。
   - 依据：`tmp/ops-cn-retrain-20261009/SUMMARY.md`。
2. **US 训练仍有 191 行缺口（覆盖率 0.99927 < 1.0）**
   - 训练域「缺失于调整后视图」191 行：**TEVA 112 / GORO 73**（Alpaca IEX 与 SIP **均无** Apr–Sep 2026 数据，单基 Alpaca 无法补齐）；**2026-04-03（Good Friday，非交易日）脏行 3**；**FIRY/HAPN @2026-06-22 共 2**；**SGLD @2026-09-01 共 1**。
   - 后果：`us_signal_train` 仍失败于 `incomplete_adjusted_coverage`（覆盖率 0.99434 → **0.99927406**，门禁要求**恰好 1.0**），**未创建 model_run / 未写预测**。
   - 可选后续（均需显式授权，本轮未执行）：① 对 TEVA/GORO 改用另一数据源补 raw（会成为「多来源 raw」）；② 清理 `us_daily` 非交易日脏行并确认 Apr–Sep 来源有效性；③ 以 `PQW_TRAINER_REQUIRE_FULL_ADJUSTED_COVERAGE=false` 写入如实标注的 mixed-basis 运行。
   - 依据：`tmp/ops-us-20261009/REPORT.md`。
3. **`model_calibration_snapshot` 仍无持久化生产者**
   - `app/api/routes/jobs.py:832` 仅有该 job 的**作业目录描述**（`{"job_type": "model_calibration_snapshot", "description": ...}`），**无实装处理器**。
   - 后果：当前 20d 明细的**唯一来源是本 run 训练窗桶**（`detail_calibration_buckets = oos_calibration_buckets or calibration_buckets`）；当 OOS 快照存在但缺 20d 键（如过期快照）时，逐键回退会使**同一行 5d/20d 期次不同源**（5d 取 OOS、20d 取训练窗）。
   - 待决策：是否补 OOS 校准快照生产者 / 回退口径是否可接受。
4. **`data_readiness` / `statistical_evidence` 证据生产者未接线**
   - 促销门当前对这两项判为 NOT_ENOUGH_EVIDENCE（见缺口 1）。**接线会收紧门禁**（而非放宽），故本轮**未擅自接线**，留待 owner 决策。
5. **PIT 撞键仍为整批回滚（未按记录隔离）**
   - `_apply_pit_universe_filter` 相关撞键处理仍是**整批回滚**语义，**未按记录隔离**；已扫描批次当前 `conflicts_so_far=0`（`tmp/pit_collision_scan3.out`），但**处理路径**未做记录级隔离。
6. **`tests/test_app.py` 的 `TRUNCATE ... CASCADE` 死锁 flaky 会级联污染**
   - `AppFlowTests.setUp` 的 `truncate_test_tables`（`tests/test_app.py:48`）在并发下触发 `psycopg.errors.DeadlockDetected`：**`setUp` 抛错 → unittest 跳过 `tearDown` → `PQW_DATA_DIR` / `PQW_OPTIN_REASON` 泄漏**到后续按字母序执行的模块，造成「下游误报」（本轮 `close` 首轮的 3 项 `added` 即此机制）。
   - 建议（只陈述，未动手）：把 `setUp` 内的环境变量变更改为 `addCleanup`/`patch.dict`，使其在 `setUp` 失败时也能回滚。
7. **已落库 run 395 的 4060 行 `expected_return_20d` 仍为 null**
   - run 395（`cn_close_2026-10-08`）`prediction_details` 4060 行：`expected_return_5d` 有值（4060），`expected_return_20d` / `expected_drawdown_20d` **全 null**（0）。
   - 根因：该 run 走的是**过期 OOS `model_calibration_snapshot`（id=8997，latest_trade_date 2026-06-26）**，其 bucket **无 `next_20d_*` 键**；`af065b2` 只补 5d。**需重训或回填**后才自洽。
8. **（审核残余，未扩围）** `reliability` 元数据仍为单一全局文件；生产旧概率校准产物需下次调度刷新才生成带 scope 版本，在此之前发布估计为「未校准」；`portfolio_book` 账本迁移到结构化表属中期事项。详见 `acceptance/supplements/audit-p1-closure-2026-10-09.md` §「残余与未扩围项」。
