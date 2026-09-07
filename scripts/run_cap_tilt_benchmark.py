"""Cap-tilt-matched benchmark comparison, on top of the same real 2019-2026
basket walk run_backtest_stats.py already produced. Re-derives the walk
from the same cached universe/fundamentals/price data (no re-fetch) rather
than importing run_backtest_stats.py directly, since scripts/ isn't a
package - this duplicates the same setup pattern the other pilot scripts
already use.

Nifty 500 TRI alone conflates two different questions: did the strategy
pick better stocks, and did it simply hold more small/mid-cap exposure
during a period when small/mid outperformed large. A blended benchmark -
NIFTY 100 (large) / NIFTY MIDCAP 150 (mid) / NIFTY SMALLCAP 250 (small),
weighted each period by the strategy's OWN actual cap-bucket composition
at the start of that period - isolates the first question from the
second. If most of the excess return survives against this blend, that's
real stock-selection alpha. If it mostly disappears, the strategy was
riding the cap-tilt cycle, not picking better names within it.

All three index TRI series pulled live from niftyindices.com's own
"Total returns Index Values" report, same nearest-trading-day-on-or-
before convention as the Nifty 500 TRI pull.
"""

from __future__ import annotations

import pickle
from datetime import date
from pathlib import Path

import pandas as pd

from magicformula.data_fetch.fundamentals import FundamentalsRecord, _latest_record_per_symbol
from magicformula.data_fetch.prices import (
    PRICE_LOOKUP_MAX_TOLERANCE_DAYS,
    SHARES_LOOKUP_MAX_TOLERANCE_DAYS,
    nearest_value_lookup,
)
from magicformula.formulas import FinancialInputs, compute_metrics
from magicformula.ranker import RankedStock, rank_universe
from magicformula.backtest import run_backtest, compute_cagr, compute_annualized_volatility, compute_max_drawdown, compute_hit_rate
from magicformula.universe import build_universe

AMFI_XLSX_URL = "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx"
REBALANCE_DATES = [date(y, 6, 1) for y in range(2019, 2027)]
TOP_N = 30
SERIES_CACHE_PATH = Path("data/raw/point_in_time_series_cache.pkl")
RISK_FREE_RATE = 0.053

# Real Nifty 500 TRI, same 8 dates as run_backtest_stats.py
NIFTY500_TRI = [14591.01, 12092.03, 20113.79, 21709.49, 24528.90, 33168.20, 36160.26, 35911.81]

# Real NIFTY 100 (large-cap proxy) TRI, pulled live from niftyindices.com,
# same nearest-trading-day-on-or-before convention.
NIFTY100_TRI = [15338.44, 12917.31, 20613.31, 22079.79, 24595.51, 31772.04, 34591.14, 33645.29]
# Real NIFTY MIDCAP 150 TRI
MIDCAP150_TRI = [7774.49, 6346.04, 11932.83, 13233.54, 15828.39, 24327.24, 26763.04, 28378.90]
# Real NIFTY SMALLCAP 250 TRI
SMALLCAP250_TRI = [6497.86, 4449.73, 9758.89, 10814.89, 12497.92, 19664.65, 21308.95, 21463.20]


def period_returns(series):
    return [series[i + 1] / series[i] - 1 for i in range(len(series) - 1)]


def load_fundamentals(symbols):
    df = pd.read_parquet("data/raw/full_universe_fundamentals.parquet")
    df = df[df["symbol"].isin(symbols)]
    fields = [
        "symbol", "sector", "broad_sector", "fiscal_year_end", "operating_profit",
        "depreciation_amortization", "total_assets", "other_liabilities", "investments",
        "cwip", "borrowings", "market_cap", "source_url", "statement", "cap_bucket",
    ]
    return [FundamentalsRecord(**row._asdict()) for row in df[fields].itertuples(index=False)]


def make_rank_fn(price_shares_by_symbol, cap_bucket_by_symbol):
    def rank_fn(eligible, rebalance_date):
        latest = _latest_record_per_symbol(eligible)
        metrics = []
        for r in latest:
            series = price_shares_by_symbol.get(r.symbol)
            if series is None:
                continue
            price_series, shares_series = series
            price = nearest_value_lookup(price_series, rebalance_date, max_days_tolerance=PRICE_LOOKUP_MAX_TOLERANCE_DAYS)
            if price is None:
                continue
            shares = nearest_value_lookup(shares_series, rebalance_date, max_days_tolerance=SHARES_LOOKUP_MAX_TOLERANCE_DAYS)
            if shares is None:
                continue
            market_cap = (price * shares) / 1e7
            inputs = FinancialInputs(
                symbol=r.symbol, operating_profit=r.operating_profit,
                depreciation_amortization=r.depreciation_amortization,
                total_assets=r.total_assets, current_liabilities=r.other_liabilities,
                market_cap=market_cap, total_debt=r.borrowings,
                cash_and_equivalents=0, other_liquid_investments=r.investments,
            )
            metrics.append(compute_metrics(inputs, roce_mode="standard"))
        return rank_universe(metrics, mode="within_bucket", cap_bucket_by_symbol=cap_bucket_by_symbol)
    return rank_fn


