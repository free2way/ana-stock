# 训练侧四项改动消融（batch 2）

- 日期：2026-10-05
- 新增：`scripts/run_training_ablation.py`（仅新增脚本，未改动 `app/` 业务逻辑）
- 实测报告：`data/experiments/training-ablation-20261005T105826Z.json|.md`

## 1. 四项改动与开关

| # | 改动 | 主要设置项 |
|---|---|---|
| ① | 标签 winsor + Huber 目标 | `trainer_label_winsorize_enabled` / `trainer_objective` |
| ② | drawdown penalty | `trainer_drawdown_penalty` |
| ③ | embargo = horizon | `trainer_embargo_sessions`（None→horizon） |
| ④ | 截面标准化 | `trainer_feature_transform_enabled` |

## 2. Harness 用法

```bash
.venv/bin/python scripts/run_training_ablation.py --list-variants
# 干跑（只解析面板/变体，不训练）
.venv/bin/python scripts/run_training_ablation.py --dry-run --pilot-tickers 24
# 真实消融（默认 5 个变体）
.venv/bin/python scripts/run_training_ablation.py --pilot-tickers 24 --window-dates 180 --history-sessions 400
# 全量开关矩阵
.venv/bin/python scripts/run_training_ablation.py \
  --variants canonical,winsor_off,objective_l2,label_robust_off,drawdown_off,embargo_off,cross_section_off,all_off
# 任意组合：在选中变体之上再叠加设置
.venv/bin/python scripts/run_training_ablation.py --variants canonical \
  --set trainer_drawdown_penalty=0 --set trainer_feature_transform_enabled=false
```

- 变体注册表：`canonical`（全开）、`winsor_off`、`objective_l2`、`label_robust_off`（①关）、
  `drawdown_off`（②关）、`embargo_off`（③关）、`cross_section_off`（④关）、`all_off`（全关）。
- 输出：`data/experiments/training-ablation-<UTC ts>.json` 与 `.md`；含每变体开关组合、
  `dataset_hash`、`panel_hash`、OOS 指标、样本量与耗时。
- 主参数：`--market`、`--pilot-tickers`/`--tickers`、`--history-sessions`、`--window-dates`、
  `--lookback-days`、`--artifact-dir`。

实现要点：复用生产训练路径 `SignalTrainer.train`（真跑 purged walk-forward、落 `model_runs`
与 `walk_forward_oos_evaluation_v1`）；运行时只 patch 两处——用固定面板替换
`app.services.trainer.load_lake_rows`，以及包裹 `SignalTrainer._oos_metric_value` 以捕获
每个成熟 top-N 样本，从而在 harness 侧计算净命中率。`app/` 源码零改动。

## 3. 同面板 / 同 OOS / 同成本

- 面板：24 只最新流动性 CN 标的、9543 行、400 会话（2025-02-13–2026-09-30）；
  `dataset_hash=selection_dataset_v1:CN:0faf3cbd1086f1350086`，
  `panel_hash=6e4a225b23969d5a…`。所有变体同一 400 会话面板。
- OOS：训练器固定取最后 60 会话预测、其中 54 个成熟日（2026-07-08–2026-09-21），
  所有变体完全一致（已核验 OOS 窗口集合唯一）。
- 成本：把训练器 per-fill 设为 commission 2.5 + slippage 22.5 bps（单边），
  使名义往返 = canonical 50 bps（`app/services/cost_basis`）。每变体标签成本核验均 = 50 bps。
- 协议：lookback=3 → horizon=6，训练窗口 180 日，模型 lightgbm（260 树），OOS top-N=5。

## 4. 实测结果（CN 24 标的，单 seed，工程 pilot）

| 变体 | mean_risk_adjusted | mean_net | 净命中率 | positive_date_rate | OOS 样本 | 训练样本 | 耗时(s) |
|---|---|---|---|---|---|---|---|
| canonical（全开） | -0.058325 | -0.032932 | 0.3593 | 0.3333 | 270 | 4210 | 28.3 |
| winsor_off（①-） | -0.062898 | -0.036886 | 0.3519 | 0.3333 | 270 | 4210 | 28.3 |
| objective_l2（①-） | -0.058325 | -0.032932 | 0.3593 | 0.3333 | 270 | 4210 | 28.1 |
| label_robust_off（①关） | -0.062898 | -0.036886 | 0.3519 | 0.3333 | 270 | 4210 | 28.0 |
| drawdown_off（②关） | -0.032932 | -0.032932 | 0.3593 | 0.3333 | 270 | 4210 | 27.9 |
| embargo_off（③关） | -0.054357 | -0.029930 | 0.3519 | 0.3333 | 270 | 4210 | 27.8 |
| cross_section_off（④关） | -0.051415 | -0.028056 | 0.3519 | 0.2963 | 270 | 4210 | 26.4 |
| all_off（全关） | -0.023517 | -0.023517 | 0.3778 | 0.4074 | 270 | 4210 | 26.2 |

观察（仅限本 pilot，不可外推为选型结论）：

- **② drawdown penalty 不改变训练**：`_build_executable_net_return_target` 的拟合目标始终是
  `net_return`，penalty 只写入标签 `target_profile.risk_adjusted_return` 并被 OOS 指标复用。
  因此 `drawdown_off` 的 `mean_net`/净命中率与 `canonical` **逐位相同**，
  `mean_risk_adjusted` 退化为 `mean_net`（-0.032932）。这是口径效应，不是模型效应。
- **① 主要是 winsor 在起作用**：`winsor_off` 与 `label_robust_off` 结果完全相同，
  而 `objective_l2` 与 `canonical` 逐位相同 → 在本 pilot 中 winsor 裁剪改变了模型，
  Huber→L2 未额外改变结果。
- **③ embargo**：关掉后 mean_net 略有改善（-0.0299 vs -0.0329），但净命中率略降
  （0.3519 vs 0.3593）。
- **④ 截面标准化**：关掉后 mean_net 改善（-0.0281）、positive_date_rate 明显下降
  （0.2963 vs 0.3333），净命中率略降。
- **all_off** 的 mean_net/命中率最好，但其 `mean_risk_adjusted` 因 penalty=0 不可与
  canonical 直接对比；应以 `mean_net`/净命中率作横向口径。

## 5. Scope 与限制

- 工程 pilot，**非正式选型口径**：最新流动性子集（幸存者/前视偏差），非 PIT 可交易宇宙。
- 训练窗口被压到 180 日（生产 252 日），为控制成本；单 seed、单面板，**未做统计检验**。
- 净命中率 = 成熟 top-N OOS 样本中 `net_return>0` 占比（harness 侧由捕获样本计算）；
  `positive_date_rate` 取训练器写入的 OOS 摘要（基于 risk_adjusted 符号，受 penalty 影响）。
- 每变体一次训练约 26–29s；8 变体端到端（含面板选择与全湖 source 哈希）约 4 分钟。

## 6. 未跑项

- 未跑 252 日生产窗口 / 全市场 / 多 seed / 多市场（US）与显著性检验；当前 harness 支持，
  只是本轮成本与「尽力完成 1–2 项」范围未覆盖。
- 未把消融接入 `experiment_framework` 的分层与 FDR/聚类 CI（harness 只产出对比表）。
- 未验证 ② 若改为「拟合 risk_adjusted 目标」的模型效应（当前实现下 ② 仅影响指标口径）。
