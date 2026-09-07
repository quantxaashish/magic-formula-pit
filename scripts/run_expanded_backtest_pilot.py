"""Second, wider pilot before the real full-universe backtest - gates the
full run the same way the first (166-company, 3-date) pilot gated this
one. Two things widen relative to that first pilot, each addressing a
gap explicitly flagged after it:

  - All three cap buckets, not just large+mid. The first pilot held
    small cap back deliberately (decision 0008's zero-cash proxy bias is
    materially worse there, up to +9.63% relative EY shift) and every
    other data-quality problem found in this project so far has been
    worse in small cap - failure rates, anomaly flags, EY bias. All four
    gates (embargo, listing-date clip, fundamentals-ratio anomaly filter,
    financial/utility exclusion) were only checked against large+mid;
    this run is what actually exercises them against small cap for the
    first time.
  - Originally 11 annual rebalance dates (2016-06-01 through
    2026-06-01) - "as far back as the data reasonably allows toward
    2015". Decision 0012 measured that directly rather than assuming it
    and found 2016 coverage is 1-15% depending on cap bucket (not
    usable) and 2017-2018 inconsistent (large-cap coverage actually
    *drops* between those two years) - 2019 is the first date where
    every bucket clears 85% and stays there. REBALANCE_DATES now starts
    at 2019-06-01, not 2016-06-01; the excluded 2016-2018 dates are not
    carried forward with a caveat, they're just not real cross-sections
    of the market.

Decision 0012 changed two more things about *how* this runs, discovered
while preparing to trust this pilot's own output:

  - `nearest_value_lookup`'s tolerance for shares-outstanding lookups was
    a bare 400 days in the first version of this script - not a
    meaningful bound for annual rebalancing. Now uses the named,
    documented `SHARES_LOOKUP_MAX_TOLERANCE_DAYS` (180) from
    magicformula.data_fetch.prices.
  - Point-in-time price/shares series are now cached to disk
    (data/raw/point_in_time_series_cache.pkl) after the first fetch, and
    exclusion reasons are tracked explicitly per (rebalance_date,
    symbol) - "no fetch at all" vs "price lookup failed" vs "shares
    lookup failed" vs "ranked" - written to its own coverage CSV, not
    just a single lumped "skipped" counter. This is what makes the
    coverage-by-year-and-bucket question answerable at all, and lets a
    corrected tolerance be re-applied without re-hitting yfinance for
    1,787 companies a second time.
  - `magicformula.formulas.compute_metrics` crashed with
    ZeroDivisionError on a real small-cap company (capital employed
    exactly 0) the first time this script covered small cap at full
    history - fixed there (EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED), not
    worked around here.

Still not the real thing: this is still `anomaly_mode="flag_only"`
(decision 0010's calibration fix is real, but nothing currently needs
exclude-mode), still the zero-cash proxy (decision 0008, worse for small
cap - watch for it explicitly in this run's small-cap holdings, don't
just assume it away), and still not wired to a benchmark series for a
real Sharpe/CAGR claim.

Usage: python scripts/run_expanded_backtest_pilot.py
Writes: data/raw/expanded_backtest_pilot_baskets.csv
        data/raw/expanded_backtest_pilot_coverage.csv (per rebalance_date
          x symbol: whether it was ranked, and if not, exactly why)
        data/raw/point_in_time_series_cache.pkl (raw price/shares series,
          reused on the next run instead of re-fetched)
        data/raw/expanded_backtest_pilot_run.log
"""

from __future__ import annotations

import csv as csv_module
import logging
import pickle
import time
from datetime import date
from pathlib import Path

import pandas as pd

from magicformula.data_fetch.fundamentals import (
    FundamentalsRecord,
    _latest_record_per_symbol,
    check_fundamentals_anomalies,
)
from magicformula.data_fetch.prices import (
    PRICE_LOOKUP_MAX_TOLERANCE_DAYS,
    SHARES_LOOKUP_MAX_TOLERANCE_DAYS,
    fetch_point_in_time_series,
    nearest_value_lookup,
)
from magicformula.formulas import FinancialInputs, compute_metrics
from magicformula.ranker import RankedStock, rank_universe
from magicformula.backtest import run_rebalance_walk
from magicformula.universe import build_universe