def main():
    entries = build_universe(AMFI_XLSX_URL, as_of=date(2026, 9, 6))
    entry_by_symbol = {e.nse_symbol: e for e in entries if e.nse_symbol}
    fundamentals_all = load_fundamentals(set(entry_by_symbol))
    all_bucket_symbols = {e.nse_symbol for e in entries if e.cap_bucket in ("large", "mid", "small") and e.nse_symbol}
    fundamentals = [r for r in fundamentals_all if r.symbol in all_bucket_symbols]
    symbols_with_fundamentals = sorted({r.symbol for r in fundamentals})
    cap_bucket_by_symbol = {s: entry_by_symbol[s].cap_bucket for s in symbols_with_fundamentals}
    sector_by_symbol = {r.symbol: r.sector for r in fundamentals if r.sector}
    listing_date_by_symbol = {
        s: entry_by_symbol[s].date_of_listing for s in symbols_with_fundamentals
        if entry_by_symbol[s].date_of_listing is not None
    }

    with SERIES_CACHE_PATH.open("rb") as f:
        price_shares_by_symbol = pickle.load(f)

    def price_lookup(symbol, d):
        series = price_shares_by_symbol.get(symbol)
        if series is None:
            return None
        price_series, _ = series
        return nearest_value_lookup(price_series, d, max_days_tolerance=PRICE_LOOKUP_MAX_TOLERANCE_DAYS)

    rank_fn = make_rank_fn(price_shares_by_symbol, cap_bucket_by_symbol)
    nifty500_returns = period_returns(NIFTY500_TRI)

    result = run_backtest(
        REBALANCE_DATES, fundamentals, rank_fn, price_lookup, nifty500_returns,
        top_n=TOP_N, sector_by_symbol=sector_by_symbol, listing_date_by_symbol=listing_date_by_symbol,
        transaction_cost_bps=25.0, risk_free_rate=RISK_FREE_RATE, periods_per_year=1.0,
    )

    # Cap-bucket composition of the basket held INTO each period (i.e. the
    # basket formed at the period's start date) - this is what the blend
    # weight should track, matching what compute_period_return itself
    # prices (step[i].outcome.weights held from step[i].date to step[i+1].date).
    large_r = period_returns(NIFTY100_TRI)
    mid_r = period_returns(MIDCAP150_TRI)
    small_r = period_returns(SMALLCAP250_TRI)

    print(f"{'Period':<26}{'Strategy':>10}{'Nifty500':>10}{'Blend':>10}{'ExcVs500':>10}{'ExcVsBlend':>12}{'L/M/S wt':>16}")
    blended_returns = []
    bucket_weights_by_period = []
    for i, step in enumerate(result.steps[:-1]):
        holdings = step.outcome.holdings
        by_bucket = {"large": 0, "mid": 0, "small": 0}
        for s in holdings:
            b = cap_bucket_by_symbol.get(s)
            if b in by_bucket:
                by_bucket[b] += 1
        n = sum(by_bucket.values())
        w_large, w_mid, w_small = by_bucket["large"] / n, by_bucket["mid"] / n, by_bucket["small"] / n
        bucket_weights_by_period.append((w_large, w_mid, w_small))
        blend = w_large * large_r[i] + w_mid * mid_r[i] + w_small * small_r[i]
        blended_returns.append(blend)

        sr = result.period_returns[i]
        br = result.benchmark_returns[i]
        start, end = step.rebalance_date, result.steps[i + 1].rebalance_date
        print(f"{start} -> {end}  {sr:+9.4f}{br:+10.4f}{blend:+10.4f}{sr-br:+10.4f}{sr-blend:+12.4f}"
              f"   {w_large:.0%}/{w_mid:.0%}/{w_small:.0%}")

    blend_cagr = compute_cagr(blended_returns, 1.0)
    blend_vol = compute_annualized_volatility(blended_returns, 1.0)
    from magicformula.backtest import compute_equity_curve
    blend_equity = compute_equity_curve(blended_returns)
    blend_dd = compute_max_drawdown(blend_equity)
    blend_hit_rate = compute_hit_rate(result.period_returns, blended_returns)

    print(f"\nBlended benchmark CAGR:  {blend_cagr:+.4%}")
    print(f"Blended benchmark vol:   {blend_vol:.4%}")
    print(f"Blended benchmark max drawdown: {blend_dd:.4%}")
    print(f"Strategy hit rate vs blend: {blend_hit_rate:.4%}")
    print(f"Strategy CAGR:           {result.stats.cagr:+.4%}")
    print(f"\nMean annual excess vs Nifty 500 TRI: {sum(r-b for r,b in zip(result.period_returns, result.benchmark_returns))/len(result.period_returns):+.4%}")
    print(f"Mean annual excess vs cap-tilt blend: {sum(r-b for r,b in zip(result.period_returns, blended_returns))/len(blended_returns):+.4%}")

    print("\nBlended benchmark equity curve:", [round(v, 4) for v in blend_equity])
    print("Strategy equity curve:         ", [round(v, 4) for v in result.equity_curve])

    import json
    with open("data/raw/cap_tilt_benchmark.json", "w") as f:
        json.dump({
            "period_returns": result.period_returns,
            "nifty500_returns": result.benchmark_returns,
            "blended_returns": blended_returns,
            "bucket_weights_by_period": bucket_weights_by_period,
            "rebalance_dates": [s.rebalance_date.isoformat() for s in result.steps],
            "blend_stats": {"cagr": blend_cagr, "vol": blend_vol, "max_drawdown": blend_dd, "hit_rate_vs_strategy": blend_hit_rate},
        }, f, indent=2)
    print("\nWritten to data/raw/cap_tilt_benchmark.json")


if __name__ == "__main__":
    main()
