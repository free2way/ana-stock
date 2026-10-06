# 训练消融报告（CN，20261005T120020Z）

- schema：`training_ablation_v1`
- scope：`engineering_pilot_not_for_model_selection`（**latest-liquidity pilot universe (survivor/look-ahead biased) and a shortened training window; results are directional ablation evidence, not a model-selection decision**）
- 面板：120 标的 / 47994 行 / 2025-01-02–2026-09-30；dataset_hash=`selection_dataset_v1:CN:30b74fde532511c5e723`
- 成本：canonical 50bps (commission 2.5 + slippage 22.5 单边，名义往返 50bps)
- 协议：lookback=3，horizon=6，训练窗口=228 日，OOS=最后 60 个会话，top-N=5，模型=lightgbm
- 随机种子：[42, 43, 44]（每变体逐 seed 重跑，跨 seed 报 mean/std；判定门槛=同一方向在 ≥⌈2/3×seed⌉ 上成立）

## 逐 run 明细（含 seed 维度）

| 变体 | seed | 说明 | mean_risk_adjusted | mean_net | 净命中率 | positive_date_rate | OOS 样本 | 训练样本 | 耗时(s) | status |
|---|---|---|---|---|---|---|---|---|---|---|
| canonical | 42 | all four changes on (shipped defaults) | -0.057669 | -0.030473 | 0.3852 | 0.3704 | 270 | 26345 | 44.6 | success |
| winsor_off | 42 | label winsorization off (Huber kept) | -0.057682 | -0.030052 | 0.4185 | 0.3519 | 270 | 26345 | 44.2 | success |
| drawdown_off | 42 | change 2 off: drawdown penalty 0.0 | -0.030473 | -0.030473 | 0.3852 | 0.4259 | 270 | 26345 | 43.7 | success |
| fit_risk_adjusted | 42 | penalty on the fit target: fit on risk_adjusted_return | -0.055428 | -0.028055 | 0.3889 | 0.3519 | 270 | 26345 | 44.4 | success |
| all_off | 42 | all four changes off | -0.025952 | -0.025952 | 0.3852 | 0.4444 | 270 | 26372 | 41.5 | success |
| canonical | 43 | all four changes on (shipped defaults) | -0.054161 | -0.026540 | 0.4074 | 0.3704 | 270 | 26345 | 43.7 | success |
| winsor_off | 43 | label winsorization off (Huber kept) | -0.068398 | -0.039971 | 0.3704 | 0.2963 | 270 | 26345 | 43.6 | success |
| drawdown_off | 43 | change 2 off: drawdown penalty 0.0 | -0.026540 | -0.026540 | 0.4074 | 0.4074 | 270 | 26345 | 43.3 | success |
| fit_risk_adjusted | 43 | penalty on the fit target: fit on risk_adjusted_return | -0.053157 | -0.026506 | 0.4222 | 0.3704 | 270 | 26345 | 43.7 | success |
| all_off | 43 | all four changes off | -0.023856 | -0.023856 | 0.4000 | 0.4444 | 270 | 26372 | 41.2 | success |
| canonical | 44 | all four changes on (shipped defaults) | -0.060622 | -0.032883 | 0.3778 | 0.3519 | 270 | 26345 | 43.6 | success |
| winsor_off | 44 | label winsorization off (Huber kept) | -0.063357 | -0.035552 | 0.3926 | 0.2778 | 270 | 26345 | 43.9 | success |
| drawdown_off | 44 | change 2 off: drawdown penalty 0.0 | -0.032883 | -0.032883 | 0.3778 | 0.3704 | 270 | 26345 | 43.5 | success |
| fit_risk_adjusted | 44 | penalty on the fit target: fit on risk_adjusted_return | -0.060117 | -0.032467 | 0.3926 | 0.4074 | 270 | 26345 | 43.7 | success |
| all_off | 44 | all four changes off | -0.020019 | -0.020019 | 0.4222 | 0.4444 | 270 | 26372 | 41.2 | success |

## 跨 seed 汇总（mean ± std，n=seed 数）

| 变体 | seed 数 | mean_net (mean±std) | mean_risk_adjusted (mean±std) | positive_date_rate (mean±std) |
|---|---|---|---|---|
| canonical | 3 | -0.029965 ± 0.003202 | -0.057484 ± 0.003234 | 0.364197 ± 0.010691 |
| winsor_off | 3 | -0.035192 ± 0.004969 | -0.063146 ± 0.005361 | 0.308642 ± 0.038549 |
| drawdown_off | 3 | -0.029965 ± 0.003202 | -0.029965 ± 0.003202 | 0.401234 ± 0.028288 |
| fit_risk_adjusted | 3 | -0.029009 ± 0.003093 | -0.056234 ± 0.003550 | 0.376543 ± 0.028287 |
| all_off | 3 | -0.023276 ± 0.003009 | -0.023276 ± 0.003009 | 0.444444 ± 0.000000 |

## 方向一致性判定（变体 vs canonical，逐 seed 配对）

