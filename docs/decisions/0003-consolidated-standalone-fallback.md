# 0003: Pre-filter ordering, 236-company pilot results, and a
consolidated/standalone fallback

Status: resolved. Pre-filter ordering bug confirmed and fixed; pilot
results analyzed; two new page-template edge cases found and fixed with a
general (not per-company) mechanism.

## The ordering question

**Confirmed: yes, this was a real bug.** The pilot ran `fetch_universe_fundamentals()`
against 236 companies with no financials/utilities pre-filter applied -
`magicformula.universe` had no such function at all before this pass.
HDFCBANK showing up mid-pilot, hitting a completely different screener.in
page template (no "Operating Profit" row - banks report Interest Earned/
Interest Expended instead), was exactly the symptom expected from fetching
first and filtering after.

**Fix**: added `exclude_likely_financials_and_utilities()` to
`magicformula.universe` (name-heuristic pre-filter, same non-authoritative
caveat as the ETF/REIT heuristic - see decision 0002), and wired it into
`scripts/pilot_fundamentals_fetch.py`'s symbol selection *before* any
fetch happens. `fetch_universe_fundamentals()`'s own docstring now states
this precondition explicitly.

### How much of the pilot's cost this actually explains

Of the 236 companies attempted, 52 failed extraction. Checking each failed
symbol's real company name against the (now-existing) financial/utility
markers: **34 of 52 (65%) match** - these would never have been fetched at
all under the corrected ordering. Three more (REC Limited, SBI Cards and
Payment Services, Aditya Birla Capital) are real financial companies whose
names slipped past the *original* marker set; refining it (adding
"CAPITAL", "CREDIT", "PAYMENT") catches two of the three - REC Limited's
short registered name carries no financial-sounding word at all and is
now a documented, accepted gap (see README's Known Limitations).

A secondary, unplanned finding: even the financial companies that *did*
extract successfully in this pilot (banks/NBFCs whose page happened to
still expose enough of the right rows, or non-bank financials like
insurers/AMCs/exchanges) show up heavily in the anomaly-check results
below - not because they're actually anomalous, but because "Financial
Services" as a single screener.in sector tag is itself too heterogeneous
(life insurers, AMCs, NBFCs, and stock exchanges have structurally
different balance sheets) for a peer-median comparison to mean much within
it. Pre-filtering financials before they ever reach ranking removes this
noise source too, not just fetch cost.

## Pilot results - PRE-FIX, NOT VALIDATED (236 companies: 76 large / 87
mid / 73 small, contiguous AMFI-rank slices)

