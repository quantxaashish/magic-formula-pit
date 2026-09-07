"""First real-data run of the backtest engine (docs/decisions/0007-0010
had to clear before this could happen; see those for what's still an
accepted, documented bias vs. what's fully resolved).

Deliberately scoped, not a full-universe production run:

  - Large + mid cap only (166 companies) - reuses the fundamentals
    already fetched in full_universe_fundamentals.parquet, no new
    screener.in fetching. Small cap is held back from this first pass
    because decision 0008 found the zero-cash proxy bias is materially
    worse there (up to +9.63% relative EY shift) - large/mid cap is
    where the "document and proceed" call actually applies cleanly.
  - 3 annual rebalance dates (2024-06-01, 2025-06-01, 2026-06-01), not a
    long historical walk - enough to hand-check basket composition
    (what this script is for), not enough to trust a Sharpe ratio from.
  - anomaly_mode="flag_only", not "exclude" - decision 0010 found the
    current 2x-median threshold flags 51.1% of the universe for a
    calibration reason (heavy right-skew), not because half the universe
    is genuinely anomalous. Hard-excluding on an unrecalibrated threshold
    would silently gut the basket.
  - Point-in-time market cap uses real price x real point-in-time shares
    outstanding (yfinance get_shares_full(), cleaned with
    filter_persisted_values per decision 0008 part 4) - not screener.in's
    stale "current" market_cap figure, per decision 0007.
  - Cash proxy: cash_and_equivalents=0, other_liquid_investments=
    Investments - the documented, bounded bias from decision 0008, not a
    real Cash source (that's still future work, not a blocker for large/
    mid cap per 0008's revised recommendation).

Usage: python scripts/run_real_backtest_pilot.py
Writes: data/raw/real_backtest_pilot_baskets.csv (one row per
        rebalance_date x symbol x weight, for hand inspection)
        data/raw/real_backtest_pilot_run.log
"""

from __future__ import annotations

import logging
import time
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
    YFinanceClient,
    compute_point_in_time_market_cap,
    filter_persisted_values,
    nearest_value_lookup,
    yf_symbol,
)
from magicformula.formulas import FinancialInputs, compute_metrics
from magicformula.ranker import RankedStock, rank_universe
from magicformula.backtest import run_rebalance_walk
from magicformula.universe import build_universe

