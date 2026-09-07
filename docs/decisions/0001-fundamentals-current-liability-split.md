# 0001: screener.in cannot supply a true current/non-current split for Capital Employed

Status: resolved 2026-09-06 - proceed with screener.in's model, with two named
exceptions to handle explicitly. See "Resolution" at the bottom; the analysis
above is kept as the record of how we got there.

## Problem

SPEC.md section 3's standard-mode formula is:

    Capital Employed = Total Assets - Current Liabilities

screener.in's free website (and, as far as could be determined without an
account, its Excel export) does not expose Current Liabilities as a line
item. Its balance sheet schema is a fixed ~8-bucket model used identically
across every company observed (TCS, Infosys, ITC, Asian Paints, Nestle
India - IT services, tobacco/FMCG conglomerate, paints, packaged foods):

    Equity Capital, Reserves, Borrowings, Other Liabilities,
    Fixed Assets, CWIP, Investments, Other Assets

"Other Liabilities" = Total Liabilities - Equity - Borrowings, i.e.
*every* non-borrowing liability, current and non-current, lumped together
(trade payables and provisions alongside lease liabilities, deferred tax,
and other non-current items). There is no way to split it from this
source.

## What we checked

1. Reverse-engineered whether "Other Liabilities as Current Liabilities
   proxy" reproduces screener's own published ROCE for 5 real large-caps
   (tests/fixtures/real_companies.py). It doesn't - computed ROCE runs
   3.3-6.6 percentage points below published, consistently.

2. Read screener.in's own methodology
   (https://www.screener.in/guides/optimizations/, verified against raw
   page text on 2026-09-06): their ROCE (a) averages opening and closing
   capital employed, (b) excludes CWIP, Investments, and other non-current
   assets from capital employed entirely, (c) uses TTM figures where
   available, (d) excludes extraordinary items. None of this matches
   SPEC.md's standard-mode definition, and averaging + excluding
   non-operating assets both push their number up relative to ours -
   consistent with the direction of the observed gap.

3. Checked whether the gap size tracks (Investments+CWIP)/Capital Employed
   per company, as a proxy for "how much does excluding non-operating
   assets matter here" - it doesn't correlate cleanly (TCS: 30.9% ratio /
   6.6pp gap; ITC: 53.0% ratio / 4.7pp gap), confirming several effects are
   stacked, not one dominant cause.

## Why this blocks fundamentals.py regardless of matching screener's number

Even setting aside screener's own displayed ROCE entirely: "Other
Liabilities" is not a stable, cross-company-comparable definition of
current liabilities. Its current/non-current composition varies by
company (lease-heavy vs. payables-heavy balance sheets, for example), so
using it as a proxy introduces a company-specific, non-cancelling bias
into Capital Employed. That corrupts cross-sectional rank order once this
runs across the full universe - which is the actual purpose of this tool -
independent of whether we ever try to reproduce screener's own number.

## Does the paid export solve this?

Likely no, though not confirmed first-hand (the export is login-gated;
could not verify the exact exported columns from inside this session, and
third-party walkthroughs of it are blocked by Cloudflare bot-checks).
Circumstantial evidence against it: screener's own methodology docs
reference only the same ~8-bucket schema across every ratio they explain,
and that identical schema held for all 5 companies checked despite very
different balance sheet complexity - consistent with a fixed backend data
model rather than a display-only simplification. This reads as a
data-modeling choice, not a paywall gate.

## Recommendation (not yet implemented)

Do not source the Current Liabilities line for Capital Employed from
screener.in. Options, in preference order:

1. Parse the actual Schedule III balance sheet from BSE/NSE XBRL filings,
   which tag current vs. non-current separately under Ind-AS, for this one
   line item (and current assets, for strict-mode NWC). Keep screener.in
   for everything else (EBIT components, ratios for spot-checking) where
   the bucket model is adequate.
2. A paid data vendor with normalized current/non-current line items
   (Capitaline, Ace Equity, Bloomberg) - reintroduces cost and a new
   compliance surface, so only worth it if XBRL parsing proves too brittle
   across filing formats/years.

