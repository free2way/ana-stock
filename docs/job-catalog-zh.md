# Job 目录与说明

更新时间：2026-04-25  
适用范围：`/Volumes/STORAGE_Jackyhu/code/ana`

## 1. 这份文档的目的

这份文档用来回答三个问题：

1. 这个应用里到底有哪些 job。
2. 哪些 job 是每天必须跑的核心生产任务。
3. 哪些 job 只是维护、补数、排障或人工操作时才需要跑。

当前系统已经不是单一 job，而是一个由多条任务链组成的研究工作台：

- A 股收盘后刷新链路
- 美股收盘后刷新链路
- 自选股/持仓分析链路
- 社交信号抓取链路
- 元数据与辅助缓存维护链路

不是所有 job 都要每天跑。  
真正需要稳定运行的，是少数几条核心链路。

## 2. Job 分层

建议把系统里的 job 分成 3 类看：

### A. 核心生产 job

这些 job 直接决定第二天你能不能看到最新行情、最新模型结果、最新日报。

### B. 后台维护 job

这些 job 不一定每天都影响主流程，但会影响覆盖率、页面速度、元数据质量、辅助分析质量。

### C. 手动运维 / 调试 job

这些 job 主要用于初始化、补历史、排错、模型试验，不应该作为每日固定生产链路。

## 3. 核心生产 Job

### 3.1 `cn_close_review`

定位：A 股收盘后的主入口 job，也是 A 股生产链路的总开关。  
代码位置：[app/services/close_review_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/close_review_scheduler.py:42)

当前职责：

- 刷新 A 股行情到本地 lake
- 重建自选相关技术快照
- 运行自选股分析
- 触发后续的 A 股训练、预计算、概念、基本面、新闻、市场快照等跟进 job

当前配置状态：

- 已启用
- 当前计划时间：`20:30 Asia/Shanghai`
- 当前 `refresh_limit=0`

这表示：

- 会跑
- 跑的是 A 股全市场，不只是自选股

最近实测耗时：

- `2026-04-24 18:00:19` 开始
- `2026-04-24 18:02:44` 完成
- 约 `145 秒`

这是 A 股生产链最核心的 job。  
如果它没跑，后面很多结果都会停在昨天。

### 3.2 `train_cn_signals`

定位：A 股 LightGBM 多因子训练 job。  
代码位置：[app/services/close_review_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/close_review_scheduler.py:483)

当前职责：

- 读取 A 股 Parquet lake
- 对全市场 A 股进行 LightGBM 信号训练
- 写入 `predictions / prediction_details`

最近实测耗时：

- `2026-04-24 18:02:44` 开始
- `2026-04-24 18:06:27` 完成
- 约 `223 秒`

最近样本规模：

- 训练 `5202` 只 A 股
- 写入 `149755` 条预测

这个 job 是模型选股页、AI 日报、交易就绪度等功能的重要输入。  
如果它没跑，A 股模型结果会停留在上一交易日。

### 3.3 `screener_precompute`

定位：把重型模型筛选结果提前算好并落库，避免页面实时计算。  
代码位置：[app/services/close_review_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/close_review_scheduler.py:338)

当前职责：

- 预计算 A 股核心模板和其他模板
- 写入 screener 快照
- 供 `/screeners`、首页、市场页、模型组合页直接读取

当前批次结构：

- `cn_full_market_core`
- `cn_full_market_rest`
- `watchlist`
- `full_market_all`

最近实测耗时：

- `2026-04-24 18:07:53` 开始
- `2026-04-24 19:16:35` 完成
- 约 `68 分钟`

这是当前最重的 A 股 job。  
系统已经是“刷新后自动预计算”，不是页面实时现算，但这条链路仍然偏重。

### 3.4 `market_snapshot_refresh`

定位：首页和市场概览等快照页面的快速缓存刷新。  
代码位置：[app/services/close_review_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/close_review_scheduler.py:686)

当前职责：

- 生成 `market_workspace`
- 生成 `premarket / monitor / postmarket`
- 生成热力图快照

最近实测耗时：

- 基本接近 `0~1 秒`

这个 job 不重，但很重要，因为它决定首页和市场页是不是直接秒开。

### 3.5 `us_market_close_refresh`

定位：美股收盘后的主入口 job。  
代码位置：[app/services/us_market_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/us_market_scheduler.py:13)

当前职责：

