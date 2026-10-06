# 验收债务登记表（存量测试失败，中文）

- 生成日期：2026-10-05（最新重算：2026-10-05；对齐代码冻结基准 **v29**，commit `e4625980ff5bd10caef9d486c5ff381e19f8deaf`）
- 负责人/期限：**已归口（Jacky Hu）**；期限：**下一批次待排期**（下表若干未闭环项统一归口，实际排期以下一批次计划为准）。
- 范围：**E3 范围豁免项**（`docs/quant-remediation-final-signoff-2026-10-03-zh.md` §2 E3）——整改前既有、非本次引入的测试失败。
- 当前基线证据：
  - 最新全量账本：`data/artifacts/acceptance-20261002/acceptance-ledger-20261005T225607.json` / `.md`
    （`Ran 1585 / pass 1514 / fail 61 / error 9 / skip 1 / not_collected 0`）
  - 运行日志：`data/artifacts/acceptance-20261002/regression-with-postgres-acceptance-20261005T225607.log`
    （SHA256 `c68596ba46a1ddaae58b160e3b1de288a04e6461e445bc0006351beb779cf7b9`）
  - 同轮（sentiment 轮）日志，指向同一账本：`data/artifacts/acceptance-20261002/acceptance-run-sentiment.log`
    （SHA256 `9fc8b1a7f5867002d4f8fe00e9d82ba9ebd2f506675661b295c81e7f99d422e5`；末行 `Ran 1585 … FAILED (failures=61, errors=9, skipped=1)` 与 `added=0 removed=24 changed=22 unchanged_failing=48`，`ledger=…acceptance-ledger-20261005T225607.json`）
  - 失败/错误名集：**70 项**（全部落在 `tests/test_app.py::AppFlowTests`；账本 `failing_by_module = {"test_app": 70}`）
  - 相对整改前基线 `data/artifacts/acceptance-20261002/regression-with-postgres-final.log`
    （`Ran 1045 / FAIL 45 / ERROR 49`，解析失败名 94；SHA256 `d0da8bea5e5593ceb48119efd1b27d6d15fa69f6cc9460170d682e1ecd790d35`）逐项比较：**added=0 / removed=24 / changed=22 / unchanged_failing=48**
    → 仍失败 = 48（unchanged）+ 22（changed，`fail↔error` 类型变化后仍失败）= **70**；`added=0` 证明非本次整改新引入。
  - 分类来源：`data/artifacts/acceptance-20261002/r9-failure-triage-2026-10-03.json`（R9 93 项归因，14 类）。本表只登记当前仍失败的 70 项，逐项按测试方法名映射到 R9 类别。
- 记账口径：**不削弱断言、不删除测试、不改业务代码**。所有未修复项如实登记为未闭环；本表负责人统一归口 **Jacky Hu**、期限统一记为 **下一批次待排期**（未确认前不臆造具体日期）。

## 0. 与旧版 73 项的差异（重算说明）

- **旧版（73 项口径）**：以账本 `acceptance-ledger-20261003T203040.json`（`Ran 1357 / pass 1284 / fail 64 / error 9`）为准——失败 **73** 项、**12** 类，含 `CAT-15`。
- **本版（最新账本口径）**：`acceptance-ledger-20261005T225607.json`，`added=0 / removed=24 / changed=22 / unchanged_failing=48` → 仍失败 **70** 项、**11** 类（`CAT-15` 消失）。
- **相对旧版 73 项：−3 项、0 项新增**（逐项比对旧表附录 73 个 id 与最新失败名集）：
  1. `test_dashboard_market_pages_support_execution_tag_filter`（原 CAT-1）→ 本轮通过；
  2. `test_dashboard_summary_is_cached_between_requests`（原 CAT-1）→ 本轮通过；
  3. `test_watchlist_uses_batched_prediction_queries`（原新增 CAT-15，顺序敏感 flake）→ 本轮通过 → **CAT-15 整类消失**。
