# 0009: Full-universe fundamentals fetch results, and a summary-reporting bug found along the way

Status: full-universe fetch complete (1,932 symbols, 6,523 seconds).
Results below are corrected after finding a bug in how the run's own
summary script computed its anomaly-check statistics - the bug did not
affect the underlying fetched data, only the numbers first reported for
it.

## The bug: summary stats computed on undeduped multi-year records

The first reported numbers from this run were `flagged_by_anomaly_check:
10558` (of 18,265) and `peer_tier_usage_counts: {"sector": 36530}` - both
far higher than expected from the pilot (83 of 196, 42.3%) or from
anything a "median x2" anomaly threshold should plausibly produce.

**Root cause, checked directly, not assumed.**
`fetch_universe_fundamentals()` (`src/magicformula/data_fetch/
fundamentals.py`) does this correctly internally: it dedupes to one
record per company (`_latest_record_per_symbol`) before running
`check_fundamentals_anomalies`, exactly as decision 0006 intended - a
current-snapshot judgment should compare each company's latest year
against its peers' latest years, not treat a company's ten historical
years as ten separate peers. But `scripts/full_universe_fundamentals_
fetch.py` then **independently re-ran `check_fundamentals_anomalies()` a
second time**, on `result.records` - the flat, multi-year, 18,265-row
list - purely to build its own summary statistics. That second call
checked *every fiscal year of every company* against a peer-group median
built from an uncontrolled mix of years (whichever record happened to be
processed last per symbol while building the peer pool), which explains
both symptoms:

- Older, naturally-drifted fiscal years get compared against a
  present-day-ish peer median and deviate far more often than a
  company's own latest year would - inflating the flagged count.
- `peer_tier_usage_counts` counted one entry per (record x metric) instead
  of one per (company x metric): 18,265 records x 2 metrics = 36,530,
  exactly the reported figure - confirming the mechanism, not just
  correlating with it.

**The underlying fetched data was not corrupted by this** - the
`anomaly_reasons` field on every record in `full_universe_fundamentals.
parquet` came from the correct, internal, latest-year-only check inside
`fetch_universe_fundamentals`, applied uniformly to every fiscal year of
a flagged company (verified directly: grouped all 18,265 records by
symbol and confirmed 0 symbols have inconsistent flagging across their
own fiscal years). The bug was confined to the script's redundant,
separate re-check for its own reporting - the numbers below are
recomputed directly from that already-correct per-record data, not from
a re-run of the fetch.

**Fixed**: `scripts/full_universe_fundamentals_fetch.py` now dedupes
(`_latest_record_per_symbol`) before its own anomaly-summary check, the
same way the library function it's summarizing already does. The
`data/raw/full_universe_fundamentals_summary.json` on disk has been
corrected in place with a note explaining the original wrong numbers,
rather than silently overwritten - so anyone diffing history sees what
changed and why, not just a different number.

