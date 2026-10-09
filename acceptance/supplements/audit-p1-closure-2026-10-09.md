# 审核闭环记录：P1/P2 逐条复现—修复—验收（2026-10-09）

- 范围：三路**独立审核**发现的问题项，按「复现 → 修复 → 定向回归 → 合树全量验收」闭环。
- 三路：**A 砺行**（`app/services/trainer.py`，选样/标签/评估口径）、**C 秉直**（`app/services/backtesting/*` + `app/services/portfolio_book.py`，执行约束与账本一致性）、**B 容之**（校准产物适用范围 / 情绪面板选样 / `/health/ready` 暴露）。
- **条目数口径提示**：委派任务书写「7 条 P1/P2」。按三路实际产出逐条核对，实为 **8 条**（A 3 + C 2 + B 3），且与 8 个修复提交一一对应；本文按事实列 **8 条**，未合并、未删减。若以「7 条」为口径，差异点在于其中一路（C / 秉直）实为 2 条独立问题（P1-A、P1-B），非 1 条。
- 复现/对照脚本位于被忽略的 `tmp/`（`tmp/audit_p1/*`、`tmp/p1a_repro.py`、`tmp/p1b_repro.py`、`tmp/audit3_repro_*.py`），不随仓库交付；具体测试名见各条「回归测试」。

## 逐条闭环

| # | 审核项（级别 + 位置） | 复现结论 | 修复（commit + 代码位置） | 回归测试 |
|---|---|---|---|---|
| A-1 | **P1** · 标的池过滤删除行情行 → 真实入场/持有窗口位移（`trainer.py::_apply_pit_universe_filter` → `_build_lightgbm_samples`） | 属实 | `e25e160` · 过滤改为**样本门控**（`pit_universe_allowed` / `pit_universe_reasons`），完整时间轴保留；`app/services/trainer.py:1770` 处按门控跳过被拒信号日 | `UniverseFilterTests.test_filter_gate_does_not_move_next_session_entry_or_hold_period`、`…test_gate_counts_signal_days_it_suppresses` |
| A-2 | **P1** · 校准按 4096 行分块手工拼**原始特征**，与训练/推理的截面 winsor/MAD 变换不一致（`trainer.py::_build_score_calibration`） | 属实 | `2eb469c` · 校准先对整窗调用 `_feature_matrix`（`trainer.py:2297`，内部按 `trade_date` 分组，同日截面不被切断），分批只用于预测 | `WinsorizeAndObjectiveTests` 三项：`test_calibration_shares_the_fit_and_inference_feature_space`、`…test_calibration_matches_shared_space_across_dates`、`…test_calibration_batching_keeps_a_same_day_cross_section_whole` |
| A-3 | **P1** · OOS 评估名单「先剔除缺 label、再取 top-N」，被事后结果可用性顶替（`trainer.py` 旧 `matured_pairs[:TOP_N]`） | 属实 | `9a7bc4e` · 新增 `_freeze_oos_candidates`（`trainer.py:2397`）先按分数冻结 top-N，再原地分类计数（missing/immature/outcome + 原因），并在 `trainer.py:3110` 落 `oos_candidate_audit` | `OosCandidateSelectionTests` 四项（`test_missing_exit_price_keeps_the_top_ranked_name_and_counts_it`、`test_rejected_outcome_is_counted_separately_from_immaturity`、`test_oos_summary_aggregates_the_unusable_candidate_counts`、`test_oos_summary_stays_unavailable_when_nothing_matured`）+ `test_trainer_promotion_evidence` 加强 |
| C-1 (P1-A) | **P1** · 期末强平绕过 A 股 T+1 可卖校验（`backtesting/engine.py` 退出循环只看 `exit_date<=trade_date` 与 `force_liquidation`） | 属实 | `015ef8d` · 新增 `market_rules.sell_reject_reason`（`market_rules.py:62`）作**唯一**卖出校验点（CN 同日建仓返回 `cn_t1_same_session_exit`）；`engine.py:344` 改走统一校验，被拦仓保留 open 并在 rejects / `gate_stats` / `outcomes.block_reason`（`engine.py:473`）显式可见 | `EventDrivenBacktestTests`：`test_cn_end_of_period_liquidation_cannot_sell_a_same_session_lot`、`test_cn_end_of_period_liquidation_still_sells_lots_bought_earlier`、`test_us_end_of_period_liquidation_keeps_same_session_ttm0_exit` |
| C-2 (P1-B) | **P1** · 卖出**非原子**（positions 与 trades 两次独立 commit，可半成功）+ NaN/Inf 绕过数值比较导致持仓被移除（`portfolio_book.py`） | 属实 | `36ca9fe` · 单事务原子提交（`portfolio_book.py:268/308/333`）；`_lock_portfolio_book`（`:125`）`pg_advisory_xact_lock` 串行化；`idempotency_key` + 服务端 nonce（`app/api/routes/portfolio.py`）；`_require_finite` 等（`:96`）拒 NaN/Inf/负/零且不改状态 | `tests/test_portfolio_book_atomicity.py` 六项 + `tests/test_app.py` 路由端到端 |
| B-1 | **P1** · 概率校准产物为**单一全局文件**：市场串用、历史请求可加载晚于请求日的拟合、失效后仍可加载（`stock_selection/reliability_artifacts.py` / `selective_calibration.py`） | 属实 | `4306b4e` · payload 增 `market`/`model_key`，磁盘按 市场/模型/持有期/分数口径 分区（`calibration_artifact_path`，`:864`）；`resolve_calibration_artifact`（`:1188`）校验 scope 并拒绝未来产物；`inject_calibration_defaults`（`:1280`）输出 `applied` / `uncalibrated:<reason>`；thin refresh 写失效标记（`:894`） | `tests/test_reliability_artifact_wiring.py::CalibrationScopeIsolationTests` 五项（市场隔离、未来产物、`insufficient_samples` 失效、horizon/score/model 错配、无产物显式未校准） |
| B-2 | **P2** · `/health/ready` 依赖失败仍返回 **200**，且响应体含 DB 异常文本与产物绝对路径（`app/api/main.py`） | 属实 | `7a3a729` · 拆出 `_collect_readiness_checks`（`main.py:282`）；公开 `readiness`（`:404`）仅返回状态，依赖探针报错返回 **503** 并写 warning 日志；详情移至鉴权端点 `readiness_diagnostics`（`:425`，`/health/ready/diagnostics`） | `tests/test_health_readiness_exposure.py::ReadinessExposureTests` 五项（503 且无内部信息、诊断端点需鉴权、`missing/stale/degraded` 仍 200、公开摘要仅状态、liveness 语义不变） |
| B-3 | **P1** · 情绪实验面板**先剔除无前向收益行、再取 top-N**，冻结的第 1 名缺价被下一名补位（`stock_selection/sentiment_forward_batch.py`） | 属实 | `3ef5f28` · 先按冻结排名切 `treated_frozen/control_frozen` 再逐一测量；无收益冻结名计入 `missing_returns`（`:799/848`）、`daily` 增 `missing_tickers` / `treated_frozen`（`:887`），不补位 | `tests/test_sentiment_forward_batch.py::SentimentForwardSelectionBiasTests` 三项；既有 `treated=4/control=8` 断言不变 |

