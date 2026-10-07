# 验收债务登记表（存量测试失败，中文）

- 生成日期：2026-10-05（最新重算：**2026-10-07**；对齐代码冻结基准 **v36**，本轮冻结产物 `data/artifacts/freeze-20261005-v36`，见 §4）
- 负责人/期限：**已归口（Jacky Hu）**；期限：**下一批次待排期**（下表若干未闭环项统一归口，实际排期以下一批次计划为准）。
- 范围：**E3 范围豁免项**（`acceptance/signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §2 E3）——整改前既有、非本次引入的测试失败。
- 当前基线证据（**焦点池夹具放量 + 技术评级默认渲染批次后，commit `68276bc`**）：
  - 最新全量账本：`data/artifacts/acceptance-20261002/acceptance-ledger-20261007T132008.json` / `.md`
    （`Ran 1705 / pass 1660 / fail 35 / error 8 / skip 2 / expected_failure 0`）
  - 运行日志：`data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261007T132008.log`
    （SHA256 `d49b37ce7f73e61149d7e04ccc583d836357d6de85e94d11a5bc5acaf38baa63`）
  - 失败/错误名集：**43 项**（全部落在 `tests/test_app.py::AppFlowTests`；账本 `failing_by_module = {"test_app": 43}`）
  - 相对整改前基线 `data/artifacts/acceptance-20261002/regression-with-postgres-final.log`
    （`Ran 1045 / FAIL 45 / ERROR 49`，解析失败名 94；SHA256 `d0da8bea5e5593ceb48119efd1b27d6d15fa69f6cc9460170d682e1ecd790d35`）逐项比较：**added=0 / removed=51 / changed=8 / unchanged_failing=35**
    → **新总数 = `changed + unchanged_failing` = 8 + 35 = 43**；`added=0` 证明非本次整改新引入。
  - 分类来源：`data/artifacts/acceptance-20261002/r9-failure-triage-2026-10-03.json`（R9 93 项归因，14 类）。本表只登记当前仍失败的 43 项，逐项按测试方法名映射到 R9 类别。
- 记账口径：**不削弱断言、不删除测试、不改业务代码**。所有未修复项如实登记为未闭环；本表负责人统一归口 **Jacky Hu**、期限统一记为 **下一批次待排期**（未确认前不臆造具体日期）。

## 0. 与上一版 45 项的差异（重算说明）

- **上一版（45 项口径）**：以账本 `acceptance-ledger-20261007T094835-dimagent-live-fallback-d2.json`（`Ran 1705 / pass 1658 / fail 37 / error 8 / skip 2`）为准——失败 **45** 项、**11** 类。
- **本版（最新账本口径）**：`acceptance-ledger-20261007T132008.json`，`added=0 / removed=51 / changed=8 / unchanged_failing=35` → 仍失败 **43** 项、**9** 类（CAT-10/CAT-11 已清零，不再计入类别）。
- **相对上一版 45 项：−2 项、0 项新增**（逐项比对上一版附录 45 个 id 与最新失败名集，差集仅单向缩小且新增为空）：
  - `CAT-10` 1 → **0**（−1）：`test_screener_can_add_current_results_to_today_focus_pool` 本轮通过（焦点池夹具放量确认，`tests/test_app.py::test_screener_can_add_current_results_to_today_focus_pool` 供给放量新会话）。
  - `CAT-11` 1 → **0**（−1）：`test_screener_page_renders_tradingview_multi_timeframe_ratings` 本轮通过（技术评级默认渲染 `_row_technical_rating_html`，`app/api/presentation/screener_components.py` + `screener_main.py`）。
  - 其余 9 类计数不变：`CAT-1` 8、`CAT-2` 5、`CAT-3` 2、`CAT-4` 12、`CAT-6` 2、`CAT-7` 2、`CAT-12` 2、`CAT-13` 2、`CAT-14` 8。
  - 合计 45 − 1 − 1 = **43**。
- **changed 口径（相对整改前基线）**：`changed=8` 全部为**基线 `error` → 本次 `fail`** 的类型变化（同一测试仍失败，仅失败形态变化），且这 8 项**恰好构成当前 `CAT-1` 的全部成员**，故 `CAT-1` 本轮状态全为 FAIL。
- **unchanged_failing 口径**：`unchanged_failing=35` 为基线/本次同名同类型的持续失败项（fail 27 / error 8），与 `CAT-2`/`CAT-3`/`CAT-14` 的 8 个 ERROR 项及各 FAIL 类共同构成剩余 43 项。
- **removed 口径**：`removed=51` 指相对**整改前基线**（非相对上一版 45 项清单）已通过 51 项；因 `added=0`，不触发“`added > 0` 必须另行处置”的规则。

## 1. 汇总

| 类别（r9 triage id） | 数量 | 状态类型 | 负责人 | 期限 | 依据 |
|---|---|---|---|---|---|
| CAT-1-lightgbm-price-history | 8 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-1；E3（本轮归因改为**重构漂移**，见 §2） |
| CAT-4-insight-model-output-404 | 12 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-4；E3 |
| CAT-14-misc-regressions | 8 | FAIL 7 / ERROR 1 | Jacky Hu | 下一批次待排期 | r9 triage CAT-14；E3 |
| CAT-2-watchlist-full-analysis-stub | 5 | ERROR | Jacky Hu | 下一批次待排期 | r9 triage CAT-2；E3 |
| CAT-12-openbb-provider-history | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-12；E3 |
| CAT-13-ai-daily-report-scope | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-13；E3 |
| CAT-3-legacy-report-freeze-conflict | 2 | ERROR | Jacky Hu | 下一批次待排期 | r9 triage CAT-3；E3 |
| CAT-6-job-pipeline-failed | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-6；E3 |
| CAT-7-watchlist-add-message-and-ui | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-7；E3 |
| CAT-10-screener-cn-template-and-csv | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-10；本轮通过（commit `68276bc`） |
| CAT-11-screener-watchlist-actions-and-tv | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-11；本轮通过（commit `68276bc`） |
| **合计** | **43** | FAIL 35 / ERROR 8 | — | — | — |

> 计数校验：8+12+8+5+2+2+2+2+2+0+0 = **43**；状态合计 FAIL 35 / ERROR 8，与账本 `fail 35 / error 8` 一致。
> `CAT-5`（并发编辑阻塞）、`CAT-8`/`CAT-9`（triage 中已修复）、`CAT-15`（顺序敏感 flake）在当前名集中不出现；`CAT-10`/`CAT-11` 已在本批次清零（数量 0）。

## 2. 分类明细

### CAT-1-lightgbm-price-history（8 项，全部 FAIL）——归因：**重构漂移**
- 完整成员（即附录 #1–#8，与账本 `changed=8` 完全重合）：
  `test_dashboard_continuous_leader_action_adds_watchlist_item`、`test_dashboard_continuous_leaders_supports_execution_tag_filters`、`test_dashboard_page_supports_chinese_language`、`test_dashboard_page_supports_lookback_snapshot_window`、`test_dashboard_watchlist_derived_context_is_cached_between_requests`、`test_execution_tag_filters_support_multiple_tags`、`test_sample_workflow_populates_dashboard_and_symbol_pages`、`test_screener_page_filters_watchlist_rules`
- **归因修正（相对旧版）**：旧版将 CAT-1 归因为“训练门槛与测试夹具不匹配（夹具每票 5–10 个交易日不足）”。**本版改判为「重构漂移」**：这 8 项在整改前基线均为 `error`（setup/夹具异常形态）、本轮均为 `fail`（断言不匹配形态），失败签名已从“样本/夹具供给不足”变为“重构后页面渲染契约与既有断言漂移”，故“夹具不足”不再是其根因。
- 证据（断言漂移，`tests/test_app.py` 行号以失败日志 traceback 为准）：
  - `:354` `test_sample_workflow_populates_dashboard_and_symbol_pages` —— 重构后 dashboard/symbol 页面结构断言失配。
  - `:583` `test_dashboard_page_supports_lookback_snapshot_window` —— `AssertionError: 'Snapshot Window' not found`（重构后 dashboard HTML 不再含该文案/区块）。
  - `:600` `test_dashboard_page_supports_chinese_language` —— 中文文案断言失配。
  - `:710` `test_dashboard_watchlist_derived_context_is_cached_between_requests` —— 派生上下文缓存调用次数 `1 != 0`（查询层重构后调用点变化）。
  - `:1500` `test_execution_tag_filters_support_multiple_tags` —— 多标签 execution tag 过滤断言失配。
  - `:2089` `test_dashboard_continuous_leader_action_adds_watchlist_item` —— 连续龙头加自选动作断言失配。
  - `:2250` `test_dashboard_continuous_leaders_supports_execution_tag_filters` —— `'/insights/AAPL?lang=en'` 链接不在重构后的页面。
  - `:3517` `test_screener_page_filters_watchlist_rules` —— screener watchlist 规则过滤断言失配。
- 结论：属**页面/查询层契约重构**导致的断言漂移（既有测试断言未随重构同步），仍未修复；不再沿用“训练门槛与夹具不匹配”的旧归因。
- 备注：旧版 CAT-1 22 项中 14 项此前已通过（removed），本轮维持余下 8 项。

### CAT-2-watchlist-full-analysis-stub（5 项，全部 ERROR）
- 样例测试：`test_watchlist_analysis_fragment_is_cached_between_requests`、`test_watchlist_page_renders_decision_console`
- 依据（代码）：`app/api/routes/watchlist.py:221`（`_lightweight_watchlist_analysis`）——测试 patch 的旧符号 `safe_symbol_analysis` / `build_symbol_decision_brief` 已不再被模块引用，patch 期 `AttributeError`。

### CAT-3-legacy-report-freeze-conflict（2 项，全部 ERROR）
- 样例测试：`test_dashboard_ai_daily_report_page_renders`、`test_dashboard_ai_daily_report_message_page_renders`
- 依据（代码）：`app/services/ai_daily_report.py:2210`（`save_ai_daily_report` 无条件调用 `freeze_final_decisions`）——legacy 结构被主动 fail-closed，测试仍按 legacy 结构断言。

### CAT-4-insight-model-output-404（12 项，全部 FAIL）
- 样例测试：`test_insight_model_output_endpoint_and_page`、`test_qlib_predictor_imports_prediction_csv`
- 依据（代码）：`app/services/repositories/shared.py:26`（`PRODUCTION_SIGNAL_MODEL_TYPES = ('lightgbm_multifactor',)`）——qlib 导入的 `model_type` 不在读取白名单，路由返回 404。

### CAT-6-job-pipeline-failed（2 项，全部 FAIL）
- 样例测试：`test_run_pipeline_job_writes_job_model_and_backtest`、`test_auto_analysis_settings_and_manual_run`
- 依据（代码）：`app/api/routes/jobs.py:2559`（`run_pipeline` 调用 `train` 未传 `market`）。

### CAT-7-watchlist-add-message-and-ui（2 项，全部 FAIL）
- 样例测试：`test_watchlist_add_and_sync_now_works`、`test_watchlist_page_adds_symbols_and_links_to_insight`
- 依据：r9 triage 记录提示语已部分修复（`app/api/routes/watchlist.py:1758/1765`），剩余为 HK 同步状态（S-13）与页面文案断言两处独立原因。

### CAT-10-screener-cn-template-and-csv（0 项，**已清零**）
- 上一项：`test_screener_can_add_current_results_to_today_focus_pool`（上一版 45 项口径中唯一成员）。
- **本轮通过**：焦点池就绪门槛（readiness ≥ 60）保持不变，夹具改为供给放量新会话（末根成交量约为 20 日均量 5 倍），候选凭自身行情数据过门槛（`tests/test_app.py`，commit `68276bc`）。
- 状态：本类别在当前账本名集中不出现，数量归零。

### CAT-11-screener-watchlist-actions-and-tv（0 项，**已清零**）
- 上一项：`test_screener_page_renders_tradingview_multi_timeframe_ratings`（上一版 45 项口径中唯一成员）。
- **本轮通过**：恢复结果页每行的 `技术评级` 默认渲染（`_row_technical_rating_html`，`app/api/presentation/screener_components.py` + `app/api/presentation/screener_main.py`，commit `68276bc`）。
- 状态：本类别在当前账本名集中不出现，数量归零。

### CAT-12-openbb-provider-history（2 项，全部 FAIL）
- 样例测试：`test_cn_history_prefers_akshare_before_yfinance`、`test_openbb_permission_error_falls_back_to_yfinance_history`
- 依据：`tests/test_app.py:5432/5466`——`OpenBBClient.fetch_historical_prices` 未走打桩的 provider 回退分支（实际返回 125 行而非 1 行）。

### CAT-13-ai-daily-report-scope（2 项，全部 FAIL）
- 样例测试：`test_ai_daily_report_defaults_to_cn_market_scope`、`test_ai_daily_report_custom_tickers_still_use_explicit_scope`
- 依据：`tests/test_app.py:6462/6464/6527`——`build_ai_daily_report` 的 scope 归因与期望不符。

### CAT-14-misc-regressions（8 项：FAIL 7 / ERROR 1）
- 样例测试：`test_market_snapshot_page_renders_boards`、`test_prediction_repository_uses_latest_trade_date_per_market`、`test_symbol_page_bundle_endpoint_returns_combined_sections`（ERROR）
- 依据：r9 triage CAT-14——其余无统一验收项的独立回归（S-11/S-14 等）。

## 3. 附录：43 项完整清单

> 闭合校验：本表 43 个 id 与账本 `comparison.changed ∪ comparison.unchanged_failing`（`acceptance-ledger-20261007T132008.json`）**逐一相等**（added=0，无账本外条目）；状态列取自账本 `tests[].status`。

| # | 状态 | 测试 id | 类别 |
|---|---|---|---|
| 1 | FAIL | `test_app.AppFlowTests.test_dashboard_continuous_leader_action_adds_watchlist_item` | CAT-1 |
| 2 | FAIL | `test_app.AppFlowTests.test_dashboard_continuous_leaders_supports_execution_tag_filters` | CAT-1 |
| 3 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_chinese_language` | CAT-1 |
| 4 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_lookback_snapshot_window` | CAT-1 |
| 5 | FAIL | `test_app.AppFlowTests.test_dashboard_watchlist_derived_context_is_cached_between_requests` | CAT-1 |
| 6 | FAIL | `test_app.AppFlowTests.test_execution_tag_filters_support_multiple_tags` | CAT-1 |
| 7 | FAIL | `test_app.AppFlowTests.test_sample_workflow_populates_dashboard_and_symbol_pages` | CAT-1 |
| 8 | FAIL | `test_app.AppFlowTests.test_screener_page_filters_watchlist_rules` | CAT-1 |
| 9 | ERROR | `test_app.AppFlowTests.test_watchlist_analysis_fragment_is_cached_between_requests` | CAT-2 |
| 10 | ERROR | `test_app.AppFlowTests.test_watchlist_analysis_fragment_renders_decision_console` | CAT-2 |
| 11 | ERROR | `test_app.AppFlowTests.test_watchlist_page_limits_full_analysis_to_top_candidates` | CAT-2 |
| 12 | ERROR | `test_app.AppFlowTests.test_watchlist_page_renders_ai_briefs_for_top_ranked_names` | CAT-2 |
| 13 | ERROR | `test_app.AppFlowTests.test_watchlist_page_renders_decision_console` | CAT-2 |
| 14 | ERROR | `test_app.AppFlowTests.test_dashboard_ai_daily_report_message_page_renders` | CAT-3 |
| 15 | ERROR | `test_app.AppFlowTests.test_dashboard_ai_daily_report_page_renders` | CAT-3 |
| 16 | FAIL | `test_app.AppFlowTests.test_import_model_output_job_populates_prediction_details` | CAT-4 |
| 17 | FAIL | `test_app.AppFlowTests.test_insight_model_output_endpoint_and_page` | CAT-4 |
| 18 | FAIL | `test_app.AppFlowTests.test_insight_page_renders_when_model_output_is_missing` | CAT-4 |
| 19 | FAIL | `test_app.AppFlowTests.test_qlib_prediction_adapter_imports_rows` | CAT-4 |
| 20 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_artifact_explanations` | CAT-4 |
| 21 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_artifact_trade_plan` | CAT-4 |
| 22 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_native_result_bundle` | CAT-4 |
| 23 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_prediction_csv` | CAT-4 |
| 24 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_artifact_manifest_metadata` | CAT-4 |
| 25 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_predictions_csv_from_artifact_directory` | CAT-4 |
| 26 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_predictions_json_from_artifact_directory` | CAT-4 |
| 27 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_predictions_jsonl_from_artifact_directory` | CAT-4 |
| 28 | FAIL | `test_app.AppFlowTests.test_auto_analysis_settings_and_manual_run` | CAT-6 |
| 29 | FAIL | `test_app.AppFlowTests.test_run_pipeline_job_writes_job_model_and_backtest` | CAT-6 |
| 30 | FAIL | `test_app.AppFlowTests.test_watchlist_add_and_sync_now_works` | CAT-7 |
| 31 | FAIL | `test_app.AppFlowTests.test_watchlist_page_adds_symbols_and_links_to_insight` | CAT-7 |
| 32 | FAIL | `test_app.AppFlowTests.test_cn_history_prefers_akshare_before_yfinance` | CAT-12 |
| 33 | FAIL | `test_app.AppFlowTests.test_openbb_permission_error_falls_back_to_yfinance_history` | CAT-12 |
| 34 | FAIL | `test_app.AppFlowTests.test_ai_daily_report_custom_tickers_still_use_explicit_scope` | CAT-13 |
| 35 | FAIL | `test_app.AppFlowTests.test_ai_daily_report_defaults_to_cn_market_scope` | CAT-13 |
| 36 | FAIL | `test_app.AppFlowTests.test_market_snapshot_page_renders_boards` | CAT-14 |
| 37 | FAIL | `test_app.AppFlowTests.test_prediction_repository_uses_latest_trade_date_per_market` | CAT-14 |
| 38 | FAIL | `test_app.AppFlowTests.test_refresh_cn_market_data_incremental_uses_last_synced_date_window` | CAT-14 |
| 39 | FAIL | `test_app.AppFlowTests.test_refresh_existing_watchlist_metadata_uses_live_profile_for_us_stock` | CAT-14 |
| 40 | FAIL | `test_app.AppFlowTests.test_run_pipeline_redirects_back_to_dashboard` | CAT-14 |
| 41 | FAIL | `test_app.AppFlowTests.test_send_ai_daily_report_endpoint_uses_notifier` | CAT-14 |
| 42 | ERROR | `test_app.AppFlowTests.test_symbol_page_bundle_endpoint_returns_combined_sections` | CAT-14 |
| 43 | FAIL | `test_app.AppFlowTests.test_symbol_page_bundle_is_cached_between_requests` | CAT-14 |

## 4. 边界与未决

- 本表登记的是 **E3 豁免边界内的存量失败**，不代表验收通过；任何修复都必须另立批次并补充独立证据。
- 分类沿用 R9 triage 的既有归因（按测试方法名映射）；**`CAT-1` 本轮已按失败签名改判为「重构漂移」**（见 §2），`CAT-14` 仍为混合桶，单项根因以 R9 triage 为准。
- **代码冻结**：本版对齐 **v36** 冻结产物 `data/artifacts/freeze-20261005-v36`（`scripts/freeze_quant_remediation_baseline.py --output-dir data/artifacts/freeze-20261005-v36 --snapshot-untracked`）；五项哈希与工作树状态见该目录冻结清单。
- **不确定项**：`test_watchlist_uses_batched_prediction_queries`（原 CAT-15）本轮账本记为通过（removed），但该测试此前被独立复核报告 §5 R7 登记为**顺序/缓存敏感 flake**；其单次通过不足以证明已根治，若后续轮次复发仍应归入 CAT-15。其余 43 项均能与 R9 类别逐一对应，无归类困难项。
- 负责人已归口 **Jacky Hu**、期限统一记为 **下一批次待排期**；实际进入排期以项目方下一批次计划为准（未确认前不臆造具体日期）。
- 本表数量与 **43** 的对应关系由 `scripts/run_acceptance_suite.py` 产出的账本持续校验（最新：`acceptance-ledger-20261007T132008.json` / `.md`）；账本与整改前基线名集差集若出现 `added > 0`，应按新失败另行处理，不得并入本表。
- 与上一版 45 项的差异（−2 项、0 新增；`CAT-10`/`CAT-11` 清零）已在本表 §0 说明。