AMFI_XLSX_URL = "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx"
REBALANCE_DATES = [date(2024, 6, 1), date(2025, 6, 1), date(2026, 6, 1)]
TOP_N = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("data/raw/real_backtest_pilot_run.log", mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("real_backtest_pilot")


def load_fundamentals(symbols: set[str]) -> list[FundamentalsRecord]:
    """Loads fundamentals for `symbols` and recomputes anomaly_reasons
    fresh with the current calibration (decision 0010: log-scale IQR,
    min_sector_size=5) rather than trusting the anomaly_reasons column
    already sitting in full_universe_fundamentals.parquet - that column
    was written by the original fetch under the OLD flat-2x-median
    threshold and would silently carry the stale, since-corrected 51.1%
    flag rate into this pilot otherwise. Recomputed on this pilot's own
    166-company subset (its own real peer groups), not copied from the
    full-universe run, since a company's peer-relative anomaly status
    can differ depending on who else is in the pool being compared - the
    single most correct answer is only had by using the exact same
    real-data path a full-universe re-run would produce for these symbols.
    """
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


def fetch_point_in_time_series(nse_symbol: str, bse_symbol: str | None) -> tuple[dict, dict] | None:
    """Real price history + cleaned point-in-time shares-outstanding
    history for one company, each fetched once (yfinance returns the
    whole period in one call, not one call per date) and reused across
    every rebalance date via nearest_value_lookup.
    """
    try:
        symbol = yf_symbol(nse_symbol, bse_symbol)
    except ValueError:
        return None

    ticker = yf.Ticker(symbol)
    try:
        hist = ticker.history(period="5y")
        if hist is None or hist.empty:
            return None
        price_series = {ts.date(): float(px) for ts, px in hist["Close"].items()}

        shares = ticker.get_shares_full(start="2020-01-01")
        if shares is None or len(shares) == 0:
            return None
        raw_shares_series = {ts.date(): float(v) for ts, v in shares.items()}
        shares_series = filter_persisted_values(raw_shares_series)
        if not shares_series:
            return None
    except Exception as exc:  # noqa: BLE001 - yfinance raises all sorts
        logger.warning("%s: price/shares fetch failed: %s", symbol, exc)
        return None

    return price_series, shares_series


def make_rank_fn(
    price_shares_by_symbol: dict[str, tuple[dict, dict]],
    cap_bucket_by_symbol: dict[str, str],
    ranked_by_date: dict[date, list[RankedStock]],
):
    """`ranked_by_date` is populated as a side effect, keyed by rebalance
    date - run_rebalance_walk's own return value (RebalanceStep) only
    carries symbol->weight through portfolio.rebalance(), not the ROCE/EY/
    rank detail behind each pick, so this is how that detail survives to
    the final report without re-deriving it after the fact.
    """
    def rank_fn(eligible: list[FundamentalsRecord], rebalance_date: date) -> list[RankedStock]:
        latest = _latest_record_per_symbol(eligible)
        metrics = []
        skipped_no_market_cap = 0
        flagged_count = sum(1 for r in latest if r.anomaly_reasons)
        for r in latest:
            # flag_only, not exclude (explicit instruction): decision 0010
            # found the 2x-median anomaly threshold flags 51.1% of the
            # universe for a calibration reason (heavy right-skew in both
            # ratios), not because half the universe is genuinely
            # structurally distorted. Excluding on it here would silently
            # rebuild the same "half the universe gone" problem inside the
            # backtest. Flagged companies stay eligible for ranking; which
            # ones ended up in the final basket is logged below so the
            # hand-check can see it, not hidden.
            series = price_shares_by_symbol.get(r.symbol)
            if series is None:
                skipped_no_market_cap += 1
                continue
            price_series, shares_series = series
            price = nearest_value_lookup(price_series, rebalance_date, max_days_tolerance=14)
            shares = nearest_value_lookup(shares_series, rebalance_date, max_days_tolerance=400)
            market_cap_raw_rupees = compute_point_in_time_market_cap(price, shares)
            if market_cap_raw_rupees is None:
                skipped_no_market_cap += 1
                continue
            # price is in Rs., shares_outstanding is a raw share count, so
            # price x shares is in raw rupees - every other FinancialInputs
            # field here (operating_profit, total_assets, borrowings,
            # investments) is in Rs. Crore, screener.in's own convention
            # throughout this project (1 Crore = 1e7). Caught by hand-
            # checking a small sample before the full pilot run: without
            # this conversion, EY came out ~1e-9 instead of a sane ~0.05-0.10.
            market_cap = market_cap_raw_rupees / 1e7

            inputs = FinancialInputs(
                symbol=r.symbol,
                operating_profit=r.operating_profit,
                depreciation_amortization=r.depreciation_amortization,
                total_assets=r.total_assets,
                current_liabilities=r.other_liabilities,
                market_cap=market_cap,
                total_debt=r.borrowings,
                cash_and_equivalents=0,  # documented bias, decision 0008
                other_liquid_investments=r.investments,
            )
            metrics.append(compute_metrics(inputs, roce_mode="standard"))

        logger.info(
            "%s: %d latest records (%d anomaly-flagged but still ranked, flag_only), "
            "%d skipped (no point-in-time market cap), %d ranked",
            rebalance_date, len(latest), flagged_count, skipped_no_market_cap, len(metrics),
        )
        ranked = rank_universe(metrics, mode="within_bucket", cap_bucket_by_symbol=cap_bucket_by_symbol)
        ranked_by_date[rebalance_date] = ranked
        return ranked

    return rank_fn


def main() -> None:
    start = time.monotonic()
    entries = build_universe(AMFI_XLSX_URL, as_of=date(2026, 9, 6))
    entry_by_symbol = {e.nse_symbol: e for e in entries if e.nse_symbol}

    fundamentals_all = load_fundamentals(set(entry_by_symbol))
    large_mid_symbols = {
        e.nse_symbol for e in entries
        if e.cap_bucket in ("large", "mid") and e.nse_symbol
    }
    fundamentals = [r for r in fundamentals_all if r.symbol in large_mid_symbols]
    symbols_with_fundamentals = sorted({r.symbol for r in fundamentals})
    logger.info(
        "Pilot universe: %d large+mid cap symbols with fetched fundamentals",
        len(symbols_with_fundamentals),
    )

    cap_bucket_by_symbol = {s: entry_by_symbol[s].cap_bucket for s in symbols_with_fundamentals}
    sector_by_symbol = {r.symbol: r.sector for r in fundamentals if r.sector}
    listing_date_by_symbol = {
        s: entry_by_symbol[s].date_of_listing
        for s in symbols_with_fundamentals
        if entry_by_symbol[s].date_of_listing is not None
    }

    logger.info("Fetching real point-in-time price + shares-outstanding history (one call each, per company)...")
    price_shares_by_symbol: dict[str, tuple[dict, dict]] = {}
    for i, symbol in enumerate(symbols_with_fundamentals):
        entry = entry_by_symbol[symbol]
        result = fetch_point_in_time_series(entry.nse_symbol, entry.bse_symbol)
        if result is not None:
            price_shares_by_symbol[symbol] = result
        if (i + 1) % 25 == 0:
            logger.info("  %d/%d companies fetched (%d with usable data so far)",
                        i + 1, len(symbols_with_fundamentals), len(price_shares_by_symbol))

    logger.info(
        "Price/shares fetch complete: %d of %d companies have usable point-in-time data",
        len(price_shares_by_symbol), len(symbols_with_fundamentals),
    )

    ranked_by_date: dict[date, list[RankedStock]] = {}
    rank_fn = make_rank_fn(price_shares_by_symbol, cap_bucket_by_symbol, ranked_by_date)

    steps = run_rebalance_walk(
        REBALANCE_DATES,
        fundamentals,
        rank_fn,
        top_n=TOP_N,
        sector_by_symbol=sector_by_symbol,
        listing_date_by_symbol=listing_date_by_symbol,
    )

    flagged_symbols = {r.symbol for r in _latest_record_per_symbol(fundamentals) if r.anomaly_reasons}

    for step in steps:
        flagged_in_basket = sorted(step.outcome.holdings & flagged_symbols)
        logger.info(
            "%s: eligible=%d, basket size=%d, buys=%d, sells=%d, anomaly-flagged names in basket=%s",
            step.rebalance_date, step.eligible_universe_size,
            len(step.outcome.holdings), len(step.outcome.buys), len(step.outcome.sells),
            flagged_in_basket if flagged_in_basket else "none",
        )

    out_path = Path("data/raw/real_backtest_pilot_baskets.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import csv as csv_module
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv_module.writer(f)
        writer.writerow([
            "rebalance_date", "symbol", "weight", "cap_bucket", "sector",
            "roce", "ey", "rank_roce", "rank_ey", "combined_rank", "position_in_bucket",
            "anomaly_flagged",
        ])
        for step in steps:
            ranked_lookup = {r.symbol: r for r in ranked_by_date.get(step.rebalance_date, [])}
            for symbol, weight in sorted(step.outcome.weights.items()):
                rs = ranked_lookup.get(symbol)
                writer.writerow([
                    step.rebalance_date.isoformat(), symbol, weight,
                    cap_bucket_by_symbol.get(symbol), sector_by_symbol.get(symbol),
                    rs.roce if rs else None, rs.ey if rs else None,
                    rs.rank_roce if rs else None, rs.rank_ey if rs else None,
                    rs.combined_rank if rs else None, rs.position if rs else None,
                    symbol in flagged_symbols,
                ])
    logger.info("Baskets written to %s (includes ROCE/EY/rank/cap_bucket/sector/anomaly_flagged columns for hand-checking)", out_path)
    logger.info("Elapsed: %.1f minutes", (time.monotonic() - start) / 60)


if __name__ == "__main__":
    main()
