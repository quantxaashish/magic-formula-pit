# 0010: Why the anomaly check flags 51.1% of the universe, and what to do about it

Status: root-caused with real distribution data, implemented, and gated
on the two real cases (Ashok Leyland, Coal India) this check exists to
catch - both re-verified flagged under the new calibration before it
shipped, per explicit instruction that a nicer aggregate rate losing
either real case would be worse than the check it replaces.

## The question

Decision 0009 found the anomaly check (decision 0001: flag a company
whose Total Assets/EBIT or Other Liabilities/Capital Employed deviates
more than 2x from its peer-group median) flags 914 of 1,787 companies
(51.1%) at full-universe scale, close to uniform across cap buckets
(48.5% large, 52.0% mid, 51.2% small). That's a much higher rate than
"anomaly" should mean if it's meant to catch the unusual few (the
design case was Ashok Leyland's consolidated NBFC subsidiary - a
genuinely rare structural distortion, not typical variation). Before
touching the threshold, three things needed checking directly.

## 1. Is the distribution skewed enough that "2x median" is a weak bar?

Pulled the actual ratio values for all 1,787 latest-fiscal-year records
(1,492 with a computable Total Assets/EBIT, 1,758 with a computable
Other Liabilities/Capital Employed - the rest excluded for non-positive
EBIT or Capital Employed, the same precondition the real check uses) and
looked at the distribution directly, not assumed.

| Ratio | median | mean | skew | % > 2x median | % < 0.5x median |
|---|---|---|---|---|---|
| Total Assets/EBIT | 12.72 | 28.88 | **12.35** | 20.6% | 11.3% |
| Other Liabilities/Capital Employed | 0.268 | 0.451 | **16.49** | 20.1% | 17.9% |

**Confirmed, decisively.** Both distributions are severely right-skewed
(skew 12-16 - a symmetric distribution has skew 0; anything past ~2-3 is
already "heavily skewed" in most practical contexts). Mean sits more
than double the median in both cases - the signature of a long right
tail dragging the mean up while most of the population clusters near
the median. Under a distribution this skewed, **20% of the population
sits beyond 2x the raw universe median on the high side alone**, before
even counting the low side - "more than 2x the median" is nowhere near
the many-sigma, rare-event bar it would be under something closer to
normal. This isn't a data-quality problem - it's the expected shape for
a ratio whose denominator (EBIT, Capital Employed) can be small-but-
positive for an entirely normal, low-margin or asset-light company,
which mechanically produces a large ratio without anything structurally
wrong. The real check compares against *per-sector* medians, not this
universe-flat one - shown here only to establish the shape - but
within-sector distributions are subject to the same mechanical
skew-from-a-small-denominator effect, just with fewer companies per
group. A quick check confirms per-sector grouping helps some (the real
51.1% company-level rate is below a naive universe-flat estimate of
~57.8%, calculated by combining the two raw-scale rates above under the
near-independence found in part 2) but doesn't fix the underlying shape
problem.

## 2. Are the two metrics correlated - one systemic pattern, or two?

Checked directly on the 1,486 companies with both ratios available:
**Pearson correlation 0.004, Spearman (rank) correlation 0.023, Pearson
on log-transformed values -0.0003** - indistinguishable from zero by any
of the three measures. **Not correlated.** A company with a large
Total Assets/EBIT is no more or less likely to also have a large Other
Liabilities/Capital Employed than chance would predict. These are two
genuinely independent signals, not the same underlying "big balance
sheet relative to earnings" story counted twice.

## 3. How many companies are flagged by exactly one metric vs. both?

| | Count |
|---|---|
| Flagged by Total Assets/EBIT only | 291 |
| Flagged by Other Liabilities/Capital Employed only | 459 |
| Flagged by both | 164 |
| **Total flagged** | **914** |

Individually: Total Assets/EBIT flags 455 companies (25.5%), Other
Liabilities/Capital Employed flags 623 (34.9%). If the two were truly
independent, the expected overlap would be 0.255 x 0.349 x 1,787 ~= 159
companies - the observed 164 matches almost exactly, confirming part 2's
correlation finding through a second, independent lens. **Other
Liabilities/Capital Employed is the larger individual contributor** (623
vs 455), worth scrutinizing somewhat more, but neither metric is doing
"all the work" - both need the same fix, not one specifically.

## Diagnosis

The 51.1% rate is explained by **distribution shape, not a bug, not
double-counting, and not one dominant metric**. Both ratios are
naturally heavy-tailed (mechanically, from dividing by a
sometimes-small-but-positive denominator), a flat "median x2" threshold
is calibrated for something closer to a normal or mildly-skewed
distribution and is far too aggressive for data shaped like this, and
the two metrics contribute close to independently rather than one
driving the other.

**Quantified how much a shape-aware threshold would actually help**,
rather than asserting it would (three thresholds tried on the same raw
data, all as two-sided outlier rules):

| Method | Total Assets/EBIT | Other Liabilities/Capital Employed |
|---|---|---|
| Current: flat 2x median (raw scale) | 26.6% | 37.4% |
| IQR fences (1.5x, raw scale) | 10.4% | 7.7% |
| MAD (3x, raw scale) | 12.3% | 9.3% |
| **IQR fences (1.5x, log scale)** | **5.0%** | **2.7%** |
| **MAD (3x, log scale)** | **4.4%** | **1.6%** |

