# 0005: Corrected pilot results (all fixes active) and the full-universe go/no-go

Status: pilot clean, scaling to the full universe.

## What changed since the first pilot (decision 0003)

The first pilot (decision 0003) ran with none of the following in place.
This pilot has all four active:

1. `exclude_likely_financials_and_utilities()` pre-filter (decision 0003),
   applied before symbol selection.
2. Consolidated -> standalone fallback inside `fetch_universe_fundamentals`
   (decision 0003).
3. Authoritative post-fetch sector exclusion (decision 0004) - catches a
   pre-filter miss (e.g. REC Limited) after exactly one fetch, using
   screener.in's own real Broad Sector tag.
4. **A fix found while setting up this run, not before it**: the fallback
   in #2 built its "standalone" URL as `.../company/<SYMBOL>/standalone/`,
   which doesn't exist on screener.in at all (confirmed live: 404).
   screener.in's standalone view is the bare company URL with no statement
   segment (`.../company/<SYMBOL>/`). This meant the fallback shipped in
   decision 0003 never actually worked - every company that needed it
   failed both statement attempts, the second one 404ing instead of
   reaching real data. A first attempt at this corrected pilot run showed
   every fallback case failing identically before this was caught. Fixed
   with a single `company_url(symbol, statement)` helper both
   `ScreenerClient` and `fetch_fundamentals` now call instead of
   formatting the URL inline.

Also found and fixed during this run: Siemens Ltd changed its fiscal year
end (September -> March), producing an 18-month stub/transition period
column labeled `"Mar 202618m"` (no space before the length suffix) - the
date parser split on whitespace and fed `"202618m"` to `int()`, crashing.
Fixed to take exactly the first 4 digits of the year token. See
`tests/test_fundamentals.py`'s Siemens regression test
(`tests/fixtures/screener_html/SIEMENS.html`, a real saved page).

Sample: 218 companies, contiguous AMFI-rank slices **shifted from the
first pilot's ranges** (large 21-100, mid 111-220, small 261-360 vs. the
first pilot's 1-80/101-190/251-330) so this exercises genuinely different
companies, not cache replay of already-diagnosed cases.

## Results

| Bucket | Attempted | Successful | Sector-excluded | Failed | Failure rate |
|--------|-----------|------------|------------------|--------|---------------|
| Large  | 58        | 53         | 4                | 1      | 1.7%          |
| Mid    | 82        | 72         | 10               | 0      | 0.0%          |
| Small  | 78        | 71         | 7                | 0      | 0.0%          |
| **Total** | **218** | **196**  | **21**           | **1**  | **0.46%**     |

(Failure rate = failed / attempted; sector-excluded is a correct outcome,
not counted as failed - see decision 0004.)

Compare to the first, pre-fix pilot's 22.0% overall failure rate
(52/236) - the two ordering/fallback fixes plus the URL fix account for
essentially all of it.

### Failure taxonomy (as requested: separated, not lumped)

Using `classify_failure_reason()` (new in this pass, in
`data_fetch/fundamentals.py`) against the one real failure:

| Category | Count | Notes |
|---|---|---|
| Sector-excluded (correct, not a failure) | 21 | Tracked separately in `UniverseFetchResult.sector_excluded`, never in `failed_extractions`. |
| `no_page_anywhere` (both statement types 404/network-fail) | 0 | |
| `page_template_mismatch` (both statement types missing the expected rows) | 0 | Financials/utilities that would produce this are now caught by fixes #1/#3 before ever reaching a parse attempt. |
| `genuine_parse_error` (something new) | 1 (SIEMENS) | Root-caused (fiscal-year-change stub period) and fixed - see above. Verified against the real saved page; not re-validated by re-running the full 218-company pilot a third time. |

**Genuine-parse-error is 1 of 218 (0.46%), and it's not a mystery
edge case - it's diagnosed and fixed.** Zero `no_page_anywhere` and zero
`page_template_mismatch` in the corrected run means the two-layer
financials/utilities defense (pre-fetch name heuristic + post-fetch real
sector check) is doing its job: nothing reached a parse attempt that
shouldn't have. This is the green light the go/no-go was waiting on.

### Peer-tier usage (anomaly detector's 3-tier fallback, decision 0001)

196 successful records x 2 ratios = 392 possible tier-resolutions:

- sector: 382 (97.4%)
- cap_bucket: 6 (1.5%)
- broad_sector: 4 (1.0%)
- none: 0

Slightly more fallback usage than the first pilot (which saw 0 at
broad_sector and only 4 at cap_bucket out of 368), consistent with a
different, non-overlapping rank slice surfacing a couple of genuinely thin
sectors - still a small minority overall, in line with the first pilot's
finding that this isn't a widespread problem.

### Anomaly-check flags (for context, not part of this go/no-go)

83 of 196 (42%) flagged by `check_fundamentals_anomalies()` - spot-checked
the list and confirmed none are leftover financials/utilities (all
reasons cite ordinary sectors: Capital Goods, Metals & Mining, FMCG,
Realty, Healthcare, Auto, etc.). This rate is a separate, pre-existing
characteristic of the 2x two-sided threshold (already noted as sensitive
in decision 0001's follow-up), not a new problem surfaced by this pilot -
out of scope for this specific go/no-go, which is about fetch reliability
and exclusion correctness, not anomaly-threshold calibration.

## Decision

Both gating conditions are met: the corrected pilot is clean (0.46%
failure rate, both remaining-unknown categories at zero, the one real
failure diagnosed and fixed), and the sector-based exclusion pass is
tested (decision 0004) and now confirmed live (NIACL - "New India
**Assurance**", not "Insurance" - was missed by the name heuristic and
correctly caught by the real sector-tag check; RECLTD likewise).
**Proceeding to fetch the full ~2000-company universe.**
