"""Full ~2000-company universe fetch, run only after the corrected pilot
(docs/decisions/0005) came back clean and the sector-based exclusion pass
was tested (docs/decisions/0004).

Same pipeline as the pilot (scripts/pilot_fundamentals_fetch.py), applied
to the entire eligible universe instead of a rank slice: every merged
NSE+BSE entity with an AMFI cap bucket and an NSE symbol, after the
name-heuristic pre-filter (ETF/REIT/InvIT, financials/utilities, listing
history). ~1,930 companies at this pipeline's default 2.5s rate limit is
roughly 80 minutes of fetching alone, more with retries/fallbacks for the
companies that need them - expect 1.5-2.5 hours total. Runs in the
background; check data/raw/full_universe_run.log for progress.

Usage: python scripts/full_universe_fundamentals_fetch.py
Writes: data/raw/full_universe_failed_extractions.csv (if any failures)
        data/raw/full_universe_fundamentals_summary.json
        data/raw/full_universe_fundamentals.parquet (successful records)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd

from magicformula.data_fetch.fundamentals import (
    ScreenerClient,
    _latest_record_per_symbol,
    check_fundamentals_anomalies,
    classify_failure_reason,
    fetch_universe_fundamentals,
)
from magicformula.universe import build_universe

# Current AMFI edition as of 2026-09-06 (see universe.py's
# AMFI_CATEGORIZATION_PAGE_URL docstring - there's no stable "latest" URL).
# Kept in sync with scripts/pilot_fundamentals_fetch.py's own copy of this
# constant - not imported from there, since a script executed directly
# (`python scripts/foo.py`) only gets its own directory on sys.path, so
# `from scripts.other_script import ...` breaks in exactly that invocation
# style (this is what originally broke this script's first run).
AMFI_XLSX_URL = "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("data/raw/full_universe_run.log", mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("full_universe")


def pick_all_eligible_symbols(entries) -> dict[str, str]:
    """Returns {symbol: cap_bucket} for every entry eligible to fetch -
    no rank slicing, the whole universe."""
    cap_bucket_by_symbol = {}
    for e in entries:
        if e.excluded or e.cap_bucket is None or e.nse_symbol is None:
            continue
        cap_bucket_by_symbol[e.nse_symbol] = e.cap_bucket
    return cap_bucket_by_symbol


def main() -> None:
    start = time.monotonic()
    entries = build_universe(AMFI_XLSX_URL, as_of=date(2026, 9, 6))
    cap_bucket_by_symbol = pick_all_eligible_symbols(entries)
    all_symbols = sorted(cap_bucket_by_symbol)
    logger.info("Total eligible symbols for full-universe fetch: %d", len(all_symbols))

    client = ScreenerClient(cache_dir=Path("data/raw/screener_html_cache"))

    result = fetch_universe_fundamentals(
        all_symbols,
        client,
        extraction_max_retries=2,
        failed_extractions_path=Path("data/raw/full_universe_failed_extractions.csv"),
        anomaly_mode="flag_only",  # observe, don't drop, so the raw dataset stays complete
        cap_bucket_by_symbol=cap_bucket_by_symbol,  # so the cap_bucket peer-tier fallback
        # (decision 0004's third tier) actually has data to use internally if sector and
        # broad_sector both fall short - see decision 0009 for why setting record.cap_bucket
        # only after this call returned made that fallback silently unreachable.
    )

    # Summary stats must be computed per-company, not per-(company, fiscal
    # year) - result.records is the flat multi-year list (fetch_universe_
    # fundamentals's own internal anomaly check already deduped correctly
    # before deciding what to flag; this script must not re-check the raw
    # multi-year list itself, or a company's older, naturally-drifted years
    # get compared against a same-symbol-overwritten, not-reliably-latest
    # peer median and inflate the flagged rate and peer-tier counts by
    # roughly the average number of fiscal years per company - confirmed
    # via docs/decisions/0009: this produced 10,558/18,265 (57.8%) instead
    # of the real 914/1,787 (51.1%) company-level rate on the first
    # full-universe run.
    anomaly_results = check_fundamentals_anomalies(_latest_record_per_symbol(result.records))
    tier_counts: dict[str, int] = {}
    flagged_count = 0
    for r in anomaly_results:
        for tier in (r.total_assets_to_ebit_peer_tier, r.other_liabilities_to_capital_employed_peer_tier):
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if r.flagged:
            flagged_count += 1

    failure_categories: dict[str, int] = {}
    for f in result.failed_extractions:
        category = classify_failure_reason(f.reason)
        failure_categories[category] = failure_categories.get(category, 0) + 1

    attempted_by_bucket: dict[str, int] = {"large": 0, "mid": 0, "small": 0}
    for bucket in cap_bucket_by_symbol.values():
        attempted_by_bucket[bucket] += 1
    failed_by_bucket: dict[str, int] = {"large": 0, "mid": 0, "small": 0}
    for f in result.failed_extractions:
        bucket = cap_bucket_by_symbol.get(f.symbol)
        if bucket:
            failed_by_bucket[bucket] += 1
    sector_excluded_by_bucket: dict[str, int] = {"large": 0, "mid": 0, "small": 0}
    for s in result.sector_excluded:
        bucket = cap_bucket_by_symbol.get(s.symbol)
        if bucket:
            sector_excluded_by_bucket[bucket] += 1

    elapsed = time.monotonic() - start

    # Save the raw successful records as parquet (SPEC.md section 9's
    # preferred storage format) for later use by ranker.py/formulas.py.
    if result.records:
        df = pd.DataFrame([vars(r) for r in result.records])
        Path("data/raw").mkdir(parents=True, exist_ok=True)
        df.to_parquet("data/raw/full_universe_fundamentals.parquet")

    summary = {
        "total_symbols_attempted": len(all_symbols),
        "successful_extractions": len(result.records),
        "failed_extractions": len(result.failed_extractions),
        "failed_symbols": [f.symbol for f in result.failed_extractions],
        "failure_categories": failure_categories,
        "attempted_by_bucket": attempted_by_bucket,
        "failed_by_bucket": failed_by_bucket,
        "failure_rate_pct_by_bucket": {
            b: round(100 * failed_by_bucket[b] / attempted_by_bucket[b], 2) if attempted_by_bucket[b] else None
            for b in ("large", "mid", "small")
        },
        "sector_excluded_count": len(result.sector_excluded),
        "sector_excluded_by_bucket": sector_excluded_by_bucket,
        # Company-level, not record-level: one check per symbol (latest
        # fiscal year only), not one per (symbol, fiscal year) - see the
        # comment above where anomaly_results is computed.
        "flagged_by_anomaly_check": flagged_count,
        "companies_checked_for_anomalies": len(anomaly_results),
        "peer_tier_usage_counts": tier_counts,
        "elapsed_seconds": round(elapsed, 1),
    }

    with open("data/raw/full_universe_fundamentals_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    logger.info(
        "%d of %d failed extraction (categories: %s)",
        len(result.failed_extractions), len(all_symbols), failure_categories,
    )
    logger.info("Failure rate by bucket: %s", summary["failure_rate_pct_by_bucket"])
    logger.info("%d excluded post-fetch on real sector", len(result.sector_excluded))
    logger.info("%d of %d companies flagged by anomaly check", flagged_count, len(anomaly_results))
    logger.info("Peer-tier usage: %s", tier_counts)
    logger.info("Elapsed: %.1f seconds (%.1f minutes)", elapsed, elapsed / 60)
    logger.info("Summary written to data/raw/full_universe_fundamentals_summary.json")
    logger.info("Records written to data/raw/full_universe_fundamentals.parquet")


if __name__ == "__main__":
    main()
