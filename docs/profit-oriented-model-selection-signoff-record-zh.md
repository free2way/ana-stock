# 盈利导向模型选股阶段性签收记录

签收时间：2026-04-26  
时区：Asia/Shanghai

对应文档：

- [profit-oriented-model-selection-acceptance-plan-zh.md](/Volumes/STORAGE_Jackyhu/code/ana/docs/profit-oriented-model-selection-acceptance-plan-zh.md)
- [profit-oriented-model-selection-acceptance-checklist-zh.md](/Volumes/STORAGE_Jackyhu/code/ana/docs/profit-oriented-model-selection-acceptance-checklist-zh.md)

---

## 1. 本次验收方式

本次采用两层验收：

1. 浏览器人工核对  
   实际打开首页，确认工作台已处于登录后的真实页面，而不是只看代码。

2. 登录态页面拉取核对  
   使用本地部署的认证 cookie，对关键页面逐页拉取 HTML，核对：
   - 页面是否正常返回 `200`
   - 关键模块文案是否实际存在
   - 关键验收项是否已经进入页面结构

辅助检查：

- `http://127.0.0.1:8000/health` 返回 `{"status":"ok"}`

---

## 2. 本次实际核对页面

本次人工验收实际核对了以下页面：

- `/dashboard?lang=zh`
- `/dashboard/top-fragment?lang=zh`
- `/screeners?lang=zh`
- `/dashboard/model-performance?lang=zh`
- `/dashboard/ai-daily-report?lang=zh`
- `/dashboard/ai-daily-report/message?lang=zh`
- `/dashboard/ai-daily-report/history?lang=zh`
- `/dashboard/ai-daily-report/history/{id}?lang=zh`
- `/dashboard/weekly-review?lang=zh`
- `/dashboard/ops?lang=zh&lookback_runs=5`
- `/portfolio?lang=zh`

本轮核对结果：

- 以上页面均返回 `200`
- 未出现新的 `Internal Server Error`

---

## 3. P0 签收结论

### 3.1 交易准入评分

结论：`签收通过`

实际核对：

- `Screeners` 页面已存在 `交易就绪度` 排序项
- `Model Performance` 页面中相关验证表与建议表已使用这套口径
- `AI 日报` 主链路已经接入交易纪律过滤

证据：

- `/screeners?lang=zh` 命中 `交易就绪度`
- `/dashboard/model-performance?lang=zh` 命中 `推荐结果历史验证`

备注：

- 页面层已经形成“准入评分 + 排序 + 日报过滤”的闭环

---

### 3.2 禁止交易规则

结论：`签收通过`

实际核对：

- 首页已出现 `受阻候选`
- 历史日报详情中已出现 `查看同类筛选`
- 被拦截原因已经从内部字段转换成可读中文链路，并能回跳筛选器

证据：

- `/dashboard?lang=zh` 命中 `受阻候选`
- `/dashboard/ai-daily-report/history/2327?lang=zh` 命中 `查看同类筛选`

备注：

- 这一项已经达到文档要求的“用户能看到被拦截原因，而不是只看到股票消失”

---

### 3.3 模型使用指导快照

结论：`签收通过`

实际核对：

- 首页异步片段已存在 `今日模型使用指导`
- 片段中已显示 `优先模型` 与 `优先组合`
- `任务中心` 已有 `刷新模型使用指导` 按钮和最近回执
- `Model Performance` 顶部已显示 `当前优先模型` 与 `优先模型组合`

证据：

- `/dashboard/top-fragment?lang=zh` 命中：
  - `今日模型使用指导`
  - `优先模型`
  - `优先组合`
- `/dashboard/model-performance?lang=zh` 命中：
  - `当前优先模型`
  - `优先模型组合`
- `/dashboard/ops?lang=zh&lookback_runs=5` 命中：
  - `刷新模型使用指导`

备注：

- 首页主页面因为是异步片段加载，直接核对了 `/dashboard/top-fragment`，验收有效

---

### 3.4 AI 日报接入模型使用建议

结论：`签收通过`

实际核对：

- 推送文本页已包含 `优先模型` 与 `优先组合`
- 日报历史链路已形成留档与验证

证据：

- `/dashboard/ai-daily-report/message?lang=zh` 命中：
  - `优先模型`
  - `优先组合`

备注：

- 当前文本样例中有些天仍会出现“样本还不够，先继续观察”，这是内容结果，不是功能缺失

