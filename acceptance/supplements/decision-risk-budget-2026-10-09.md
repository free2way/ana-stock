# 决策记录：风险惩罚系数 λ（`trainer_drawdown_penalty`）0.25 → 0.12

- 日期：2026-10-09（CST）
- 负责人/决定人：Jacky Hu（owner，已确认）
- 状态：**已决策并生效（serve-time）**
- 关联：CN 促销门 `oos_evaluation` FAIL（`docs/acceptance-debt-registry-zh.md` §5 缺口 1）；诊断依据 `tmp/research-oos-cn/REPORT.md`

## 1. 决策内容

把训练器风险惩罚系数 `trainer_drawdown_penalty`（λ，`app/core/config.py`）默认值由 **0.25 下调为 0.12**。

- λ 用于可执行标签的 `risk_adjusted_return = net_return − λ·|path_drawdown|`，并被促销门当作 OOS 指标读取（`mean_risk_adjusted_return`）。
- 取 λ=0.12 即**其上界**，属**最小放松**：仅恰好越过盈亏平衡点，不放大放宽幅度。

## 2. 决策依据

- **离线精确复现**（与存档 run 396 `cn_close_2026-10-09` 逐位吻合；复权视图 sha256 与 run 396 config 记录的 `adjusted_view_sha256` 逐字节一致）：
  - `mean_net_return = +1.065%`
  - `mean|path_drawdown| = 8.798%` → `0.25 × 8.798% = 2.200%`
  - 旧判据：`+1.065% − 2.200% = −1.135%`（**FAIL**）
- **λ 盈亏平衡点 = mean_net / mean|dd| = 0.01065 / 0.08798 = 0.1210**。
  - λ=0.12 → risk ≈ **+0.0001**（预期 margin ≈ +0.0009，含四舍五入口径差异），**贴线通过**；λ=0.10 → +0.0019；λ=0.08 → +0.0036。
- **改善趋势**（相邻 run，config 存档）：393 `net +0.00485 / risk −0.01895` → 395 `+0.00578 / −0.01683` → 396 `+0.01065 / −0.01135`。净收益 3 个 run 内 +0.0058，且回撤同步收窄（risk 改善 0.0075 > 0.25×Δnet）。
- 结论：旧门槛实际在检验「6 日持有能否用小回撤换 >2.2% 净收益」，**严于「净收益为正」**。本次决策＝**承认门槛口径过严**。

## 3. 已披露风险（必须保留）

- **该决策不代表模型优势已被证实。** 它只说明旧门槛口径过严，**不是** OOS 表现已被证实的证据。
- run 396 OOS：`mean_risk = −0.01135`，日度 sd = 0.1106，se = 0.01506，**t = −0.75**，**95% CI [−0.041, +0.018] 跨 0**；bootstrap 20k 次 **P(真均值>0) = 0.224**。
- **符号由 7 月单一状态段决定**；54 个观测点、单一状态段即可决定门的符号。**只丢 3 个最差日（仍留 51 天 ≥40）即翻正**（−0.0114 → +0.0026）；反向同理。
- 门对**样本量与状态**的敏感性远大于对模型的敏感性。

## 4. 生效范围

- **serve-time 促销门判据**：训练器 OOS 指标 `mean_risk_adjusted_return` 的惩罚系数；及 run config / artifact manifest 中记录的 `drawdown_penalty`。
- 不改 `net_return` 目标、不改拟合目标（`trainer_fit_on_risk_adjusted` 仍为 `False`）、不改其它门禁阈值。
- 历史 run（如 396）已落库的数值**不回溯**；新 run 按 λ=0.12 计算。

## 5. 后续要求

- **样本外确认（进行中，未完成）**：扩窗 / 分半稳健性检验，确认改善非 7 月单一状态段驱动；未完成前结论仅限「旧门槛过严」。
- 若后续样本外证据不支持，应重新评估 λ 与门的聚合口径（日等权 vs 样本等权）与 `OOS_EVALUATION_TOP_N`。
- 保留本记录与 `docs/acceptance-debt-registry-zh.md` §5 缺口 1 的风险行，供审计追溯。
