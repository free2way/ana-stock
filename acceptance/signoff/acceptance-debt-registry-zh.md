# 验收债务登记表（存量测试失败，中文）

- 生成日期：2026-10-05（最新重算：**2026-10-08**，**CAT-1 / CAT-4 收口**后重跑；**2026-10-09 复核**：三路审核 P1/P2 闭环 + 合树干净全量，见 §0「本轮审核修复（P1/P2）闭环与样本集影响」）
- 负责人/期限：**已归口（Jacky Hu）**；期限：**下一批次待排期**（下表未闭环项统一归口，实际排期以下一批次计划为准）。
- 范围：**E3 范围豁免项**（`acceptance/signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §2 E3）——整改前既有、非本次引入的测试失败。
- 当前基线证据（**本批收口提交之后的全量重跑**）：
  - 最新全量账本：`tmp/acceptance-cat1c/acceptance-ledger-20261008T225900-cat1c.json` / `.md`
    （`Ran 1706 / pass 1703 / fail 1 / error 0 / skip 2 / expected_failure 0`）
  - 运行日志：`tmp/acceptance-cat1c/regression-with-postgres-acceptance-20261008T225900-cat1c.log`
    （SHA256 `c060e5c320c99377e563e5f3fae3336fea1fafdeed3760a63f7f0f1decb8d9f0`）
  - 失败名集：**1 项**（`test_app.AppFlowTests.test_watchlist_add_and_sync_now_works`；账本 `failing_by_module = {"test_app": 1}`）。
    上一版口径里另有的 **2 项概念页同步用例（环境性失败）已随机构桩消除**（见 §0「环境性失败（已消除）」）。
  - 上一版可比账本（**6 项 E3 债务 + 2 项环境项**口径）：
    `tmp/acceptance-catfinal/acceptance-ledger-20261008T215857-catfinal.json` / `.md`
    （`Ran 1706 / pass 1696 / fail 8 / error 0 / skip 2`）。
  - 相对上一版 6 项口径逐项比较：**added=0 / removed=7 / changed=0 / unchanged_failing=1**。
    - `removed=7` = **CAT-1 4 项**（commit `f14585b` / `fc04e9f`）+ **CAT-4 余 1 项**（commit `5329982`）+ **环境项 2 项**（commit `d6bbdc5` 打桩，见 §0）。
    - `unchanged_failing=1`：`test_watchlist_add_and_sync_now_works`（CAT-7-1，S-13 HK 同步语义待决策）。
  - 更早的 7 项口径：`tmp/acceptance-cat12/acceptance-ledger-20261008T192710-cat12.json`；15 项口径：`tmp/acceptance-cat14/acceptance-ledger-20261007T195733-cat14.json`。
  - 分类来源：`data/artifacts/acceptance-20261002/r9-failure-triage-2026-10-03.json`（R9 93 项归因，14 类）。本表只登记当前仍失败的 E3 项，逐项按测试方法名映射到 R9 类别。
- 记账口径：**不削弱断言、不删除测试、不改业务代码**。所有未修复项如实登记为未闭环；本表负责人统一归口 **Jacky Hu**、期限统一记为 **下一批次待排期**（未确认前不臆造具体日期）。
- **记录口径（总量）**：签署件（`acceptance/signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §5 R9）登记的存量失败口径为 **70 项**（= unchanged_failing 49 + changed 21；上一版 v17 口径 73 项），本轮收口后降至 **1 项**；本表自身逐版口径为 **43 → 32 → 30 → 28 → 15 → 7 → 6 → 1**，**每批 `added=0`**（无新增失败，只做单向缩小）。

## 0. 重算说明（43 → 32 → 30 → 28 → 15 → 7 → 6 → 1）

- **43 项口径**：以账本 `acceptance-ledger-20261007T132008.json`（`Ran 1705 / pass 1660 / fail 35 / error 8 / skip 2`）为准——失败 **43** 项。
- **CAT-4 收尾（过程账本，32 项）**：账本 `tmp/acceptance-cat4/acceptance-ledger-20261007T164843-cat4.json`——相对 43 项口径 `added=0 / removed=11 / changed=0 / unchanged_failing=32`。
- **CAT-1 收尾（30 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T172735-cat1.json`——相对 43 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=30`。
- **CAT-1/CAT-4 余项收口（28 项）**：账本 `tmp/acceptance-cat1/acceptance-ledger-20261007T190140-cat1fix4.json`——相对上一版 30 项口径 `added=0 / removed=2 / changed=0 / unchanged_failing=28`。
- **CAT-2/CAT-14 收口（15 项）**：账本 `tmp/acceptance-cat14/acceptance-ledger-20261007T195733-cat14.json`——相对上一版 28 项口径 `added=0 / removed=13 / changed=0 / unchanged_failing=15`
  （CAT-2 5 项 commit `c2de781`；CAT-14 8 项 commit `79cf8cb`）。
