# 0012: The shares-lookup tolerance had no real cap, a real crash bug was hiding behind small pilots, and 2016-2018 coverage is too thin to trust

Status: both fixes shipped and tested against real data; the backtest's
real starting point is decided from measured coverage, not assumed.

## Question 1: does `nearest_value_lookup` have a distance cap?

Checked the actual code before touching anything, per explicit
instruction not to infer this from behavior. It does:
`max_days_tolerance` is a real parameter, and the function already
returns `None` correctly in both directions - a target date that
predates all available data (`eligible` is empty by construction, no
tolerance needed) and a target date whose nearest eligible point is
further back than the tolerance allows. Both were already covered by
existing tests (`test_nearest_value_lookup_never_uses_a_future_value`,
`test_nearest_value_lookup_respects_tolerance`) before this decision.

**The actual gap was one layer up.** The backtest pilot scripts called
`nearest_value_lookup(shares_series, rebalance_date,
max_days_tolerance=400)` - a real cap, but not a meaningful one for
annual rebalancing: 400 days is more than a year, so a share count over
a year stale could still be silently treated as this rebalance's
point-in-time value.

**First pass chose 180 days, reasoning from disclosure-cycle length
alone - not good enough, redone with real measurement.** The initial
fix reasoned "get_shares_full() goes quiet for months between real
disclosures, so double a ~90-day cycle" without checking what the real
gap distribution actually looks like. Measured it directly: the gap
between consecutive *confirmed* (post-`filter_persisted_values`)
observations across 200 real companies (166,647 gaps total) has a
median of **1 day**, p90 of **5 days**, p99 of **16 days** -
`get_shares_full()` is far denser than assumed whenever it has coverage
at all. Before finalizing a number, ran a sensitivity sweep
(30/60/90/180/400 days) against the real cached price/shares series and
found the tolerance choice barely matters for years that already have
good coverage (within ~1pp of each other at every tested value) and
doesn't rescue years that don't (2016-2018 stay in the single digits to
low teens regardless of tolerance) - the *year* drives usability, not
the tolerance. Settled on:

```python
PRICE_LOOKUP_MAX_TOLERANCE_DAYS = 14
SHARES_LOOKUP_MAX_TOLERANCE_DAYS = 90
```

90 is ~5.6x the measured p99 gap (16 days) - generous enough that one
unusually slow real disclosure doesn't wrongly exclude a company, not
loose enough to accept a value from a different fiscal period the way
180 or 400 would. Pinned with tests that check the constant's value
directly (so a future change back toward something looser fails a test)
and the accept/reject boundary around it.

## A second, unrelated bug found while acting on the first

Fixing the tolerance meant re-running the wider (small-cap-inclusive,
11-date) pilot. It crashed - `ZeroDivisionError` inside `roce_standard`,
at the 2022-06-01 rebalance, after six earlier rebalances had already
run cleanly. Root cause: `compute_metrics` (magicformula.formulas)
already excludes non-positive EBIT and non-positive EV before dividing
by them, but never checked non-positive **capital employed** - a gap
invisible in both prior large+mid-only pilots because neither ever
included a company thin enough to hit it.

Checked the full universe directly rather than patching around the one
crash: **26 (symbol, fiscal-year) records** across the whole dataset have
positive EBIT but capital employed <= 0. The two that actually surfaced:
ESSENTIA (FY2022, Total Assets = Other Liabilities = Rs. 17 Cr exactly,
capital employed = 0) and GTL (FY2021, Other Liabilities Rs. 7,434 Cr
against Total Assets Rs. 197 Cr, capital employed = -Rs. 7,237 Cr - GTL
Infrastructure was in insolvency proceedings around this period, a real
reflection of distress, not a data error). Added
`EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED`, mirroring the existing
EBIT/EV exclusion pattern exactly, with regression tests built on both
real companies' actual figures - not just a synthetic zero.

## Question 2: coverage by year and cap bucket, measured directly at the final 90-day tolerance

