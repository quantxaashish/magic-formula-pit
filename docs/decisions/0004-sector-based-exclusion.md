# 0004: Authoritative sector-based exclusion (closing the REC Limited gap)

Status: resolved.

## Problem

`magicformula.universe.exclude_likely_financials_and_utilities()` is a
pre-fetch name heuristic - a cost-saving filter, not a guarantee (decision
0003). REC Limited (a well-known NBFC, formerly Rural Electrification
Corporation) has no financial-sounding word anywhere in its registered
name and slips past it. Silently keeping a known financial company in the
ranked universe because its name happened not to match a keyword list is
not an acceptable outcome, even though the heuristic is documented as
non-authoritative - the point of documenting a limitation is to fix it
where it's cheap to fix, not to use the documentation as cover.

## What was asked

Use screener.in's own real "Sector" tag - already fetched for the peer
hierarchy (decision 0001) - as a second, authoritative exclusion pass
after fetch: a company whose real sector is Financial Services or a
utility gets dropped even if the name heuristic missed it. Add REC Limited
itself as the regression fixture, same pattern as the Coal India and Ashok
Leyland fixtures in decision 0001.

## What was found while implementing it

The natural reading of "a pass after fetch" assumes the company's
financial data fetches and parses successfully, and the check runs on the
resulting record. That doesn't hold for REC Limited: it's a bank-style
NBFC, and its screener.in P&L reports Interest Earned/Interest Expended,
not Operating Profit - the same template mismatch that makes HDFC
Bank/ICICI Bank/etc. fail extraction entirely (decision 0003). Confirmed
directly: `parse_company_html()` raises before ever reaching sector
extraction, because it looks for the P&L/balance-sheet tables first and
raises if they're not found in the expected shape. A check gated on a
successful full parse would never see REC Limited at all.

(This doesn't hold for every Financial Services company, though - insurers,
AMCs, exchanges, and wealth managers, e.g. LICI, HDFC Life, HDFC AMC, BSE
Ltd, generally do report an Operating-Profit-style P&L and parse
successfully. The pilot in decision 0003 shows several of these being
fetched fine and then flagged by the anomaly detector as sector outliers -
correctly parsed, just not correctly excluded before reaching that check.)

## Fix

`fetch_fundamentals()` now reads screener.in's real "Sector"/"Broad
Sector" tags immediately after the first successful HTML fetch, via a new
`parse_sector_tags()` - deliberately separate from `parse_company_html()`
because the Peer Comparison section renders for every company regardless
of P&L template, so this step always succeeds even when the full parse
never will. If `broad_sector` is `"Financial Services"` or `"Utilities"`
(both values confirmed live against HDFC Bank/REC Limited/LIC and Tata
Power/NTPC Green), `SectorExcluded` is raised before any attempt to parse
the financial tables.

`SectorExcluded` is deliberately not retried and does not trigger the
consolidated/standalone fallback (decision 0003) - the company's sector is
the same regardless of statement type or attempt number, so retrying
or falling back would just spend fetches to learn the same fact twice.
This means the total cost of a heuristic miss like REC Limited is exactly
one fetch, not the up-to-six a genuine parse failure could cost under the
full retry+fallback budget.

`UniverseFetchResult` gained a `sector_excluded: list[SectorExclusion]`
field, kept separate from `failed_extractions` - a sector exclusion is a
correct, informed outcome, not a failure, and conflating the two would
mislead anyone reading "N of M failed extraction" into thinking the
pipeline is less reliable than it is.

## Test coverage

`tests/test_fundamentals.py`, using REC Limited's real saved HTML
(`tests/fixtures/screener_html/RECLTD.html`) and Tata Power's
(`TATAPOWER.html`) as the utility-side fixture, same pattern as decision
0001's Ashok Leyland/Coal India real-data fixtures:

- `parse_sector_tags()` against both real fixtures.
- `fetch_fundamentals()` raises `SectorExcluded` for the real REC Limited
  page (network mocked via a pre-seeded cache, not a live call).
- `fetch_universe_fundamentals()` routes a sector-excluded company to
  `sector_excluded`, not `failed_extractions`, confirmed to cost exactly
  one fetch attempt (no retry, no consolidated/standalone fallback probe).