- 刷新美股 grouped daily
- 成功后自动触发美股信号训练
- 成功后自动触发美股 screener 预计算

默认配置：

- 已启用
- 默认时间：`10:00`（本地应用时区）

这是美股生产链的总开关，作用和 A 股的 `cn_close_review` 对应。

### 3.6 `us_signal_train`

定位：美股 LightGBM 信号训练。  
代码位置：[app/services/us_market_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/us_market_scheduler.py:165)

当前职责：

- 从美股 lake 训练 LightGBM
- 写入预测
- 顺带写回一部分回测行和 workspace 快照

这是美股模型选股和美股日报验证的重要输入。

### 3.7 `us_screener_precompute`

定位：美股筛选快照预计算。  
代码位置：[app/services/us_market_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/us_market_scheduler.py:222)

当前职责：

- 预计算美股 screener 模板结果
- 落库供页面直接读取

它的重要性和 A 股 `screener_precompute` 一样，只是美股当前模板范围更小。

### 3.8 `social_signal_poll`

定位：每 4 小时轮询 X 账号，抓取社交信号。
代码位置：[app/services/social_signal_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/social_signal_scheduler.py:11)

当前职责：

- 抓取你配置的 X 账号贴文
- 解析股票代码
- 写入社交信号结果

默认频率：

- `4 小时`

如果你的研究流程依赖社交信号，这条 job 就属于核心生产 job。  
如果你暂时不用 X 信号，它可以被视为“核心可选”。

## 4. 后台维护 Job

### 4.1 `us_symbol_metadata_refresh`

定位：补美股名称、交易所、行业、板块等元数据。  
代码位置：[app/services/us_symbol_metadata_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/us_symbol_metadata_scheduler.py:10)

默认配置：

- 已启用
- 默认时间：`23:00`
- 默认每次补 `300` 只

它不直接决定第二天模型有没有结果，但会影响：

- 美股热力图是否好看
- 市场概览里的 sector/industry 是否准确
- 页面标签是否完整

这是“重要维护 job”，不是主交易链入口。

### 4.2 `watchlist_auto_analysis`

定位：对自选股/持仓做自动分析。  
代码位置：[app/services/auto_analysis.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/auto_analysis.py:150)

当前职责：

- 对自选股做行情同步或直接读取 lake
- 训练自选/局部模型
- 跑回测
- 生成 AI 日报
- 推送消息

这条链更像“自选股工作台”任务，不是全市场刷新入口。  
如果你主要用首页、自选股、持仓日报，它很有价值。

### 4.3 `send_ai_daily_report`

定位：发送 AI 日报到已配置渠道。  
代码位置：[app/api/routes/jobs.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/jobs.py:214)

当前职责：

- 读取已生成的日报
- 渠道发送
- 记录发送结果

它不负责生产分析结果，只负责分发结果。

### 4.4 `sync_cn_fundamentals`

定位：同步 A 股基本面。  
代码位置：[app/services/close_review_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/close_review_scheduler.py:528)

影响范围：

- 基本面模板
- 估值、ROE、利润增速相关筛选
- 多模型组合里的基本面因子

### 4.5 `sync_cn_concepts`

定位：同步 A 股概念映射。  
代码位置：[app/services/close_review_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/close_review_scheduler.py:587)

影响范围：

- 市场概览中的概念板块
- 概念追踪
- 概念热力图

### 4.6 `news_enrichment`

定位：新闻增强与新闻快照。  
代码位置：[app/services/close_review_scheduler.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/close_review_scheduler.py:643)

影响范围：

- 新闻覆盖
- 新闻风险快照
- 自选股/持仓新闻卡片

这条 job 不是模型主链，但会影响资讯解释层。

## 5. 手动运维 / 调试 Job

这些 job 不是每天必须自动跑，更适合人工操作。

### 5.1 数据初始化 / 补历史

- `sync_cn_symbol_universe`
- `init_cn_market_data`
- `refresh_cn_market_data`
- `refresh_cn_market_data_daily`
- `refresh_cn_market_data_lake_only`
- `refresh_us_grouped_daily`
- `refresh_us_grouped_daily_range`

用途：

- 初始化全市场
- 补历史区间
- 修复缺失交易日
- 做全量或半全量重刷

### 5.2 模型与回测

- `train`
- `train_cn_signals`
- `train_us_signals`
- `backtest`
- `import_model_output`