XBRL parsing across ~2000 companies and multiple years is real, nontrivial
work (inconsistent tagging across filers and years is a known problem with
Indian XBRL). This should be scoped as its own task before fundamentals.py
is built, not folded in as a side effect of another module.

## What we deliberately did NOT do

Did not widen formulas.py's test tolerance to paper over this. The 8pp
tolerance in tests/fixtures/real_companies.py stays as documented there;
this file is the record of *why* the gap exists, not a justification for
ignoring it in fundamentals.py.

## Resolution (2026-09-06)

The question above - "does screener's methodology match ours" - turned out
to be the wrong question to gate a build decision on. What actually matters
for a rank-based strategy is whether the Other Liabilities proxy *reorders*
companies relative to each other, not whether it matches screener's
absolute ROCE. A uniform level shift is harmless to Magic Formula ranking;
reordering, especially near the ranks that decide basket membership, is
not.

Ran scripts/roce_bucketing_correlation_check.py: computed our standard-mode
ROCE (via the actual compute_metrics(), Other Liabilities as the Current
Liabilities proxy) against screener.in's own published ROCE, across 64 real
companies (one more, VIP Industries, excluded by our own negative-EBIT rule)
spanning large/mid/small cap and a range of non-financial, non-utility
sectors. Result:

- Full sample (n=64): Spearman rho = 0.9500
- Top half by published ROCE (n=32, the zone closest to where basket
  cutoffs actually bite): Spearman rho = 0.9109

Both clear the 0.9 bar. **Decision: build fundamentals.py against
screener.in's model as the default data source. XBRL parsing is not
justified as a blocker.**

This is not a uniformly clean result, though, and two specific patterns are
worth carrying forward rather than filing away:

1. **Companies with a captive NBFC/financing subsidiary consolidated in**
   (Ashok Leyland, via Hinduja Leyland Finance) show a much larger gap than
   everything else in the sample - our ROCE came out at 1.99% vs.
   screener's published 13.6%, an order of magnitude, not a level shift.
   Total Assets / Operating Profit for Ashok Leyland is ~37x, vs. ~7x for a
   comparable auto OEM (Maruti) - consolidating a large loan book onto an
   industrial company's balance sheet wrecks any EBIT-based capital-return
   ratio computed off Total Assets. SPEC.md section 5 already excludes
   pure financial-services companies; this is the reminder that a handful
   of *industrial* companies need the same scrutiny because of a
   consolidated subsidiary, not their own sector classification. Worth a
   targeted check in fundamentals.py (e.g., flag or use standalone
   financials for names known to carry a captive financing arm) rather
   than assuming sector exclusion alone catches this.

2. **Coal India** (a PSU miner) moved from published rank 7 to our rank 18
   - an 11-rank displacement inside the top 15, i.e. exactly the range
   that decides basket membership. This one large, heavy-provisioning
   company (mine reclamation, employee benefit obligations sitting in the
   Other Liabilities bucket) is the clearest real instance of the original
   concern: the bucket's current/non-current composition genuinely does
   vary enough, for specific balance-sheet archetypes, to matter near the
   cutoff - it's just concentrated in a minority of names rather than
   uniform across the universe.

Net: proceed with screener.in, but when fundamentals.py is built, add a
specific check (or at minimum a logged flag) for (a) industrial companies
with a consolidated financial-services subsidiary and (b) unusually large
non-current-liability buckets (heavy provisioning, PSU-style), rather than
treating the high aggregate correlation as clearance for every company.

## Follow-up (2026-09-06): the anomaly checks generalized cleanly, but
## surfaced a new open question about sector definition

Built the two checks above into data_fetch/fundamentals.py as a general,
sector-median-based detector (not a hardcoded Ashok Leyland/Coal India
list) - see check_fundamentals_anomalies() and tests/test_fundamentals.py.

