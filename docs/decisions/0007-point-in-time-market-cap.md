# 0007: Point-in-time market cap for historical EV/EY

Status: design decided and documented, generic mechanism built and
synthetic-tested, live data source NOT wired in. This is a design
decision to review, not a finished feature - see "What's still open"
below.

## The problem

`FundamentalsRecord.market_cap` is screener.in's *current* figure, applied
identically to every historical fiscal year returned for a company
(decision 0006). That's fine for ROCE (EBIT/Capital Employed, no price
involved) and wrong for historical Earnings Yield: EV = Market Cap + Debt
- Cash - Other Liquid Investments, and using *today's* market cap against
a 2018 rebalance's EBIT is nonsensical. A real backtest needs
`market_cap(as_of_date) = price(as_of_date) x shares_outstanding(as_of_date)`,
not today's value replayed backward.

`price(as_of_date)` is already solvable - `magicformula.data_fetch.prices`
has both yfinance history and the NSE bhavcopy fallback. The open question
is `shares_outstanding(as_of_date)`.

## The question this decision actually turns on

Is a constant-shares assumption (use today's shares outstanding for every
historical period) safe enough to use, or do corporate actions happen
often enough in this universe - especially mid/small cap - that it would
introduce real errors?

**Checked directly, not assumed.** Pulled `yfinance`'s `.splits` for 14
real tickers spanning large/mid/small cap and multiple sectors (not
cherry-picked - the same kind of diverse sample used throughout this
project's validation):

| Ticker | Splits found |
|---|---|
| TCS | 3 (2006, 2009, 2018) |
| Reliance | 4 (1997, 2009, 2017, **2024**) |
| Infosys | 8 |
| Asian Paints | 2 (including a 1.5:1 in 2003) |
| Maruti | 0 |
| PGHL | 0 |
| APL Apollo | 2 (2020, 2021) |
| Kajaria Ceramics | 2 |
| Cera Sanitaryware | 1 |
| V-Guard Industries | 2 |
| Century Plyboards | 1 |
| TTK Prestige | 2 |
| Graphite India | 1 |
| Welspun Corp | 1 |

**12 of 14 had at least one split/bonus event; several had multiple.**
Splits are not a rare edge case in this universe - they're the norm. A
constant-shares assumption would misstate market cap (and therefore EV and
EY) by exactly the split ratio for every rebalance before the split date,
for most companies eventually.

It gets worse than "clean splits" suggest. Checked `yfinance`'s
`get_shares_full()` (an actual point-in-time shares-outstanding series,
not just discrete split events) for a few of the same names:

- **Maruti** - zero recorded splits, but real shares-outstanding still
  drifted from ~291M to ~331M (+13.7%) over the checked window, almost
  certainly ESOP exercises. `.splits` alone would have called Maruti
  "stable"; it wasn't.
- **AAKASH** (a small, thinly-covered name) - shares outstanding ranged
  from ~6.16M to ~112.0M, roughly ~~**18x**~~ across the checked window.
  This is not a clean split ratio - it's a scale of change that would make
  a constant-shares assumption catastrophically wrong for this specific
  name, and it's exactly the kind of small/obscure company where nobody
  would think to double check.

  **Correction (see [0008](0008-cash-proxy-aakash-shares-reliability.md)):**
  verified directly against NSE's own corporate-actions API (not another
  scraper) - the swing is real (two genuine, dated, NSE-registered
  actions: a 1.5:1 bonus issue on the SME segment in 2020, then a 10:1
  face-value split on migration to the mainboard in 2022, confirming a
  true **15x**), but the "18x" figure above came from a single-day noise
  spike in `get_shares_full()`, not a real share count. The underlying
  conclusion (constant-shares is unsafe here) survives; the number was
  wrong. 0008 also found this noise pattern is pervasive across large and
  mid cap names too, not unique to small/obscure ones - see that doc
  before wiring `nearest_value_lookup` to a live source.

**Conclusion: constant shares is not a safe assumption anywhere in this
universe, and it gets worse, not better, in smaller/more obscure names -
the opposite of "large caps are complicated, small caps are simple."**
`.splits` alone (clean stock splits and bonus issues) would already have
been necessary; it also isn't sufficient, since it doesn't explain
Maruti's or capture QIPs/rights issues/buybacks, all of which change share
count without registering as a "split" in Yahoo's data model.

## Decision

Use `get_shares_full()`-style point-in-time shares-outstanding data,
looked up at each record's as-of date, multiplied by point-in-time price
(already available via `magicformula.data_fetch.prices`) - not a
constant-shares assumption, and not `.splits` alone as a coarse patch.

