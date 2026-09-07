# Magic Formula India

NSE/BSE quantitative value screener implementing Joel Greenblatt's Magic
Formula for the Indian equity market. See `SPEC.md` for the full
specification this project is being built against.

This README currently covers Known Limitations only (SPEC.md section 13
asks for a fuller README - data source reliability, standard vs. strict
ROCE, survivorship bias, AMFI update instructions - once more of the
pipeline is built). The limitations below are tracked here because they
affect real decisions (what to trust, what needs a human check) starting
now, not once the pipeline is "done."

## Known Limitations

### Survivorship bias - the single most important caveat on every backtest number below

**The universe is sourced from NSE's and BSE's live, current equity lists,
not a point-in-time historical listing snapshot** -
`NSE_EQUITY_LIST_URL` (`nsearchives.nseindia.com/.../EQUITY_L.csv`) is
NSE's present-day master list, and `BSE_ACTIVE_EQUITY_API_URL`'s name says
it outright: **active** scrips only. A company that was delisted, went
through NCLT insolvency, or was otherwise removed from trading at any
point before "today" (whenever `build_universe()` is run) is invisible to
this pipeline - not excluded by a filter, absent from the source data
itself, for every rebalance date in every backtest, past or future.

**Checked directly against real companies, not assumed.** Nine well-known
names that were delisted, went into insolvency, or were extinguished via
NCLT resolution during 2019-2026, checked against the current NSE equity
list:

| Company | Status 2019-2026 | In current universe? |
|---|---|---|
| DHFL | Insolvency, equity extinguished 2021 | **Absent** |
| Reliance Capital | Insolvency, equity extinguished 2024 | **Absent** |
| Future Retail | Insolvency, delisted 2023-24 | **Absent** |
| Future Lifestyle Fashions | Insolvency, delisted | **Absent** |
| Future Consumer | Insolvency, delisted | **Absent** |
| Jet Airways | Grounded 2019, insolvency | **Absent** |
| IL&FS Transportation Networks | Insolvency-linked distress | **Absent** |
| Reliance Communications | Distressed, still listed (control case) | Present |
| Yes Bank | 2020 crisis, still listed (control case) | Present |

Seven of seven genuinely delisted/extinguished names are absent; both
control cases (companies that had real crises but stayed listed) are
correctly present - confirming this is specifically a delisting gap, not
a general data-quality problem.

**Rough scale, not a full count** (a full count would require
cross-referencing NCLT resolutions, voluntary delistings/buyouts, BSE's
own list, and mergers against historical fundamentals eligibility - not
pursued, no reliably obtainable point-in-time listing source was found
within reasonable effort this session): NSE's own published "Orders of
Delisting Committee" record **at least 57 companies compulsorily
delisted within the 2019-06-01 to 2026-06-01 window** on the NSE channel
alone. This is a lower bound - it excludes BSE's separate list, voluntary
delistings, mergers, and critically the NCLT-insolvency-resolution
channel where the largest, best-known casualties above (DHFL, Future
Retail, Reliance Capital, ITNL) actually sit, since that's a different
process from the Delisting Committee's compulsory-delisting orders. Most
names on the NSE list are small/dormant companies that likely never had
real fundamentals data to begin with; the real, Magic-Formula-relevant
count (companies that would have had a genuine shot at ranking at some
point) is almost certainly smaller than 57 but larger than the 7
hand-confirmed cases above - a real, non-trivial number either way, not
a rounding error.

**A related, same-direction effect**: `compute_period_return` silently
drops a holding's weight from a period's return if its price is missing
at the period's end date, rather than counting it as a loss - reasonable
in isolation (a genuine data gap shouldn't be assumed to be a total
wipeout), but it means a stock that *was* bought into a basket and *then*
failed mid-holding-period also doesn't get counted as the loss it
actually was. Same direction of bias as the universe gap, compounding it.