- **CAT-3/6/7-2/12/13 收口（7 项口径）**：账本 `tmp/acceptance-cat12/acceptance-ledger-20261008T192710-cat12.json`——相对上一版 15 项口径 `added=0 / removed=8 / changed=0 / unchanged_failing=7`。
  - **CAT-3 2 项**（commit `87fbde2`，`fix(ai-daily-report): legacy 报告走只读快照路径（CAT-3 2 项）`）：`save_ai_daily_report` 识别 legacy 结构，改走只读快照（`publication_state="legacy_unverified"`，跳过 `freeze_final_decisions`），带 messages 的 legacy 发布仍 fail-closed ——
    `test_dashboard_ai_daily_report_page_renders`、`test_dashboard_ai_daily_report_message_page_renders`。
  - **CAT-6 2 项**（commit `13a9342` + `d496d74`）：`extend_sample_rows` 新增 `market` 参数，pipeline/自析夹具改走多年度日历有效历史，回测名断言对齐 `event_v2_top_n_pipeline_demo` ——
    `test_run_pipeline_job_writes_job_model_and_backtest`、`test_auto_analysis_settings_and_manual_run`。
  - **CAT-12 2 项**（commit `d496d74`）：CN/OpenBB 历史用例补 `TushareClient.fetch_cn_daily_history` 与 `_fetch_with_eastmoney_cn` 桩 ——
    `test_cn_history_prefers_akshare_before_yfinance`、`test_openbb_permission_error_falls_back_to_yfinance_history`。
  - **CAT-7 文案子因 1 项**（commit `d61203f`）：watchlist hero h1 还原为「跨市场跟踪股票 / Follow Stocks Across Markets」 ——
    `test_watchlist_page_adds_symbols_and_links_to_insight`。
  - **CAT-13 显式范围子因 1 项**（commit `5977640`）：`build_ai_daily_report` 传入 tickers 时按显式范围出报（`scope=custom_tickers`） ——
    `test_ai_daily_report_custom_tickers_still_use_explicit_scope`。
- **CAT-13 收尾（6 项口径）**：账本 `tmp/acceptance-catfinal/acceptance-ledger-20261008T215857-catfinal.json`——相对上一版 7 项口径 `added=1（环境性）/ removed=1 / changed=0 / unchanged_failing=6`（`added` 为环境性失败，见下）。
  - 相对上一版 7 项：**−1 项**（`test_ai_daily_report_defaults_to_cn_market_scope`，commit `6b2aa1d`：按现行产品契约把默认 CN 范围期望更新为 `prefer_snapshot=True` 全市场快照池 + `scope=portfolio_plus_cn_full_market_top5`，**仅改测试期望，未动产品代码**）。
- **本版（CAT-1 / CAT-4 收口，1 项）**：账本 `tmp/acceptance-cat1c/acceptance-ledger-20261008T225900-cat1c.json`——相对上一版 6 项口径 `added=0 / removed=7 / changed=0 / unchanged_failing=1`。
  - **CAT-1 4 项**：commit `f14585b`（`test(dashboard): CAT-1 首页断言对齐四步工作台契约`）+ `fc04e9f`（`fix(model-badge): 模型徽标按分位归一，修正全站恒为 Neutral（CAT-1）`）。
    - 首页断言 **3 项**（`f14585b`，断言对齐四步工作台新契约；同 commit 删除已无渲染点的死键 `DASHBOARD_TEXT[lang]["title"]/["hero"]`，未改产品渲染逻辑）：
      `test_dashboard_page_supports_chinese_language`、`test_dashboard_page_supports_lookback_snapshot_window`、`test_dashboard_watchlist_derived_context_is_cached_between_requests`。
    - 模型分数阈值 **1 项**（`fc04e9f`，`build_model_state` 在有横截面分位时按分位五分位渲染，徽标 0.12/0.03 绝对阈值仅作无分位兜底）：
      `test_screener_page_filters_watchlist_rules`。
  - **CAT-4 余 1 项**：commit `5329982`（`feat(trainer): 落地 20 日标签 horizon（CAT-4）`）——`SignalTrainer._future_path_metrics_20d` 新增 20 个交易日后验窗口标签并接入现役/legacy 标签 profile，OOS 校准生产者同步产出 20 日键，`expected_return_20d` / `expected_drawdown_20d` 不再恒为 None：
    `test_insight_model_output_endpoint_and_page`。
  - **changed 口径**：`changed=0` 指相对上一版无“同测试类型变化”；余下 1 项持续 `FAIL`，无 `error → fail` 或反向变化成员。