This is a stronger conclusion than the question as posed ("check whether
`.splits` can detect which companies need adjusting") - the investigation
found that `.splits` under-covers real share-count change (misses
ESOP-driven drift entirely, and QIPs/rights/buybacks by design), while
`get_shares_full()` gives the actual series directly, for every company
checked so far including a small, obscure one. There's no evident need for
a two-tier "flag which companies need adjustment vs. which don't" scheme
when the more complete data source is already available at roughly the
same cost.

**Mechanism built now** (`magicformula.data_fetch.prices`), synthetic-
tested, not wired to a live source:

- `nearest_value_lookup(series, target_date, max_days_tolerance=None)` -
  most recent known value on-or-before `target_date`, from a plain
  `{date: float}` series. Never returns a value dated after the target (no
  future leakage), and can reject a match that's stale beyond a tolerance
  window rather than silently using a value from a period the series
  doesn't really cover.
- `compute_point_in_time_market_cap(price, shares_outstanding)` - trivial
  multiplication that returns `None` (not a misleading `0`) if either
  input is missing.

Both are generic over how the underlying series was obtained - a live
`get_shares_full()` pull is one caller away, not a redesign, matching how
`PriceLookup` and `RankFn` are already injected elsewhere in
`magicformula.backtest`.

## What's still open (not resolved by this decision)

- **Wiring `get_shares_full()` into an actual fetch/cache layer.** This
  decision establishes *what* data source to use and *how* to look it up
  point-in-time; it does not build a `SharesOutstandingClient` or cache
  live share-count history the way `ScreenerClient`/`YFinanceClient`/
  `BhavcopyClient` do for fundamentals/prices. That's real data wiring,
  explicitly still on hold.
- **Coverage/reliability at full-universe scale.** **Checked properly in
  [0008](0008-cash-proxy-aakash-shares-reliability.md)** against a random
  24-ticker sample spanning all three AMFI cap buckets, not just ~7
  ad hoc names. Result was a surprise in both directions: coverage is
  universal (24/24 returned data, including a company listed less than a
  year), so the "small cap may return sparse/no data" concern above did
  not materialize. But reliability is worse and differently-shaped than
  expected - single-day noise spikes (some off by 2000x+) show up across
  large, mid, *and* small cap, uncorrelated with liquidity. The fallback
  path this bullet anticipated (falling back to a single current
  shares-outstanding figure) is not what's needed. **Resolved**: 0008's
  part 4 root-caused the spikes (real corporate actions in some cases,
  genuine data corruption in others - a magnitude threshold can't tell
  them apart, only persistence can) and built+tested
  `filter_persisted_values()` as a cleaning pass ahead of
  `nearest_value_lookup`, not a change to it. See 0008 for the full
  per-ticker data and the filter design.
- **How this composes with `Investments`-as-cash-proxy.** Historical EV
  still needs `cash_and_equivalents`/`other_liquid_investments`, which
  `FundamentalsRecord` doesn't carry as a distinct cash field at all
  (screener.in's free-tier balance sheet doesn't expose one - see the
  companion confirmation below). The existing convention from
  `tests/fixtures/real_companies.py` (`cash_and_equivalents=0`,
  `other_liquid_investments=record.investments`) is what a real backtest
  should keep using; this decision doesn't change that, just flags that it
  needs to be applied consistently once historical EV is actually computed.

## Companion confirmation: Total Debt and Cash extraction (asked alongside this)

Checked directly against three real companies across their full multi-year
history (TCS, JSW Steel, Asian Paints) rather than assumed:

- **`borrowings` (Total Debt) is genuinely extracted per fiscal year**, not
  constant. TCS: 358 (FY2015) -> 8,174 (FY2020) -> 11,283 (FY2026). JSW
  Steel: 38,754 -> 99,310 across the same window (consistent with its
  known debt-financed capacity expansion). This uses the same per-year
  `bs_i` indexing as `total_assets`/`other_liabilities` - it was fixed
  automatically by decision 0006's rework, not something that needed a
  separate patch.
- **There is no dedicated "Cash & Equivalents" field on `FundamentalsRecord`
  at all** - not "silently constant like market_cap", genuinely absent,
  because screener.in's free-tier balance sheet doesn't expose cash as its
  own line item (confirmed back when `tests/fixtures/real_companies.py`
  was first built - `SPEC.md` section 3's `other_liquid_investments`
  parameter is what stands in for it, populated from the `Investments`
  bucket). This is a pre-existing, already-documented data-source
  limitation, not a new bug - flagged here again only because it's
  directly relevant to finishing the point-in-time EV picture above.