**This is not a minor caveat and should not be read as one.** Every
number in this backtest - the +29.4% CAGR net of transaction costs, the
0.71 Sharpe ratio, the -10.3% max drawdown, and especially the **100%
hit rate against Nifty 500 TRI across all 7 periods** - is computed
against a universe that, by construction, never includes the real
failures a genuine point-in-time small/mid-cap value screen would have
been exposed to. The 2019-2020 COVID-crash period is the clearest place
to see why this matters: the strategy's basket fell far less than either
benchmark that year, a result read elsewhere in this README as evidence
of real stock-selection alpha - but a screen that can never pick a stock
that goes to zero is mechanically going to look more resilient in a
crash than one that can, and 2019-2021 is exactly when several of the
confirmed-absent names above (DHFL, Jet Airways, Reliance Capital) were
failing. The true picture likely still shows real outperformance - the
margin over even the cap-tilt-matched blend is large enough that
survivorship bias alone is unlikely to explain all of it - but the exact
size of that margin, and the unusually clean 100% hit rate specifically,
should be treated as inflated by an unknown, likely non-trivial amount
until a point-in-time listing source is found and wired in. No further
number in this document should be taken at face value without this
context.

### Surveillance/ASM-GSM watchlist flagging - not implemented

SPEC.md section 1 asks to exclude companies under NSE/BSE's surveillance
(ASM) or Graded Surveillance Measure (GSM) watchlists. **This pipeline has
no way to detect either right now.** `magicformula.universe`'s NSE feed
captures the "SERIES" column (BE/BZ mean trade-to-trade settlement, a
liquidity/settlement mechanism, not the same thing as a surveillance flag)
as metadata only - it is not used to exclude anything. A real
implementation needs NSE/BSE's own dedicated ASM/GSM lists, which aren't
wired up yet.

**This is a hard blocker for the small-cap bucket specifically before this
pipeline is ever used to size a real position.** Surveillance flags and
thin, manipulable liquidity concentrate heavily in small/micro caps - far
more than in large or mid cap - and a stock the Magic Formula ranks highly
because it's statistically cheap can simultaneously be one a regulator has
flagged for suspicious trading activity. Treat any small-cap name that
comes out of a basket as unverified on this dimension until a real
surveillance check exists, not as a general caveat to keep in the back of
your mind.

### Insolvency (NCLT) status - not implemented

Same category of gap as above, no data source wired up. A company
undergoing NCLT insolvency proceedings should be excluded per SPEC.md
section 1, and currently isn't checked at all.

**Also a hard blocker for the small-cap bucket specifically**: distressed
companies heading toward insolvency are disproportionately small/micro
cap, and are exactly the kind of "statistically cheap for a reason" trap
the Magic Formula's plain two-factor version is known to walk into without
the Phase 2 quality overlay (Altman Z-score, etc. - SPEC.md section 8),
which also isn't built yet.

### ETF/REIT/InvIT exclusion - name heuristic, not authoritative

`magicformula.universe.exclude_non_standard_instruments()` matches
whole-word "REIT"/"InvIT"/"Trust" in a company's name. Audited against the
full live NSE+BSE universe
(`docs/decisions/0002-etf-reit-invit-heuristic-audit.md`): every real,
regulator-registered REIT/InvIT checked (Embassy Office Parks, Mindspace,
Brookfield India REIT, IRB InvIT, IndiGrid, PowerGrid InvIT, IndInfravit,
Bharat Highways InvIT) turned out to be structurally absent from both
NSE's equity list and BSE's equity-segment feed entirely - they trade
under a different instrument type on both exchanges, so this data source
excludes them before the heuristic ever runs. Zero false negatives found.
What the heuristic does catch in the live universe (16 entries) is mostly
"Investment Trust"/"Capital Trust"-style financial companies that aren't
literally REITs - a defensible exclusion in spirit, mislabeled if called a
REIT catch. Not proven bulletproof beyond what was checked; treat as a
reasonable default, not a guarantee.

### Financial Services/Utilities pre-filter - name heuristic, not authoritative