Validating it against real screener.in sector tags for all 64 sample
companies surfaced a concrete instance of exactly this kind of sensitivity:
Coal India's own screener.in "Sector" tag is "Oil, Gas & Consumable Fuels",
alongside only Reliance Industries and ONGC in this sample (n=3, the
minimum). Reliance is a diversified conglomerate (refining + telecom +
retail all consolidated) with its own elevated Other Liabilities/Capital
Employed ratio (0.67), which pulls that sector's median up enough that Coal
India's ratio (1.13) no longer clears 2x it (threshold 1.33) - the check
goes quiet on exactly the case it was built to catch, purely because of who
else is in the peer group.

Grouping Coal India with "Metals & Mining" (JSW Steel, Tata Steel,
Hindalco - screener's own real tag for those three) instead - a deliberate,
economically-motivated override, not Coal India's literal tag - restores
the flag (median 0.49, threshold 0.99, Coal India's 1.13 clears it). A
mining company's balance-sheet structure is a closer match to steel/
aluminium miners than to an oil-refining-telecom-retail conglomerate, even
though screener classifies coal as a "consumable fuel" alongside oil & gas.

This is a real, open design question for fundamentals.py's full build, not
resolved here:
- Fall back to "Broad Industry" or "Broad Sector" when the literal
  "Sector" tag has too few peers? (Broad Sector for Coal India is
  "Energy" - would need checking whether that's any less skewed.)
- Maintain a small manual override list for known
  commodity-adjacent/PSU names where screener's tag doesn't match the
  economically relevant peer set?
- Raise min_sector_size well above 3 (running the same two checks against
  the full 64-company sample at real "Sector" granularity flagged 11 of 63
  companies - some clearly real, like Ashok Leyland; others plausibly
  false positives, like Nestle's and Marico's unusually high Other
  Liabilities/Capital Employed, which likely just reflects a genuinely
  negative-working-capital FMCG business model rather than a data
  problem)? A larger minimum would reduce small-sample skew but also
  reduce how many sectors are large enough to check at all until the full
  universe is pulled.

None of this is a reason to abandon the check - the default "exclude, log
the reason" behavior means a false positive costs a smaller basket, not a
wrong ranking, and that's a trade worth making. But the sector-grouping
question should be decided deliberately when fundamentals.py pulls the
real universe (where every sector will have far more members and this
sensitivity should shrink), not left as an implicit default.

## Resolution of the sector-grouping question (2026-09-06)

Built a three-tier peer-group fallback into check_fundamentals_anomalies()
instead of picking a side per-company: try screener.in's narrow "Sector"
tag first (needs >= min_sector_size peers with a computable ratio); if
that's too thin, fall back to screener.in's coarser "Broad Sector" tag;
if that's *also* too thin, fall back to cap_bucket (large/mid/small) as
the widest, last-resort peer group. Resolved independently per record and
per ratio (the two ratios can land on different tiers for the same
company), and the tier actually used is recorded on the result
(*_peer_tier) as a data-quality signal, not thrown away.

Re-tested Coal India under this hierarchy with real data, no hand-picked
override: its own "Sector" (Oil, Gas & Consumable Fuels, n=2 in-sample)
and "Broad Sector" (Energy, still n=2 - no wider real-tag pooling
available in this sample) both fall short of min_sector_size=3, so it
falls through to cap_bucket ("large", pooled with real large-cap peers
across unrelated sectors). There, its Other Liabilities/Capital Employed
ratio (1.13) clears 2x the large-cap median (0.49 -> threshold 0.97) and
gets flagged correctly - see
tests/test_fundamentals.py::test_coal_india_resolves_via_cap_bucket_fallback_without_a_hand_picked_override.
Ashok Leyland is unaffected by any of this - its real "Sector" (Capital
Goods, n=10 in-sample) always had enough peers and resolves at tier 1.

At full-universe scale this should need the cap_bucket fallback rarely
(most real "Sector" groups will have well more than 3 members), but the
mechanism is now principled rather than something requiring a judgment
call per thin sector.