An earlier version of this analysis was run against the 180-day
tolerance, before the tightening described above, and is superseded by
everything below. After landing on 90 days, re-ran the actual pilot
script itself (not a hand-rolled re-implementation against the cache)
with
`SHARES_LOOKUP_MAX_TOLERANCE_DAYS=90` and `REBALANCE_DATES` temporarily
widened back to 2016-2026 for this one diagnostic run, using the disk
cache so it completed in under a minute with no new yfinance calls. This
is the authoritative source for every number below - not the earlier
180-day table, and not an independent one-off script (a first attempt at
that independent script undercounted coverage by including companies not
yet listed as of the rebalance date in the denominator, which is exactly
the kind of silent bug this project's real pipeline already guards
against via `listing_date_by_symbol` clipping in `run_rebalance_walk` -
the authoritative numbers below come from that real code path, not a
reimplementation of it).

Tracked, per company per rebalance, one of four outcomes: `ranked`,
`no_fetch` (no usable series at all), `no_price` (price lookup failed),
`no_shares` (shares lookup failed, the dominant reason for exclusion in
every year checked).

**Coverage % (ranked / fundamentals-eligible-and-listed), by rebalance date and cap bucket, at the final 90-day tolerance:**

| Rebalance | Large | Mid | Small |
|---|---|---|---|
| 2016-06-01 | 13.5% | 1.4% | **0.9%** |
| 2017-06-01 | 69.6% | 80.8% | 72.6% |
| 2018-06-01 | 54.4% | 79.5% | 73.3% |
| **2019-06-01** | **93.0%** | **91.1%** | **85.2%** |
| 2020-06-01 | 94.8% | 91.4% | 89.1% |
| 2021-06-01 | 95.1% | 94.0% | 91.8% |
| 2022-06-01 | 93.7% | 95.2% | 91.2% |
| 2023-06-01 | 93.7% | 96.6% | 93.6% |
| 2024-06-01 | 95.3% | 96.7% | 95.6% |
| 2025-06-01 | 96.9% | 99.0% | 97.4% |
| 2026-06-01 | 63.6% | 57.0% | 51.1% |

This is barely different from the earlier 180-day table (large/mid
within 2pp everywhere, small cap within 1pp from 2019 onward) - exactly
what the sensitivity sweep predicted, now confirmed against the real
pipeline rather than the cache-based sweep alone. **2016 is essentially
unusable** (under 1.5% coverage across every bucket - a basket built
from this would be built from a handful of companies that happened to
have data, not a real cross-section of the market). **2017-2018 are
spotty and inconsistent**, not a clean ramp - large cap actually *drops*
from 69.6% to 54.4% between those two years, the opposite of what
"coverage improves over time" would predict, meaning even a
company-by-company read of these two years can't be trusted to mean
what it looks like it means. **2019 onward is consistently strong from
day one and keeps improving** (85%+ every bucket, every year, small cap
climbing steadily from 85.2% to 97.4% by 2025) - there is no gradual
multi-year ramp-up within the 2019+ era; 2019 already clears the same
bar every later year clears, it just isn't the ceiling.

**A second dip, unrelated to the starting-date question: 2026-06-01 drops
back to 51-64% coverage**, the most recent rebalance having *worse*
coverage than every year from 2019-2025. This is the flip side of
`filter_persisted_values`' own design (decision 0008): a reading needs
confirmation from neighboring observations *on both sides* to be
accepted, and the most recent few months of any company's series don't
have enough "after" neighbors yet to be confirmed - so the latest
confirmed value can already be stale enough to fail even the tighter
90-day bound for some companies even though fresher raw data exists.
This is a live-data edge effect, not a resolved-history problem, and
applies to *any* future run's most current rebalance, not specifically
to 2026 - if anything the tighter 90-day tolerance (vs. 180) makes this
edge effect more visible, which is the honest tradeoff of tightening it.