AMFI_XLSX_URL = "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx"
REBALANCE_DATES = [date(y, 6, 1) for y in range(2019, 2027)]  # 2019-06-01 .. 2026-06-01 (decision 0012, reconfirmed at 90-day tolerance)
TOP_N = 30
SERIES_CACHE_PATH = Path("data/raw/point_in_time_series_cache.pkl")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("data/raw/expanded_backtest_pilot_run.log", mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("expanded_backtest_pilot")


def load_fundamentals(symbols: set[str]) -> list[FundamentalsRecord]:
    """Loads every fiscal year's raw records, unfiltered by anomaly status.

    Anomaly status is NOT computed here anymore. It used to be: computed
    once, globally, on each symbol's single latest fiscal year, then
    stamped onto every fiscal-year record for that symbol via
    apply_anomaly_filter's symbol-keyed (not (symbol, fiscal_year)-keyed)
    lookup - so a company anomalous only in its most recent year showed
    as flagged in every historical rebalance's basket too, real bug,
    confirmed on VEDL (flagged FY2019-FY2025 baskets purely because of an
    FY2026 balance-sheet event; point-in-time re-check showed those six
    years were clean). Fixed by moving the anomaly check into make_rank_fn,
    computed fresh per rebalance date against that date's own
    point-in-time-eligible latest-year records - see make_rank_fn.
    """
    df = pd.read_parquet("data/raw/full_universe_fundamentals.parquet")
    df = df[df["symbol"].isin(symbols)]
    fields = [
        "symbol", "sector", "broad_sector", "fiscal_year_end", "operating_profit",
        "depreciation_amortization", "total_assets", "other_liabilities", "investments",
        "cwip", "borrowings", "market_cap", "source_url", "statement", "cap_bucket",
    ]
    return [FundamentalsRecord(**row._asdict()) for row in df[fields].itertuples(index=False)]


def fetch_all_series(symbols_with_fundamentals, entry_by_symbol) -> dict[str, tuple[dict, dict]]:
    """Loads from SERIES_CACHE_PATH if present - decision 0012's fix
    changes lookup-time tolerances, not the raw series themselves, so a
    cached fetch remains valid across a tolerance change and doesn't
    need to re-hit yfinance for 1,787 companies again."""
    if SERIES_CACHE_PATH.exists():
        with SERIES_CACHE_PATH.open("rb") as f:
            cached = pickle.load(f)
        missing = [s for s in symbols_with_fundamentals if s not in cached]
        if not missing:
            logger.info("Loaded all %d companies' price/shares series from cache (%s)",
                        len(cached), SERIES_CACHE_PATH)
            return cached
        logger.info("Cache has %d/%d companies; fetching %d missing ones",
                    len(cached) - len(missing), len(symbols_with_fundamentals), len(missing))
    else:
        cached = {}
        missing = list(symbols_with_fundamentals)

    for i, symbol in enumerate(missing):
        entry = entry_by_symbol[symbol]
        result = fetch_point_in_time_series(entry.nse_symbol, entry.bse_symbol)
        if result is not None:
            cached[symbol] = result
        if (i + 1) % 100 == 0:
            logger.info("  %d/%d missing companies fetched (%d total cached so far)",
                        i + 1, len(missing), len(cached))

    SERIES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SERIES_CACHE_PATH.open("wb") as f:
        pickle.dump(cached, f)
    logger.info("Cached %d companies' price/shares series to %s", len(cached), SERIES_CACHE_PATH)
    return cached


def make_rank_fn(
    price_shares_by_symbol: dict[str, tuple[dict, dict]],
    cap_bucket_by_symbol: dict[str, str],
    sector_by_symbol_all: dict[str, str],
    ranked_by_date: dict[date, list[RankedStock]],
    coverage_rows: list[tuple],
    flagged_by_date: dict[date, set[str]],
):
    """`coverage_rows` accumulates (rebalance_date, symbol, cap_bucket,
    sector, reason) for every fundamentals-eligible company at every
    rebalance, `reason` in {"ranked", "no_fetch", "no_price", "no_shares"}
    - the exact breakdown decision 0012 asked for, not a single lumped
    "skipped" count.

    `flagged_by_date` accumulates this rebalance date's own anomaly-flagged
    symbol set, computed fresh here (not reused from a prior date or a
    globally-latest-year snapshot) - the point-in-time fix for the
    load_fundamentals bug described above. mode stays flag_only: nothing
    is dropped from `metrics`, this only records which symbols were
    flagged for this specific date's basket/CSV/log reporting.
    """
    def rank_fn(eligible: list[FundamentalsRecord], rebalance_date: date) -> list[RankedStock]:
        latest = _latest_record_per_symbol(eligible)
        anomaly_results = check_fundamentals_anomalies(latest)
        flagged_by_date[rebalance_date] = {r.symbol for r in anomaly_results if r.flagged}

        metrics = []
        reason_counts: dict[str, int] = {}
        for r in latest:
            reason = None
            series = price_shares_by_symbol.get(r.symbol)
            if series is None:
                reason = "no_fetch"
            else:
                price_series, shares_series = series
                price = nearest_value_lookup(
                    price_series, rebalance_date, max_days_tolerance=PRICE_LOOKUP_MAX_TOLERANCE_DAYS
                )
                if price is None:
                    reason = "no_price"
                else:
                    shares = nearest_value_lookup(
                        shares_series, rebalance_date, max_days_tolerance=SHARES_LOOKUP_MAX_TOLERANCE_DAYS
                    )
                    if shares is None:
                        reason = "no_shares"
                    else:
                        market_cap = (price * shares) / 1e7  # raw rupees -> Rs. Crore
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
                        reason = "ranked"

            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            coverage_rows.append((
                rebalance_date.isoformat(), r.symbol, r.cap_bucket,
                sector_by_symbol_all.get(r.symbol), reason,
            ))

        by_bucket = {}
        for r in latest:
            by_bucket[r.cap_bucket] = by_bucket.get(r.cap_bucket, 0) + 1
        logger.info(
            "%s: %d latest records %s, coverage breakdown=%s",
            rebalance_date, len(latest), by_bucket, reason_counts,
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
    all_bucket_symbols = {
        e.nse_symbol for e in entries
        if e.cap_bucket in ("large", "mid", "small") and e.nse_symbol
    }
    fundamentals = [r for r in fundamentals_all if r.symbol in all_bucket_symbols]
    symbols_with_fundamentals = sorted({r.symbol for r in fundamentals})
    by_bucket_count: dict[str, int] = {}
    for r in _latest_record_per_symbol(fundamentals):
        by_bucket_count[r.cap_bucket] = by_bucket_count.get(r.cap_bucket, 0) + 1
    logger.info(
        "Expanded pilot universe: %d symbols with fetched fundamentals, by bucket: %s",
        len(symbols_with_fundamentals), by_bucket_count,
    )

    cap_bucket_by_symbol = {s: entry_by_symbol[s].cap_bucket for s in symbols_with_fundamentals}
    sector_by_symbol = {r.symbol: r.sector for r in fundamentals if r.sector}
    listing_date_by_symbol = {
        s: entry_by_symbol[s].date_of_listing
        for s in symbols_with_fundamentals
        if entry_by_symbol[s].date_of_listing is not None
    }

    logger.info(
        "Fetching real point-in-time price + shares-outstanding history for %d companies "
        "(one call each, period=max, reused across all %d rebalance dates)...",
        len(symbols_with_fundamentals), len(REBALANCE_DATES),
    )
    price_shares_by_symbol = fetch_all_series(symbols_with_fundamentals, entry_by_symbol)
    logger.info(
        "Price/shares fetch complete: %d of %d companies have usable point-in-time data",
        len(price_shares_by_symbol), len(symbols_with_fundamentals),
    )

    ranked_by_date: dict[date, list[RankedStock]] = {}
    coverage_rows: list[tuple] = []
    flagged_by_date: dict[date, set[str]] = {}
    rank_fn = make_rank_fn(
        price_shares_by_symbol, cap_bucket_by_symbol, sector_by_symbol,
        ranked_by_date, coverage_rows, flagged_by_date,
    )

    steps = run_rebalance_walk(
        REBALANCE_DATES,
        fundamentals,
        rank_fn,
        top_n=TOP_N,
        sector_by_symbol=sector_by_symbol,
        listing_date_by_symbol=listing_date_by_symbol,
    )

    for step in steps:
        basket_by_bucket: dict[str, int] = {}
        for s in step.outcome.holdings:
            b = cap_bucket_by_symbol.get(s)
            basket_by_bucket[b] = basket_by_bucket.get(b, 0) + 1
        flagged_in_basket = sorted(step.outcome.holdings & flagged_by_date[step.rebalance_date])
        logger.info(
            "%s: eligible=%d, basket size=%d %s, buys=%d, sells=%d, anomaly-flagged in basket=%s",
            step.rebalance_date, step.eligible_universe_size, len(step.outcome.holdings),
            basket_by_bucket, len(step.outcome.buys), len(step.outcome.sells),
            flagged_in_basket if flagged_in_basket else "none",
        )

    out_path = Path("data/raw/expanded_backtest_pilot_baskets.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv_module.writer(f)
        writer.writerow([
            "rebalance_date", "symbol", "weight", "cap_bucket", "sector",
            "roce", "ey", "rank_roce", "rank_ey", "combined_rank", "position_in_bucket",
            "anomaly_flagged",
        ])
        for step in steps:
            ranked_lookup = {r.symbol: r for r in ranked_by_date.get(step.rebalance_date, [])}
            flagged_this_date = flagged_by_date[step.rebalance_date]
            for symbol, weight in sorted(step.outcome.weights.items()):
                rs = ranked_lookup.get(symbol)
                writer.writerow([
                    step.rebalance_date.isoformat(), symbol, weight,
                    cap_bucket_by_symbol.get(symbol), sector_by_symbol.get(symbol),
                    rs.roce if rs else None, rs.ey if rs else None,
                    rs.rank_roce if rs else None, rs.rank_ey if rs else None,
                    rs.combined_rank if rs else None, rs.position if rs else None,
                    symbol in flagged_this_date,
                ])
    logger.info("Baskets written to %s", out_path)

    coverage_path = Path("data/raw/expanded_backtest_pilot_coverage.csv")
    with coverage_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv_module.writer(f)
        writer.writerow(["rebalance_date", "symbol", "cap_bucket", "sector", "reason"])
        writer.writerows(coverage_rows)
    logger.info("Coverage detail (every fundamentals-eligible company x rebalance, ranked or why not) written to %s", coverage_path)

    logger.info("Elapsed: %.1f minutes", (time.monotonic() - start) / 60)


if __name__ == "__main__":
    main()
