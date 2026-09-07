# 0014: quality_overlay.py - adapted F-score/Z-score, and what they show on real cases already investigated

Status: built, synthetic-tested (21 tests, hand-computed ground truth),
validated against real Ashok Leyland/VEDL/PAGEIND data already fetched
and investigated earlier in this project - not fresh cases.

## What's built, and why two signals are adapted, not textbook

`magicformula.quality_overlay` adds five independently-toggleable Phase 2
signals (SPEC.md section 8) on top of the plain two-factor (ROCE/EY)
ranking, without changing it: `apply_quality_overlay` with every config
flag at its default returns the input ranking's order and membership
completely unchanged - checked directly by a test that feeds it
deliberately terrible quality inputs and asserts nothing gets excluded,
not just assumed from the code shape.

**F-score**: 7 of the 9 standard Piotroski (2000) tests. Dropped "delta
Current Ratio" and "delta Gross Margin" - both need line items
screener.in's free tier doesn't expose (no current-assets/current-
liabilities split; no COGS/opex breakdown, just a lump "Expenses"
figure). Kept: ROA>0, CFO>0, CFO>NetProfit, ROA improved, leverage
improved, no new shares issued, asset turnover improved.

**Z-score**: the original 1968 Altman formula, 4 of its 5 terms - the
1.2 x (Working Capital/Total Assets) term is dropped entirely, not
zero-filled with a fake proxy, for the same current-assets-split reason.
Retained Earnings is approximated by Reserves (screener.in's own label -
close enough absent large revaluation reserves, not exact). Zone cutoffs
(distress <1.8, grey 1.8-3.0, safe >3.0) are this project's own
approximate thresholds, not Altman's original 1.81/2.99 - those were
calibrated for the full 5-term formula and dropping a term shifts the
real distribution, so this is a directional read, not a precise
reproduction.

**Accrual flag** (Sloan ratio, (NetProfit-CFO)/TotalAssets > 10% default)
and **promoter-pledge flag** (>20% default) use real, unadapted
definitions - no data gaps to work around for either.

**Momentum**: trailing price return via the existing
`nearest_value_lookup` point-in-time price series - no adaptation
needed, this data was already fully available.

## Parser extension this required

Added `sales`, `net_profit`, `cash_from_operations`, `equity_capital`,
`reserves` (per fiscal year - all already on the cached Profit & Loss/
Cash Flow/Balance Sheet tables, no new scraping) and
`promoter_pledge_percentage` (a *current-snapshot* text extraction from
screener.in's "Insights" bullet - "Promoters have pledged X% of their
holding", present only when material; confirmed live: Ashok Leyland
shows this bullet at 40.1%, TCS/Siemens/Tata Power/RECLTD show none at
all). New fields default to None on any unparseable cell rather than
dropping an otherwise-good fiscal-year record.

Copied ASHOKLEY, VEDL, and PAGEIND's real cached HTML into
`tests/fixtures/screener_html/` (previously only in gitignored
`data/raw/`) so this validation - and the parser tests built on it - are
reproducible from a clean clone.

## Real validation: does Ashok Leyland trip the Z-score the way it tripped the anomaly check?

**Yes, genuinely, and it gets worse in exactly the years that matter.**
Ashok Leyland's Z-score, computed across its real 12-year history:

| FY | F-score | Z-score | Zone |
|---|---|---|---|
| 2015 | 2/3 (only 3 evaluable, no prior year) | 5.21 | safe |
| ... | ... | ... | (safe/grey through 2024) |
| 2024 | 3/7 | 2.21 | grey |
| **2025** | 4/7 | **1.66** | **distress** |
| **2026** | **1/7** | **1.41** | **distress** |

Z-score crosses into distress territory in exactly the two most recent
years, and F-score hits its worst score (1/7) in FY2026 - the same year
the original Total Assets/EBIT anomaly check flagged. This is a real,
independent corroboration: Z-score uses none of the same inputs as the
anomaly check's peer-comparison ratio (it's built from reserves, EBIT,
market cap, total liabilities, sales - a completely different
calculation), and it still finds the same year weak. Promoter pledge is
flagged in every single year (40.1%, a current-snapshot figure) - a
separate, standing governance concern layered on top regardless of any
given year's balance-sheet numbers.

## Real validation: do VEDL's and PAGEIND's flagged years look different once quality signals are added?

**VEDL: quality signals do NOT single out FY2026 the way the anomaly
check did - reinforcing that it was a real but narrow, one-off event,
not a broader deterioration.** VEDL's Z-score has been persistently
weak for most of its 12-year history (distress or grey in 9 of 12
years, including FY2016 at 0.62 - far worse than FY2026's 1.25), typical
for a highly-leveraged commodity/mining conglomerate. FY2026 is not a
new low. More tellingly, **VEDL's F-score in FY2026 is a perfect 7/7** -
its best possible score, on the same year the Other Liabilities/Capital
Employed ratio spiked and got it anomaly-flagged. This directly
corroborates decision 0013's finding (VEDL's flag was a genuine but
one-year balance-sheet event, not a structural distortion): every
profitability, cash-flow, leverage-trend, and efficiency-trend signal
this project can independently check says FY2026 was operationally a
strong year for VEDL - consistent with the earlier hypothesis that the
liability spike was a specific capital-structure/classification event
(plausibly demerger-related), not a sign the underlying business
weakened.

**PAGEIND: quality signals overwhelmingly confirm "genuinely excellent
business," not "distress hiding behind a good ratio."** PAGEIND's
Z-score is dramatically, consistently safe across every single year
(21.5 to 61.7 - roughly 7-20x the "safe" threshold of 3.0), and its
F-score is solidly positive most years (5-6/7, one weak year at 2/7 in
FY2023 - not FY2026). FY2026 specifically shows F=5/7 and Z=21.5 -
unremarkable, healthy, nothing like a distress signature. This directly
reinforces decision 0013's conclusion: PAGEIND's persistent Total
Assets/EBIT anomaly flag is a peer-group mismatch (an asset-light
licensing business compared against capital-intensive textile
manufacturers), not evidence of a real problem - every independent
quality measure available says this is a fundamentally sound company.

## A parsing detail this validation surfaced (not a new bug)

The PAGEIND "consolidated" cached page (128KB, smaller than the other
fixtures) has no proper annual Profit & Loss/Balance Sheet table at all -
`parse_company_html` correctly raised rather than guessing. This is
decision 0003's already-established consolidated/standalone fallback
working as intended: Page Industries' real, usable annual data comes
from the *standalone* statement (214KB, full 12-year history), which the
production pipeline's fallback logic already uses - this was only
surfaced because a one-off validation script called the parser directly
on the wrong cached file, bypassing the fallback `fetch_fundamentals`
already applies. Fixed by using the correct standalone fixture, not by
changing any parsing logic.

## What this doesn't decide

Whether to actually turn any of these five signals on for a production
ranking (min F-score threshold, Z-score distress exclusion, momentum
tilt weight) is a separate call from building and validating them - not
made here. The plain two-factor baseline remains the default; this
overlay is available, real, and tested, not yet switched on.
