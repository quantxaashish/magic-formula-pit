"""Renders the two SPEC.md section 7 chart-data series (compute_equity_curve,
compute_rolling_excess_return) from data/raw/backtest_stats.json - the
actual chart-rendering step section 7 asks for, deliberately not built into
magicformula.backtest itself (that module assigns rendering to the
dashboard/reporting layer, see its module docstring).
"""

import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import date

from magicformula.backtest import compute_rolling_excess_return

with open("data/raw/backtest_stats.json") as f:
    data = json.load(f)

dates = [date.fromisoformat(d) for d in data["rebalance_dates"]]
equity = data["equity_curve"]
bench_equity = data["benchmark_equity_curve"]
period_returns = data["period_returns"]
benchmark_returns = data["benchmark_returns"]

fig, ax = plt.subplots(figsize=(10, 5.5))
ax.plot(dates, equity, marker="o", linewidth=2, color="#1f77b4", label="Magic Formula India (net of 25bps txn cost)")
ax.plot(dates, bench_equity, marker="o", linewidth=2, color="#7f7f7f", label="Nifty 500 TRI (benchmark)")
ax.set_yscale("log")
ax.set_ylabel("Growth of ₹1 (log scale)")
ax.set_title("Equity Curve: Magic Formula India vs Nifty 500 TRI (2019-06-01 to 2026-06-01)")
ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
ax.grid(True, which="both", alpha=0.3)
ax.legend(loc="upper left")
for d, v in zip(dates, equity):
    ax.annotate(f"{v:.2f}x", (d, v), textcoords="offset points", xytext=(0, 8), fontsize=8, color="#1f77b4")
fig.tight_layout()
fig.savefig("data/raw/equity_curve.png", dpi=150)
print("Wrote data/raw/equity_curve.png")

# Rolling excess-return: window=1 at this annual cadence (periods_per_year=1),
# which reduces to the raw per-period excess return - see
# compute_rolling_excess_return's docstring. Charted honestly as such, not
# smoothed to look like something it isn't.
window = 1
rolling_excess = compute_rolling_excess_return(period_returns, benchmark_returns, window)
period_labels = [f"{dates[i].isoformat()}\n->{dates[i+1].isoformat()}" for i in range(len(period_returns))]

fig2, ax2 = plt.subplots(figsize=(10, 5.5))
colors = ["#2ca02c" if v >= 0 else "#d62728" for v in rolling_excess]
ax2.bar(range(len(rolling_excess)), [v * 100 for v in rolling_excess], color=colors)
ax2.set_xticks(range(len(rolling_excess)))
ax2.set_xticklabels(period_labels, fontsize=7)
ax2.axhline(0, color="black", linewidth=0.8)
ax2.set_ylabel("Excess return over Nifty 500 TRI (%)")
ax2.set_title("Per-Period Excess Return vs Nifty 500 TRI\n(window=1 at annual cadence - see note)")
ax2.grid(True, axis="y", alpha=0.3)
for i, v in enumerate(rolling_excess):
    ax2.annotate(f"{v*100:+.1f}%", (i, v * 100), ha="center",
                 va="bottom" if v >= 0 else "top", fontsize=8)
fig2.tight_layout()
fig2.savefig("data/raw/rolling_excess_return.png", dpi=150)
print("Wrote data/raw/rolling_excess_return.png")