`magicformula.universe.exclude_likely_financials_and_utilities()` is a
name-pattern pre-filter, run *before* fundamentals.py ever fetches a
company, to avoid wasting a scrape (and its retries) on a company excluded
from ranking anyway (SPEC.md section 5) - banks/NBFCs/insurers use a
completely different screener.in page template (no "Operating Profit" row
at all). A 236-company pilot found this heuristic's marker set initially
missed real cases (Aditya Birla Capital, SBI Cards, REC Limited) before
being refined; REC Limited's short registered name still carries no
financial-sounding word at all and remains an accepted, undetectable-by-
name gap. A company that slips past this pre-filter and gets fetched
successfully still needs a real, sector-based exclusion once its actual
sector is known - this heuristic only controls fetch cost, it is not the
authoritative section 5 filter.

### Point-in-time market cap - real coverage only from 2019 onward

Historical market cap for the backtest is computed from real point-in-time
price x shares-outstanding, not screener.in's stale "current" figure
(`docs/decisions/0007`) - but `yfinance`'s `get_shares_full()` (the shares
side of that computation) has a real historical depth limit that varies by
company (checked directly, roughly 2015-2018 depending on the name -
`docs/decisions/0008`), and a lookup whose nearest confirmed reading is
more than 90 days from the requested date is correctly treated as
missing rather than silently matched to a stale value
(`SHARES_LOOKUP_MAX_TOLERANCE_DAYS`, `docs/decisions/0012`). That 90-day
bound was set from a direct measurement of real disclosure gaps (median 1
day, p99 16 days across 200 companies) and a sensitivity sweep against
30-400 day alternatives, not a guess - see `docs/decisions/0012` for the
measurement.

**Measured directly, not assumed: 2016 rebalance coverage is under 1.5%
in every cap bucket - not usable.** 2017-2018 are spotty and
inconsistent (large-cap coverage actually *drops* between those two
years, from 69.6% to 54.4%). 2019 onward is consistently 85%+ across
every cap bucket from day one, climbing further from there (small cap:
85.2% in 2019 to 97.4% by 2025) - `docs/decisions/0012` has the full
year-by-year, bucket-by-bucket table, reconfirmed against the actual
pipeline at the final 90-day tolerance, not just the earlier 180-day
pass. **The real backtest's starting point is 2019, not 2016** - this
isn't a caveat attached to earlier years, those years are excluded
outright.

The coverage gap in the 2016-2018 window is not meaningfully skewed by
sector (a fairly uniform 42-63% exclusion rate across 19 sectors) but
*is* skewed by two other dimensions. By cap bucket, severely: small
cap's 2016 coverage (0.9%) is an order of magnitude worse than large
cap's (13.5%), and since ranking runs within each cap bucket, a
thin-coverage year doesn't produce a smaller sample of the true
small-cap universe - it produces an unrepresentative one. By listing
vintage, moderately: companies listed under 5 years show a ~59%
exclusion rate in this window vs. ~41% for the best-covered 10-20-year
cohort - real and directionally expected, but not extreme (see
`docs/decisions/0012` for the full vintage breakdown). Separately,
expect the *most recent* rebalance in any run to show similarly reduced
coverage regardless of date - `filter_persisted_values` needs confirming
observations on both sides of a reading before accepting it, and the
newest few months of any series don't have enough "after" neighbors yet;
this edge effect is somewhat more visible at the tighter 90-day
tolerance than it was at 180.

### Fundamentals data quality - anomaly detection is a heuristic, not a guarantee

`magicformula.data_fetch.fundamentals.check_fundamentals_anomalies()`
flags companies whose Total Assets/EBIT or Other Liabilities/Capital
Employed ratio deviates sharply from a peer-group median (see
`docs/decisions/0001-fundamentals-current-liability-split.md` for what
this catches and why). The peer group falls back through a three-tier
hierarchy (narrow sector -> broad sector -> cap bucket) when a sector has
too few members - see the same decision doc's follow-up for how that was
validated. This reduces, but does not eliminate, the risk of a distorted
fundamentals record flowing into a rank. Default behavior excludes a
flagged company from ranking entirely (configurable to flag-only for
manual review).
