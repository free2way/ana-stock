# 盘后选股命中率提升 v1 验收文档

更新时间：2026-04-29

关联文档：

- [A 股盘后短线选股模型整改验收文档](/Volumes/STORAGE_Jackyhu/code/ana/docs/a-share-after-hours-screener-acceptance-plan-zh.md)
- [盈利导向模型选股增强技术方案与验收文档](/Volumes/STORAGE_Jackyhu/code/ana/docs/profit-oriented-model-selection-acceptance-plan-zh.md)
- [盈利导向模型选股验收清单](/Volumes/STORAGE_Jackyhu/code/ana/docs/profit-oriented-model-selection-acceptance-checklist-zh.md)

## 1. 文档目标

本文档用于跟踪“每日盘后股票筛选命中率提升 v1”的开发与验收。

本阶段目标不是让系统承诺稳定盈利，也不是接入交易平台，而是让系统更贴近业余股票交易员的真实盘后决策流程：

1. 盘后能从全市场完整候选池中筛选股票。
2. 模型评测更接近次日真实可执行收益，而不是只看 close-to-close。
3. LightGBM 给出的预期收益与回撤更接近样本外表现。
4. AI 日报 Top 5 与模型评测总览保持一致。
5. 推荐准入规则能根据市场强弱、涨跌停制度和追高风险动态调整。

最终验收标准是：用户每天收盘后打开应用，能更清楚地知道“明天优先看什么、什么不能追、哪个模型近期更值得信”。

## 2. Review Findings 对应整改范围

| Finding | 问题 | 优先级 | 目标结果 |
| --- | --- | --- | --- |
| F1 | 技术形态全市场筛选仍在过滤前硬截断 | P1 | 全量使用技术快照，避免漏掉符合条件股票 |
| F2 | LightGBM 预期收益校准来自训练集内分桶 | P1 | 增加样本外校准，降低乐观偏差 |
| F3 | 模型评测偏 close-to-close | P1 | 增加次日执行口径评测 |
| F4 | AI 日报 Top 5 未纳入 LightGBM 和多模型组合主力模板 | P2 | 日报候选来源跟随模型评测优先级 |
| F5 | 推荐准入阈值固定 | P2 | 准入阈值随市场环境和板块制度动态调整 |

## 3. P0 / P1 开发项

### 3.1 全市场技术形态筛选取消过滤前截断

代码范围：

- [app/services/screener.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/screener.py)
- [app/services/screener_snapshots.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/screener_snapshots.py)
- [app/services/technical_snapshot_cache.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/technical_snapshot_cache.py)

当前问题：

- `_screen_technical_patterns` 对 A 股全市场先把候选压到 `80`。
- `_screen_next_tesla_swing` 对 A 股全市场先把候选压到 `180`。
- 后续再做趋势分、量能、形态和交易就绪度过滤，存在漏选风险。

实施要求：

1. 全市场 A 股技术形态筛选必须优先读取完整 `technical_snapshot`。
2. 有 `required_patterns` 时，先对完整快照做布尔过滤。
3. 无 `required_patterns` 时，可以按轻量评分排序，但候选池数量必须可配置，且默认不低于全市场有效快照数量。
4. 页面请求不做全市场重计算，只读预计算快照。
5. 如果候选为空，页面需要说明是“形态不满足”还是“快照缺失”，不能只显示空结果。

验收标准：

- Screeners 全市场筛选时，候选池统计展示 `universe_count / snapshot_count / matched_pattern_count / filtered_count`。
- “底部放量突破”“均线多头排列”“布林带收口待突破”“三连阳”等筛选不因 80/180 截断导致结果失真。
- 多模型组合筛选为空时，页面能显示具体原因。
- `screener_precompute` job 完成后，页面访问不触发全市场逐票计算。

### 3.2 LightGBM 增加样本外校准

代码范围：

- [app/services/trainer.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/trainer.py)
- [app/services/template_evaluation.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/template_evaluation.py)
- [app/services/repository.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/repository.py)

当前问题：

- 当前 `calibration_buckets` 来自 `train_window` 内部。
- 这能解释训练集分数区间，但不能代表最近真实盘后使用效果。
- AI 日报和 Screeners 如果直接信这个 expected return，容易过度乐观。

