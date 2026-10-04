"""Quick sanity check for the arena script (temporary, not committed)."""
import importlib.util
import sys
import time

import polars as pl

spec = importlib.util.spec_from_file_location(
    "arena", "scripts/backtest_screening_model_arena.py"
)
arena = importlib.util.module_from_spec(spec)
sys.modules["arena"] = arena
spec.loader.exec_module(arena)

t0 = time.time()
panel = arena.build_panel()
print("panel rows:", panel.height, "cols:", panel.width, "build_s:", round(time.time() - t0, 1))
dates = panel["date"].unique().sort().to_list()
print("dates:", len(dates), dates[0], "..", dates[-1])

need = {
    "prev_adj_close", "hi20_for_range", "lo20_for_range", "path_max_jump_5d",
    "net_ret_5d", "net_ret_5d_signal_close", "label_5d_mature", "legacy_composite",
    "eligible", "entry_unbuyable", "corp_action_suspect",
}
missing = need - set(panel.columns)
print("missing cols:", missing or "none")

d = dates[-6]
elig = panel.filter((pl.col("date") == d) & pl.col("eligible"))
print("eligible on", d, ":", elig.height)
sc = arena.static_scores("baseline_lowvol", elig)
scored = elig.with_columns(pl.Series("score", sc))
out = arena.frozen_top_n_outcomes(scored, 5, "score")
print("top5 slots:", out["n_slots"], "unbuyable:", out["unbuyable"], "pending:", out["pending"])
print("slot_rets:", [round(r, 4) for r in out["slot_returns"]])

lc = panel.filter(pl.col("eligible") & pl.col("legacy_composite").is_not_null()).height
print("eligible rows with legacy target:", lc)
mature = panel.filter(pl.col("eligible") & pl.col("label_5d_mature") & pl.col("net_ret_5d").is_not_null()).height
print("eligible rows with mature exec label:", mature)
print("mean net_ret_5d (eligible, matured):", round(
    panel.filter(pl.col("eligible") & pl.col("label_5d_mature"))["net_ret_5d"].mean(), 5))
print("SANITY_OK")
