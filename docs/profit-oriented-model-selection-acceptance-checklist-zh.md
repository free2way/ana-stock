# 盈利导向模型选股验收清单

更新时间：2026-04-25

阶段性签收记录见：[profit-oriented-model-selection-signoff-record-zh.md](/Volumes/STORAGE_Jackyhu/code/ana/docs/profit-oriented-model-selection-signoff-record-zh.md)

本文档是 [profit-oriented-model-selection-acceptance-plan-zh.md](/Volumes/STORAGE_Jackyhu/code/ana/docs/profit-oriented-model-selection-acceptance-plan-zh.md) 的落地验收版，用来回答三件事：

- 现在到底做到了哪一步。
- 每一条验收项应该去哪个页面看。
- 当前是“可验收 / 基本可验收 / 部分完成”中的哪一种。

---

## 1. 当前验收结论

### 1.1 总体判断

- P0：`已基本完成，可验收`
- P1：`大部分完成，少数项仍需继续打磨`
- 当前版本：`适合做阶段性签收`

### 1.2 一句话结论

当前系统已经从“模型选股页面集合”升级成了“带交易纪律、带模型使用指导、带日报留档、带历史验证”的业余交易员复盘工作台。P0 主线已经形成闭环，第三阶段里的组合风险摘要也已经补到第一版可用水位，P1 剩余主要集中在数据质量解释力和后续增强深度。

---

## 2. P0 验收映射

### 2.1 交易准入评分

状态：`可验收`

页面入口：

- [模型选股](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/screener.py)
- [AI 日报](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/dashboard.py)

实际访问路径：

- `/screeners?lang=zh`
- `/dashboard/ai-daily-report?lang=zh`

核对动作：

1. 打开 `Screeners`，确认结果表可按 `交易就绪度` 排序。
2. 随机打开一只候选股，确认存在 `trade_readiness_score / readiness_bucket / readiness_reason` 对应的人话展示。
3. 打开 `AI 日报`，确认 Top 结果也带有交易就绪度与可执行说明。

截图点：

- `Screeners` 结果表中 `交易就绪度`
- `AI 日报 Top 5` 中的可交易性说明

代码锚点：

- [app/api/routes/screener.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/screener.py)
- [app/services/ai_daily_report.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/ai_daily_report.py)

---

### 2.2 禁止交易规则

状态：`可验收`

页面入口：

- `/dashboard?lang=zh`
- `/screeners?lang=zh`
- `/dashboard/ai-daily-report?lang=zh`
- `/dashboard/ai-daily-report/history?lang=zh`

核对动作：

1. 在首页 `Blocked Candidates / 受阻候选` 中确认会显示 `Blocked / Do Not Chase`。
2. 在 `Screeners` 中确认低就绪度、追高风险、缺行情等不会进入优先推荐。
3. 在 `AI 日报` 和 `历史日报详情` 中确认“不能买原因”是中文解释，而不是内部 code。
4. 点击原因文字，确认能跳到对应筛选页。

截图点：

- 首页 `受阻候选`
- `AI 日报历史详情` 中原因跳转

代码锚点：

- [app/api/routes/dashboard.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/dashboard.py)
- [app/services/ai_daily_report.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/ai_daily_report.py)

---

### 2.3 模型使用指导快照

状态：`可验收`

页面入口：

- `/dashboard?lang=zh`
- `/dashboard/model-performance?lang=zh`
- `/dashboard/ops?lang=zh`

核对动作：

1. 首页第一屏确认存在 `今日模型使用指导` 卡片。
2. `Model Performance` 顶部确认存在 `当前优先模型 / 优先模型组合`。
3. `任务中心` 确认存在 `刷新模型使用指导` 按钮与最近回执。
4. 查看页面文案，确认能看到 `后台快照` 或 `实时回退` 来源说明。

截图点：

- 首页 `今日模型使用指导`
- `Model Performance` 顶部指导卡片
- `任务中心` 的手动刷新按钮

代码锚点：

- [app/services/model_selection_guidance.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/model_selection_guidance.py)
- [app/services/cn_market_universe.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/cn_market_universe.py)
- [app/api/routes/dashboard.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/dashboard.py)

---

### 2.4 AI 日报接入模型使用建议

状态：`可验收`

页面入口：

- `/dashboard/ai-daily-report?lang=zh`
- `/dashboard/ai-daily-report/message?lang=zh`
- `/dashboard/ai-daily-report/history?lang=zh`

核对动作：

1. 打开 `AI 日报` 页面，确认存在 `优先模型 / 优先组合` 相关建议。
2. 打开推送文本页，确认推送内容已经带模型使用建议，而不是只给股票列表。
3. 打开历史日报详情，确认历史留档里也能回看到这部分建议。

截图点：

- `AI 日报` 的模型使用建议区
- 推送文本中的 `优先模型 / 优先组合`