实施要求：

1. 保留现有训练内校准作为 fallback。
2. 新增样本外校准逻辑，使用最近已完成预测日的真实表现。
3. 每个分数桶至少输出：
   - `sample_count`
   - `next_1d_avg_return`
   - `next_1d_hit_rate`
   - `next_3d_avg_return`
   - `next_3d_hit_rate`
   - `max_drawdown_avg`
   - `execution_hit_rate`
4. 样本外校准结果写入 artifact 或 workspace snapshot。
5. Screeners、AI 日报、模型评测展示预期收益时，优先使用样本外校准。

建议新增快照类型：

```text
model_calibration_snapshot
```

验收标准：

- 模型训练或后处理 job 能生成样本外校准结果。
- 模型评测页面能区分“训练内估计”和“样本外真实表现”。
- 当样本外样本不足时，页面明确提示“样本不足，暂不强信”。
- AI 日报 Top 5 不直接使用训练内乐观收益作为唯一排序依据。

### 3.3 模型评测增加次日执行口径

代码范围：

- [app/services/template_evaluation.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/template_evaluation.py)
- [app/api/routes/dashboard.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/dashboard.py)
- [app/services/model_selection_guidance.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/model_selection_guidance.py)

当前问题：

- `template_forward_return_from_history` 主要计算信号日收盘价到未来收盘价。
- 盘后策略真实执行通常是次日开盘、开盘后确认、回踩买入或突破买入。
- close-to-close 无法识别高开低走、涨停买不到、盘中回撤过深。

实施要求：

新增执行评测函数，例如：

```text
template_execution_profile_from_history
```

每条样本至少输出：

- `next_open_gap_pct`
- `next_open_to_high_pct`
- `next_open_to_close_pct`
- `next_low_drawdown_pct`
- `next_close_return_pct`
- `max_3d_high_return_pct`
- `max_3d_drawdown_pct`
- `gap_blocked`
- `limit_unbuyable`
- `tradable_next_day`
- `execution_hit`

执行命中建议定义：

- 默认：次日可交易，且 `next_open_to_high_pct >= 2.0`，同时 `next_low_drawdown_pct > -4.0`。
- 弱市：要求更严格，例如 `next_open_to_high_pct >= 2.5` 且回撤不超过 `-3.0`。
- 强市：可放宽部分追高约束，但必须保留 `gap_blocked` 和 `limit_unbuyable`。

验收标准：

- `/dashboard/model-performance?lang=zh` 展示每个模型的 close-to-close 与 execution-based 两套结果。
- 模型评测总览展示 `可交易命中率`、`高开买不到比例`、`高开低走比例`、`平均最大回撤`。
- 强票反向归因页面能看到哪些模型在前一日捕捉到了次日强票。
- AI 日报推荐理由包含“明天如何验证买点”，而不是只显示模型分。

## 4. P2 开发项

### 4.1 AI 日报 Top 5 来源跟随模型评测

代码范围：

- [app/services/ai_daily_report.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/ai_daily_report.py)
- [app/services/model_selection_guidance.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/model_selection_guidance.py)
- [app/services/screener_snapshots.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/screener_snapshots.py)

当前问题：

- 日报全市场候选主要来自固定模板：
  - `technical_momentum`
  - `cn_bollinger_squeeze_watch`
  - `cn_three_white_soldiers`
  - `cn_volume_breakout`
- 未显式纳入 `lightgbm_top_picks` 和多模型组合快照。
- 这会导致 AI 日报推荐与模型评测总览的“优先模型/优先组合”不一致。

实施要求：

1. AI 日报候选池构建时，先读取 `model_selection_guidance_snapshot`。
2. 将 `top_model` 对应模板加入候选池。
3. 将 `top_combo` 对应的组合预设加入候选池。
4. 固定技术模板作为保底来源，而不是唯一来源。
5. 每只 Top 5 股票记录来源：
   - `source_template`
   - `source_combo`
   - `source_model_recent_hit_rate`
   - `source_reason`

验收标准：

