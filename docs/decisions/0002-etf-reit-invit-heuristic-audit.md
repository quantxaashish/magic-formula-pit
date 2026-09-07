# 0002: ETF/REIT/InvIT name-heuristic audit

Status: audited against the live universe, one real bug fixed, heuristic
kept as a documented non-authoritative pre-filter.

## What was asked

Before trusting `exclude_non_standard_instruments()` (a name-pattern
heuristic in `magicformula.universe`), pull 15-20 flagged-excluded names
and 15-20 flagged-included names from the real universe and manually check
both directions: false positives (real operating companies excluded
because their name happens to match) and false negatives (an actual
REIT/InvIT slipping through unflagged).

## Method

Ran the heuristic against the full, live merged NSE+BSE universe (5,127
entities after ISIN-merge, fetched 2026-09-06/07). Two checks:

1. **False positives**: inspected every one of the 16 entries the
   heuristic actually flagged (small enough to check all of them, not
   just a sample).
2. **False negatives**: checked whether 13 real, regulator-registered
   Indian REITs/InvITs (Embassy Office Parks REIT, Mindspace Business
   Parks REIT, Brookfield India Real Estate Trust, Nexus Select Trust,
   IRB InvIT Fund, India Grid Trust/IndiGrid, PowerGrid Infrastructure
   Investment Trust, IndInfravit Trust, Bharat Highways InvIT, and others)
   appear anywhere in the merged universe at all, flagged or not.

## Findings

### False negatives: zero, and not for the reason expected

None of the 13 known real REITs/InvITs appear in the merged universe *at
all* - not unflagged, not flagged, simply absent. NSE's `EQUITY_L.csv` and
BSE's `segment=Equity` API both structurally exclude REIT/InvIT units:
these instruments trade under a different instrument type/segment on both
exchanges, not regular equity. This data source excludes them before any
in-code heuristic runs. (One coincidental near-match: "Embassy
Developments Limited" is a real, unrelated real-estate *development*
company, correctly not flagged - it isn't the Embassy Office Parks REIT.)

Practical conclusion: for the specific job of keeping REITs/InvITs out of
this universe, the heuristic is currently unnecessary - the data source
already does it. It's kept as a defensive backstop in case that changes
(e.g. a future data source that doesn't segment them out), not because it
found anything real.

### False positives: real bug found and fixed

Of the 16 entries flagged, effectively zero were REITs/InvITs. Breakdown:

- **7 were pure noise from a since-removed `"-RE"` marker**: it matched
  any name containing the substring "-RE" anywhere, which turned out to
  include mutual fund unit names ending in `-Regular` or
  `-Reinvestment` (e.g. "Infinity Hybrid Long-Short Fund-Regular-Growth")
  and unrelated companies whose registered name happens to end in "-RE"
  (Ducon Infratech Ltd-RE, Jaykay Enterprises Limited-RE, Kshitij Polyline
  Ltd-RE, Viceroy Hotels Ltd-RE - a hotel company, a plastics company, and
  others, none REITs). Zero real REIT/InvIT catches came from this marker
  in the entire universe. **Removed entirely.**
- **1 was a substring false positive on "TRUST"**: "Trustwave Securities
  Ltd" matched because "TRUST" is a substring of "TRUSTWAVE", not because
  it's a trust. **Fixed by switching from substring containment to
  whole-word regex matching** (`\bTRUST\b` etc.).
- **~8 were genuine whole-word "Trust" matches that aren't literally
  REITs**: Capital Trust Limited, Industrial Investment Trust Limited,
  Master Trust Limited, The Investment Trust Of India Limited, Kartik
  Investments Trust Ltd, Kinetic Trust Ltd, Trustedge Capital Ltd, Rajkot
  Investment Trust Ltd. These are investment/financial holding companies -
  a defensible exclusion in spirit (not the kind of operating business
  Magic Formula should rank), but mislabeled if the exclusion reason
  claims a "REIT/InvIT" catch. **Exclusion reason wording corrected** to
  say "likely an investment/holding company rather than a literal
  REIT/InvIT."

## Fix applied

- Replaced `NON_STANDARD_NAME_MARKERS` (substring list including the
  broken `"-RE"`) with `NON_STANDARD_NAME_PATTERN`, a single whole-word
  regex over `REIT|INVIT|INVITS|TRUST`.
- Updated `exclude_non_standard_instruments()`'s exclusion reason to
  accurately describe what's actually being caught.
- Added regression tests
  (`test_exclude_non_standard_instruments_does_not_false_positive_on_substring_matches`,
  `test_exclude_non_standard_instruments_still_catches_whole_word_trust_reit_invit`)
  covering both the real false positives found and confirming whole-word
  matches still work.

## What this does not prove

This audit checked the heuristic's behavior against ~5,100 real names and
13 known real REITs/InvITs - not an exhaustive proof it will never
misclassify a future or obscure name. Treat it as a reasonable default
that's been checked once, not a guarantee, per the caveat already in
`magicformula.universe`'s module docstring.