### 环境性失败（已消除）

- 上一版（6 项口径）另有 2 项“新增”失败为**运行时刻环境**导致，与代码缺陷无关：
  `test_dashboard_concept_detail_add_top_n_to_watchlist`、`test_dashboard_concept_detail_adds_tickers_to_watchlist_and_syncs`
  —— 美股当日盘中触发 `sync_market_data(tickers=["AAPL"], provider="auto")` 时，provider 返回的当日（`2026-10-08`）bar 被 `market_sync.py` 的“不写未完成交易日行”守卫拒绝，`Synced 0/1` 使断言失配。
- **本轮已消除**：commit `d6bbdc5`（`test(dashboard): 显式打桩 US 价格 provider，消除盘中环境噪声`）对这两用例显式打桩 US 价格 provider，失败名集不再含这两项。本表当前**不含任何环境性失败**。

### 本轮审核修复（P1/P2）闭环与样本集影响（2026-10-09 复核）

- **闭环**：三路独立审核（A 砺行 `trainer.py` / C 秉直 执行与账本 / B 容之 校准·面板·健康）共 **8 条 P1/P2** 全部**复现属实**并闭环，对应 8 个修复提交：`e25e160`、`2eb469c`、`9a7bc4e`（A）；`015ef8d`、`36ca9fe`（C）；`4306b4e`、`3ef5f28`、`7a3a729`（B）。逐条复现/修复/回归明细见 `acceptance/supplements/audit-p1-closure-2026-10-09.md`。
- **合树干净全量**（隔离库 `pqw_close_test`，确认无并发 sibling 全量；首次合树全量：此前三路 `added=0` 账本均早于秉直 11:50 的两个提交）：**两轮**全量——首轮 `Ran 1739 / pass 1733 / fail 3 / error 1 / skip 2`，相对基线 `added=3`（**经机制验证 + 单独复跑判定为 DB 并发死锁 → `setUp` 中止 → `PQW_DATA_DIR`/`PQW_OPTIN_REASON` 泄漏到下游模块的测试编排伪失败**，非产品回归），次轮同参复跑 **`added=0 / removed=0 / changed=0 / unchanged_failing=1`**（`Ran 1739 / pass 1736 / fail 1 / error 0 / skip 2`）。**产品代码回归集为空**；唯一失败仍为 **CAT-7-1**。**存量债务总数不变，仍为 1**（本条不新增债务）。明细见 `acceptance/supplements/audit-p1-closure-2026-10-09.md`。
- **样本集变化（重要，影响可比性）**：砺行 **P1-1**（`e25e160`，标的池过滤改为样本门控）修复后，对照实验中**部分信号日的入场/持有窗口发生位移（15 个共同信号日）**，**标注样本 204 → 208**，训练样本集口径随之变化。**此前 v40/v41 冻结基准下的模型产出与本版不可直接比较**（口径不同，非同分布 A/B）；在**重训产出新模型**之前，请勿用旧基准的模型指标与本版对比。
- **残余（未扩围，如实登记）**：① **reliability 元数据**仍是单一全局文件（本批仅对概率校准产物做了 thin refresh 失效，元数据未按市场/模型分区）；② 生产旧产物 `probability_calibration_latest.json`（无 `market` 标记）按设计**不再被加载**，需**下次调度刷新**才生成带 scope 的产物，在此之前发布估计为「**未校准**」；③ 账本从 `app_settings` JSON 迁移到结构化**持仓/成交流水表**属**中期事项**；④ 生产 runner **默认未开 `liquidate_at_end`**，T+1 强平缺陷为**防御性修复**（经引擎配置即可触发）。

## 1. 汇总

