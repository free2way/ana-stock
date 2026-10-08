# 验收债务登记表（存量测试失败，中文）

- 生成日期：2026-10-05（最新重算：**2026-10-08**，**CAT-3 / CAT-6 / CAT-7-2 / CAT-12 / CAT-13 收口**后重跑）
- 负责人/期限：**已归口（Jacky Hu）**；期限：**下一批次待排期**（下表若干未闭环项统一归口，实际排期以下一批次计划为准）。
- 范围：**E3 范围豁免项**（`acceptance/signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §2 E3）——整改前既有、非本次引入的测试失败。
- 当前基线证据（**本批收口提交之后的全量重跑**）：
  - 最新全量账本：`tmp/acceptance-catfinal/acceptance-ledger-20261008T215857-catfinal.json` / `.md`
    （`Ran 1706 / pass 1696 / fail 8 / error 0 / skip 2 / expected_failure 0`）
  - 运行日志：`tmp/acceptance-catfinal/regression-with-postgres-acceptance-20261008T215857-catfinal.log`
    （SHA256 `4a02201329b64e180fed54dac347d85576043bce219171e9af3b5ee4f7eadd2e`）
  - 失败名集：**8 项**（全部落在 `tests/test_app.py::AppFlowTests`；账本 `failing_by_module = {"test_app": 8}`）。
    其中 **6 项为 E3 范围内既有债务**（本表登记），另 **2 项为环境性失败**（见下「环境性失败」小节，不计入本表）。
  - 上一版可比账本（**本批收口、未含 CAT-13 收尾**时的 7 项口径）：
    `tmp/acceptance-cat12/acceptance-ledger-20261008T192710-cat12.json` / `.md`
    （`Ran 1705 / pass 1696 / fail 7 / error 0 / skip 2`）。
  - 相对上一版 7 项口径逐项比较：**added=1（见「环境性失败」）/ removed=1 / changed=0 / unchanged_failing=6**。
    - `removed=1`：`test_ai_daily_report_defaults_to_cn_market_scope`（CAT-13 收尾，见 §0）。
    - `added=1`（按名集计 2 项、其中 1 项相对 7 项口径为新增）：`test_dashboard_concept_detail_add_top_n_to_watchlist`、
      `test_dashboard_concept_detail_adds_tickers_to_watchlist_and_syncs`，**均为环境性失败，另行处置、不并入本表**（见下）。
  - 更早一版 15 项口径（CAT-2/CAT-14 收口时点）：`tmp/acceptance-cat14/acceptance-ledger-20261007T195733-cat14.json`
    （相对其上一版 28 项：`added=0 / removed=13 / changed=0 / unchanged_failing=15`）。
  - 分类来源：`data/artifacts/acceptance-20261002/r9-failure-triage-2026-10-03.json`（R9 93 项归因，14 类）。本表只登记当前仍失败的 E3 项，逐项按测试方法名映射到 R9 类别。
- 记账口径：**不削弱断言、不删除测试、不改业务代码**。所有未修复项如实登记为未闭环；本表负责人统一归口 **Jacky Hu**、期限统一记为 **下一批次待排期**（未确认前不臆造具体日期）。

## 0. 重算说明（43 → 32 → 30 → 28 → 15 → 7 → 6）

- **43 项口径**：以账本 `acceptance-ledger-20261007T132008.json`（`Ran 1705 / pass 1660 / fail 35 / error 8 / skip 2`）为准——失败 **43** 项。
- **CAT-4 收尾（过程账本，32 项）**：账本 `tmp/acceptance-cat4/acceptance-ledger-20261007T164843-cat4.json`——相对 43 项口径 `added=0 / removed=11 / changed=0 / unchanged_failing=32`。
- **CAT-1 收尾（30 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T172735-cat1.json`——相对 43 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=30`。
- **CAT-1/CAT-4 余项收口（28 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T190140-cat1fix4.json`——相对上一版 30 项口径 `added=0 / removed=2 / changed=0 / unchanged_failing=28`。
- **CAT-2/CAT-14 收口（15 项）**：账本 `tmp/acceptance-cat14/acceptance-ledger-20261007T195733-cat14.json`——相对上一版 28 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=15`
  （CAT-2 5 项 commit `c2de781`；CAT-14 8 项 commit `79cf8cb`）。
- **本批收口（7 项口径）**：账本 `tmp/acceptance-cat12/acceptance-ledger-20261008T192710-cat12.json`——相对上一版 15 项口径 `added=0 / removed=8 / changed=0 / unchanged_failing=7`。
  - 相对上一版 15 项：**−8 项、0 项新增**（逐项比对 15 个 id 与 7 项失败名集，差集单向缩小且新增为空）：
    - **CAT-3 2 项**（commit `87fbde2`，`fix(ai-daily-report): legacy 报告走只读快照路径（CAT-3 2 项）`）：`save_ai_daily_report` 识别 legacy 结构，改走只读快照（`publication_state="legacy_unverified"`，跳过 `freeze_final_decisions`），带 messages 的 legacy 发布仍 fail-closed ——
      `test_dashboard_ai_daily_report_page_renders`、`test_dashboard_ai_daily_report_message_page_renders`。
    - **CAT-6 2 项**（commit `13a9342` `fix(sample-data): … CAT-6` + `d496d74` `test(fixtures): … CAT-6/CAT-12`）：`extend_sample_rows` 新增 `market` 参数，pipeline/自析夹具改走多年度日历有效历史，回测名断言对齐 `event_v2_top_n_pipeline_demo` ——
      `test_run_pipeline_job_writes_job_model_and_backtest`、`test_auto_analysis_settings_and_manual_run`。
    - **CAT-12 2 项**（commit `d496d74`）：CN/OpenBB 历史用例补 `TushareClient.fetch_cn_daily_history` 与 `_fetch_with_eastmoney_cn` 桩 ——
      `test_cn_history_prefers_akshare_before_yfinance`、`test_openbb_permission_error_falls_back_to_yfinance_history`。
    - **CAT-7 文案子因 1 项**（commit `d61203f`，`fix(watchlist): 还原跨市场跟踪 h1 文案（CAT-7）`）：watchlist hero h1 还原为「跨市场跟踪股票 / Follow Stocks Across Markets」 ——
      `test_watchlist_page_adds_symbols_and_links_to_insight`。
    - **CAT-13 显式范围子因 1 项**（commit `5977640`，`fix(ai-daily-report): 恢复 custom_tickers 显式范围（CAT-13）`）：`build_ai_daily_report` 传入 tickers 时按显式范围出报（`scope=custom_tickers`） ——
      `test_ai_daily_report_custom_tickers_still_use_explicit_scope`。
  - **changed 口径**：`changed=0` 指相对 15 项基线无“同测试类型变化”；余下 7 项仍为持续 `FAIL`，无 `error → fail` 或反向变化成员。
- **本版（CAT-13 收尾，6 项）**：账本 `tmp/acceptance-catfinal/acceptance-ledger-20261008T215857-catfinal.json`——相对上一版 7 项口径 `removed=1 / changed=0 / unchanged_failing=6`（`added` 为环境性失败，见下）。
  - 相对上一版 7 项：**−1 项**（`test_ai_daily_report_defaults_to_cn_market_scope`，commit `6b2aa1d` `test(ai-daily-report): 期望对齐全市场快照新契约（CAT-13 收尾）`：按现行产品契约把默认 CN 范围期望更新为 `prefer_snapshot=True` 全市场快照池 + `scope=portfolio_plus_cn_full_market_top5`，**仅改测试期望，未动产品代码，其余断言未改**）。未变动成员 6 项持续 `FAIL`。

### 环境性失败（不计入本表）

- 最新全量账本中 2 项“新增”失败为**运行时刻环境**导致，与本次改动无关，**不并入债务表**：
  `test_dashboard_concept_detail_add_top_n_to_watchlist`、`test_dashboard_concept_detail_adds_tickers_to_watchlist_and_syncs`。
- 复现与根因：两用例在概念页动作里执行 `sync_market_data(tickers=["AAPL"], start_date="2025-01-01", provider="auto")`（`app/api/routes/dashboard/concepts.py:796`），
  实跑返回 `status=failed`、`message="Refusing to write future US market row for 2026-10-08."`（`app/services/market_sync.py` 的“不写未完成交易日行”守卫）。
  即：在**美股当日盘中**触发同步时，provider 返回的当日（2026-10-08）bar 被守卫拒绝，`Synced 0/1` 使断言失配；**美股盘中之外**（当日上午 19:40 及此前账本）该两用例均 `pass`。
- 结论：属**时刻相关的外部数据依赖**（非代码缺陷、非本批引入）。因此 7 项口径的平均 fail 数为 7；本版口径为 **6 项 E3 债务 + 2 项环境**。

## 1. 汇总

| 类别（r9 triage id） | 数量 | 状态类型 | 负责人 | 期限 | 依据 |
|---|---|---|---|---|---|
| CAT-1-lightgbm-price-history | 4 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-1；E3（**产品语义变更待决策**，见 §2） |
| CAT-4-insight-model-output-404（余项） | 1 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-4；E3（**20 日标签未实现待决策**，见 §2） |
| CAT-7-watchlist-add-message-and-ui（余 CAT-7-1） | 1 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-7；E3（**S-13 HK 同步语义待决策**，见 §2） |
| CAT-3-legacy-report-freeze-conflict | 0 | 已清零 | Jacky Hu | — | 本批 commit `87fbde2` |
| CAT-6-job-pipeline-failed | 0 | 已清零 | Jacky Hu | — | 本批 commit `13a9342` / `d496d74` |
| CAT-12-openbb-provider-history | 0 | 已清零 | Jacky Hu | — | 本批 commit `d496d74` |
| CAT-13-ai-daily-report-scope | 0 | 已清零 | Jacky Hu | — | 本批 commit `5977640` / `6b2aa1d` |
| CAT-7-watchlist-add-message-and-ui（文案子因） | 0 | 已清零 | Jacky Hu | — | 本批 commit `d61203f`（余 CAT-7-1 见上） |
| CAT-2-watchlist-full-analysis-stub | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-2；commit `c2de781` |
| CAT-14-misc-regressions | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-14；commit `79cf8cb` |
| CAT-10-screener-cn-template-and-csv | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-10；commit `68276bc` |
| CAT-11-screener-watchlist-actions-and-tv | 0 | 已清零 | Jacky Hu | — | r9 triage CAT-11；commit `68276bc` |
| **合计** | **6** | FAIL 6 / ERROR 0 | — | — | — |

> 计数校验：4+1+1+0+0+0+0+0+0+0+0+0 = **6**；状态合计 FAIL 6 / ERROR 0，与账本 6 项 E3 名集一致（账本另含 2 项环境性失败，见 §0）。
> **待决策项标注**：**CAT-1 4 项 = 首页断言/模型分数阈值待落地（产品语义变更待决策）**；**CAT-4 余 1 项 = 20 日标签未实现待决策**；**CAT-7 余 1 项 = S-13 HK 同步语义待决策**。三者均需先做产品/口径决策，再另立批次处理（未决策前不动断言、不删测试）。
> **本批归零**：**CAT-2 / CAT-3 / CAT-6 / CAT-12 / CAT-13 / CAT-14 归零**；**CAT-7** 的 UI 文案子因（`test_watchlist_page_adds_symbols_and_links_to_insight`）归零，余 CAT-7-1 1 项（S-13 HK 同步语义）待决策。
> `CAT-5`（并发编辑阻塞）、`CAT-8`/`CAT-9`（triage 中已修复）、`CAT-10`/`CAT-11`（已清零）、`CAT-2`/`CAT-14`（更早版清零）、`CAT-15`（顺序敏感 flake）在当前名集中不出现。

## 2. 分类明细

### CAT-1-lightgbm-price-history（4 项，全部 FAIL）——归因：**重构漂移**；处置：**产品语义变更待决策**
- 完整成员（附录 #1–#4）：
  `test_dashboard_page_supports_chinese_language`、`test_dashboard_page_supports_lookback_snapshot_window`、`test_dashboard_watchlist_derived_context_is_cached_between_requests`、`test_screener_page_filters_watchlist_rules`
- **待决策说明**：4 项均为**页面文案/查询语义**类断言。修与不修取决于产品口径——需先确认是「按新语义同步更新测试断言」还是「保留旧文案/区块」，属**产品语义变更待决策**（决策前保持现状，不削弱断言、不删除测试、不改业务代码）。
- 证据（断言漂移，行号以最新账本 traceback 为准）：
  - `test_dashboard_page_supports_lookback_snapshot_window` —— `AssertionError: 'Snapshot Window' not found`（重构后 dashboard HTML 不含该文案/区块）。
  - `test_dashboard_page_supports_chinese_language` —— `AssertionError: '个人量化工作台' not found`（中文文案断言失配）。
  - `test_dashboard_watchlist_derived_context_is_cached_between_requests` —— `AssertionError: 1 != 0`（`WatchlistRepository.list_ticker_map` 期望 1 次实际 0 次：查询层重构后调用点变化）。
  - `test_screener_page_filters_watchlist_rules` —— `AssertionError: False is not true`（`Strong/Positive/Weak` 规则文案在重构后 screener 页不出现）。
- 结论：属**页面/查询层契约重构**导致的断言漂移（既有测试断言未随重构同步）；**是否按新语义更新断言未决**，待产品决策后另立批次。

### CAT-4-insight-model-output-404（1 项，FAIL）——**收尾后余项**；处置：**20 日标签未实现待决策**
- 完整成员（附录 #5）：`test_insight_model_output_endpoint_and_page`
- `test_insight_model_output_endpoint_and_page`（`tests/test_app.py:2647`）：`AssertionError: unexpectedly None` —— `payload["expected_drawdown_20d"]` 为空。读路径已可读到该票的模型输出，但 **`expected_drawdown_20d`（20 日标签/明细字段）在导入/写链路上仍未落地**（字段级缺口，非路由 404）。该字段是否落地、以何口径生成属**产品决策**，故标注为**20 日标签未实现待决策**。
- 结论：原归因（qlib `model_type` 不在读取白名单 → 路由 404）已闭环（commit `ba188c4`）；余 1 项属**字段级缺口**，待决策后另立小批次处理。

### CAT-7-watchlist-add-message-and-ui（余 1 项，FAIL）——处置：**S-13 HK 同步语义待决策**
- 完整成员（附录 #6）：`test_watchlist_add_and_sync_now_works`
- 现象：加入 `0700.HK` 后同步返回 `Added 0700.HK; dashboard updated, but sync failed: Fetched 2 row(s) for 0700.HK via unknown, but HK market data is not supported by the Parquet lake; nothing was written.` —— 提示消息部分已在 `d61203f`/更早修复，**剩余为 HK 数据不被 Parquet lake 支持时的同步状态语义（S-13）**，需先做产品决策（是保持 fail、还是降级为 unsupported 提示），决策前不动断言。
- 说明：CAT-7 原始 2 项中，`test_watchlist_page_adds_symbols_and_links_to_insight`（页面文案）已随 `d61203f` 通过。

### CAT-3-legacy-report-freeze-conflict（0 项，**已清零**）
- 本批清零：commit `87fbde2`——`save_ai_daily_report` 对 legacy 结构（有 `rows` 但无 `market_recommendations` 候选契约）改走**只读快照**（`publication_state="legacy_unverified"`，跳过 `freeze_final_decisions`）；带 `messages` 的 legacy 发布仍 fail-closed 拒绝。
- 历史依据（清零前）：`app/services/ai_daily_report.py` 的 `save_ai_daily_report` 无条件调用 `freeze_final_decisions`，legacy 结构被主动 fail-closed。

### CAT-6-job-pipeline-failed（0 项，**已清零**）
- 本批清零：commit `13a9342`（`extend_sample_rows` 新增 `market` 参数，默认 US、向后兼容）+ `d496d74`（夹具改用多年度日历有效历史；回测名断言对齐 `event_v2_top_n_pipeline_demo`）。
- 历史依据（清零前）：`app/api/routes/jobs.py` 的 `run_pipeline` 调用 `train` 未传 `market`，样例用例因夹具三日样本无法满足 PIT 宇宙过滤/标签样本量而失败。

### CAT-12-openbb-provider-history（0 项，**已清零**）
- 本批清零：commit `d496d74`——CN/OpenBB 历史用例补 `TushareClient.fetch_cn_daily_history` 与 `_fetch_with_eastmoney_cn` 桩，使 `OpenBBClient.fetch_historical_prices` 走打桩的 provider 回退分支。

### CAT-13-ai-daily-report-scope（0 项，**已清零**）
- 本批清零：commit `5977640`（`build_ai_daily_report` 对显式 tickers 恢复 `scope=custom_tickers` 显式范围出报）+ `6b2aa1d`（默认 CN 用例期望对齐全市场快照新契约 `portfolio_plus_cn_full_market_top5`，仅改测试期望）。
- 历史依据（清零前）：`build_ai_daily_report` 的 scope 归因与期望不符；默认 CN 用例 `rows` 为空、显式 tickers 用例 scope 返回 `portfolio_plus_cn_full_market_top5` 而非 `custom_tickers`。

### CAT-2-watchlist-full-analysis-stub（0 项，**已清零**）
- 历版清零：commit `c2de781` 在 `app/api/routes/watchlist.py` 接入共享分析链（模块级导入 `safe_symbol_analysis` / `build_symbol_decision_brief` / `AIAnalysisService`，新增 `_watchlist_overview_from_item` / `_full_watchlist_analysis` / `_watchlist_ai_headline` 与 Decision Console 面板）。

### CAT-14-misc-regressions（0 项，**已清零**）
- 历版清零：commit `79cf8cb` 还原八项独立回归（`symbols.py` 恢复 `fetch_symbol_headlines`；`predictions.py` 候选 run 允许 `market in {market,"ALL"}` 且最新交易日按 market 过滤；`screener_pages.py` 快照页词表；watchlist Market 单元格 exchange 副行；`market_risk.py` 空 lake 空态；`jobs.py` send-ai-daily-report 传 `trusted_regime_snapshots`），并还原 `tests/test_app.py` 四处夹具。

### CAT-10-screener-cn-template-and-csv（0 项，**已清零**）/ CAT-11-screener-watchlist-actions-and-tv（0 项，**已清零**）
- 两类在 commit `68276bc`（焦点池夹具放量 + 技术评级默认渲染）清零，当前账本名集中不出现。

## 3. 附录：6 项完整清单

> 闭合校验：本表 6 个 id 与最新账本 `comparison.unchanged_failing`（`acceptance-ledger-20261008T215857-catfinal.json`）**逐一相等**；账本另含 2 项环境性失败（见 §0），不计入本表；状态列取自账本 `tests[].status`。

| # | 状态 | 测试 id | 类别 |
|---|---|---|---|
| 1 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_chinese_language` | CAT-1 |
| 2 | FAIL | `test_app.AppFlowTests.test_dashboard_page_supports_lookback_snapshot_window` | CAT-1 |
| 3 | FAIL | `test_app.AppFlowTests.test_dashboard_watchlist_derived_context_is_cached_between_requests` | CAT-1 |
| 4 | FAIL | `test_app.AppFlowTests.test_screener_page_filters_watchlist_rules` | CAT-1 |
| 5 | FAIL | `test_app.AppFlowTests.test_insight_model_output_endpoint_and_page` | CAT-4 |
| 6 | FAIL | `test_app.AppFlowTests.test_watchlist_add_and_sync_now_works` | CAT-7 |

