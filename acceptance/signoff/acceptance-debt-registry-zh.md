# 验收债务登记表（存量测试失败，中文）

- 生成日期：2026-10-05（最新重算：**2026-10-07**，**CAT-2 完整分析链 / Decision Console 与 CAT-14 八项回归还原**后重跑）
- 负责人/期限：**已归口（Jacky Hu）**；期限：**下一批次待排期**（下表若干未闭环项统一归口，实际排期以下一批次计划为准）。
- 范围：**E3 范围豁免项**（`acceptance/signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §2 E3）——整改前既有、非本次引入的测试失败。
- 当前基线证据（**CAT-2/CAT-14 收口提交之后的全量重跑**）：
  - 最新全量账本：`tmp/acceptance-cat14/acceptance-ledger-20261007T195733-cat14.json` / `.md`
    （`Ran 1705 / pass 1688 / fail 13 / error 2 / skip 2 / expected_failure 0`）
  - 运行日志：`tmp/acceptance-cat14/regression-with-postgres-acceptance-20261007T195733-cat14.log`
    （SHA256 `34e6cf448f5a026119ec2068156b0f77e7cbff7aceebeca82fe2ab94ee6c3a9a`）
  - 失败/错误名集：**15 项**（全部落在 `tests/test_app.py::AppFlowTests`；账本 `failing_by_module = {"test_app": 15}`）
  - 相对上一版 28 项口径（`tmp/acceptance-cat1/acceptance-ledger-20261007T190140-cat1fix4.json`）逐项比较：
    **added=0 / removed=13 / changed=0 / unchanged_failing=15**
    → **新总数 = `changed + unchanged_failing` = 0 + 15 = 15**；`added=0` 证明非本次整改新引入。
  - 上一版 28 项口径（CAT-1/CAT-4 余项收口时点）：`tmp/acceptance-cat1/acceptance-ledger-20261007T190140-cat1fix4.json`
    （`added=0 / removed=2 / changed=0 / unchanged_failing=28`）。
  - 过程账本（**CAT-4 收尾时点**，30 项之前的上一稿依据）：`tmp/acceptance-cat4/acceptance-ledger-20261007T164843-cat4.json`
    （`added=0 / removed=11 / changed=0 / unchanged_failing=32`，对应上一稿 **32** 项；其后 CAT-1 收尾再移除 2 项 → 30 项）。
  - 更早一版 43 项口径：`data/artifacts/acceptance-20261002/acceptance-ledger-20261007T132008.json`。
  - 分类来源：`data/artifacts/acceptance-20261002/r9-failure-triage-2026-10-03.json`（R9 93 项归因，14 类）。本表只登记当前仍失败的 15 项，逐项按测试方法名映射到 R9 类别。
- 记账口径：**不削弱断言、不删除测试、不改业务代码**。所有未修复项如实登记为未闭环；本表负责人统一归口 **Jacky Hu**、期限统一记为 **下一批次待排期**（未确认前不臆造具体日期）。

## 0. 重算说明（43 → 32 → 30 → 28 → 15）

- **43 项口径**：以账本 `acceptance-ledger-20261007T132008.json`（`Ran 1705 / pass 1660 / fail 35 / error 8 / skip 2`）为准——失败 **43** 项。
- **CAT-4 收尾（过程账本，32 项）**：账本 `tmp/acceptance-cat4/acceptance-ledger-20261007T164843-cat4.json`——相对 43 项口径 `added=0 / removed=11 / changed=0 / unchanged_failing=32`。移除的 11 项 = `CAT-4` 10 项（模型输出读路径三层回退 + 白名单放宽，commit `d278f13`）+ `CAT-1` 1 项（`test_sample_workflow_populates_dashboard_and_symbol_pages`，随 `d278f13` 通过）。
- **CAT-1 收尾（30 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T172735-cat1.json`——相对 43 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=30`。移除的 2 项为 `test_dashboard_continuous_leaders_supports_execution_tag_filters`、`test_execution_tag_filters_support_multiple_tags`，均由 commit `cfc0320` 修复（`_lightweight_market_context` 不再硬编码 `continuous_leaders: []`，改为按需复用 `build_continuous_leaders_snapshot` 并补齐 trade-plan `execution_tags`）。
- **CAT-1/CAT-4 余项收口（28 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T190140-cat1fix4.json`——相对上一版 30 项口径 `added=0 / removed=2 / changed=0 / unchanged_failing=28`。
  - 相对上一版 30 项：**−2 项、0 项新增**：
    - `test_dashboard_continuous_leader_action_adds_watchlist_item`（CAT-1）：首页连续强势 CTA 文案规范化为 `连续强势股` / `Continuous Leaders`（commit **`bedec99`**）后通过。
    - `test_qlib_predictor_imports_artifact_trade_plan`（CAT-4）：`_load_concept_tracker_rows` 增加第三级回退（commit **`ba188c4`**）后通过。
