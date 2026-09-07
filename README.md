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