代码锚点：

- [app/services/ai_daily_report.py](/Volumes/STORAGE_Jackyhu/code/ana/app/services/ai_daily_report.py)
- [app/api/routes/dashboard.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/dashboard.py)

---

### 2.5 推荐结果历史验证

状态：`可验收`

页面入口：

- `/dashboard/model-performance?lang=zh`
- `/dashboard/ai-daily-report/history?lang=zh`
- `/dashboard/weekly-review?lang=zh`

核对动作：

1. 打开 `Model Performance`，确认存在 `推荐结果历史验证` 模块。
2. 确认该模块会同时显示：
   - `今日优先模型`
   - `今日优先组合`
   - `AI 日报 Top 5`
3. 确认表中有 `1D / 3D / 5D / 10D` 四个窗口。
4. 打开 `AI 日报历史`，确认历史页现在也展示 `1D / 3D / 5D / 10D`。
5. 打开 `每周复盘`，确认 `推荐验证` 表已经进入周报。

截图点：

- `Model Performance` 的推荐结果历史验证表
- `AI 日报历史` 的 1/3/5/10 日对比
- `每周复盘` 的推荐验证表

代码锚点：

- [app/api/routes/dashboard.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/dashboard.py)

---

## 3. P1 验收映射

### 3.1 多模型组合表现评测

状态：`基本可验收`

页面入口：

- `/screeners?lang=zh`
- `/dashboard/model-performance?lang=zh`
- `/dashboard/ops?lang=zh`

核对动作：

1. 在 `Screeners` 中使用多模型组合或组合预设进入筛选。
2. 在 `Model Performance` 中查看 `建议组合` 表，确认已有 `1D / 3D / 5D / 10D`。
3. 在 `任务中心` 中确认存在：
   - `核心预计算`
   - `组合预计算`
   - `补全预计算`

当前判断：

- 组合预计算链路已打通。
- 组合评测已可查看。
- “保存成我的策略”的体验已有基础，但还可以继续打磨。

---

### 3.2 Portfolio 组合风险摘要

状态：`基本可验收`

页面入口：

- `/portfolio?lang=zh`
- `/dashboard?lang=zh`

已完成：

- 持仓动作建议宽表
- `HOLD / REVIEW / TRIM / EXIT`
- 盈亏、收益率、卖出记录、卖出原因结构化
- 卖出进入周报和建议审计链
- 顶部已新增组合风险摘要
- 已展示最大行业暴露、最大单票、市场暴露
- 已展示当前风险姿态与退出 / 减仓 / 复核队列

验收口径：

- 单股层和组合摘要层都已经达到第一版可验收水位。
- 后续仍可继续增强风险预算、现金占比和更完整的组合层暴露解释，但不再属于本轮阻塞项。

---

### 3.3 Screeners 准入评分排序

状态：`可验收`

页面入口：

- `/screeners?lang=zh`

核对动作：

1. 确认默认或可选排序中存在 `交易就绪度`。
2. 确认低质量候选不会排在前面。
3. 确认多模型组合页面同样遵守这套排序与硬过滤。

代码锚点：

- [app/api/routes/screener.py](/Volumes/STORAGE_Jackyhu/code/ana/app/api/routes/screener.py)

---

### 3.4 数据质量状态卡

状态：`基本可验收`

页面入口：

- `/dashboard/ops?lang=zh`
- `/dashboard/weekly-review?lang=zh`

核对动作：

1. 在任务中心确认能看到主要 job 的成功、失败、部分完成状态。
2. 确认预计算链路已经拆成：
   - 总控预计算
   - 核心预计算
   - 组合预计算
   - 补全预计算
3. 在周报确认能看到本周失败 / partial job。

当前判断：

- 任务质量状态已经能支撑日常运维。
- 更细的数据质量解释卡还可以继续做，但主验收已足够。

---

## 4. 建议签收方式

### 4.1 本轮建议签收范围

建议按以下范围签收：

- P0 全部签收
- P1 中以下项目签收：
  - 多模型组合表现评测
  - Screeners 准入评分排序
  - 数据质量状态卡

### 4.2 暂不作为阻塞项的内容

- Portfolio 更完整的风险预算与现金管理摘要
- 更深的新闻情绪覆盖
- 更完整的财报摘要
- 个性化模型参数闭环

---

## 5. 最终签收结论

当前版本已经具备以下能力：

- 能告诉用户今天优先看什么模型和模型组合。
- 能明确告诉用户哪些股票虽然入选，但现在不能买。
- 能把推荐结果自动留档并做 1/3/5/10 日验证。
- 能把 AI 日报、模型评测、每周复盘串成一条可追踪链。

因此，`盈利导向模型选股增强` 这一阶段，已经达到 `阶段性可验收` 水平。