- **本版（CAT-2/CAT-14 收口后，15 项）**：账本 `tmp/acceptance-cat14/acceptance-ledger-20261007T195733-cat14.json`——相对上一版 28 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=15`。
  - 相对上一版 28 项：**−13 项、0 项新增**（逐项比对 28 个 id 与最新失败名集，差集单向缩小且新增为空）：
    - **CAT-2 5 项**（commit **`c2de781`**，`fix(watchlist): 接入完整分析链与 Decision Console（CAT-2 5 项）`）：watchlist 分析片段接入共享分析链 `safe_symbol_analysis` / `build_symbol_decision_brief` / `AIAnalysisService`，并以 lightweight stub 兜底，patch 期不再因符号缺失 `AttributeError`——
      `test_watchlist_analysis_fragment_is_cached_between_requests`、`test_watchlist_analysis_fragment_renders_decision_console`、`test_watchlist_page_limits_full_analysis_to_top_candidates`、`test_watchlist_page_renders_ai_briefs_for_top_ranked_names`、`test_watchlist_page_renders_decision_console`。
    - **CAT-14 8 项**（commit **`79cf8cb`**，`fix(dashboard): CAT-14 八项回归还原（symbols/predictions/screener_pages/watchlist-exchange/market_risk/jobs + 夹具）`）：`symbols.py` 恢复 `fetch_symbol_headlines`、`predictions.py` 候选 run 允许 `market in {market,"ALL"}` 且最新交易日按 market 过滤、`screener_pages.py` 快照页词表还原、watchlist Market 单元格追加 exchange 副行、`market_risk.py` 空 lake 返回空态而非抛 IO Error、`jobs.py` send-ai-daily-report 传 `trusted_regime_snapshots`，并还原 `tests/test_app.py` 四处夹具（相对日期 / 工作区快照 / 日报夹具 / 900 日历史 + reversal→momentum）——
      `test_market_snapshot_page_renders_boards`、`test_prediction_repository_uses_latest_trade_date_per_market`、`test_refresh_cn_market_data_incremental_uses_last_synced_date_window`、`test_refresh_existing_watchlist_metadata_uses_live_profile_for_us_stock`、`test_run_pipeline_redirects_back_to_dashboard`、`test_send_ai_daily_report_endpoint_uses_notifier`、`test_symbol_page_bundle_endpoint_returns_combined_sections`、`test_symbol_page_bundle_is_cached_between_requests`。
  - **changed 口径**：`changed=0` 指相对 28 项基线无“同测试类型变化”；余下 15 项仍为持续 `FAIL`/`ERROR`，无 `error → fail` 或反向变化成员。
  - **removed 口径**：`removed=13` 为相对**上一版 28 项**清单（非相对整改前基线）；因 `added=0`，不触发“`added > 0` 必须另行处置”的规则。

## 1. 汇总

| 类别（r9 triage id） | 数量 | 状态类型 | 负责人 | 期限 | 依据 |
|---|---|---|---|---|---|
| CAT-1-lightgbm-price-history | 4 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-1；E3（**产品语义变更待决策**，见 §2） |
| CAT-12-openbb-provider-history | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-12；E3 |
| CAT-13-ai-daily-report-scope | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-13；E3 |
| CAT-3-legacy-report-freeze-conflict | 2 | ERROR | Jacky Hu | 下一批次待排期 | r9 triage CAT-3；E3 |
| CAT-6-job-pipeline-failed | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-6；E3 |
| CAT-7-watchlist-add-message-and-ui | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-7；E3 |
| CAT-4-insight-model-output-404 | 1 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-4；E3（**20 日标签未实现待决策**，见 §2） |
| CAT-2-watchlist-full-analysis-stub | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-2；commit `c2de781` |
| CAT-14-misc-regressions | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-14；commit `79cf8cb` |
| CAT-10-screener-cn-template-and-csv | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-10；commit `68276bc` |
| CAT-11-screener-watchlist-actions-and-tv | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-11；commit `68276bc` |
| **合计** | **15** | FAIL 13 / ERROR 2 | — | — | — |

> 计数校验：4+2+2+2+2+2+1+0+0+0+0 = **15**；状态合计 FAIL 13 / ERROR 2，与账本 `fail 13 / error 2` 一致。
> **待决策项标注**：**CAT-1 剩 4 项 = 产品语义变更待决策**；**CAT-4 剩 1 项 = 20 日标签未实现待决策**。两者均需先做产品/口径决策，再另立批次处理（未决策前不动断言、不删测试）。
> `CAT-5`（并发编辑阻塞）、`CAT-8`/`CAT-9`（triage 中已修复）、`CAT-10`/`CAT-11`（已清零）、`CAT-2`/`CAT-14`（本版清零）、`CAT-15`（顺序敏感 flake）在当前名集中不出现。

## 2. 分类明细

### CAT-1-lightgbm-price-history（4 项，全部 FAIL）——归因：**重构漂移**；处置：**产品语义变更待决策**
- 完整成员（附录 #1–#4）：
  `test_dashboard_page_supports_chinese_language`、`test_dashboard_page_supports_lookback_snapshot_window`、`test_dashboard_watchlist_derived_context_is_cached_between_requests`、`test_screener_page_filters_watchlist_rules`
- **缩编说明**：上一版 5 项中，1 项已通过——`test_dashboard_continuous_leader_action_adds_watchlist_item`（随 `bedec99`，首页 CTA 文案规范化为 `连续强势股` / `Continuous Leaders`）。
- **待决策说明**：余下 4 项均为**页面文案/查询语义**类断言。修与不修取决于产品口径——需先确认是「按新语义同步更新测试断言」还是「保留旧文案/区块」，属**产品语义变更待决策**（决策前保持现状，不削弱断言、不删除测试、不改业务代码）。
- 证据（断言漂移，行号以最新账本 traceback 为准，与上一版一致）：
  - `:584` `test_dashboard_page_supports_lookback_snapshot_window` —— `AssertionError: 'Snapshot Window' not found`（重构后 dashboard HTML 不再含该文案/区块）。
  - `:601` `test_dashboard_page_supports_chinese_language` —— `AssertionError: '个人量化工作台' not found`（中文文案断言失配）。
  - `:711` `test_dashboard_watchlist_derived_context_is_cached_between_requests` —— `AssertionError: 1 != 0`（`WatchlistRepository.list_ticker_map` 期望 1 次实际 0 次：查询层重构后调用点变化）。
  - `:3524` `test_screener_page_filters_watchlist_rules` —— `AssertionError: False is not true`（`Strong/Positive/Weak` 规则文案在重构后 screener 页不出现）。
- 结论：属**页面/查询层契约重构**导致的断言漂移（既有测试断言未随重构同步）；**是否按新语义更新断言未决**，待产品决策后另立批次。
- 备注：旧版 CAT-1 22 项中 18 项此前已通过（removed），本轮与前两轮共收窄至 4 项。

### CAT-4-insight-model-output-404（1 项，FAIL）——**收尾后余项**；处置：**20 日标签未实现待决策**
- 完整成员（附录 #15）：`test_insight_model_output_endpoint_and_page`
- 上一版 2 项中 1 项（`test_qlib_predictor_imports_artifact_trade_plan`）已随 `ba188c4` 通过（概念跟踪导出三级回退）。余 1 项性质如下（**非**原 404/白名单根因）：
  1. `test_insight_model_output_endpoint_and_page`（`tests/test_app.py:2647`）：`AssertionError: unexpectedly None` —— `payload["expected_drawdown_20d"]` 为空。读路径已可读到该票的模型输出，但 **`expected_drawdown_20d`（20 日标签/明细字段）在导入/写链路上仍未落地**（字段级缺口，非路由 404）。该字段是否落地、以何口径生成属**产品决策**，故标注为**20 日标签未实现待决策**。
- 结论：原归因（qlib `model_type` 不在读取白名单 → 路由 404）已闭环；余 1 项属**字段级缺口**，待决策后另立小批次处理。

### CAT-3-legacy-report-freeze-conflict（2 项，全部 ERROR）
- 样例测试：`test_dashboard_ai_daily_report_page_renders`、`test_dashboard_ai_daily_report_message_page_renders`
- 依据（代码）：`app/services/ai_daily_report.py:2210`（`save_ai_daily_report` 无条件调用 `freeze_final_decisions`）——legacy 结构被主动 fail-closed，测试仍按 legacy 结构断言。

### CAT-6-job-pipeline-failed（2 项，全部 FAIL）
- 样例测试：`test_run_pipeline_job_writes_job_model_and_backtest`、`test_auto_analysis_settings_and_manual_run`
- 依据（代码）：`app/api/routes/jobs.py:2559`（`run_pipeline` 调用 `train` 未传 `market`）。

### CAT-7-watchlist-add-message-and-ui（2 项，全部 FAIL）
- 样例测试：`test_watchlist_add_and_sync_now_works`、`test_watchlist_page_adds_symbols_and_links_to_insight`
- 依据：r9 triage 记录提示语已部分修复（`app/api/routes/watchlist.py:1758/1765`），剩余为 HK 同步状态（S-13）与页面文案断言两处独立原因。

### CAT-12-openbb-provider-history（2 项，全部 FAIL）
- 样例测试：`test_cn_history_prefers_akshare_before_yfinance`、`test_openbb_permission_error_falls_back_to_yfinance_history`
- 依据：`tests/test_app.py:5432/5466`——`OpenBBClient.fetch_historical_prices` 未走打桩的 provider 回退分支（实际返回 125 行而非 1 行）。

### CAT-13-ai-daily-report-scope（2 项，全部 FAIL）
- 样例测试：`test_ai_daily_report_defaults_to_cn_market_scope`、`test_ai_daily_report_custom_tickers_still_use_explicit_scope`
- 依据：`tests/test_app.py:6462/6464/6527`——`build_ai_daily_report` 的 scope 归因与期望不符。

### CAT-2-watchlist-full-analysis-stub（0 项，**已清零**）
- 本版清零：commit `c2de781` 在 `app/api/routes/watchlist.py` 接入共享分析链（模块级导入 `safe_symbol_analysis` / `build_symbol_decision_brief` / `AIAnalysisService`，新增 `_watchlist_overview_from_item` / `_full_watchlist_analysis` / `_watchlist_ai_headline` 与 Decision Console 面板），patch 期旧符号重新可解析，原 5 项 ERROR 全部转通过。
- 历史依据（清零前）：`app/api/routes/watchlist.py:221`（`_lightweight_watchlist_analysis`）——测试 patch 的符号 `safe_symbol_analysis` / `build_symbol_decision_brief` 当时不再被模块引用，patch 期 `AttributeError`。

### CAT-14-misc-regressions（0 项，**已清零**）
- 本版清零：commit `79cf8cb` 还原八项独立回归（`symbols.py` 恢复 `fetch_symbol_headlines`；`predictions.py` 候选 run 允许 `market in {market,"ALL"}` 且最新交易日按 market 过滤；`screener_pages.py` 快照页词表；watchlist Market 单元格 exchange 副行；`market_risk.py` 空 lake 空态；`jobs.py` send-ai-daily-report 传 `trusted_regime_snapshots`），并还原 `tests/test_app.py` 四处夹具。
- 历史依据：r9 triage CAT-14——无统一验收项的独立回归（S-11/S-14 等）。

### CAT-10-screener-cn-template-and-csv（0 项，**已清零**）/ CAT-11-screener-watchlist-actions-and-tv（0 项，**已清零**）
- 两类在 commit `68276bc`（焦点池夹具放量 + 技术评级默认渲染）清零，当前账本名集中不出现。

## 3. 附录：15 项完整清单

> 闭合校验：本表 15 个 id 与账本 `comparison.changed ∪ comparison.unchanged_failing`（`acceptance-ledger-20261007T195733-cat14.json`）**逐一相等**（added=0，无账本外条目）；状态列取自账本 `tests[].status`。

| # | 状态 | 测试 id | 类别 |
|---|---|---|---|
| 1 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_chinese_language` | CAT-1 |
| 2 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_lookback_snapshot_window` | CAT-1 |
| 3 | FAIL | `test_app.AppFlowTests.test_dashboard_watchlist_derived_context_is_cached_between_requests` | CAT-1 |
| 4 | FAIL | `test_app.AppFlowTests.test_screener_page_filters_watchlist_rules` | CAT-1 |
| 5 | ERROR | `test_app.AppFlowTests.test_dashboard_ai_daily_report_message_page_renders` | CAT-3 |
| 6 | ERROR | `test_app.AppFlowTests.test_dashboard_ai_daily_report_page_renders` | CAT-3 |
| 7 | FAIL | `test_app.AppFlowTests.test_auto_analysis_settings_and_manual_run` | CAT-6 |
| 8 | FAIL | `test_app.AppFlowTests.test_run_pipeline_job_writes_job_model_and_backtest` | CAT-6 |
| 9 | FAIL | `test_app.AppFlowTests.test_watchlist_add_and_sync_now_works` | CAT-7 |
| 10 | FAIL | `test_app.AppFlowTests.test_watchlist_page_adds_symbols_and_links_to_insight` | CAT-7 |
| 11 | FAIL | `test_app.AppFlowTests.test_cn_history_prefers_akshare_before_yfinance` | CAT-12 |
| 12 | FAIL | `test_app.AppFlowTests.test_openbb_permission_error_falls_back_to_yfinance_history` | CAT-12 |
| 13 | FAIL | `test_app.AppFlowTests.test_ai_daily_report_custom_tickers_still_use_explicit_scope` | CAT-13 |
| 14 | FAIL | `test_app.AppFlowTests.test_ai_daily_report_defaults_to_cn_market_scope` | CAT-13 |
| 15 | FAIL | `test_app.AppFlowTests.test_insight_model_output_endpoint_and_page` | CAT-4 |

## 4. 边界与未决

- 本表登记的是 **E3 豁免边界内的存量失败**，不代表验收通过；任何修复都必须另立批次并补充独立证据。
- 分类沿用 R9 triage 的既有归因（按测试方法名映射）；**`CAT-1` 保持「重构漂移」归因**（见 §2）并标注**产品语义变更待决策**，**`CAT-4` 余 1 项标注 20 日标签未实现待决策**，其余类别单项根因以 R9 triage 为准。
- **代码冻结**：本版重算依据的收口提交为 `c2de781`（watchlist 完整分析链 + Decision Console，CAT-2）与 `79cf8cb`（CAT-14 八项回归还原）；对应冻结基准 **v39**（`data/artifacts/freeze-20261005-v39`，`scripts/freeze_quant_remediation_baseline.py --output-dir data/artifacts/freeze-20261005-v39 --snapshot-untracked`）随本次重算登记于 `acceptance/freeze-manifest-2026-10-05.md`；上一版基准为 v38（`data/artifacts/freeze-20261005-v38`）。
- **已闭环子因**（不计入上表）：
  - watchlist 分析片段/Decision Console 的共享分析链（CAT-2）已在 `c2de781` 接入并以 lightweight stub 兜底。
  - CAT-14 八项独立回归已在 `79cf8cb` 还原（symbols/predictions/screener_pages/watchlist-exchange/market_risk/jobs + 夹具）。
  - 连续强势页/导出行集为空（`_lightweight_market_context` 硬编码空列表）已在 `cfc0320` 修复——改为按需复用 `build_continuous_leaders_snapshot`（并补齐 trade-plan `execution_tags`），无快照时页面与导出行为一致。
  - 首页连续强势 CTA 文案与页面首屏契约不一致（小写 `Continuous leaders`）已在 `bedec99` 规范化。
  - 概念跟踪页面/导出在生产信号缺失时为空已在 `ba188c4` 修复——`_load_concept_tracker_rows` 增加第三级回退 `_concept_tracker_for_summary`（按需由最新成功运行构造 concept tracker）。
- **不确定项**：`test_watchlist_uses_batched_prediction_queries`（原 CAT-15）本版账本仍记为通过（removed），但该测试此前被独立复核报告 §5 R7 登记为**顺序/缓存敏感 flake**；单次通过不足以证明已根治，后续轮次若复发仍应归入 CAT-15。其余 15 项均能与 R9 类别逐一对应，无归类困难项。
- 负责人已归口 **Jacky Hu**、期限统一记为 **下一批次待排期**；实际进入排期以项目方下一批次计划为准（未确认前不臆造具体日期）。
- 本表数量与 **15** 的对应关系由 `scripts/run_acceptance_suite.py` 产出的账本持续校验（最新：`acceptance-ledger-20261007T195733-cat14.json` / `.md`）；账本与上一版名集差集若出现 `added > 0`，应按新失败另行处理，不得并入本表。