| 类别（r9 triage id） | 数量 | 状态类型 | 负责人 | 期限 | 依据 |
|---|---|---|---|---|---|
| CAT-1-lightgbm-price-history | 0 | 已清零 | Jacky Hu | — | 本批 commit `f14585b`（首页断言×3）/ `fc04e9f`（模型分数阈值×1） |
| CAT-4-insight-model-output-404（余项） | 0 | 已清零 | Jacky Hu | — | 本批 commit `5329982`（20 日标签 horizon 落地） |
| CAT-7-watchlist-add-message-and-ui（余 CAT-7-1） | 1 | FAIL | Jacky Hu | 下一批次待排期 | r9 triage CAT-7；E3（**S-13 HK 同步语义待决策**，见 §2） |
| CAT-3-legacy-report-freeze-conflict | 0 | 已清零 | Jacky Hu | — | commit `87fbde2` |
| CAT-6-job-pipeline-failed | 0 | 已清零 | Jacky Hu | — | commit `13a9342` / `d496d74` |
| CAT-12-openbb-provider-history | 0 | 已清零 | Jacky Hu | — | commit `d496d74` |
| CAT-13-ai-daily-report-scope | 0 | 已清零 | Jacky Hu | — | commit `5977640` / `6b2aa1d` |
| CAT-7-watchlist-add-message-and-ui（文案子因） | 0 | 已清零 | Jacky Hu | — | commit `d61203f`（余 CAT-7-1 见上） |
| CAT-2-watchlist-full-analysis-stub | 0 | 已清零 | Jacky Hu | — | commit `c2de781` |
| CAT-14-misc-regressions | 0 | 已清零 | Jacky Hu | — | commit `79cf8cb` |
| CAT-10-screener-cn-template-and-csv | 0 | 已清零 | Jacky Hu | — | commit `68276bc` |
| CAT-11-screener-watchlist-actions-and-tv | 0 | 已清零 | Jacky Hu | — | commit `68276bc` |
| **合计** | **1** | FAIL 1 / ERROR 0 | — | — | — |

> 计数校验：0+0+1+0+0+0+0+0+0+0+0+0 = **1**；状态合计 FAIL 1 / ERROR 0，与最新账本 1 项失败名集逐一相等（本版账本已无环境性失败，见 §0）。
> **本批归零**：**CAT-1 4 项**（首页断言×3 + 模型分数阈值×1）、**CAT-4 余 1 项**（20 日标签落地）本轮归零。至此 E3 债务仅余 **CAT-7-1 1 项**。
> **待决策项（保留）**：**CAT-7 余 1 项 = S-13 HK 同步语义待决策**（决策前不动断言）。
> **待决策项（本批新增，怀安收口报告）**：
> (i) **CAT-4 20 日明细的校准来源回退**：`app/services/trainer.py` 明细发布**优先 OOS 校准快照，缺快照时回退本次 run 训练窗校准桶**（`oos_calibration_buckets or calibration_buckets`，run 元数据 `detail_estimate_calibration_source` 记录来源）；仓库当前**无 `model_calibration_snapshot` 生产者**（该 job 仅存在于 `app/api/routes/jobs.py` 作业目录描述中，无实装处理器），故常态走训练窗回退。**是否补 OOS 校准快照生产者 / 回退口径是否可接受，待产品决策**。
> (ii) **`build_signal_label` 仍为旧分数标度**：`app/services/model_signal_summary.py::build_signal_label` 仍按旧标度阈值判定（买点 ≥0.18 / 观察 ≥0.05 / 卖点 ≤−0.05），而现役 score 语义为 `executable_next_open_net_return`（压缩到 ~0.005 量级，运行侧 `_clamp(..., -0.35, 0.35)`）；徽标已改按分位归一（`fc04e9f`），但**买点/观察文本标签继续恒为 Hold**，未改。**标签是否同步改为分位口径，待产品决策**。
> 以上三项均需先做产品/口径决策，再另立批次处理（未决策前不动断言、不删测试）。

## 2. 分类明细

### CAT-1-lightgbm-price-history（0 项，**已清零**）
- 本批清零：commit `f14585b`（首页三段断言对齐四步工作台契约；`app/api/routes/dashboard/_common.py` 删除死键 `DASHBOARD_TEXT[lang]["title"]/["hero"]`）+ `fc04e9f`（`build_model_state` 在有横截面分位（`score_source=percentile_0_100`）时按分位五分位渲染徽标 ≥80 强 / ≥60 偏强 / ≤40 谨慎 / ≤20 偏弱 / 其余中性，无分位时保持原绝对阈值行为；`enrich_model_output` 与首页信号徽标透传 `percentile`，使 screener / dashboard / insights / concepts 全站一致）。
- 历史依据（清零前，均为**页面文案/查询语义断言漂移**）：`test_dashboard_page_supports_lookback_snapshot_window`（`'Snapshot Window' not found`）、`test_dashboard_page_supports_chinese_language`（`'个人量化工作台' not found`）、`test_dashboard_watchlist_derived_context_is_cached_between_requests`（`1 != 0`，`WatchlistRepository.list_ticker_map` 调用次数）、`test_screener_page_filters_watchlist_rules`（`Strong/Positive/Weak` 规则文案未出现，因徽标恒 Neutral）。