- AI 日报页面显示“今日 Top 5 来源于哪些模型/组合”。
- AI 日报推荐与模型评测总览的优先模型不冲突。
- 如果优先模型样本不足，日报明确提示“样本不足，降级使用固定技术模板”。
- Telegram 推送和历史日报详情保留来源说明。

### 4.2 推荐准入阈值动态化

代码范围：

- [app/services/ai_daily_report.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/ai_daily_report.py)
- [app/services/tradability_filter.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/tradability_filter.py)
- [app/services/market_context.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/market_context.py)

当前问题：

- `RECOMMENDATION_MIN_READINESS`
- `RECOMMENDATION_MAX_RISK_FLAGS`
- `RECOMMENDATION_CHASE_MOMENTUM_5`

这些阈值目前偏全局固定，不能适配 A 股不同市场阶段。

实施要求：

1. 新增动态准入配置函数，例如：

```text
build_recommendation_gate_config(market_snapshot, candidate)
```

2. 根据以下因素动态调整：
   - 市场状态：`risk_on / watchful / defensive`
   - 市场广度
   - 涨停家数
   - 炸板率
   - 题材拥挤度
   - 股票涨跌停制度：`5cm / 10cm / 20cm / 30cm`
   - 候选类型：`pullback / breakout / momentum / support_hold`

3. 输出人话解释：
   - 弱市突破确认不足
   - 连续加速后不适合追高
   - 强势题材允许观察，但只等回踩或分歧转一致
   - ST 或低流动性股票降低优先级

验收标准：

- 同一只股票在强市和弱市下，`trade_readiness_score` 或 gate 结论可能不同。
- AI 日报能说明“今天为什么提高/降低推荐门槛”。
- Screeners 中 `Do Not Chase / Blocked` 原因更可读。
- 推荐 Top 5 不再只靠固定 18% 追高阈值判断。

## 5. Job 链路要求

每日 18 点 A 股收盘主链路应包括：

1. `cn_close_review`
2. `technical_snapshot_refresh`
3. `train_cn_signals`
4. `screener_precompute_core`
5. `screener_precompute_combos`
6. `screener_precompute_rest`
7. `model_selection_guidance_snapshot`
8. `model_calibration_snapshot`
9. `generate_ai_daily_report`

链路要求：

- `generate_ai_daily_report` 必须在核心预计算、组合预计算、模型使用建议、样本外校准之后执行。
- 页面访问不得触发全市场大计算。
- 任一关键 job 失败时，任务中心必须显示失败原因和影响范围。

验收标准：

- `/dashboard/ops?lang=zh` 能看到以上关键 job 最近状态。
- AI 日报的数据日期、Screeners 快照日期、模型评测日期一致。
- 任务中心能区分“行情刷新成功但模型预计算失败”和“全链路完成”。

## 6. 页面验收路径

### 6.1 Screeners 验收

URL：

```text
/screeners?lang=zh
```

操作：

1. 选择 A 股全市场。
2. 分别测试技术动量、放量突破、布林带收口、三连阳。
3. 测试多模型组合预设。

通过标准：

- 页面展示候选池统计。
- 筛选为空时能解释原因。
- 页面加载不触发全市场实时计算。
- 全市场技术筛选不再受 80/180 预筛限制。

### 6.2 模型评测验收

URL：

```text
/dashboard/model-performance?lang=zh
```

操作：

1. 查看 LightGBM、技术模板、多模型组合表现。
2. 对比 close-to-close 与 execution-based 评测。
3. 查看强票反向归因。

通过标准：

- 能看到近期优先模型。
- 能看到近期优先组合。
- 能看到可交易命中率。
- 能看到高开低走、买不到、回撤过深等失败原因统计。

### 6.3 AI 日报验收

URL：

```text
/dashboard/ai-daily-report?lang=zh
```

操作：

1. 查看全市场 Top 5。
2. 检查每只股票来源模板和组合。
3. 检查不能买原因和明日买点验证。
4. 打开历史日报详情。

通过标准：

- Top 5 来自全市场。
- Top 5 来源与模型评测总览一致。
- 每只股票有“为什么入选 / 为什么不能追 / 明天如何验证”。
- Telegram 推送与历史日报详情不丢失这些字段。

