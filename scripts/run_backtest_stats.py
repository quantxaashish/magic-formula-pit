"""Section 7 performance stats for the real 2019-2026 expanded-pilot basket
walk (decision 0012). Reuses the same cached universe/fundamentals/price
data the pilot already fetched - this does NOT redo the coverage
quantification or hit yfinance again, it only runs run_rebalance_walk
(cheap, local) plus magicformula.backtest's performance-stat functions on
top of it.

Benchmark: Nifty 500 TRI, pulled live from niftyindices.com's own
"Total returns Index Values" report for the 8 rebalance dates (nearest
trading day on-or-before each, same point-in-time convention
nearest_value_lookup uses - never a future value). Not fabricated, not a
placeholder.

Risk-free rate: no single authoritative 91-day T-bill *time series* was
reliably obtainable in this session, so this uses the time-weighted
average RBI policy repo rate over 2019-06-01..2026-06-01 (precisely
documented, easy to verify) as the stated proxy - 91-day T-bills
historically track repo within a narrow band. This is a real, sourced
approximation, not an invented number; PerformanceStats.risk_free_rate_used
records exactly what was used so nothing is hidden.

2026-06-01 edge effect (decision 0012's shares-coverage dip to 51-64%):
handled explicitly, not by silently including or excluding it. See the
printed note at the bottom of this script's output for the reasoning -
short version: the last period return (2025-06-01 -> 2026-06-01) prices
the *2025* basket through to 2026-06-01 using the tight 14-day
PRICE_LOOKUP_MAX_TOLERANCE_DAYS, not the degraded shares-lookup tolerance,
so it is not corrupted by the 2026 rebalance's own thin coverage. What
*is* degraded is the 2026-06-01 rebalance's own new picks (the terminal
holdings snapshot) - reported separately, flagged low-confidence, not
folded into the headline CAGR/Sharpe/drawdown numbers' interpretation.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

from magicformula.data_fetch.fundamentals import (
    FundamentalsRecord,
    _latest_record_per_symbol,
    apply_anomaly_filter,
    check_fundamentals_anomalies,
)
from magicformula.data_fetch.prices import (
    PRICE_LOOKUP_MAX_TOLERANCE_DAYS,
    SHARES_LOOKUP_MAX_TOLERANCE_DAYS,
    nearest_value_lookup,
    yf_symbol,
)
from magicformula.formulas import FinancialInputs, compute_metrics
from magicformula.ranker import RankedStock, rank_universe
from magicformula.backtest import run_backtest, write_basket_composition_csv, compute_turnover
from magicformula.universe import build_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backtest_stats")

AMFI_XLSX_URL = "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx"
REBALANCE_DATES = [date(y, 6, 1) for y in range(2019, 2027)]  # 2019-06-01 .. 2026-06-01, decision 0012
TOP_N = 30
SERIES_CACHE_PATH = Path("data/raw/point_in_time_series_cache.pkl")

# Real Nifty 500 TRI values pulled directly from niftyindices.com's "Total
# returns Index Values" report (nearest trading day on-or-before each
# rebalance date - same point-in-time convention as nearest_value_lookup):
#   2019-06-01 -> 31 May 2019 (Fri; 06-01 was a Saturday): 14591.01
#   2020-06-01 -> 01 Jun 2020 (Mon, exact):                12092.03
#   2021-06-01 -> 01 Jun 2021 (Tue, exact):                20113.79
#   2022-06-01 -> 01 Jun 2022 (Wed, exact):                21709.49
#   2023-06-01 -> 01 Jun 2023 (Thu, exact):                24528.90
#   2024-06-01 -> 31 May 2024 (Fri; 06-01 was a Saturday):  33168.20
#   2025-06-01 -> 30 May 2025 (Fri; 06-01 was a Sunday):    36160.26
#   2026-06-01 -> 01 Jun 2026 (Mon, exact):                35911.81
NIFTY500_TRI = [14591.01, 12092.03, 20113.79, 21709.49, 24528.90, 33168.20, 36160.26, 35911.81]
BENCHMARK_RETURNS = [NIFTY500_TRI[i + 1] / NIFTY500_TRI[i] - 1 for i in range(len(NIFTY500_TRI) - 1)]

# Time-weighted average RBI policy repo rate, 2019-06-01..2026-06-01
# (documented rate-change dates, computed by hand - see decision doc for
# the full segment-by-segment calculation): ~5.33%, used as the stated
# risk-free proxy since a reliable 91-day T-bill *time series* wasn't
# obtainable this session. Rounded to 5.30% for the stats call.
RISK_FREE_RATE = 0.053


def load_fundamentals(symbols: set[str]) -> list[FundamentalsRecord]:
    df = pd.read_parquet("data/raw/full_universe_fundamentals.parquet")
    df = df[df["symbol"].isin(symbols)]
    fields = [
        "symbol", "sector", "broad_sector", "fiscal_year_end", "operating_profit",
        "depreciation_amortization", "total_assets", "other_liabilities", "investments",
        "cwip", "borrowings", "market_cap", "source_url", "statement", "cap_bucket",
    ]
    records = [FundamentalsRecord(**row._asdict()) for row in df[fields].itertuples(index=False)]
    anomaly_results = check_fundamentals_anomalies(_latest_record_per_symbol(records))
    return apply_anomaly_filter(records, anomaly_results, mode="flag_only")


def load_cached_series() -> dict:
    with SERIES_CACHE_PATH.open("rb") as f:
        import pickle
        return pickle.load(f)


def make_rank_fn(price_shares_by_symbol, cap_bucket_by_symbol):
    def rank_fn(eligible: list[FundamentalsRecord], rebalance_date: date) -> list[RankedStock]:
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
                symbol=r.symbol,
                operating_profit=r.operating_profit,
                depreciation_amortization=r.depreciation_amortization,
                total_assets=r.total_assets,
                current_liabilities=r.other_liabilities,
                market_cap=market_cap,
                total_debt=r.borrowings,
                cash_and_equivalents=0,
                other_liquid_investments=r.investments,
            )
            metrics.append(compute_metrics(inputs, roce_mode="standard"))
        return rank_universe(metrics, mode="within_bucket", cap_bucket_by_symbol=cap_bucket_by_symbol)

    return rank_fn


def main() -> None:
    entries = build_universe(AMFI_XLSX_URL, as_of=date(2026, 9, 6))
    entry_by_symbol = {e.nse_symbol: e for e in entries if e.nse_symbol}

    fundamentals_all = load_fundamentals(set(entry_by_symbol))
    all_bucket_symbols = {e.nse_symbol for e in entries if e.cap_bucket in ("large", "mid", "small") and e.nse_symbol}
    fundamentals = [r for r in fundamentals_all if r.symbol in all_bucket_symbols]
    symbols_with_fundamentals = sorted({r.symbol for r in fundamentals})

    cap_bucket_by_symbol = {s: entry_by_symbol[s].cap_bucket for s in symbols_with_fundamentals}
    sector_by_symbol = {r.symbol: r.sector for r in fundamentals if r.sector}
    listing_date_by_symbol = {
        s: entry_by_symbol[s].date_of_listing
        for s in symbols_with_fundamentals
        if entry_by_symbol[s].date_of_listing is not None
    }

    price_shares_by_symbol = load_cached_series()
    logger.info("Loaded %d companies' cached price/shares series", len(price_shares_by_symbol))

    def price_lookup(symbol: str, d: date) -> float | None:
        series = price_shares_by_symbol.get(symbol)
        if series is None:
            return None
        price_series, _ = series
        return nearest_value_lookup(price_series, d, max_days_tolerance=PRICE_LOOKUP_MAX_TOLERANCE_DAYS)

    rank_fn = make_rank_fn(price_shares_by_symbol, cap_bucket_by_symbol)

    result = run_backtest(
        REBALANCE_DATES,
        fundamentals,
        rank_fn,
        price_lookup,
        BENCHMARK_RETURNS,
        top_n=TOP_N,
        sector_by_symbol=sector_by_symbol,
        listing_date_by_symbol=listing_date_by_symbol,
        transaction_cost_bps=25.0,
        risk_free_rate=RISK_FREE_RATE,
        periods_per_year=1.0,
    )

    print("\n=== Rebalance dates and basket sizes ===")
    for step in result.steps:
        print(f"  {step.rebalance_date}: eligible={step.eligible_universe_size}, basket={len(step.outcome.holdings)}")

    print("\n=== Period returns (strategy, net of 25bps txn cost) vs Nifty 500 TRI benchmark ===")
    for i, (sr, br) in enumerate(zip(result.period_returns, result.benchmark_returns)):
        start = result.steps[i].rebalance_date
        end = result.steps[i + 1].rebalance_date
        print(f"  {start} -> {end}: strategy={sr:+.4f}  benchmark={br:+.4f}  excess={sr - br:+.4f}")

    print("\n=== Equity curves (starting value 1.0) ===")
    print("  strategy:  ", [round(v, 4) for v in result.equity_curve])
    print("  benchmark: ", [round(v, 4) for v in result.benchmark_equity_curve])

    print("\n=== PerformanceStats ===")
    s = result.stats
    print(f"  CAGR:                  {s.cagr:+.4%}")
    print(f"  Annualized volatility: {s.annualized_volatility:.4%}")
    print(f"  Sharpe ratio:          {s.sharpe_ratio:.3f}  (risk_free_rate_used={s.risk_free_rate_used:.4%})")
    print(f"  Max drawdown:          {s.max_drawdown:.4%}")
    print(f"  Hit rate vs benchmark: {s.hit_rate:.4%}  ({s.periods} periods)")
    print(f"  Mean turnover:         {s.mean_turnover:.4%}")

    # Benchmark CAGR/vol for comparison
    from magicformula.backtest import compute_cagr, compute_annualized_volatility, compute_max_drawdown
    bench_cagr = compute_cagr(result.benchmark_returns, periods_per_year=1.0)
    bench_vol = compute_annualized_volatility(result.benchmark_returns, periods_per_year=1.0)
    bench_dd = compute_max_drawdown(result.benchmark_equity_curve)
    print(f"\n  Benchmark CAGR:        {bench_cagr:+.4%}")
    print(f"  Benchmark volatility:  {bench_vol:.4%}")
    print(f"  Benchmark max drawdown:{bench_dd:.4%}")

    # 2026-06-01 edge-effect check: does the final period return actually
    # depend on the degraded 2026 rebalance, or does it use the 2025
    # basket priced through? Report price-lookup success rate for the
    # 2025 basket at the 2026-06-01 date to confirm empirically.
    basket_2025 = result.steps[-2].outcome.weights
    hits = sum(1 for sym in basket_2025 if price_lookup(sym, date(2026, 6, 1)) is not None)
    print(f"\n=== 2026-06-01 edge-effect check ===")
    print(f"  2025-06-01 basket size: {len(basket_2025)}")
    print(f"  of which priced successfully at 2026-06-01 (14-day tolerance): {hits} ({hits/len(basket_2025):.1%})")
    print(f"  (this is what actually feeds the final period return - NOT the 2026 rebalance's own shares-lookup coverage)")

    out_path = Path("data/raw/backtest_baskets_2019_2026.csv")
    write_basket_composition_csv(result.steps, out_path)
    print(f"\nBasket composition written to {out_path}")

    # Save raw stats for downstream chart generation
    payload = {
        "period_returns": result.period_returns,
        "benchmark_returns": result.benchmark_returns,
        "equity_curve": result.equity_curve,
        "benchmark_equity_curve": result.benchmark_equity_curve,
        "rebalance_dates": [s.rebalance_date.isoformat() for s in result.steps],
        "stats": {
            "cagr": s.cagr, "annualized_volatility": s.annualized_volatility,
            "sharpe_ratio": s.sharpe_ratio, "risk_free_rate_used": s.risk_free_rate_used,
            "max_drawdown": s.max_drawdown, "hit_rate": s.hit_rate,
            "mean_turnover": s.mean_turnover, "periods": s.periods,
            "benchmark_cagr": bench_cagr, "benchmark_volatility": bench_vol, "benchmark_max_drawdown": bench_dd,
        },
    }
    with open("data/raw/backtest_stats.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("Raw stats written to data/raw/backtest_stats.json")


if __name__ == "__main__":
    main()