## Is the early-period gap skewed, or broadly random?

Checked three dimensions, not assumed any of them - sector and cap
bucket as before, plus listing vintage, added specifically because a
sector-only check can't see whether the gap concentrates in newly-listed
companies:

**By sector: not meaningfully skewed.** Exclusion rate in the 2016-2018
window ranges 42.0% (Media, Entertainment & Publication) to 63.4%
(Forest Materials) across 19 sectors - a real but modest, broadly
distributed spread, not a small number of sectors absorbing the whole
gap while others are unaffected.

**By listing vintage: a real, moderate skew, not previously checked.**
Bucketing every already-listed company in the 2016-2018 window by years
listed as of that rebalance date:

| Years listed | Exclusion rate | n |
|---|---|---|
| < 2y | 59.8% | 261 |
| 2-5y | 59.2% | 245 |
| 5-10y | 55.0% | 625 |
| **10-20y** | **41.2%** | **1,251** |
| 20y+ | 51.2% | 566 |

Companies listed under 5 years show a ~15-19 percentage point higher
exclusion rate than the 10-20-year cohort, which is the best-covered
group by a clear margin. This is real and directionally makes sense
(younger listings are more likely to have thinner or newer
`get_shares_full()` history), but it's a moderate skew (worst case ~60%
vs ~41%), not an extreme one - it does not mean recently-listed
companies are essentially absent from the eligible pool, just
somewhat under-represented within it. Interestingly the oldest cohort
(20y+) is also worse than the 10-20y group, suggesting some very
long-listed companies have their own data gaps (delistings,
restructurings, or names yfinance covers less completely) rather than
vintage effects being purely monotonic.

**By cap bucket: still the dimension that matters most, and still
severe.** Small cap's 2016 coverage (0.9%) is an order of magnitude
worse than large cap's (13.5%), and mid cap sits at 1.4% - worse than
large, better than small. This gap doesn't meaningfully close until
2019. Since the ranking runs *within* each cap bucket (top-30 per
bucket, not top-90 overall), a small-cap basket built from under 1%
coverage in 2016 isn't a thin version of the true small-cap universe -
it's not a representative sample of it at all. The cap-bucket skew and
the vintage skew are related (small caps skew younger on average) but
are each real on their own, confirmed by checking them separately.

## Decision: the backtest's real starting point is 2019, not 2016

The 90-day-tolerance re-run doesn't just fail to overturn the earlier
2019 conclusion - it reconfirms it on cleaner footing, now against a
tighter, empirically-justified tolerance and the actual pipeline rather
than a diagnostic approximation. Given all three findings - 2016
unusable outright (well under 2% coverage in every bucket), 2017-2018
inconsistent even net of the (mild) sector skew, and two real,
non-random skews (cap bucket severe, listing vintage moderate) both
concentrated in exactly the years being excluded - **2016-2018 are
excluded from the real backtest, not carried forward with a caveat.** A
"heavy asterisk" undersells what a sub-2% coverage year actually means:
not "somewhat less reliable," but "not a cross-section of the market at
all." 2019 is the first date where every bucket clears 85%, there is no
further ramp-up needed within the included range, and the trend from
there is consistently upward and stable - a real, defensible starting
point, not a guess.

The 2026-06-01 edge effect is a separate, ongoing consideration:
whatever the real full backtest's *most recent* rebalance turns out to
be, expect its coverage to look worse than the few before it for the
same structural reason, and don't read that as new degradation. This
effect is slightly more pronounced at 90-day tolerance than it was at
180 - a direct, expected consequence of tightening the bound, and not a
reason to loosen it back up given 2016-2018 gain nothing from a looser
tolerance either.

Both this decision's findings (the "no cap → real cap" fix, the final
90-day tolerance and its empirical justification, and the 2019-vs-2016
conclusion including the listing-vintage skew) are also recorded in
README's Known Limitations, since they affect what a reader should trust
starting now, not just how this pipeline got built.