**These numbers are from the original pilot run, before any of the fixes
in this document existed - no financials/utilities pre-filter, no
consolidated/standalone fallback, and (discovered later, see "A second bug
found after this pilot" below) the fallback as first shipped was itself
broken by a URL bug and would not have helped even if it had existed at
the time. Do not read these as validated pipeline behavior. They are kept
only as the record of what the ordering bug and the missing fallback
actually cost, motivating the fixes below. The corrected numbers, from a
pilot run with every fix actually active, are in
docs/decisions/0005-corrected-pilot-results.md.**

### Failure rate by cap bucket (PRE-FIX)

| Bucket | Attempted | Failed | Rate |
|--------|-----------|--------|------|
| Large  | 76        | 13     | 17.1% |
| Mid    | 87        | 23     | 26.4% |
| Small  | 73        | 16     | 21.9% |

**Mid cap failed most, not small cap** - the opposite of the hypothesis
going in. The likely reason: mid-cap rank 101-190 happens to contain a
denser concentration of both (a) mid-size banks/NBFCs/insurers (the
bank-template failure) and (b) MNC-subsidiary-style companies with thin or
absent consolidated financials (Pfizer, Bayer CropScience, AstraZeneca
Pharma, Abbott India, Honeywell Automation, Gillette India all fall in
this rank band) - both failure modes cluster there more than in the very
largest or the smaller/less MNC-heavy names checked. Not a claim that
small cap is safe from either failure mode in general, just that this
particular 73-company slice didn't happen to concentrate them the same
way.

### Peer-tier usage (PRE-FIX) (anomaly detector's 3-tier fallback, decision 0001)

184 successful records x 2 ratios = 368 possible tier-resolutions:

- **sector: 364** (99%)
- **cap_bucket: 4** (1%)
- broad_sector: 0, none: 0

At this scale, screener.in's narrow "Sector" tag almost always had enough
real peers - the cap_bucket fallback triggered only twice (2 companies x 2
ratios, or a mix). This is reassuring for the concern that "most of
small/mid cap would fall through to the noisy cap_bucket tier": it didn't,
at least not in this slice. Worth re-checking at full-universe scale,
where sector groups will be even larger (more room to resolve at tier 1)
but this pilot already suggests it isn't a widespread problem.

### New page-template edge cases found in the PRE-FIX pilot (beyond the bank/financial template)

Investigated the 18 non-financial-by-name failures directly. All 18 trace
to one root cause with two visible symptoms, both on screener.in's
`/consolidated/` page for companies that don't have real consolidated
financials there:

1. **Stale date window** (Colgate-Palmolive, Tata Elxsi): the server-
   rendered HTML's P&L/balance-sheet table shows an old default date range
   (e.g. Mar 2006-Mar 2010) instead of the most recent decade, and has no
   "TTM" column - the parser correctly refuses to guess rather than
   silently report 20-year-old numbers as current.
2. **Empty table** (Garden Reach Shipbuilders, Motherson Sumi Wiring,
   Bharti Hexacom, GE Vernova T&D, and others): the table's row labels
   ("Sales+", "Operating Profit", ...) are present but every row has
   exactly one cell (the label) and zero data columns - the header row
   itself is a single empty cell.

Confirmed directly for three of these (Garden Reach Shipbuilders, Bharti
Hexacom, Motherson Sumi Wiring, Colgate, Tata Elxsi) that the `/` (standalone)
page for the same company has full, correctly-dated data. No confirmed
single trigger for *which* companies hit this (not simply "MNC
subsidiary" - Bharti Hexacom is a domestic subsidiary of Bharti Airtel;
not simply "long-listed" - Tata Elxsi has been listed since the 1990s,
same vintage as many companies that render fine) - most likely a
screener.in backend/caching quirk specific to how that company's page
was last generated, not something to over-theorize without access to
their internals.

### Fix: consolidated -> standalone fallback

`_fetch_with_retry()` in `data_fetch/fundamentals.py` now tries the
requested statement type (consolidated by default) with its full retry
budget, and if every attempt fails, tries the other statement type
(standalone) with its own retry budget before giving up. `FundamentalsRecord`
gained a `statement` field recording which one actually supplied the data,
so a downstream consumer can see when a company's numbers came from the
fallback rather than the requested type. `FailedExtraction.statement`
becomes `"consolidated+standalone"` when both were exhausted, and
`.attempts` sums both, so `failed_extractions.csv` still gives an honest
accounting.

This does not help the bank/financial-template failures (their
standalone page has the same missing-row problem) - but per the ordering
fix above, those should never reach this code at all in a corrected run.

### A second bug found after this pilot: the fallback URL was wrong

The fallback above was implemented by building the standalone URL as
`.../company/<SYMBOL>/standalone/` - mirroring the consolidated URL's
`.../company/<SYMBOL>/consolidated/` shape. **That URL doesn't exist.**
screener.in's standalone view is the bare company URL with no statement
segment at all (`.../company/<SYMBOL>/`); the `/standalone/` path 404s for
every company. Confirmed live: `/company/COLPAL/standalone/` -> 404,
`/company/COLPAL/` -> 200 with the real data.

This meant the fallback described above was non-functional from the
moment it was written until this bug was caught - every company that
needed it (Colgate, Garden Reach Shipbuilders, Motherson Sumi Wiring,
Bharti Hexacom, Siemens, Bharat Dynamics, and others) failed *both*
statement attempts, the second one 404ing instead of reaching real data.
It surfaced only when a second, corrected pilot run (docs/decisions/0005)
showed every fallback attempt failing identically with a 404. Fixed with a
single `company_url(symbol, statement)` helper that both `ScreenerClient`
and `fetch_fundamentals` now call, instead of each formatting the URL
inline - see `tests/test_fundamentals.py`'s `company_url` tests.

## What wasn't re-run

The 236-company pilot above reflects the *pre-fix* pipeline (no
financials pre-filter, no consolidated/standalone fallback) - it's kept as
the record of what the ordering bug actually cost, not re-run against the
fixed pipeline. A future pilot at full-universe scale will exercise both
fixes together.