## 残余与未扩围项（如实记录）

1. **reliability 元数据仍为单一全局文件**：本批只对概率校准产物做了 scope 分区 + thin refresh 失效标记；**reliability 元数据**本身仍是全局单文件、未按市场/模型分区。作为同类残余风险登记，**未扩围**。
2. **生产旧产物需下次调度刷新**：`data/artifacts/stock_selection_research/calibration/probability_calibration_latest.json`（无 `market` 标记）按设计不再被任何市场请求加载。需**下一次调度刷新**才生成带 scope 的新产物；在此之前发布估计为「**未校准**」（`uncalibrated:<reason>`），非静默回退到旧拟合。
3. **账本迁移属中期事项**：`portfolio_book` 仍以 `app_settings` 整段 JSON 承载持仓/流水（原子性、锁、幂等键均在应用层保证）。迁移到结构化 `portfolio_positions`/`portfolio_trades` 表（`Numeric` 精度、唯一索引、行级锁/乐观 version 列）为**中期事项**，本批未做。
4. **生产 runner 默认未开 `liquidate_at_end`**：C-1 的期末强平缺陷在生产默认配置下不触发（默认 `False`），本次为**防御性修复**；经引擎配置项即可触发（外部审核即用引擎直跑复现）。

## 合树全量验收

- 背景：三路各自验收时均报 `added=0`，但三批改动**从未在合树上跑过一次干净全量**。经核对提交时间：秉直的两个提交（`015ef8d` / `36ca9fe`）落盘于 **11:50:51–11:50:56**，晚于三路各自的“最终”全量（11:23 / 11:28 / 11:42）；即**此前所有 `added=0` 的账本均不含这两个提交**。本次为首次在合树上的干净全量。
- 方法：确认无其它 agent 在跑验收（`ps` / `pg_stat_activity` 仅本进程），自建隔离库 `pqw_close_test`（`scripts/setup_test_database.py --init-schema`）设置 `PQW_TEST_DATABASE_URL`，**后台 detached** 运行：

```
.venv/bin/python scripts/run_acceptance_suite.py \
  --baseline tmp/acceptance-cat1c/regression-with-postgres-acceptance-20261008T225900-cat1c.log \
  --output-dir tmp/acceptance-close-20261009 --label close2
```

### 首次合树全量（`close2`，2026-10-09T12:09:07）

- 计数：`Ran 1739 / pass 1733 / fail 3 / error 1 / skip 2`（基线 1706 → 新增 33 项为本批/他批新增用例）、`not_collected 0`。
- 相对基线：**added = 3 / removed = 0 / changed = 0 / unchanged_failing = 1**。`unchanged_failing = 1` 即既有债务 **CAT-7-1**（`test_app.AppFlowTests.test_watchlist_add_and_sync_now_works`）。
- `added` 三项：
  - `test_app.AppFlowTests.test_sell_route_rejects_nan_quantity_without_changing_the_book`（ERROR）
  - `test_engine_default_and_manifest_binding.EngineDefaultTests.test_adjustment_version_binds_actions_and_view_hashes`（FAIL，0.55ms）
  - `test_optin_audit.OptinAuditTests.test_disabled_optin_has_no_reason_requirement`（FAIL，0.32ms）