- **类别计数变化**：`CAT-1` 24 → **22**（−2）；`CAT-15` 1 → **0**（−1）；其余 10 类计数不变。合计 73 → **70**。
- **changed 口径（相对整改前基线）**：`changed=22` 全部为**基线 `error` → 本次 `fail`** 的类型变化（同一测试仍失败，仅失败形态变化），且这 22 项恰好构成当前 `CAT-1` 的全部成员，故 `CAT-1` 本轮状态全为 FAIL。`unchanged_failing=48` 为基线/本次同名同类型的持续失败项（fail 39 / error 9）。
- **removed 口径**：`removed=24` 指相对**整改前基线**（非相对旧版 73 项清单）已通过 24 项；因 `added=0`，不触发“`added > 0` 必须另行处置”的规则。
- **口径对账（紧邻上一版账本）**：`acceptance-ledger-20261005T215906-dimagent-fix2.json` 与本版失败名集**完全相同（70 项）**，仅 `test_watchlist_supports_execution_tag_filters` 由 `error` 变为 `fail`，故其 `changed/unchanged_failing = 21/49`。本表以更新的 sentiment 轮账本 `…T225607` 为准。

## 1. 汇总

| 类别（r9 triage id） | 数量 | 状态类型 | 负责人 | 期限 | 依据 |
|---|---|---|---|---|---|
| CAT-1-lightgbm-price-history | 22 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-1；E3 |
| CAT-4-insight-model-output-404 | 12 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-4；E3 |
| CAT-10-screener-cn-template-and-csv | 8 | FAIL 7 / ERROR 1 | Jacky Hu | 下一批次待排期 | r9 triage CAT-10；E3 |
| CAT-14-misc-regressions | 8 | FAIL 7 / ERROR 1 | Jacky Hu | 下一批次待排期 | r9 triage CAT-14；E3 |
| CAT-2-watchlist-full-analysis-stub | 5 | ERROR | Jacky Hu | 下一批次待排期 | r9 triage CAT-2；E3 |
| CAT-11-screener-watchlist-actions-and-tv | 5 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-11；E3 |
| CAT-3-legacy-report-freeze-conflict | 2 | ERROR | Jacky Hu | 下一批次待排期 | r9 triage CAT-3；E3 |
| CAT-6-job-pipeline-failed | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-6；E3 |
| CAT-7-watchlist-add-message-and-ui | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-7；E3 |
| CAT-12-openbb-provider-history | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-12；E3 |
| CAT-13-ai-daily-report-scope | 2 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-13；E3 |
| **合计** | **70** | FAIL 61 / ERROR 9 | — | — | — |

> 计数校验：22+12+8+8+5+5+2+2+2+2+2 = **70**；状态合计 FAIL 61 / ERROR 9，与账本 `fail 61 / error 9` 一致。
> `CAT-5`（并发编辑阻塞）、`CAT-8`/`CAT-9`（triage 中已修复）在当前名集中不出现；`CAT-15`（顺序敏感 flake）本轮已通过，不再登记。

## 2. 分类明细

### CAT-1-lightgbm-price-history（22 项，全部 FAIL）
- 样例测试：`test_dashboard_concept_detail_add_top_n_to_watchlist`、`test_dashboard_page_supports_chinese_language`、`test_watchlist_supports_execution_tag_filters`
- 状态说明：本类 22 项在整改前基线均为 `error`、本次均为 `fail`（即账本 `changed=22` 的全部来源）；类型变化后仍失败，故计入存量债务。
- 依据（代码）：`app/services/trainer.py:1743`（`len(all_dates) <= warmup_dates + 5`）、`app/services/trainer.py:1772`（`len(train_pool) < 1000`）、`app/services/stock_selection/executable_outcomes.py:65`（`market` 非 CN/US/HK 抛 `explicit market required`）、`app/services/sample_data.py`（每票 5–10 交易日的夹具）
- 结论：训练门槛与夹具不匹配，`train()` 未传 `market` 时全部样本被排除，随后因样本不足失败。属既有失败，未修复。
- 备注：旧版曾登记的 `test_dashboard_summary_is_cached_between_requests`（原 CAT-1）本轮已通过（removed），不再计入。

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
- 依据（代码）：`app/api/routes/jobs.py:2559`（`run_pipeline` 调用 `train` 未传 `market`）——与 CAT-1 同源。

### CAT-7-watchlist-add-message-and-ui（2 项，全部 FAIL）
- 样例测试：`test_watchlist_add_and_sync_now_works`、`test_watchlist_page_adds_symbols_and_links_to_insight`
- 依据：r9 triage 记录提示语已部分修复（`app/api/routes/watchlist.py:1758/1765`），剩余为 HK 同步状态（S-13）与页面文案断言两处独立原因。