## 7. 数据验收指标

每次验收至少记录：

| 指标 | 目标 |
| --- | --- |
| A 股 universe 数量 | 接近当前可交易 A 股全市场 |
| technical_snapshot 覆盖率 | 大于 95% |
| Screeners 预计算模板数 | 核心模板和组合模板均完成 |
| LightGBM 最新 run 日期 | 等于最近交易日 |
| 样本外校准样本数 | 不少于 100 条，低于该值必须提示样本不足 |
| AI 日报 Top 5 来源完整率 | 100% |
| 推荐后验追踪 | 至少支持 1D / 3D / 5D |

## 8. 验收状态表

| 编号 | 项目 | 优先级 | 当前状态 | 验收结论 |
| --- | --- | --- | --- | --- |
| H1 | 全市场技术形态筛选取消过滤前截断 | P1 | 已开发 | 待人工验收 |
| H2 | LightGBM 样本外校准 | P1 | 已开发 | 待跑夜间 job 后验收 |
| H3 | 模型评测增加次日执行口径 | P1 | 已开发 | 待人工验收 |
| H4 | AI 日报 Top 5 跟随模型评测优先级 | P2 | 已开发 | 待人工验收 |
| H5 | 推荐准入阈值动态化 | P2 | 已开发 | 待人工验收 |
| H6 | 任务中心展示新链路状态 | P2 | 已开发 | 待人工验收 |
| H7 | Telegram 与历史日报展示执行理由 | P2 | 已增强 | 待复验 |

## 9. 阶段签收标准

### P1 签收标准

满足以下条件可签收 P1：

- Screeners 不再因技术形态预筛截断造成漏选。
- LightGBM 能展示样本外校准结果。
- 模型评测页能展示 execution-based 评测。
- 强票反向归因能说明前一日哪些模型捕捉到次日强票。

### P2 签收标准

满足以下条件可签收 P2：

- AI 日报 Top 5 来源与模型评测总览一致。
- 推荐准入规则随市场环境动态调整。
- 每只推荐股都有明日执行条件和不能追原因。
- 历史日报和 Telegram 推送保留完整推荐依据。

## 10. 最终验收口径

本阶段完成后，用户每天盘后应能在 10 分钟内完成以下判断：

1. 今天行情和模型是否已经刷新完成。
2. 今天应该优先使用哪些模型或模型组合。
3. 哪些股票进入明日重点观察池。
4. 哪些股票虽然模型分高但不能追。
5. 过去几天哪个模型真实命中率更高。
6. 今天 AI 日报 Top 5 为什么和模型评测结论一致。

如果以上 6 点都能稳定回答，则本阶段可以签收。

## 11. 本轮开发签收记录

时间：2026-04-29

已完成：

- H1：取消 A 股全市场技术动量、技术形态、强趋势二次启动在过滤前的小样本硬截断，改为依赖后台预计算快照和全量候选过滤。
- H2：新增 `model_calibration_snapshot` 任务链路，LightGBM 训练优先读取历史预测落地后的样本外 score calibration buckets；没有快照时才回退训练窗口分桶。
- H3：模型评测新增次日执行口径，包括次日开盘缺口、开盘到最高、开盘到收盘、盘中最大回撤、买不到/涨停阻断、高开低走、可交易命中率。
- H4：AI 日报候选源从固定技术模板扩展为固定模板 + 模型评测优先单模型 + 优先多模型组合快照。
- H5：推荐准入规则加入市场环境、市场广度、涨跌停板幅度动态调整，不再只用固定阈值。
- H6：任务中心流水线增加“模型样本外校准”节点，Screeners 运行收据展示候选返回数、快照落库数和上限。
- H7：AI 日报候选保留来源榜单、不能买原因、执行校验说明，历史日报和推送文本可继续复用这些字段。

自动验证：

- Python 语法检查通过。
- LightGBM 评测冒烟通过：最近 8 个 run、Top20 样本共 160 条，生成 10 个样本外校准桶。
- AI 日报构建冒烟通过：可读取 9 个候选来源，其中包含模型评测优先模板和组合快照；当前样本因交易纪律全部拦截，属于可解释的空推荐状态。
