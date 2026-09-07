"""Flag-only vs exclude-mode, run side by side against the real 2019-2026
basket walk, same rebalance dates, same Nifty 500 TRI benchmark, same
25bps transaction cost - the only thing that changes is whether a
company anomaly-flagged (fresh, per-rebalance-date, point-in-time -
the fixed computation, not the old buggy global one) is kept and ranked
(flag_only) or dropped from the eligible pool before ranking (exclude).

This is the real, checkable comparison decision 0012's flag-only-vs-
exclude-mode question asked for, once the per-date anomaly bug was fixed
and its flagged rate was confirmed sane (rest of the flag counts: 1-7
distinct companies per rebalance, not a single false-positive-dominated
handful reused every year).
"""

from __future__ import annotations

import pickle
from datetime import date
from pathlib import Path

import pandas as pd

from magicformula.data_fetch.fundamentals import FundamentalsRecord, _latest_record_per_symbol, check_fundamentals_anomalies
from magicformula.data_fetch.prices import (
    PRICE_LOOKUP_MAX_TOLERANCE_DAYS,
    SHARES_LOOKUP_MAX_TOLERANCE_DAYS,
    nearest_value_lookup,
)
from magicformula.formulas import FinancialInputs, compute_metrics
from magicformula.ranker import rank_universe
from magicformula.backtest import run_backtest, compute_cagr, compute_annualized_volatility, compute_max_drawdown

from magicformula.universe import build_universe

AMFI_XLSX_URL = "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx"
REBALANCE_DATES = [date(y, 6, 1) for y in range(2019, 2027)]
TOP_N = 30
SERIES_CACHE_PATH = Path("data/raw/point_in_time_series_cache.pkl")
RISK_FREE_RATE = 0.053
NIFTY500_TRI = [14591.01, 12092.03, 20113.79, 21709.49, 24528.90, 33168.20, 36160.26, 35911.81]


def load_fundamentals(symbols):
    df = pd.read_parquet("data/raw/full_universe_fundamentals.parquet")
    df = df[df["symbol"].isin(symbols)]
    fields = [
        "symbol", "sector", "broad_sector", "fiscal_year_end", "operating_profit",
        "depreciation_amortization", "total_assets", "other_liabilities", "investments",
        "cwip", "borrowings", "market_cap", "source_url", "statement", "cap_bucket",
    ]
    return [FundamentalsRecord(**row._asdict()) for row in df[fields].itertuples(index=False)]


def make_rank_fn(price_shares_by_symbol, cap_bucket_by_symbol, mode: str, flagged_counts: dict):
    """mode: 'flag_only' keeps every company; 'exclude' drops any company
    this rebalance date's fresh, point-in-time anomaly check flags, before
    ranking - the actual selection-level test the flag-only version never
    was.
    """
    def rank_fn(eligible, rebalance_date):
        latest = _latest_record_per_symbol(eligible)
        anomaly_results = check_fundamentals_anomalies(latest)
        flagged = {r.symbol for r in anomaly_results if r.flagged}
        flagged_counts[rebalance_date] = len(flagged)

        candidates = latest if mode == "flag_only" else [r for r in latest if r.symbol not in flagged]

        metrics = []
        for r in candidates:
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
    symbols = sorted({r.symbol for r in fundamentals})
    cap_bucket_by_symbol = {s: entry_by_symbol[s].cap_bucket for s in symbols}
    sector_by_symbol = {r.symbol: r.sector for r in fundamentals if r.sector}
    listing_date_by_symbol = {
        s: entry_by_symbol[s].date_of_listing for s in symbols if entry_by_symbol[s].date_of_listing is not None
    }
    with SERIES_CACHE_PATH.open("rb") as f:
        price_shares_by_symbol = pickle.load(f)

    def price_lookup(symbol, d):
        series = price_shares_by_symbol.get(symbol)
        if series is None:
            return None
        price_series, _ = series
        return nearest_value_lookup(price_series, d, max_days_tolerance=PRICE_LOOKUP_MAX_TOLERANCE_DAYS)

    nifty500_returns = [NIFTY500_TRI[i + 1] / NIFTY500_TRI[i] - 1 for i in range(len(NIFTY500_TRI) - 1)]

    results = {}
    flagged_counts_by_mode = {}
    for mode in ("flag_only", "exclude"):
        flagged_counts: dict = {}
        rank_fn = make_rank_fn(price_shares_by_symbol, cap_bucket_by_symbol, mode, flagged_counts)
        result = run_backtest(
            REBALANCE_DATES, fundamentals, rank_fn, price_lookup, nifty500_returns,
            top_n=TOP_N, sector_by_symbol=sector_by_symbol, listing_date_by_symbol=listing_date_by_symbol,
            transaction_cost_bps=25.0, risk_free_rate=RISK_FREE_RATE, periods_per_year=1.0,
        )
        results[mode] = result
        flagged_counts_by_mode[mode] = flagged_counts

    print(f"{'Period':<26}{'flag_only':>12}{'exclude':>12}{'diff':>10}")
    for i, step in enumerate(results["flag_only"].steps[:-1]):
        fo = results["flag_only"].period_returns[i]
        ex = results["exclude"].period_returns[i]
        start, end = step.rebalance_date, results["flag_only"].steps[i + 1].rebalance_date
        print(f"{start} -> {end}  {fo:+11.4f}{ex:+12.4f}{ex-fo:+10.4f}")

    print("\n=== Basket size and excluded-company count per rebalance (exclude mode) ===")
    for step in results["exclude"].steps:
        n_flagged = flagged_counts_by_mode["exclude"][step.rebalance_date]
        print(f"  {step.rebalance_date}: basket={len(step.outcome.holdings)}, flagged-and-excluded={n_flagged}")

    print("\n=== Side-by-side stats ===")
    for mode in ("flag_only", "exclude"):
        s = results[mode].stats
        print(f"\n[{mode}]")
        print(f"  CAGR:                  {s.cagr:+.4%}")
        print(f"  Annualized volatility: {s.annualized_volatility:.4%}")
        print(f"  Sharpe ratio:          {s.sharpe_ratio:.3f}")
        print(f"  Max drawdown:          {s.max_drawdown:.4%}")
        print(f"  Hit rate vs Nifty500:  {s.hit_rate:.4%}")
        print(f"  Mean turnover:         {s.mean_turnover:.4%}")
        print(f"  Final equity:          {results[mode].equity_curve[-1]:.4f}x")


if __name__ == "__main__":
    main()