**Fixed, not just flagged.** A related issue found while tracing this:
the script set `record.cap_bucket` *after* calling
`fetch_universe_fundamentals()`, so the anomaly check's internal
cap_bucket peer-tier fallback (decision 0004's third tier) never had
cap_bucket available to use internally, for either the buggy or the
corrected call. It didn't affect this run's results - every company
resolved at the "sector" tier, so the fallback was never needed - but it
meant the fallback was unreachable regardless of whether it was ever
needed. `fetch_universe_fundamentals()` now accepts an optional
`cap_bucket_by_symbol` parameter and applies it to every fetched record
*before* the internal anomaly check runs, not after; both
`full_universe_fundamentals_fetch.py` and `pilot_fundamentals_fetch.py`
now pass it in, and their own redundant post-fetch anomaly re-checks
(which had the same undeduped-multi-year-list bug described above,
latent in the pilot script since it predates decision 0006's multi-year
rework) were fixed the same way as this doc's main fix. A new regression
test, `test_fetch_universe_fundamentals_cap_bucket_fallback_fires_end_to_
end`, constructs a company whose sector and broad_sector both fall short
of `min_sector_size` and confirms the cap_bucket fallback genuinely
resolves - through the real `fetch_universe_fundamentals()` integration
path, not just at the `check_fundamentals_anomalies()` unit level already
covered elsewhere - so a future regression that reintroduces "cap_bucket
set too late" would be caught.

## Corrected results

**Failure taxonomy**: 18 of 1,932 symbols (0.9%) failed extraction, all
in the `page_template_mismatch` category, all in the small-cap bucket
(1.03% of small-cap attempts; 0% for large and mid). No `no_page_
anywhere` or `genuine_parse_error` failures at all - consistent with the
pilot's finding that this pipeline's remaining failure mode is narrow and
small-cap-concentrated.

**Sector exclusions**: 127 companies (across all buckets: 5 large, 15
mid, 107 small) were excluded post-fetch because their real screener.in
sector tag turned out to be Financial Services or Utilities despite
passing the pre-fetch name-heuristic filter - the same "real sector
check catches what the name heuristic misses" mechanism validated in
decision 0004, working as designed at full scale.

**Anomaly check**: 914 of 1,787 successfully-fetched, non-excluded
companies (51.1%) flagged by the 2x-peer-median deviation check on
Total Assets/EBIT or Other Liabilities/Capital Employed - up from the
pilot's 42.3% (83 of 196), not a dramatic jump. Broken out by cap
bucket, the rate is close to uniform, not concentrated where most other
data-quality problems in this project have been:

| Bucket | Companies | Flagged | Rate |
|---|---|---|---|
| Large | 66 | 32 | 48.5% |
| Mid | 100 | 52 | 52.0% |
| Small | 1,621 | 830 | 51.2% |

**This is worth flagging on its own terms, separate from the bug above.**
A "2x median, two-sided" anomaly check flagging roughly half of *any*
cap bucket - not just the noisy small-cap tail - suggests the threshold
itself may be too tight for the natural cross-sectional spread of
Total Assets/EBIT and Other Liabilities/Capital Employed across a broad,
heterogeneous universe, rather than genuinely catching rare, structurally
distorted companies the way it caught Ashok Leyland's NBFC subsidiary in
the original design case. Both the pilot (42.3%) and the full universe
(51.1%) run well above what an "anomaly" label should mean if it's
meant to flag the unusual few, not roughly half the population. Not
something to fix now - `quality_overlay.py` is still on hold, and this
check currently runs in `flag_only` mode for the full-universe fetch
(nothing gets dropped) - but the calibration should be revisited before
this flag is used to exclude anything, since as built it isn't
distinguishing "unusual" from "normal variation."

**Peer-tier usage**: all 3,574 tier resolutions (1,787 companies x 2
metrics) resolved at the **sector** tier - zero fallback to broad_sector
or cap_bucket anywhere in the full universe. This directly answers the
standing question from an earlier session ("flag if cap_bucket-tier
usage jumps meaningfully at scale") - it moved in the opposite
direction: the pilot's small amount of fallback usage (382 sector / 6
cap_bucket / 4 broad_sector, out of 392 total) disappeared entirely at
full-universe scale, because every sector segment - even in the
1,621-company small-cap bucket - had enough peers with a computable
ratio to clear `min_sector_size` on its own. The 3-tier fallback built
in decision 0004 remains correct to keep (a future run with a stricter
`min_sector_size` or a thinner universe slice could still need it), but
it wasn't load-bearing here.

## What this changes

Nothing about the backtest.py gating from 0007/0008 - this was a
reporting task, not a new data-source validation. The cap_bucket-timing
gap is fixed (see above). The anomaly-check calibration question is not
- it's investigated on its own terms in
[0010](0010-anomaly-check-calibration.md), since 51.1% flagged deserves
more than a guess before deciding what, if anything, to change.