（环境性、不计入：`test_dashboard_concept_detail_add_top_n_to_watchlist`、`test_dashboard_concept_detail_adds_tickers_to_watchlist_and_syncs`。）

## 4. 边界与未决

- 本表登记的是 **E3 豁免边界内的存量失败**，不代表验收通过；任何修复都必须另立批次并补充独立证据。
- 分类沿用 R9 triage 的既有归因（按测试方法名映射）；**`CAT-1` 保持「重构漂移」归因**并标注**产品语义变更待决策**，**`CAT-4` 余 1 项标注 20 日标签未实现待决策**，**`CAT-7` 余 1 项标注 S-13 HK 同步语义待决策**，其余类别以本批收口提交为准。
- **代码冻结**：本版重算依据的收口提交为 `87fbde2`（CAT-3 legacy 只读快照）、`5977640`（CAT-13 显式范围）、`6b2aa1d`（CAT-13 收尾期望）、`13a9342`/`d496d74`（CAT-6）、`d61203f`（CAT-7 文案）；对应冻结基准 **v40**（`data/artifacts/freeze-20261008-v40`，`scripts/freeze_quant_remediation_baseline.py --output-dir data/artifacts/freeze-20261008-v40 --snapshot-untracked`）随本次重算登记于 `acceptance/freeze-manifest-2026-10-05.md`；上一版基准为 v39（`data/artifacts/freeze-20261005-v39`）。
- **已闭环子因**（不计入上表）：
  - legacy 报告发布（CAT-3）已在 `87fbde2` 改走只读快照。
  - pipeline 夹具与市场日历（CAT-6）已在 `13a9342`/`d496d74` 修复。
  - CN provider 回退桩（CAT-12）已在 `d496d74` 补齐。
  - AI 日报显式范围与默认范围契约（CAT-13）已在 `5977640`/`6b2aa1d` 对齐。
  - watchlist hero 文案（CAT-7 文案子因）已在 `d61203f` 还原。
  - watchlist 分析片段/Decision Console 的共享分析链（CAT-2）已在 `c2de781` 接入并以 lightweight stub 兜底。
  - CAT-14 八项独立回归已在 `79cf8cb` 还原。
  - 概念跟踪页面/导出在生产信号缺失时为空已在 `ba188c4` 修复（第三级回退）。
