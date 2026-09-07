# 0011: The point-in-time embargo was applying its 60-day lag twice

Status: fixed, tested against a real-data regression case (TCS FY2026),
full suite green. Not a judgment call - SPEC.md section 6 wants a single
embargo mechanism, not two independent ones stacked for the same purpose.

## How it was found

While verifying the backtest pilot's gates for real (not "looks sane" -
checking each one actually engaged), the point-in-time embargo check
turned up something that didn't match its own label: the "2026-06-01"
rebalance was using **FY2025** fundamentals, not FY2026, for TCS - a
company with an unbroken fiscal-year history back to 2015. That's one
full fiscal year older than the rebalance date's name suggests.

## The mechanism: two independent 60-day buffers, compounding

Two separate places in the codebase each implement *the same* SEBI LODR
60-day filing-lag concept, unaware of each other:

1. `FundamentalsRecord.__post_init__` (`magicformula.data_fetch.
   fundamentals`): `as_of_date = fiscal_year_end + ANNUAL_FILING_LAG_DAYS
   (60)` - a per-record proxy for "the date this fiscal year's figures
   actually became public."
2. `filter_point_in_time` (`magicformula.backtest`, before this fix):
   `cutoff = rebalance_date - fundamental_lag_days (default 60)`, then
   kept only records with `as_of_date <= cutoff`.

Composing these: a record is usable only once `fiscal_year_end + 60 <=
rebalance_date - 60`, i.e. `fiscal_year_end <= rebalance_date - 120
days` - roughly double the intended lag, not the single ~60-day SEBI
buffer SPEC.md section 6 describes. Each piece was reasonable in
isolation and both were tested in isolation (the unit tests for
`as_of_date` and for `filter_point_in_time`'s cutoff arithmetic each
passed on their own numbers) - the bug only shows up when they're
composed, which is exactly why it survived until an end-to-end,
real-data check went looking for it specifically.

## Practical effect: real, checked against TCS, not estimated

For TCS specifically (fiscal years available back to 2015, no gaps):

| Rebalance date | Fiscal year actually used (before fix) | Fiscal year that should be usable |
|---|---|---|
| 2024-06-01 | FY2023 (Mar 2023) | FY2023 (Mar 2024's as_of_date, 2024-05-30, is after the rebalance) |
| 2025-06-01 | FY2024 (Mar 2024) | FY2024 |
| 2026-06-01 | **FY2025** (Mar 2025) | **FY2026** (as_of_date 2026-05-30 is before the 2026-06-01 rebalance) |

The 2024 and 2025 rebalances happened to land on the *correct* fiscal
year by coincidence of timing (their genuinely-current fiscal year's
`as_of_date` fell late enough in the year that even the doubled lag
didn't push the selection back a further year) - only the 2026-06-01
case, checked directly, exposed the actual one-fiscal-year staleness.
This is safe in direction (further from a look-ahead bias, not closer)
but means the backtest was quietly working with data appreciably older
than the regulatory minimum the design intends, for no benefit.

## The fix: one buffer, living in `as_of_date`

Two ways to collapse this to a single buffer were on the table:
(a) keep `as_of_date = fiscal_year_end + 60` and make
`filter_point_in_time`'s cutoff exactly `as_of_date <= rebalance_date`
(no further subtraction), or (b) keep the buffer in
`filter_point_in_time` and set `as_of_date` to the raw `fiscal_year_end`.

**Chose (a).** `as_of_date` is named and documented, throughout this
codebase and in decisions 0006/0007, as *the* point-in-time proxy for
"when this record's data became public" - `run_rebalance_walk`'s own
docstring already says "each record's own as_of_date determines which
rebalance(s) it's eligible for." Stripping the lag out of `as_of_date`
(option b) would leave a field named "as of" that's just the raw fiscal
year end, misleading by name everywhere else it's read. Keeping the
lag where its name already says it lives, and making the embargo
function a plain comparison, is the smaller, more honest change.

**`filter_point_in_time`'s `fundamental_lag_days` parameter is removed
entirely, not defaulted to 0.** A no-op parameter left in the signature
is exactly the kind of thing a future change could "helpfully" set to a
nonzero value again without realizing `as_of_date` already carries the
real lag - removing it outright, with a docstring explaining where the
lag actually lives, is what makes this not silently reintroducible.
Removed the same way from `run_rebalance_walk` and `run_backtest`,
which only ever passed it straight through.

```python
def filter_point_in_time(
    records: list[FundamentalsRecord],
    rebalance_date: date,
) -> list[FundamentalsRecord]:
    """... No lag is subtracted here - the public-availability buffer is
    as_of_date itself ... not a second, independent buffer applied again
    at this layer."""
    return [r for r in records if r.as_of_date <= rebalance_date]
```

If a wider embargo is ever genuinely wanted, the correct place to widen
it is `ANNUAL_FILING_LAG_DAYS` in `magicformula.data_fetch.fundamentals`
- not a new subtraction in `magicformula.backtest`. Both files now say
this explicitly in their own docstrings, at the two places someone would
naturally reach for first.

## Regression test: real TCS data, not synthetic dates

`tests/test_backtest.py::test_tcs_2026_06_01_rebalance_uses_fy2026_not_
fy2025` builds real TCS FY2025 and FY2026 `FundamentalsRecord`s (same
consolidated figures already used in `tests/fixtures/real_companies.py`),
lets `as_of_date` compute naturally from `__post_init__` rather than
hand-overriding it, runs a real `run_rebalance_walk` for the
`2026-06-01` rebalance, and asserts FY2026 (`fiscal_year_end ==
date(2026, 3, 31)`) is both present in what `rank_fn` sees and is the
latest record - checking `operating_profit == 72_398` (FY2026's real
figure) specifically, not FY2025's. Verified this test would have failed
under the pre-fix code by reproducing the old cutoff arithmetic directly
(`2026-06-01 - 60 = 2026-04-02`; FY2026's `as_of_date` of `2026-05-30`
fails that cutoff, FY2025's `2025-05-30` passes it) before trusting the
test as a real guard, not just a passing assertion.

Three existing synthetic tests in `test_backtest.py`
(`test_filter_point_in_time_includes_record_exactly_at_the_cutoff`,
`..._excludes_record_one_day_past_the_cutoff`, and the embargo/carry-
forward walk test) had their hand-computed cutoff values and comments
updated to the new single-lag arithmetic - they were testing the same
mechanism, just calibrated to the double-lag cutoff before.

## What this changes

The backtest pilot (166 companies, 3 rebalance dates) needs re-running
fresh under the corrected embargo, not assumed to produce the same
basket - a full extra fiscal year of fresher data at 2026-06-01
specifically can change who ranks where, not just shift numbers slightly.
Reported as its own result, not folded quietly into this doc.
