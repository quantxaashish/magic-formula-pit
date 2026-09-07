# 0006: fetch_fundamentals() was only extracting the latest fiscal year

Status: resolved. Confirmed as a real gap before assuming otherwise, fixed,
tested against real multi-year data. backtest.py was not touched further
until this was done, per instruction.

## What was asked

Before doing anything else: check directly whether `fetch_fundamentals()`
extracts the full multi-year time series screener.in shows, or only the
latest column - every validation so far (ROCE cross-checks, anomaly
detection, both pilots) only ever needed the latest figure, so this had
never actually been exercised. If it's latest-only, that's a real blocker
for section 7 (point-in-time backtesting needs a real history, not one
snapshot repeated), and needs reworking before backtest.py touches real
data.

## What was found

Confirmed directly from the code, not assumed: `parse_company_html()`
read the *entire* multi-year row via `_row()` (a full row already existed
in memory, e.g. TCS's 13-element Operating Profit row spanning Mar
2015-Mar 2026 plus TTM) and then immediately discarded everything except
one value - `op_row[-2]`, `ta_row[-1]`, etc. One `FundamentalsRecord` per
company, always the latest year. This is exactly the gap suspected: never
tested because every use case so far only needed the latest figure.

## Fix

`parse_company_html()` now returns `list[FundamentalsRecord]` - every
fiscal year present in *both* the P&L and balance-sheet tables, oldest
first. `fetch_fundamentals()`, `_fetch_one_statement()`, `_fetch_with_retry()`,
and `fetch_universe_fundamentals()` all updated to carry a list through
instead of a single record; `UniverseFetchResult.records` is now a flat
list spanning every company and every one of its available years.

**Matching fiscal years between the two tables is by parsed
`fiscal_year_end` date, not raw column position or label text.** This
matters concretely: Siemens' fiscal-year-change stub period is labeled
`"Mar 202618m"` in the P&L header but plain `"Mar 2026"` in the balance
sheet header - the same real period, two different strings. Both header
rows are parsed into `{fiscal_year_end: column_index}` maps
(`_fiscal_year_index_map`, reusing the existing stub-period-aware
`_parse_fiscal_year_end`), and a record is only produced for a fiscal year
present in both maps' *keys* (parsed dates), not by assuming the two
tables' columns line up positionally. A year present in only one table is
skipped and logged (`AAKASH`'s real Sep 2024 balance-sheet-only column,
found while running the full-universe fetch, is a live example), not
silently misaligned against the wrong column of the other table.

A per-row value that fails to parse (a genuinely blank/missing cell for
one line item in one year - found live for Nestle India's earliest
displayed column) is caught and that one fiscal year is skipped and
logged, rather than failing the whole company's extraction over one bad
historical cell.

## As-of date: confirmed computed per record, not once for a snapshot

`FundamentalsRecord.as_of_date` is set in `__post_init__` from that same
instance's own `fiscal_year_end`. Since one `FundamentalsRecord` now
exists per fiscal year, this was correct by construction once the
multi-record rework was in place - not something that needed a separate
fix, but explicitly verified: TCS's earliest record (FY2015-03-31) carries
`as_of_date = 2015-05-30`, its latest (FY2026-03-31) carries
`2026-05-30` - two different dates on two different records, not one
date reused. See `tests/test_fundamentals.py`'s
`test_parse_company_html_extracts_full_multi_year_history_for_tcs`.

## How far back the history actually goes: checked, varies a lot

Live-checked against 7 real companies, deliberately spanning very
different situations rather than assuming a uniform depth:

| Company | Years returned | Why |
|---|---|---|
| TCS, Tata Power | 12 (2015-2026) | The common case - screener.in's apparent default display window. |
| Siemens | 12 (Sep 2014-Sep 2024, then the Mar 2026 stub) | Fiscal-year change mid-history, still 12 periods total. |
| Colgate, Garden Reach Shipbuilders | 12 (via standalone) | Same 12-year window once fetched from the statement type that actually has data (decision 0003). |
| 3B Blackbio | 12 (2015-2026) | Listed on NSE only in April 2026 - screener shows financials **predating its own listing**. The underlying entity's audited history is what's shown, not years-since-NSE-listing. The point-in-time listing-history exclusion (magicformula.universe) is a separate, later concern from what this parser reports - it isn't this parser's job to decide tradability, only to report what screener shows. |
| Swiggy | 7 (2020-2026) | IPO'd Nov 2024; same pattern as 3B Blackbio - pre-IPO history shown. |
| Nestle India | 3 (2024-2026, one column skipped) | Changed fiscal year end (Dec -> Mar). screener.in only displays periods consistent with the *current* fiscal year convention, not Nestle's full multi-decade listed history - a real, sharp truncation, not a data completeness issue this parser can fix. One additional column (Dec 2023) was present but had an unparseable value and was skipped per-year rather than crashing the whole fetch. |

**Takeaway: don't assume 10 years of usable point-in-time history is
available for every company.** A fiscal-year change can cut it to as few
as 3-4 usable years even for a long-listed, large company. Section 7's
backtest should be built to tolerate a variable-length history per
company, not assume a fixed lookback window.

## A limitation this did NOT fix, flagged for whoever wires backtest.py to real data next

`market_cap` on every returned record - including historical ones - is
screener.in's **current** figure, identical across all of a company's
fiscal years in a given fetch. screener doesn't show historical market cap
in this table structure. This is fine for ROCE (EBIT/Capital Employed
doesn't involve price), but **wrong for historical Earnings Yield**
(EBIT/EV, where EV includes market cap) - using today's market cap against
a 2018 rebalance's EBIT would be nonsensical. A real backtest must
recompute market cap from a point-in-time share price
(`magicformula.data_fetch.prices`) before using a historical record for
EV/EY, not trust this field beyond the latest fiscal year. Documented
directly on `FundamentalsRecord.market_cap` and in `parse_company_html`'s
docstring so this isn't rediscovered the hard way later.

## Test coverage

`tests/test_fundamentals.py`:
- `test_parse_company_html_extracts_full_multi_year_history_for_tcs` -
  real TCS fixture, 12 years, both endpoints' values and as-of dates
  checked, confirms per-record (not shared) as-of dates.
- `test_parse_company_html_matches_differently_labeled_stub_period_across_tables` -
  real Siemens fixture, confirms the cross-table date-based matching
  produces exactly 12 records (not 11 dropped or 13 duplicated) despite
  the label mismatch on the stub period.
- `test_fetch_universe_fundamentals_flags_company_by_latest_year_only` -
  synthetic, confirms the anomaly check (a current-snapshot judgment)
  uses only a company's latest fiscal year, and that flagging excludes
  *all* of that company's historical records, not just the latest.
- All existing single-record tests updated to the list-based return type.

131/131 tests passing after the rework.

## What's next

The full-universe fetch (in progress at the time of this fix) was
restarted with the corrected parser rather than left to complete on the
old, latest-only code and re-run again later - the disk cache made this
cheap (most already-fetched HTML was reused, only re-parsed). Its results,
including the real failure taxonomy and peer-tier distribution the
previous instruction asked for, will be reported once it completes.
backtest.py is not being touched further until that full run is in and
reviewed, per instruction.