- **环境性未决**：`test_dashboard_concept_detail_add_top_n_to_watchlist`、`test_dashboard_concept_detail_adds_tickers_to_watchlist_and_syncs` 依赖实时 provider 同步，在**美股当日盘中**因“不写未完成交易日行”守卫而失败（见 §0）。非本批引入、非代码缺陷，按环境项另记，不并入债务总数；后续建议将该两用例的外部同步显式打桩以消除时刻依赖。
- **不确定项**：`test_watchlist_uses_batched_prediction_queries`（原 CAT-15）本版账本仍记为通过（removed），但该测试此前被独立复核报告 §5 R7 登记为**顺序/缓存敏感 flake**；单次通过不足以证明已根治，后续轮次若复发仍应归入 CAT-15。
- 负责人已归口 **Jacky Hu**、期限统一记为 **下一批次待排期**；实际进入排期以项目方下一批次计划为准（未确认前不臆造具体日期）。
- 本表数量与 **6**（E3）的对应关系由 `scripts/run_acceptance_suite.py` 产出的账本持续校验（最新：`acceptance-ledger-20261008T215857-catfinal.json` / `.md`）；账本与上一版名集差集若出现 `added > 0`，应按新失败另行处理，不得并入本表（本次 `added=2` 已按环境项另记）。
