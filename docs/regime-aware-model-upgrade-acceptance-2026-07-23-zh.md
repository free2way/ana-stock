# 市场状态感知模型升级验收文档

日期：2026-07-23  
对应方案：[市场状态感知的选股模型升级开发方案](./regime-aware-model-upgrade-plan-2026-07-23-zh.md)

## 1. 验收结论

本次完成方案的 Phase 1（严格评测口径）以及 Phase 2 中市场状态与候选风控的生产门禁接入。系统不再将“预测日期与训练区间重叠”的结果误报为严格样本外胜率；不足样本的模型明确为观察状态，不能作为普通 `READY` 候选。

Phase 3 的 XGBoost / CatBoost 赛马和 Phase 4 的状态内自动权重切换尚未启用。这是有意控制：当前还没有连续 20 个交易日的严格样本外记录，不能在缺少证据时接入新的生产模型或自动更换主模型。

## 2. 已交付功能与方案对照

| 方案项 | 验收结果 | 实现说明 |
|---|---|---|
| 严格滚动样本外标记 | 通过 | 新训练 run 写入 `walk_forward_purged_v1`、OOS 起始日和标签隔离期；评测依据该协议判断，而不再仅比较 `train_end`。 |
| 隔离期与可追溯数据版本 | 通过 | `ModelEvaluation` 保存 `purge_gap_days`、股票池版本、严格 OOS 样本数与覆盖交易日。 |
| 成本后收益与回撤 | 通过 | 1/3/5/10/20 日评测继续扣除往返成本，保存净收益、命中率、回撤及置信区间。 |
| 市场状态分层 | 通过 | 指标按预测当天持久化的 `market_regime` / `risk_regime` / `buy_gate` 分组，不将当前状态倒灌历史。 |
| 公司行为异常排除 | 通过 | 价格路径单日跳变超过阈值时排除，并记录排除计数。 |
| 启用门槛 | 通过 | 覆盖少于 20 个交易日或严格 OOS 样本少于 100 条时强制 `observation_insufficient_oos`；正收益也只能到 `eligible_for_champion_review`，不会自动生产启用。 |
| 候选页风险门禁 | 通过 | `BLOCK` 市场状态阻断候选；模型为 observation/unverified 时，原本 READY 的候选会降为 DEFER 并附加 `model-observation-only` 风险标签。 |
| 任务中心与评测页 | 通过 | 任务中心明确严格 OOS 口径；模型评测总览展示严格 OOS 样本、覆盖交易日、隔离期与启用状态。 |
| XGBoost / CatBoost 赛马 | 未启用 | 等待足够 OOS 数据后，以共享股票池、切分和成本假设接入，避免“为了换模型而换模型”。 |
| 自动状态权重与生产切换 | 未启用 | 需先完成状态内基线对照、最小样本和回滚机制。 |

## 3. 数据库兼容性

`init_db()` 的兼容迁移会为既有 PostgreSQL 数据库补充以下字段，不需要重建数据库：

- `oos_sample_count`
- `oos_coverage_days`
- `purge_gap_days`
- `benchmark_avg_return`
- `universe_version`
- `activation_status`

旧 run 没有滚动前推协议时，仍用保守的旧规则处理，并保持观察/未确认，不会被提升为严格样本外。

## 4. 自动验收结果

执行命令：

```bash
.venv/bin/python -m compileall -q app tests
.venv/bin/python -m unittest tests.test_model_evaluation tests.test_risk_guardrails tests.test_job_lineage
```

结果：`Ran 8 tests ... OK`。

覆盖的关键断言：

1. 成本后收益与最大回撤计算正确；
2. 疑似拆股/反向拆股价格跳变被排除；
3. 即使 run 的滚动训练边界延伸到后续日期，已记录的 walk-forward 预测仍按协议认定为严格 OOS；
4. 评测保存 OOS 样本数、覆盖天数、隔离期与观察状态；
5. `BLOCK` 市场状态会阻断强候选；
6. `REVIEW` 市场状态会推迟突破型候选；
7. 未达到严格 OOS 门槛的模型不能产生普通 READY 信号；
8. 任务谱系回归测试通过。

## 5. 上线后验证与下一步

1. 下一次 A 股/美股训练会生成协议版本为 `walk_forward_purged_v1` 的新 run；随后运行“结构化模型评测”。
2. 在模型评测总览确认至少 20 个交易日、每市场/状态/持有期达到样本下限后，再启动 XGBoost 与 CatBoost 的同口径赛马。
3. 只有在成本后收益、最大回撤和状态内稳定性都不劣于 LightGBM 基线时，才允许进入 `eligible_for_champion_review` 后的人工审批与受控切换。
4. `benchmark_avg_return` 已预留字段；首次赛马前应补齐市场等权/指数基准，以完成“模型超额收益”而非仅绝对收益的最终验收。

本验收不声明收益或胜率提升；其确认的是评测、状态门禁和候选降级已可追溯、可测试地生效。

## 6. 追加验收：全市场模板统一门禁与赛马启动条件

2026-07-23 已补充以下能力：

1. 全市场技术、形态、基本面、TradingView、强趋势二次启动和 LightGBM 排名模板统一经过 `apply_candidate_governance()`；Parquet 加速的 A 股/美股技术动量预计算路径也不再绕过该步骤。
2. 每条候选加载其最新模型 run 的 `activation_status`。`unverified` 或 `observation_*` 会附加 `model-observation-only` 风险标签，并将原本 `READY` 候选降为 `DEFER`。
3. 新增“模型赛马门槛检查”任务：每个市场需同时达到至少 20 个严格 OOS 交易日、100 条严格 OOS 样本，才允许启动 XGBoost/CatBoost 对照训练。
4. XGBoost 与 CatBoost 已加入运行依赖清单并安装到应用虚拟环境，训练器支持 `model_type=xgboost` 与 `model_type=catboost`；它们仍不会在门槛不足时触发。

生产库核对结果：现有 A 股、美股结构化评测均为旧口径记录，严格 OOS 样本数与覆盖交易日均为 `0`。因此赛马任务当前正确显示为 `waiting_for_oos`，没有启动任何 challenger 训练。
