"""CORRECTED pilot run (v2) of the real fetch pipeline against ~230-250
companies before scaling to the full ~2000-company universe (per user
instruction: find edge cases at 300 companies, not 2,000).

The first pilot (see docs/decisions/0003-consolidated-standalone-fallback.md)
ran with none of the fixes below in place and its numbers are explicitly
labeled PRE-FIX there - do not treat them as validated. This run has all
three fixes actually wired in and active:

  1. exclude_likely_financials_and_utilities() applied *before* symbol
     selection (decision 0003) - a name-heuristic pre-filter for
     financials/utilities, run against universe.py's merged entities
     before any fetch happens.
  2. The consolidated->standalone fallback inside fetch_universe_fundamentals
     (decision 0003) - for companies with no usable consolidated
     financials on screener.in.
  3. Authoritative post-fetch sector exclusion (decision 0004) - a company
     whose real Broad Sector tag is Financial Services/Utilities is
     excluded after exactly one fetch even if fix #1's name heuristic
     missed it (REC Limited is the known case).

Builds the real NSE+BSE+AMFI universe (magicformula.universe), takes a
stratified, contiguous slice by AMFI rank across all three cap buckets -
DIFFERENT rank ranges from the first pilot, not a re-run of the same
companies, so this is a genuinely fresh sample, not cache replay of
already-diagnosed cases.

Usage: python scripts/pilot_fundamentals_fetch.py
Writes: data/raw/pilot_failed_extractions_v2.csv (if any failures)
        data/raw/pilot_fundamentals_summary_v2.json
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from magicformula.data_fetch.fundamentals import ScreenerClient, fetch_universe_fundamentals
from magicformula.universe import build_universe as _build_universe
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("data/raw/pilot_run_v2.log", mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("pilot")

# Current AMFI edition as of 2026-09-06 (see universe.py's
# AMFI_CATEGORIZATION_PAGE_URL docstring - there's no stable "latest" URL).
AMFI_XLSX_URL = "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx"

# Shifted from the first pilot's ranges (1-80 / 101-190 / 251-330) so this
# run exercises genuinely different companies, not cache replay of names
# already individually diagnosed.
LARGE_SLICE = (21, 100)   # rank 21-100 of 1-100
MID_SLICE = (111, 220)    # rank 111-220 of 101-250
SMALL_SLICE = (261, 360)  # rank 261-360


def build_universe():
    """Thin wrapper so this script's own logger sees the progress lines -
    the actual pipeline lives in magicformula.universe.build_universe()
    (shared with scripts/full_universe_fundamentals_fetch.py to avoid
    cross-script imports, which break under `python scripts/foo.py`
    direct execution)."""
    return _build_universe(AMFI_XLSX_URL, as_of=date(2026, 9, 6))


def pick_pilot_symbols(entries) -> dict[str, list[str]]:
    by_bucket: dict[str, list] = {"large": [], "mid": [], "small": []}
    for e in entries:
        if e.excluded or e.cap_bucket is None or e.nse_symbol is None:
            continue
        by_bucket[e.cap_bucket].append(e)

    for bucket in by_bucket:
        by_bucket[bucket].sort(key=lambda e: e.cap_rank)

    def slice_by_rank(bucket_entries, lo, hi):
        return [e.nse_symbol for e in bucket_entries if lo <= e.cap_rank <= hi]

    picked = {
        "large": slice_by_rank(by_bucket["large"], *LARGE_SLICE),
        "mid": slice_by_rank(by_bucket["mid"], *MID_SLICE),
        "small": slice_by_rank(by_bucket["small"], *SMALL_SLICE),
    }
    for bucket, symbols in picked.items():
        logger.info("Pilot slice %s: %d symbols (rank range checked)", bucket, len(symbols))
    return picked


def main() -> None:
    start = time.monotonic()
    entries = build_universe()
    by_isin = {e.isin: e for e in entries}
    picked = pick_pilot_symbols(entries)

    all_symbols = picked["large"] + picked["mid"] + picked["small"]
    logger.info("Total pilot symbols: %d", len(all_symbols))

    cap_bucket_by_symbol = {}
    for bucket, symbols in picked.items():
        for s in symbols:
            cap_bucket_by_symbol[s] = bucket

    client = ScreenerClient(cache_dir=Path("data/raw/screener_html_cache"))

    result = fetch_universe_fundamentals(
        all_symbols,
        client,
        extraction_max_retries=2,
        failed_extractions_path=Path("data/raw/pilot_failed_extractions_v2.csv"),
        anomaly_mode="flag_only",  # pilot: observe, don't drop, so we can inspect everything
        cap_bucket_by_symbol=cap_bucket_by_symbol,  # so the cap_bucket peer-tier fallback
        # (decision 0004's third tier) actually has data to use internally if sector and
        # broad_sector both fall short - see decision 0009.
    )

    # Summary stats must be computed per-company, not per-(company, fiscal
    # year): result.records is the flat multi-year list since decision
    # 0006's rework. Re-checking that raw list directly (instead of
    # deduping to one record per company first) inflates the flagged rate
    # and peer-tier counts by roughly the average number of fiscal years
    # per company - see decision 0009 for how this was caught on the
    # full-universe run.
    from magicformula.data_fetch.fundamentals import (
        _latest_record_per_symbol,
        check_fundamentals_anomalies,
    )

    anomaly_results = check_fundamentals_anomalies(_latest_record_per_symbol(result.records))
    tier_counts: dict[str, int] = {}
    flagged = []
    for r in anomaly_results:
        for tier in (r.total_assets_to_ebit_peer_tier, r.other_liabilities_to_capital_employed_peer_tier):
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if r.flagged:
            flagged.append({"symbol": r.symbol, "reasons": r.reasons})

    elapsed = time.monotonic() - start

    attempted_by_bucket: dict[str, int] = {b: len(s) for b, s in picked.items()}
    failed_by_bucket: dict[str, int] = {"large": 0, "mid": 0, "small": 0}
    for f in result.failed_extractions:
        bucket = cap_bucket_by_symbol.get(f.symbol)
        if bucket:
            failed_by_bucket[bucket] += 1
    failure_rate_by_bucket = {
        b: round(100 * failed_by_bucket[b] / attempted_by_bucket[b], 1) if attempted_by_bucket[b] else None
        for b in ("large", "mid", "small")
    }

    summary = {
        "total_symbols_attempted": len(all_symbols),
        "successful_extractions": len(result.records),
        "failed_extractions": len(result.failed_extractions),
        "failed_symbols": [f.symbol for f in result.failed_extractions],
        "attempted_by_bucket": attempted_by_bucket,
        "failed_by_bucket": failed_by_bucket,
        "failure_rate_pct_by_bucket": failure_rate_by_bucket,
        "sector_excluded_count": len(result.sector_excluded),
        "sector_excluded_symbols": [
            {"symbol": s.symbol, "sector": s.sector, "broad_sector": s.broad_sector}
            for s in result.sector_excluded
        ],
        "flagged_by_anomaly_check": len(flagged),
        "flagged_details": flagged,
        "peer_tier_usage_counts": tier_counts,
        "elapsed_seconds": round(elapsed, 1),
    }

    Path("data/raw").mkdir(parents=True, exist_ok=True)
    with open("data/raw/pilot_fundamentals_summary_v2.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    logger.info(
        "%d of %d companies failed extraction", len(result.failed_extractions), len(all_symbols)
    )
    logger.info("Failure rate by bucket: %s", failure_rate_by_bucket)
    logger.info(
        "%d companies excluded post-fetch on real sector (heuristic missed them)",
        len(result.sector_excluded),
    )
    logger.info("%d of %d companies flagged by anomaly check", len(flagged), len(result.records))
    logger.info("Peer-tier usage: %s", tier_counts)
    logger.info("Elapsed: %.1f seconds", elapsed)
    logger.info("Summary written to data/raw/pilot_fundamentals_summary_v2.json")


if __name__ == "__main__":
    main()