用途：

- 手动训练
- 试验新参数
- 导入外部模型结果
- 重新回测

### 5.3 筛选与缓存

- `rebuild_technical_snapshots`
- `precompute_us_screeners`
- `screener_precompute`
- `market_snapshot_refresh`

用途：

- 修缓存
- 刷页面快照
- 校验预计算结果

### 5.4 数据维护

- `sync_global_fundamentals`
- `cleanup_market_csv`
- `cleanup_stale_jobs`

用途：

- 补全球基本面
- 清理历史 CSV
- 收口卡死任务

### 5.5 老链路 / 兼容链路

- `build_dataset`
- `run_pipeline`
- `sync_market_data`

这些更偏历史兼容或手工试验链路。  
当前主生产链已经以 `Parquet lake + LightGBM + 预计算快照` 为主，不建议把这些当成每日主流程。

## 6. 当前推荐的每日运行集合

如果目标是“每天复盘 + 第二天用模型选股”，我建议每日固定生产集合是：

### A 股

1. `cn_close_review`
2. `train_cn_signals`
3. `screener_precompute`
4. `market_snapshot_refresh`
5. `send_ai_daily_report`

### 美股

1. `us_market_close_refresh`
2. `us_signal_train`
3. `us_screener_precompute`

### 社交与维护

1. `social_signal_poll`
2. `us_symbol_metadata_refresh`

## 7. 当前链路的现实瓶颈

根据最近实测：

- `cn_close_review`：约 `2~3 分钟`
- `train_cn_signals`：约 `3~4 分钟`
- `screener_precompute`：约 `68 分钟`

所以真正的瓶颈不是行情刷新，也不是 LightGBM 训练，而是：

- A 股全市场 screener 预计算太重
- 部分历史运行里出现过 `Too many open files`

这意味着：

- “18:00 刷新后马上自动训练”是可行的
- “18:00 刷新后把所有模板都完整预计算完”也是可行的，但会慢，而且失败风险更高

更合理的架构是：

### 第一层：核心结果优先

先跑：

- `lightgbm_top_picks`
- `next_tesla_swing`
- `technical_momentum`
- 你最常用的多模型组合

目标是在 `18:10~18:25` 之间就拿到可用结果。

### 第二层：其他模板补齐

再跑：

- 形态模板
- 基本面模板
- 其它观察型模板

目标是后台慢慢补齐，不阻塞你最核心的选股流程。

## 8. 建议保留的最终 job 架构

建议未来长期保留下面这套结构：

### 主生产链

- `cn_close_review`
- `train_cn_signals`
- `screener_precompute`
- `screener_precompute_core`
- `screener_precompute_combos`
- `screener_precompute_rest`
- `market_snapshot_refresh`
- `send_ai_daily_report`
- `us_market_close_refresh`
- `us_signal_train`
- `us_screener_precompute`

其中 A 股筛选预计算现在建议按下面这条顺序拆开理解：

- `screener_precompute`
  负责作为总控父 job，串起下面三个阶段，并汇总最终结果。
- `screener_precompute_core`
  先产出 `lightgbm_top_picks`、`next_tesla_swing`、`technical_momentum` 这批最常用结果。
- `screener_precompute_combos`
  在核心模板快照已经存在的前提下，直接把多模型组合结果提前落库。
- `screener_precompute_rest`
  后台补齐其余 A 股全市场模板、观察型模板和 watchlist 快照。

### 辅助链

- `social_signal_poll`
- `us_symbol_metadata_refresh`
- `sync_cn_fundamentals`
- `sync_cn_concepts`
- `news_enrichment`

### 运维链

- `refresh_cn_market_data_lake_only`
- `refresh_us_grouped_daily_range`
- `cleanup_market_csv`
- `cleanup_stale_jobs`
- `rebuild_technical_snapshots`

## 9. 一句话总结

当前系统里的 job 很多，但真正影响每日使用体验的主链只有几条：

- A 股收盘刷新
- A 股 LightGBM 训练
- A 股核心筛选预计算与组合预计算
- 美股收盘刷新
- 美股训练与预计算
- 社交信号轮询

其余 job 更多是维护、补数、调试和人工运维工具。  
后续优化的重点，不是继续增加 job 数量，而是把重计算 job 拆成“核心先出结果、组合快速可用、剩余后台补齐”的分层架构。