| 指标 | 对比 | 共享 seed | 变体更高/更低/持平 | 方向 | 门槛 | 初步支持 |
|---|---|---|---|---|---|---|
| mean_risk_adjusted_return | winsor_off vs canonical | 3 | 0/3/0 | lower | ≥2 | True |
| mean_risk_adjusted_return | drawdown_off vs canonical | 3 | 3/0/0 | higher | ≥2 | True |
| mean_risk_adjusted_return | fit_risk_adjusted vs canonical | 3 | 3/0/0 | higher | ≥2 | True |
| mean_risk_adjusted_return | all_off vs canonical | 3 | 3/0/0 | higher | ≥2 | True |
| mean_net_return | winsor_off vs canonical | 3 | 1/2/0 | lower | ≥2 | True |
| mean_net_return | drawdown_off vs canonical | 3 | 0/0/3 | mixed | ≥2 | False |
| mean_net_return | fit_risk_adjusted vs canonical | 3 | 3/0/0 | higher | ≥2 | True |
| mean_net_return | all_off vs canonical | 3 | 3/0/0 | higher | ≥2 | True |

> 判定是逐 seed 配对方向，不是单 seed 结论；`初步支持` 仅在方向达标时成立。

## 变体开关快照

- **canonical**（seed=42，all four changes on (shipped defaults)）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "net_return", "random_seed": 42, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **winsor_off**（seed=42，label winsorization off (Huber kept)）：`{"label_winsorize": {"enabled": false, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "net_return", "random_seed": 42, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **drawdown_off**（seed=42，change 2 off: drawdown penalty 0.0）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.0, "fit_target": "net_return", "random_seed": 42, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **fit_risk_adjusted**（seed=42，penalty on the fit target: fit on risk_adjusted_return）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "risk_adjusted_return", "random_seed": 42, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **all_off**（seed=42，all four changes off）：`{"label_winsorize": {"enabled": false, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "l2"}, "drawdown_penalty": 0.0, "fit_target": "net_return", "random_seed": 42, "embargo_sessions": 0, "purge_gap_days": 6, "feature_transform": {"enabled": false, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **canonical**（seed=43，all four changes on (shipped defaults)）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "net_return", "random_seed": 43, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **winsor_off**（seed=43，label winsorization off (Huber kept)）：`{"label_winsorize": {"enabled": false, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "net_return", "random_seed": 43, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **drawdown_off**（seed=43，change 2 off: drawdown penalty 0.0）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.0, "fit_target": "net_return", "random_seed": 43, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **fit_risk_adjusted**（seed=43，penalty on the fit target: fit on risk_adjusted_return）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "risk_adjusted_return", "random_seed": 43, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **all_off**（seed=43，all four changes off）：`{"label_winsorize": {"enabled": false, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "l2"}, "drawdown_penalty": 0.0, "fit_target": "net_return", "random_seed": 43, "embargo_sessions": 0, "purge_gap_days": 6, "feature_transform": {"enabled": false, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **canonical**（seed=44，all four changes on (shipped defaults)）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "net_return", "random_seed": 44, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **winsor_off**（seed=44，label winsorization off (Huber kept)）：`{"label_winsorize": {"enabled": false, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "net_return", "random_seed": 44, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **drawdown_off**（seed=44，change 2 off: drawdown penalty 0.0）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.0, "fit_target": "net_return", "random_seed": 44, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **fit_risk_adjusted**（seed=44，penalty on the fit target: fit on risk_adjusted_return）：`{"label_winsorize": {"enabled": true, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "huber"}, "drawdown_penalty": 0.25, "fit_target": "risk_adjusted_return", "random_seed": 44, "embargo_sessions": 6, "purge_gap_days": 6, "feature_transform": {"enabled": true, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}
- **all_off**（seed=44，all four changes off）：`{"label_winsorize": {"enabled": false, "lower_quantile": 0.025, "upper_quantile": 0.975}, "objective": {"alpha": 0.9, "objective": "l2"}, "drawdown_penalty": 0.0, "fit_target": "net_return", "random_seed": 44, "embargo_sessions": 0, "purge_gap_days": 6, "feature_transform": {"enabled": false, "method": "cross_sectional_winsor_mad_zscore", "scope": "per_trade_date", "winsor_lower": 0.025, "winsor_upper": 0.975, "zscore_clip": 3.0}}`
  - 标签成本核验：{"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5, "nominal_round_trip_bps": 50.0, "cost_model_hash": "1521199f34132d45baa9e531f1dd82361df5555ede7885242181e4a88e804d0c"}

## 限制与未跑项

- pilot universe is a latest-liquidity subset, not a point-in-time tradable universe
- training window shortened via trainer_*_window_dates for cost; not the 252-session production window
- drawdown_off keeps the default fit target (net_return): it changes only mean_risk_adjusted_return mechanically and leaves mean_net_return untouched by construction; use fit_risk_adjusted to test the penalty as a fit target
- net hit rate is computed from the matured top-N OOS samples captured around the trainer's OOS metric accessor
- multi-seed dispersion makes estimator variance visible, but a single pilot panel is reused across seeds, so panel-level (universe/session) uncertainty is not estimated; the 2/3-seed direction threshold is a weak screen, not a significance test