Moving to a robust dispersion measure alone (IQR or MAD instead of a
flat multiple-of-median) roughly halves the flag rate even on the raw
scale. Doing it on the **log** of the ratio - the mathematically
appropriate transform for a positive, multiplicative quantity like
these, since it turns the heavy right skew into something close to
symmetric - brings both metrics down to single-digit percentages: the
range a genuine rare-anomaly check should produce, and roughly what this
check caught in its original pilot-scale design case.

## Implementation: IQR, not MAD - and why that choice mattered

The recommendation was MAD- or IQR-based on log(ratio). Before picking
one, both were checked directly against the two real ground-truth cases
this whole check exists to catch (Ashok Leyland - Total Assets/EBIT,
n=6 real "Capital Goods" peers; Coal India - Other Liabilities/Capital
Employed, n=8 real large-cap peers, resolved via the cap_bucket fallback)
- not assumed to both work just because the aggregate rate looked good.

| Case | MAD (3x) modified z-score | Flagged at threshold 3.0? | IQR (1.5x) fences | Flagged? |
|---|---|---|---|---|
| Ashok Leyland | z = 3.24 | Yes (margin: thin) | outside [2.76, 41.0] | Yes |
| Coal India | z = 2.21 | **No** | outside [1.24, 2.70] | **Yes** |

**MAD would have silently lost the Coal India case.** At threshold 3.0 -
the value decision 0010's earlier full-universe rate estimate (4.4%/1.6%)
was based on - Coal India's real modified z-score is 2.21, short of the
cutoff. A lower MAD threshold could be tuned to catch it, but that's
exactly the kind of after-the-fact threshold-chasing the flat-multiple
check was replaced for being unprincipled about. **IQR(1.5x) fences catch
both cases cleanly, with no tuning**, because Coal India's real outlier
status shows up more clearly in the tails of the distribution (where IQR
looks) than in a MAD-based center-and-spread summary at this particular
sample size. IQR was implemented as `_log_iqr_bounds` /
`_deviates` in `src/magicformula/data_fetch/fundamentals.py`;
`check_fundamentals_anomalies`'s old `deviation_multiple` parameter is
now `iqr_k` (default 1.5, standard Tukey fences).

**Small peer groups: checked empirically, not assumed stable.** A
peer-group threshold that works at n=6-8 (the two real cases) isn't
automatically safe at n=3, the old `min_sector_size` default. Ran 200
randomized trials of "2 natural-spread peers + one 5x outlier" (n=3) and
"3 natural-spread peers + one 5x outlier" (n=4): **IQR fences caught the
outlier 0% of the time at n=3, 92% at n=4, 100% at n=5+** - a
mathematical property of quartiles computed from very few points, not
specific to this data. `min_sector_size`'s default is now **5**, not 3;
below that, a tier is treated as too thin to trust and the hierarchy
falls back wider (or lands on "none" - ratio computed, judgment
withheld - exactly like it already did for "zero peers", just at a
higher bar). Encoded as two tests
(`test_iqr_check_has_no_power_at_four_peers_but_catches_the_same_case_at_
five`, `test_iqr_check_below_min_sector_size_falls_through_to_none_not_a_
false_clear`) using exact, precomputed figures rather than the random
trials themselves, so the regression is deterministic.

**A real bug caught along the way, not shipped**: the first implementation
computed IQR fences in log space then exponentiated them back to compare
against the raw ratio. In a near-degenerate pool (several peers sharing
almost exactly the same ratio, IQR near 0), `exp(log(x))` doesn't always
round-trip back to exactly `x` - floating-point error was enough to
spuriously flag a peer against its own, functionally-identical value
(caught by a synthetic test with 4 identical peers + 1 real 10x outlier -
the identical peers were flagging each other). Fixed by comparing
`log(value)` against the log-space fences directly, never round-tripping
through `exp()` for the decision itself - only for the human-readable
range in the `reasons` message.

**Verified on the real full-universe data** (not just the synthetic and
two ground-truth tests) before calling this done - recomputed directly
from the already-fetched `full_universe_fundamentals.parquet`, no re-fetch
needed:

| | Old (flat 2x median) | New (log-scale IQR, min_sector_size=5) |
|---|---|---|
| Flagged | 914 of 1,787 (51.1%) | **132 of 1,787 (7.4%)** |
| By Total Assets/EBIT only | 291 | 81 |
| By Other Liabilities/Capital Employed only | 459 | 47 |
| By both | 164 | 4 |
| Large cap flagged | 48.5% | 3.0% |
| Mid cap flagged | 52.0% | 7.0% |
| Small cap flagged | 51.2% | 7.6% |
| Peer-tier usage | 100% sector | **still 100% sector** |

Raising `min_sector_size` from 3 to 5 did not push any company to a
wider fallback tier at full-universe scale - every sector still has at
least 5 members with a computable ratio, the same finding as decision
0009 at the old threshold. The "sector tier can still be thin outside
the large-cap names already checked" concern this was checked against
did not materialize here, though it was a real risk worth checking
directly rather than assuming away.

7.4% is a sane, single-digit anomaly rate, both metrics contribute
(TA/EBIT is now the larger individual contributor, a reversal from
before), and it did not cost either ground-truth case.

**Still not turned on for exclusion.** `anomaly_mode="flag_only"`
remains the setting for both the full-universe fetch and the backtest
pilot - this decision fixes the *calibration* of what gets flagged, not
the standing decision to hold `anomaly_mode="exclude"` until
`quality_overlay.py` (the actual consumer of a trustworthy exclude
signal) resumes.