---

### 3.5 推荐结果历史验证

结论：`签收通过`

实际核对：

- `Model Performance` 已新增 `推荐结果历史验证`
- `AI 日报历史` 已展示 `1D / 3D / 5D / 10D`
- `每周复盘` 已新增 `推荐验证`
- `每周复盘` 中已能看到：
  - `今日优先模型`
  - `今日优先组合`
  - 本周 AI 日报 Top 5 的聚合验证

证据：

- `/dashboard/model-performance?lang=zh` 命中：
  - `推荐结果历史验证`
  - `1D`
  - `3D`
  - `5D`
  - `10D`
- `/dashboard/ai-daily-report/history?lang=zh` 命中：
  - `1D`
  - `3D`
  - `5D`
  - `10D`
- `/dashboard/weekly-review?lang=zh` 命中：
  - `推荐验证`
  - `今日优先模型`
  - `今日优先组合`

---

## 4. P1 签收结论

### 4.1 多模型组合表现评测

结论：`基本通过`

实际核对：

- `Model Performance` 已显示组合表，并支持 `1D / 3D / 5D / 10D`
- `任务中心` 中已存在：
  - `总控预计算`
  - `核心模型预计算`
  - `组合预计算`
  - `补全预计算`

证据：

- `/dashboard/model-performance?lang=zh` 中组合表已扩到 `1D / 3D / 5D / 10D`
- `/dashboard/ops?lang=zh&lookback_runs=5` 中已出现：
  - `总控预计算`
  - `核心模型预计算`
  - `组合预计算`
  - `补全预计算`

备注：

- 当前页面文案是 `核心模型预计算`，不是清单里原写的 `核心预计算`
- 当前 `组合预计算 / 补全预计算` 仍显示“还没有任务记录”，不影响功能存在，但说明这一层的最新任务记录还不够完整

---

### 4.2 Portfolio 组合风险摘要

结论：`基本通过，可纳入本轮签收`

实际核对：

- `Portfolio` 已有：
  - 动作建议宽表
  - 中文动作语义
  - 卖出与删除分离
  - 卖出进入结构化复盘
  - 顶部 `组合风险摘要`
  - `最大单票`
  - `市场暴露`
  - `退出候选 / 减仓候选 / 优先复核`

证据：

- `/portfolio?lang=zh` 命中：
  - `组合风险摘要`
  - `最大单票`
  - `市场暴露`
  - `动作建议`
  - `持有`
  - `复核`
  - `减仓`
  - `退出`

验收口径：

- 当前已经具备第一版组合级风险摘要，足以支撑第三阶段收口
- 后续仍可继续增强风险预算、现金占比和更完整的组合暴露解释，但不再构成本轮阻塞项

---

### 4.3 Screeners 准入评分排序

结论：`签收通过`

实际核对：

- `Screeners` 已存在 `交易就绪度` 排序项

证据：

- `/screeners?lang=zh` 命中 `交易就绪度`

---

### 4.4 数据质量状态卡

结论：`基本通过`

实际核对：

- `任务中心` 已可查看关键任务链路
- 周报已可查看失败或部分完成任务
- 模型使用指导手动按钮已在任务中心出现

证据：

- `/dashboard/ops?lang=zh&lookback_runs=5` 命中：
  - `刷新模型使用指导`
  - `总控预计算`
  - `组合预计算`
  - `补全预计算`

残留问题：

- 某些预计算分层有页面结构，但最新 job 记录还没有全部沉到任务中心

---

## 5. 本轮签收判断

### 5.1 建议签收范围

本轮建议正式签收以下范围：

- P0 全部签收
- P1 中以下内容签收：
  - 多模型组合表现评测
  - Screeners 准入评分排序
  - 数据质量状态卡

### 5.2 暂不作为阻塞项

本轮暂不作为签收阻塞项的内容：

- Portfolio 更完整的风险预算与现金管理摘要
- 预计算分层任务记录的进一步收口
- 更深层的长期归因解释

---

## 6. 最终签收意见

本轮人工验收后的最终意见：

- 当前版本已经达到 `阶段性可签收`
- P0 主链路已经闭环
- P1 关键功能大部分具备，少量项仍处于“可用但可继续增强”的状态

最简结论：

`盈利导向模型选股增强` 这一阶段，可以正式按“阶段性版本”签收。
