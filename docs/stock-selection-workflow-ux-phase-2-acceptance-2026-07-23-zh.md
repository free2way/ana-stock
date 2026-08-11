# 选股流程产品收敛 · 第二阶段验收记录

日期：2026-07-23  
对应方案：[选股流程产品优化方案](stock-selection-workflow-ux-optimization-plan-2026-07-23-zh.md)

## 本阶段目标

把核心页面从“信息面板”推进为“行动队列”：候选页先做日常筛选，自选页先处理风险和主攻名单，持仓页先处理仓位动作。

## 已实现

### 发现候选

- 模型筛选首屏仍保留筛选总览和模板选择；
- 模型解释、运行回执、LightGBM 执行偏好、OOS/模板评测、多模型共振与质量画像统一收进默认折叠的“高级研究与模型证据”；
- 常用入口只保留工作台和自选，焦点池、市场快照、Kronos、数据同步和模型评测收进高级研究菜单；
- 页面继续明确：模型用于候选优先级，不能替代入场触发与失效条件。

### 自选与执行

- 使用已有的模型结论、执行风险标签和同步状态，将自选股划分为：主攻、观察、风险、归档；
- 风险队列和主攻队列默认展开，观察和归档按需展开；
- 带执行风险标签或 SELL/STRONG SELL 信号的股票优先进入风险队列，即使同步已关闭也不会被归档掩盖；
- 每项展示代码、市场、下一步、原因和置信度，点击可进入个股分析。

### 持仓与复盘

- 在组合总览后、完整持仓表前新增“今日处理优先”；
- 退出、减仓、复核与风险队列在此处前置，完整动作表继续保留在锚点 `#action-queue-detail`；
- 新增、导入导出、宽表和历史卖出记录不再抢占风险处理的优先级。

## 自动验证

```text
.venv/bin/python -m py_compile app/api/routes/screener.py app/api/routes/watchlist.py app/api/routes/portfolio.py tests/test_watchlist_workflow.py
.venv/bin/python -m unittest -v tests.test_workspace_nav tests.test_dashboard_workflow_home tests.test_watchlist_workflow
```

结果：6/6 通过。

额外覆盖：风险标签优先于归档；主攻、观察、归档、风险四类行动队列按预期分类。

## 未改变的边界

- 没有变更行情数据、模型、触发条件、后台 Job 或自动交易能力；
- A 股和美股仍按已有市场字段分组与统计；
- 行动队列是复核优先级，不构成自动交易或收益承诺。