### CAT-4-insight-model-output-404（0 项，**已清零**）
- 本批清零：commit `5329982`——`SignalTrainer._future_path_metrics_20d` 新增可复用的 20 个交易日后验窗口标签（信号日收盘为锚，百分比口径，窗口不足 20 根一律 None，不用 1/3/5 日回填），接入现役 profile `executable_net_return_v1` 与 legacy `legacy_short_horizon_composite_v1`；`template_evaluation.aggregate_lightgbm_score_calibration` 同步产出 20 日键（新增 `template_forward_drawdown_from_history`）。
- 历史依据（清零前）：`test_insight_model_output_endpoint_and_page`（`tests/test_app.py:2647`）`AssertionError: unexpectedly None`——`expected_return_20d` / `expected_drawdown_20d` 的校准度量键（`next_20d_close_return_avg` / `next_20d_max_drawdown_avg`）全仓无生产者（`c948f53` 已移除“用 5 日极值冒充 20 日”的兼容路径），字段恒为 None（原路由 404 归因更早由 `ba188c4` 闭环）。
- **遗留待决策**：见 §1 待决策项 (i)（缺 `model_calibration_snapshot` 生产者时的训练窗回退口径）。

### CAT-7-watchlist-add-message-and-ui（余 1 项，FAIL）——处置：**S-13 HK 同步语义待决策**
- 完整成员（附录 #1）：`test_watchlist_add_and_sync_now_works`
- 现象：加入 `0700.HK` 后同步返回 `Added 0700.HK; dashboard updated, but sync failed: Fetched 2 row(s) for 0700.HK via unknown, but HK market data is not supported by the Parquet lake; nothing was written.` —— 提示消息部分已在 `d61203f`/更早修复，**剩余为 HK 数据不被 Parquet lake 支持时的同步状态语义（S-13）**，需先做产品决策（是保持 fail、还是降级为 `unsupported_market` 提示），决策前不动断言。
- 说明：CAT-7 原始 2 项中，`test_watchlist_page_adds_symbols_and_links_to_insight`（页面文案）已随 `d61203f` 通过。

### CAT-3-legacy-report-freeze-conflict（0 项，已清零）
- 历版清零：commit `87fbde2`——`save_ai_daily_report` 对 legacy 结构（有 `rows` 但无 `market_recommendations` 候选契约）改走**只读快照**（`publication_state="legacy_unverified"`，跳过 `freeze_final_decisions`）；带 `messages` 的 legacy 发布仍 fail-closed 拒绝。

### CAT-6-job-pipeline-failed（0 项，已清零）
- 历版清零：commit `13a9342`（`extend_sample_rows` 新增 `market` 参数，默认 US、向后兼容）+ `d496d74`（夹具改用多年度日历有效历史；回测名断言对齐 `event_v2_top_n_pipeline_demo`）。

### CAT-12-openbb-provider-history（0 项，已清零）
- 历版清零：commit `d496d74`——CN/OpenBB 历史用例补 `TushareClient.fetch_cn_daily_history` 与 `_fetch_with_eastmoney_cn` 桩，使 `OpenBBClient.fetch_historical_prices` 走打桩的 provider 回退分支。

### CAT-13-ai-daily-report-scope（0 项，已清零）
- 历版清零：commit `5977640`（`build_ai_daily_report` 对显式 tickers 恢复 `scope=custom_tickers` 显式范围出报）+ `6b2aa1d`（默认 CN 用例期望对齐全市场快照新契约 `portfolio_plus_cn_full_market_top5`，仅改测试期望）。

### CAT-2-watchlist-full-analysis-stub（0 项，已清零）
- 历版清零：commit `c2de781` 在 `app/api/routes/watchlist.py` 接入共享分析链（模块级导入 `safe_symbol_analysis` / `build_symbol_decision_brief` / `AIAnalysisService`，新增 `_watchlist_overview_from_item` / `_full_watchlist_analysis` / `_watchlist_ai_headline` 与 Decision Console 面板）。