### CAT-10-screener-cn-template-and-csv（8 项：FAIL 7 / ERROR 1）
- 样例测试：`test_screener_export_csv_returns_rows`、`test_cn_high_roe_steady_growth_template_filters_on_revenue_and_leverage`、`test_screener_sort_by_dividend_desc_orders_results`（ERROR）
- 依据（代码）：`app/api/routes/screener.py:1085-1089`（快照未就绪即 303，CSV 导出在 TestClient 自动跟随下返回 HTML）、CN 模板过滤与 `dividend_yield` 排序目标缺失。

### CAT-11-screener-watchlist-actions-and-tv（5 项，全部 FAIL）
- 样例测试：`test_screener_can_bulk_add_results_to_watchlist`、`test_screener_page_renders_tradingview_multi_timeframe_ratings`
- 依据：结果页缺少批量加自选/同步入口与 `技术评级` 文案（r9 triage 证据 `tests/test_app.py:3920/4552/4597/4715/1126`）。

### CAT-12-openbb-provider-history（2 项，全部 FAIL）
- 样例测试：`test_cn_history_prefers_akshare_before_yfinance`、`test_openbb_permission_error_falls_back_to_yfinance_history`
- 依据：`tests/test_app.py:5432/5466`——`OpenBBClient.fetch_historical_prices` 未走打桩的 provider 回退分支（实际返回 125 行而非 1 行）。

### CAT-13-ai-daily-report-scope（2 项，全部 FAIL）
- 样例测试：`test_ai_daily_report_defaults_to_cn_market_scope`、`test_ai_daily_report_custom_tickers_still_use_explicit_scope`
- 依据：`tests/test_app.py:6462/6464/6527`——`build_ai_daily_report` 的 scope 归因与期望不符。

### CAT-14-misc-regressions（8 项：FAIL 7 / ERROR 1）
- 样例测试：`test_market_snapshot_page_renders_boards`、`test_prediction_repository_uses_latest_trade_date_per_market`、`test_symbol_page_bundle_endpoint_returns_combined_sections`（ERROR）
- 依据：r9 triage CAT-14——其余无统一验收项的独立回归（S-11/S-14 等）。

## 3. 附录：70 项完整清单

