# 0013: Survivorship bias confirmed and quantified; transaction-cost and exclude-mode checks close out section 7

Status: survivorship bias confirmed real and documented prominently (not
fixed - no reliably obtainable point-in-time listing source found within
reasonable effort); transaction cost confirmed genuinely net; exclude-mode
compared side by side against flag-only on real data.

## Survivorship bias: real, structural, not previously checked

**The question**: does `magicformula.universe`'s NSE/BSE pull include
companies that were delisted, went bankrupt, or were acquired between
2019 and 2026, or only companies currently listed?

**Checked the actual source, not inferred it**:
`NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"`
is NSE's present-day master list. `BSE_ACTIVE_EQUITY_API_URL` fetches from
BSE's `ListofScripData` endpoint - **active** scrips, by name. Neither is
a versioned, point-in-time historical archive. A company delisted at any
point before "today" (whenever `build_universe()` runs) is structurally
absent from the data, for every rebalance date a backtest ever computes,
past or present.

**Confirmed against nine real companies.** Checked NSE's current
`EQUITY_L.csv` directly for symbols of companies that were delisted, went
through NCLT insolvency, or had their equity extinguished during
2019-2026:

| Symbol | Company | What happened | In current universe |
|---|---|---|---|
| DHFL | Dewan Housing Finance | Insolvency, equity extinguished 2021 | Absent |
| RELCAPITAL | Reliance Capital | Insolvency, equity extinguished 2024 | Absent |
| FRETAIL | Future Retail | Insolvency, delisted 2023-24 | Absent |
| FLIFESTYLE | Future Lifestyle Fashions | Insolvency, delisted | Absent |
| FCONSUMER | Future Consumer | Insolvency, delisted | Absent |
| JETAIRWAYS | Jet Airways | Grounded 2019, insolvency | Absent |
| ITNL | IL&FS Transportation Networks | Insolvency-linked distress | Absent |
| RCOM | Reliance Communications | Distressed, **still listed** (control) | Present |
| RPOWER | Reliance Power | Fell out of major indices, **still listed** (control) | Present |
| YESBANK | Yes Bank | 2020 crisis, **still listed** (control) | Present |

All seven genuinely delisted/extinguished names are absent. All three
control cases - companies with real, well-known crises that nonetheless
stayed listed - are correctly present. This isolates the effect cleanly:
it's specifically about delisting, not a general data-quality gap (a
tenth candidate, IBULHSGFIN, initially looked like an eighth absence but
turned out to be a rename to SAMMAANCAP, present under the new ticker -
corrected before counting it, not left in to pad the case).

**Rough scale.** A full count would require cross-referencing NCLT
resolutions, voluntary delistings/buyouts, BSE's own list, and mergers
against historical fundamentals eligibility - not pursued; no reliably
obtainable point-in-time NSE/BSE listing snapshot was found within
reasonable effort this session, and manufacturing one from scratch is out
of proportion to what this check needs. What is directly checkable: NSE's
own published "Orders of Delisting Committee" record **at least 57
companies compulsorily delisted within 2019-06-01 to 2026-06-01** on the
NSE channel alone (read directly off
`nseindia.com/static/list/orders-of-delisting`). This is a lower bound -
it excludes BSE's separate list, voluntary delistings, mergers, and
critically the NCLT-insolvency-resolution channel where the largest names
above actually sit (a different process from Delisting-Committee
compulsory orders). Most names on that NSE list are small/dormant
companies unlikely to have ever had real fundamentals data; the
Magic-Formula-relevant count sits somewhere between the 7 hand-confirmed
cases and 57 - real and non-trivial either way.

**A related, same-direction effect found while checking this**:
`compute_period_return` (backtest.py) silently drops a holding's weight
from a period's return if its price is missing at the period's end date,
rather than treating the gap as a loss - defensible in isolation (a data
gap isn't necessarily a wipeout), but it means a stock bought into a
basket that *then* fails mid-holding-period also isn't counted as the
loss it was. Compounds the universe-level bias in the same direction.

**Why this matters for every number already reported**: the +29.4% CAGR,
0.71 Sharpe, -10.3% max drawdown, and especially the 100% hit rate against
Nifty 500 TRI across all 7 periods are all computed against a universe
that can never contain a name that actually went to zero. The 2019-2020
COVID-crash period - read elsewhere as evidence of stock-selection alpha
protecting the basket during a stress period - is exactly when DHFL, Jet
Airways, and (later) Reliance Capital were failing; a screen that cannot
select a name that fails is mechanically going to look more crash-
resilient than one that can. The margin over the cap-tilt-matched blend
(section on that below) is large enough that survivorship bias probably
doesn't explain all of the outperformance, but the exact size of the
margin - and the unusually clean 100% hit rate specifically - should be
treated as inflated by an unknown, likely non-trivial amount. Documented
as the first, most prominent Known Limitation in README.md, not folded in
alongside the others.

## Transaction cost: confirmed genuinely net, not silently dropped

Reran the exact same 2019-2026 walk with `transaction_cost_bps=0` and
compared directly against the reported `transaction_cost_bps=25` run:

| | Gross (0bps) | Net (25bps, as reported) |
|---|---|---|
| CAGR | +29.32% | +29.20% |
| Final equity | 6.05x | 6.01x |

The cost is real and correctly subtracted every period (gross is always
higher than net, by an amount consistent with ~44% mean annual turnover
x 25bps), not double-counted or silently omitted. The previously-reported
CAGR is the net figure.

## Flag-only vs. exclude-mode: run side by side, not decided on judgment

With the per-rebalance anomaly bug fixed (decision 0013's sibling
finding, same session - see the fix in `run_expanded_backtest_pilot.py`),
reran the full 2019-2026 walk twice, identical rebalance dates, identical
Nifty 500 TRI benchmark, identical 25bps cost - the only difference is
whether a company this rebalance date's fresh, point-in-time anomaly
check flags is kept (`flag_only`) or dropped from the eligible pool
before ranking (`exclude`).

| | flag_only | exclude |
|---|---|---|
| CAGR | +29.20% | +28.86% |
| Annualized volatility | 42.25% | 42.01% |
| Sharpe ratio | 0.702 | 0.695 |
| Max drawdown | -10.32% | **-9.25%** |
| Hit rate vs Nifty 500 | 100% | 100% |
| Mean turnover | 44.37% | 44.10% |

Exclude-mode's max drawdown is about 1 point better; its CAGR and Sharpe
are both very slightly *worse*, not better. This doesn't cleanly satisfy
either branch of "comparably or better with lower risk" (Sharpe moved the
wrong way) or "barely changes anything" (a full point of drawdown isn't
nothing) - it's a genuine, small trade-off in different directions on
different metrics, not a decisive result either way. Left as the user's
call with the real numbers in hand, not decided here.

Each rebalance date's flagged-and-excluded count (88-132 out of a
several-thousand-company eligible pool, roughly 1-2%) is in a sane range,
consistent with decision 0010's calibration goal - not the ~51%
flat-threshold-driven flood the pre-0010 approach produced.
