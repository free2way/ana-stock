"""Block-level distribution analysis of the full arena artifact (temporary)."""
import json

import numpy as np
import polars as pl

art = json.load(open("data/artifacts/screening_model_arena/arena_20260930T125103Z.json"))
rows = art["daily_rows"]
start = art["config"]["test_start_idx"]
horizon = art["config"]["horizon"]

df = pl.DataFrame(rows).with_columns(pl.col("date").str.slice(0, 10).alias("d"))
dates = sorted(df["d"].unique().to_list())
didx = {d: i for i, d in enumerate(dates)}
df = df.with_columns(pl.col("d").replace_strict(didx, return_dtype=pl.Int64).alias("idx"))

for m in ["profit_logit", "legacy_composite_lgbm", "exec_net_lgbm",
          "baseline_momentum_20d", "baseline_lowvol"]:
    sub = df.filter(
        (pl.col("model") == m) & (pl.col("top_n") == 5)
        & ((pl.col("idx") - start) % horizon == 0)
    ).sort("idx")
    b = sub["batch_net"].to_numpy()
    print(
        f"{m:24s} blocks={len(b)} mean={b.mean() * 1e4:7.1f}bp "
        f"med={np.median(b) * 1e4:7.1f}bp pos%={(b > 0).mean() * 100:5.1f}% "
        f"best={b.max() * 100:6.2f}% worst={b.min() * 100:6.2f}% "
        f"t={b.mean() / b.std() * np.sqrt(len(b)):5.2f}"
    )

sub = df.filter(
    (pl.col("model") == "profit_logit") & (pl.col("top_n") == 5)
    & ((pl.col("idx") - start) % horizon == 0)
).sort("idx")
b = sub["batch_net"].to_numpy()
n = len(b) // 2
print("profit_logit N5 first-half mean bp/d", round(b[:n].mean() * 1e4, 1),
      "second-half", round(b[n:].mean() * 1e4, 1))
sub = df.filter(
    (pl.col("model") == "legacy_composite_lgbm") & (pl.col("top_n") == 5)
    & ((pl.col("idx") - start) % horizon == 0)
).sort("idx")
b2 = sub["batch_net"].to_numpy()
print("legacy N5 first-half mean bp/d", round(b2[:n].mean() * 1e4, 1),
      "second-half", round(b2[n:].mean() * 1e4, 1))

# Single-signal pooled stats (all tradable slots pooled across dates)
for m in ["profit_logit", "legacy_composite_lgbm", "exec_net_lgbm"]:
    sub = df.filter((pl.col("model") == m) & (pl.col("top_n") == 5))
    tm = sub.filter(pl.col("tradable_mean").is_not_null())
    arr = np.repeat(tm["tradable_mean"].to_numpy(), (tm["n_slots"] - tm["unbuyable"] - tm["pending"]).to_numpy())
    # approximate pooled mean from daily means weighted by tradable count proxy
    w = tm["n_slots"].to_numpy()
    pooled = float(np.average(tm["tradable_mean"].to_numpy(), weights=w))
    print(f"{m:24s} pooled-tradable-mean≈{pooled * 1e4:7.1f}bp  dates_with_data={tm.height}")