| # | 状态 | 测试 id | 类别 |
|---|---|---|---|
| 1 | FAIL | `test_app.AppFlowTests.test_dashboard_concept_detail_add_top_n_to_watchlist` | CAT-1 |
| 2 | FAIL | `test_app.AppFlowTests.test_dashboard_concept_detail_adds_tickers_to_watchlist_and_syncs` | CAT-1 |
| 3 | FAIL | `test_app.AppFlowTests.test_dashboard_concept_detail_shows_sortable_columns_and_five_day_metric` | CAT-1 |
| 4 | FAIL | `test_app.AppFlowTests.test_dashboard_concept_detail_shows_top_movers_comparison` | CAT-1 |
| 5 | FAIL | `test_app.AppFlowTests.test_dashboard_concept_detail_single_ticker_action_adds_watchlist_item` | CAT-1 |
| 6 | FAIL | `test_app.AppFlowTests.test_dashboard_concept_detail_supports_chinese_language` | CAT-1 |
| 7 | FAIL | `test_app.AppFlowTests.test_dashboard_concept_detail_supports_signal_strength_filter` | CAT-1 |
| 8 | FAIL | `test_app.AppFlowTests.test_dashboard_continuous_leader_action_adds_watchlist_item` | CAT-1 |
| 9 | FAIL | `test_app.AppFlowTests.test_dashboard_continuous_leaders_supports_execution_tag_filters` | CAT-1 |
| 10 | FAIL | `test_app.AppFlowTests.test_dashboard_market_page_supports_chinese_language` | CAT-1 |
| 11 | FAIL | `test_app.AppFlowTests.test_dashboard_market_pages_support_excluding_execution_tag` | CAT-1 |
| 12 | FAIL | `test_app.AppFlowTests.test_dashboard_ops_page_supports_chinese_language` | CAT-1 |
| 13 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_chinese_language` | CAT-1 |
| 14 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_lookback_snapshot_window` | CAT-1 |
| 15 | FAIL | `test_app.AppFlowTests.test_dashboard_watchlist_derived_context_is_cached_between_requests` | CAT-1 |
| 16 | FAIL | `test_app.AppFlowTests.test_execution_tag_filters_support_multiple_tags` | CAT-1 |
| 17 | FAIL | `test_app.AppFlowTests.test_sample_workflow_populates_dashboard_and_symbol_pages` | CAT-1 |
| 18 | FAIL | `test_app.AppFlowTests.test_screener_page_filters_watchlist_rules` | CAT-1 |
| 19 | FAIL | `test_app.AppFlowTests.test_screener_page_supports_chinese_language` | CAT-1 |
| 20 | FAIL | `test_app.AppFlowTests.test_screener_page_supports_snapshot_persistence_filter` | CAT-1 |
| 21 | FAIL | `test_app.AppFlowTests.test_screener_supports_execution_tag_filters` | CAT-1 |
| 22 | FAIL | `test_app.AppFlowTests.test_watchlist_supports_execution_tag_filters` | CAT-1 |
| 23 | ERROR | `test_app.AppFlowTests.test_watchlist_analysis_fragment_is_cached_between_requests` | CAT-2 |
| 24 | ERROR | `test_app.AppFlowTests.test_watchlist_analysis_fragment_renders_decision_console` | CAT-2 |
| 25 | ERROR | `test_app.AppFlowTests.test_watchlist_page_limits_full_analysis_to_top_candidates` | CAT-2 |
| 26 | ERROR | `test_app.AppFlowTests.test_watchlist_page_renders_ai_briefs_for_top_ranked_names` | CAT-2 |
| 27 | ERROR | `test_app.AppFlowTests.test_watchlist_page_renders_decision_console` | CAT-2 |
| 28 | ERROR | `test_app.AppFlowTests.test_dashboard_ai_daily_report_message_page_renders` | CAT-3 |
| 29 | ERROR | `test_app.AppFlowTests.test_dashboard_ai_daily_report_page_renders` | CAT-3 |
| 30 | FAIL | `test_app.AppFlowTests.test_import_model_output_job_populates_prediction_details` | CAT-4 |
| 31 | FAIL | `test_app.AppFlowTests.test_insight_model_output_endpoint_and_page` | CAT-4 |
| 32 | FAIL | `test_app.AppFlowTests.test_insight_page_renders_when_model_output_is_missing` | CAT-4 |
| 33 | FAIL | `test_app.AppFlowTests.test_qlib_prediction_adapter_imports_rows` | CAT-4 |
| 34 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_artifact_explanations` | CAT-4 |
| 35 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_artifact_trade_plan` | CAT-4 |
| 36 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_native_result_bundle` | CAT-4 |
| 37 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_imports_prediction_csv` | CAT-4 |
| 38 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_artifact_manifest_metadata` | CAT-4 |
| 39 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_predictions_csv_from_artifact_directory` | CAT-4 |
| 40 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_predictions_json_from_artifact_directory` | CAT-4 |
| 41 | FAIL | `test_app.AppFlowTests.test_qlib_predictor_reads_predictions_jsonl_from_artifact_directory` | CAT-4 |
| 42 | FAIL | `test_app.AppFlowTests.test_auto_analysis_settings_and_manual_run` | CAT-6 |
| 43 | FAIL | `test_app.AppFlowTests.test_run_pipeline_job_writes_job_model_and_backtest` | CAT-6 |
| 44 | FAIL | `test_app.AppFlowTests.test_watchlist_add_and_sync_now_works` | CAT-7 |
| 45 | FAIL | `test_app.AppFlowTests.test_watchlist_page_adds_symbols_and_links_to_insight` | CAT-7 |
| 46 | FAIL | `test_app.AppFlowTests.test_cn_growth_value_template_uses_fundamental_rules` | CAT-10 |
| 47 | FAIL | `test_app.AppFlowTests.test_cn_high_roe_steady_growth_template_filters_on_revenue_and_leverage` | CAT-10 |
| 48 | FAIL | `test_app.AppFlowTests.test_cn_low_valuation_high_dividend_template_filters_on_dividend` | CAT-10 |
| 49 | FAIL | `test_app.AppFlowTests.test_global_growth_value_template_uses_us_hk_snapshots` | CAT-10 |
| 50 | FAIL | `test_app.AppFlowTests.test_screener_can_add_current_results_to_today_focus_pool` | CAT-10 |
| 51 | FAIL | `test_app.AppFlowTests.test_screener_export_csv_returns_rows` | CAT-10 |
| 52 | ERROR | `test_app.AppFlowTests.test_screener_sort_by_dividend_desc_orders_results` | CAT-10 |
| 53 | FAIL | `test_app.AppFlowTests.test_technical_screener_name_prefers_symbol_name` | CAT-10 |
| 54 | FAIL | `test_app.AppFlowTests.test_screener_can_bulk_add_results_to_watchlist` | CAT-11 |
| 55 | FAIL | `test_app.AppFlowTests.test_screener_can_bulk_add_top_n_and_enable_sync` | CAT-11 |
| 56 | FAIL | `test_app.AppFlowTests.test_screener_can_sync_top_results` | CAT-11 |
| 57 | FAIL | `test_app.AppFlowTests.test_screener_page_renders_tradingview_multi_timeframe_ratings` | CAT-11 |
| 58 | FAIL | `test_app.AppFlowTests.test_screener_page_shows_add_to_watchlist_for_untracked_results` | CAT-11 |
| 59 | FAIL | `test_app.AppFlowTests.test_cn_history_prefers_akshare_before_yfinance` | CAT-12 |
| 60 | FAIL | `test_app.AppFlowTests.test_openbb_permission_error_falls_back_to_yfinance_history` | CAT-12 |
| 61 | FAIL | `test_app.AppFlowTests.test_ai_daily_report_custom_tickers_still_use_explicit_scope` | CAT-13 |
| 62 | FAIL | `test_app.AppFlowTests.test_ai_daily_report_defaults_to_cn_market_scope` | CAT-13 |
| 63 | FAIL | `test_app.AppFlowTests.test_market_snapshot_page_renders_boards` | CAT-14 |
| 64 | FAIL | `test_app.AppFlowTests.test_prediction_repository_uses_latest_trade_date_per_market` | CAT-14 |
| 65 | FAIL | `test_app.AppFlowTests.test_refresh_cn_market_data_incremental_uses_last_synced_date_window` | CAT-14 |
| 66 | FAIL | `test_app.AppFlowTests.test_refresh_existing_watchlist_metadata_uses_live_profile_for_us_stock` | CAT-14 |
| 67 | FAIL | `test_app.AppFlowTests.test_run_pipeline_redirects_back_to_dashboard` | CAT-14 |
| 68 | FAIL | `test_app.AppFlowTests.test_send_ai_daily_report_endpoint_uses_notifier` | CAT-14 |
| 69 | ERROR | `test_app.AppFlowTests.test_symbol_page_bundle_endpoint_returns_combined_sections` | CAT-14 |
| 70 | FAIL | `test_app.AppFlowTests.test_symbol_page_bundle_is_cached_between_requests` | CAT-14 |

## 4. 边界与未决

- 本表登记的是 **E3 豁免边界内的存量失败**，不代表验收通过；任何修复都必须另立批次并补充独立证据。
- 分类沿用 R9 triage 的既有归因（按测试方法名映射），未重新做根因实验；`CAT-14` 为混合桶，单项根因以 R9 triage 为准。
- **不确定项**：`test_watchlist_uses_batched_prediction_queries`（原 CAT-15）本轮账本记为通过（removed），但该测试此前被独立复核报告 §5 R7 登记为**顺序/缓存敏感 flake**；其单次通过不足以证明已根治，若后续轮次复发仍应归入 CAT-15。其余 70 项均能与 R9 类别逐一对应，无归类困难项。
- 负责人已归口 **Jacky Hu**、期限统一记为 **下一批次待排期**；实际进入排期以项目方下一批次计划为准（未确认前不臆造具体日期）。
- 本表数量与 **70** 的对应关系由 `scripts/run_acceptance_suite.py` 产出的账本持续校验（最新：`acceptance-ledger-20261005T225607.json` / `.md`）；账本与整改前基线名集差集若出现 `added > 0`，应按新失败另行处理，不得并入本表。
- 与旧版 73 项的差异（−3 项、0 新增；`CAT-15` 消失）已在本表 §0 说明。