- 运行日志/账本：`tmp/acceptance-close-20261009/regression-with-postgres-acceptance-20261009T120907-close2.log`；`acceptance-ledger-20261009T120907-close2.md` / `.json`。

### 三项 `added` 的定性与根因（**均非产品回归**）

- **单一根因**：ERROR 的 `test_sell_route_...` 在 `AppFlowTests.setUp` 的 `truncate_test_tables(...)`（`tests/test_app.py:48`）触发 `psycopg.errors.DeadlockDetected`（两条连接互相等对方持有的 relation 锁）。**setUp 抛异常 → unittest 不执行 tearDown → `_set_test_environment()` 已写入的 `PQW_DATA_DIR`（临时目录）/ `PQW_OPTIN_REASON`（夹具串）泄漏到其后按字母序执行的模块**，导致另外两项在本机全量中失败。
- **机制验证（可复现）**：
  - `PQW_DATA_DIR=<空目录>` 单独跑 → `test_adjustment_version_binds_actions_and_view_hashes` 同样报 `'actions=' not found in 'raw_prices_with_actions_v1'`（该用例读 `get_settings().data_dir` 下的 `corporate_actions` / 调整后视图快照，临时目录下为空）；不设该变量→通过。
  - `PQW_OPTIN_REASON="test_app end-to-end raw label fixture"` 单独跑 → `test_disabled_optin_has_no_reason_requirement` 报 `'test_app end-to-end raw label fixture' is not None`（`build_optin_audit` 会回退读该环境变量）；不设→通过。
- **单独复跑（三项各自/合并）均 PASS**：`python -m unittest <三项>` → `Ran 3 ... OK`。结论：三项是 **DB 并发死锁 → setUp 中止 → 环境变量泄漏** 的测试编排伪失败（harness artifact），**不是本批 8 个提交的产品缺陷**；死锁本身与“并发 sibling 全量”历史现象同类。
- 与既有记录的印证：砺行在 11 前后的中间全量中亦曾报同一批 `test_engine_default_and_manifest_binding` / `test_optin_audit` 失败，并判定为其他 agent 未提交半成品引起的泄漏 —— 现两处现象同源，属测试隔离问题。
- **工作树提示**：本次全量在当前工作树上运行，该树含 2 个**作者不明**的未提交测试改动（`tests/test_price_basis_contract.py`、`tests/test_runner_corporate_actions.py`）。二者按字母序位于 `test_engine_*` / `test_optin_audit` **之后**，且仅用上下文管理器 patch，**不可能**触发上述泄漏；本批未提交、未纳入。

### 复跑来判定瞬态与否（`close3`，2026-10-09T12:29:46）——确认为瞬态

- 由于首轮根因是**瞬时 DB 死锁**，以同参复跑第二次全量（新目录 `tmp/acceptance-close2-20261009`）：
  - **结果：`added = 0 / removed = 0 / changed = 0 / unchanged_failing = 1`**；`Ran 1739 / pass 1736 / fail 1 / error 0 / skip 2 / not_collected 0`；唯一失败仍为 **CAT-7-1**。
  - 运行日志：`tmp/acceptance-close2-20261009/regression-with-postgres-acceptance-20261009T122946-close3.log`（SHA256 `16796e5cccadaad2ba271c785192a3e9ccb516b6fcb157096b751c7e9eaa3a52`）；账本 `acceptance-ledger-20261009T122946-close3.md` / `.json`。
- 结论：首轮 3 项 `added` 确认为**瞬态测试编排伪失败**（非稳定失败、非产品回归）。**合树干净全量 = `added=0`，仅剩 CAT-7-1**。

> **说明**：本批 8 个提交的**产品代码回归集为空**（首轮三项 `added` 已由机制验证 + 单独复跑 + 第二轮 `added=0` 三重排除）。存量债务总数不因本批变化，仍为 **1**（仅 CAT-7-1）。

## 结论

- 8 条 P1/P2 全部**复现属实**并经定向回归确认修复；**合树干净全量两轮**：首轮出现 3 项 `added`（经机制验证 + 单独复跑判定为 **DB 并发死锁 → setUp 中止 → 环境变量泄漏** 的测试编排伪失败），次轮即 **`added=0`**——**产品代码回归集为空**。
- 存量债务总数不因本批变化，仍为 **1**（仅 **CAT-7-1**）。
- 样本集变化说明见 `acceptance/signoff/acceptance-debt-registry-zh.md` §「本轮审核修复的样本集影响」。
- **后续建议（只陈述，未擅自动手）**：修复 `tests/test_app.py` 的测试隔离（`setUp` 内完成的 env 变更应在 `setUp` 失败时也能回滚，或改为 `addCleanup`/`patch.dict`），以消除“死锁 → 泄漏 → 下游误报”的传导。