### CAT-14-misc-regressions（0 项，已清零）
- 历版清零：commit `79cf8cb` 还原八项独立回归（`symbols.py` 恢复 `fetch_symbol_headlines`；`predictions.py` 候选 run 允许 `market in {market,"ALL"}` 且最新交易日按 market 过滤；`screener_pages.py` 快照页词表；watchlist Market 单元格 exchange 副行；`market_risk.py` 空 lake 空态；`jobs.py` send-ai-daily-report 传 `trusted_regime_snapshots`），并还原 `tests/test_app.py` 四处夹具。

### CAT-10-screener-cn-template-and-csv（0 项，已清零）/ CAT-11-screener-watchlist-actions-and-tv（0 项，已清零）
- 两类在 commit `68276bc`（焦点池夹具放量 + 技术评级默认渲染）清零，当前账本名集中不出现。

## 3. 附录：1 项完整清单

> 闭合校验：本表 1 个 id 与最新账本 `comparison.unchanged_failing`（`acceptance-ledger-20261008T225900-cat1c.json`）**逐一相等**；状态列取自账本 `tests[].status`。

| # | 状态 | 测试 id | 类别 |
|---|---|---|---|
| 1 | FAIL | `test_app.AppFlowTests.test_watchlist_add_and_sync_now_works` | CAT-7 |

（本版账本已无环境性失败；上一版 2 项概念页同步用例已随 `d6bbdc5` 显式打桩消除，见 §0。）

## 4. 边界与未决

- 本表登记的是 **E3 豁免边界内的存量失败**，不代表验收通过；任何修复都必须另立批次并补充独立证据。
- 分类沿用 R9 triage 的既有归因（按测试方法名映射）；**`CAT-7` 余 1 项标注 S-13 HK 同步语义待决策**，其余类别以本批收口提交为准。
- **代码冻结**：本版重算依据的收口提交为 `d6bbdc5`（环境噪声打桩）、`f14585b`（CAT-1 首页断言）、`fc04e9f`（CAT-1 模型徽标分位）、`5329982`（CAT-4 20 日标签）；对应冻结基准 **v41**（`data/artifacts/freeze-20261008-v41`，`scripts/freeze_quant_remediation_baseline.py --output-dir data/artifacts/freeze-20261008-v41 --snapshot-untracked`）随本次重算登记于 `acceptance/freeze-manifest-2026-10-05.md`；上一版基准为 v40（`data/artifacts/freeze-20261008-v40`）。
- **本批归零子因**（不计入上表）：
  - CAT-1 首页三段断言漂移（`f14585b`）与模型徽标恒 Neutral（`fc04e9f`）已消除，4 项转通过。
  - CAT-4 20 日标签字段级缺口（`5329982`）已消除，余 1 项转通过。
  - 概念页同步用例的美股盘中环境噪声（`d6bbdc5` 显式打桩 US 价格 provider）已消除，2 项转通过。
- **已闭环子因（更早批次，不计入上表）**：legacy 报告只读快照（CAT-3 `87fbde2`）；pipeline 夹具与市场日历（CAT-6 `13a9342`/`d496d74`）；CN provider 回退桩（CAT-12 `d496d74`）；AI 日报范围契约（CAT-13 `5977640`/`6b2aa1d`）；watchlist hero 文案（`d61203f`）；watchlist 共享分析链（CAT-2 `c2de781`）；CAT-14 八项回归（`79cf8cb`）；概念页空态三级回退（`ba188c4`）。
- **待决策（产品/口径，决策前不动断言）**：CAT-7-1 S-13 HK 同步语义；CAT-4 20 日明细的 OOS 校准快照缺失回退口径（本批新增）；`build_signal_label` 旧分数标度致买点/观察标签恒 Hold（本批新增）。三项均见 §1 说明。
- **不确定项**：`test_watchlist_uses_batched_prediction_queries`（原 CAT-15）本版账本仍记为通过，但该测试此前被独立复核报告 §5 R7 登记为**顺序/缓存敏感 flake**；单次通过不足以证明已根治，后续轮次若复发仍应归入 CAT-15。
- 负责人已归口 **Jacky Hu**、期限统一记为 **下一批次待排期**；实际进入排期以项目方下一批次计划为准（未确认前不臆造具体日期）。
- 本表数量与 **1**（E3）的对应关系由 `scripts/run_acceptance_suite.py` 产出的账本持续校验（最新：`acceptance-ledger-20261008T225900-cat1c.json` / `.md`）；账本与上一版名集差集若出现 `added > 0`，应按新失败另行处理，不得并入本表。
