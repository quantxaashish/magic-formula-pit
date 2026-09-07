# 0008: Cash proxy validation, AAKASH share-swing verification, and
yfinance shares-outstanding reliability across cap buckets

Status: three checks completed against primary sources, plus a widened
Cash check and a built-and-tested outlier filter added afterward. None
of them came back clean, and one (Cash proxy in small cap) got revised
after a wider sample changed the answer.

## 1. Cash proxy validation (real Cash & Equivalents vs the Investments proxy)

**Revised methodology.** The first pass at this check (superseded, see
below) cross-referenced two secondary aggregators (moneycontrol,
stockanalysis.com) and found they disagreed by ~2x on TCS's Cash figure.
That comparison couldn't say which one was right, only that they used
different definitions - a second scraper wouldn't have resolved it either.
So this check was redone against the actual primary source: each
company's own audited financial results, as filed with BSE/NSE under
SEBI LODR Regulation 33 for the quarter/year ended March 31, 2026 - the
literal Ind-AS balance sheet a company's auditor signs off on, not a
vendor's re-classification of it.

**How the filings were located and read**, no manual UI navigation needed:
screener.in's own "Raw PDF" links per quarter resolve through a redirect
chain (`screener.in/company/source/quarter/<internal-id>/3/2026/` ->
BSE's `AnnPdfOpen.aspx` -> the actual filing at
`bseindia.com/xml-data/corpfiling/AttachHis/<guid>.pdf`) straight to the
exchange-hosted PDF. Fetched with `curl`, text-extracted with `pypdf`, and
read the literal "Cash and cash equivalents" line off each company's
**Audited Consolidated Balance Sheet as at 31 March 2026** (matching the
fixtures' existing FY and consolidation basis):

| Company | Literal "Cash and cash equivalents" (Rs. Cr) | Separate "Other/Bank balances" line (Rs. Cr) | Source PDF |
|---|---|---|---|
| TCS | 6,417 | 6,491 ("Other balances with banks") | BSE filing, consolidated BS p.10 |
| Infosys | 22,201 | *(no separate line - this is the full figure)* | BSE filing, consolidated BS p.27 |
| ITC | 643.46 | 2,365.33 ("Bank balances other than (iii) above") | BSE filing, consolidated BS p.12 |
| Asian Paints | 672.83 | 400.85 ("Other Balances with Banks") | BSE filing, consolidated BS p.16 |
| Nestle India | 1,320.57 | 20.30 ("Bank balances other than cash and cash equivalents") | BSE filing, consolidated BS p.10 (figures in the filing are Rs. Million, converted /10) |

**This also explains the earlier moneycontrol/stockanalysis.com
disagreement precisely, for every company, not just approximately.**
moneycontrol's "Cash And Cash Equivalents" turns out to equal literal Cash
+ the separate Other-Bank-Balances line, to the rupee, in all four cases
where that second line exists (e.g. ITC: 643.46 + 2,365.33 = 3,008.79 =
moneycontrol's figure exactly; Asian Paints: 672.83 + 400.85 = 1,073.68 =
moneycontrol's figure exactly). stockanalysis.com's TCS figure (6,417)
matches the literal balance-sheet caption directly. So: moneycontrol
folds two distinct Ind-AS line items into one display row;
stockanalysis.com (at least for TCS) doesn't. Neither vendor was wrong,
exactly as suspected - they draw the aggregation boundary in different
places. This is now resolved with certainty rather than left as a
guess.

**The Investments-bucket proxy is confirmed correct here too**, via the
same primary filings (not just the earlier aggregator cross-check):
summing each filing's non-current + current "Investments" lines
(including "Investment in associates", which both moneycontrol and
screener fold into the same bucket) reproduces screener's `Investments`
figure exactly for all five companies - e.g. ITC: 12,089.55 (non-current
Investments) + 5,288.24 (investment in associates, equity method) +
20,750.64 (current Investments) = 38,128.43 = screener's 38,128. No change
needed there.

**The size and consistency of the Cash gap, using the literal primary-
source figure** (`cash_and_equivalents=0` in the current fixtures/proxy,
vs. the real audited number above):

| Symbol | EBIT | EV (old, cash=0) | EY old % | EV (new, real cash) | EY new % | relative shift |
|---|---|---|---|---|---|---|
| TCS | 66,838 | 810,902 | 8.242 | 804,485 | 8.308 | +0.80% |
| Infosys | 37,378 | 445,876 | 8.383 | 423,675 | 8.822 | **+5.24%** |
| ITC | 25,595 | 295,183 | 8.671 | 294,540 | 8.690 | +0.22% |
| Asian Paints | 5,471 | 239,285 | 2.286 | 238,612 | 2.293 | +0.28% |
| Nestle India | 4,562 | 271,902 | 1.678 | 270,581 | 1.686 | +0.49% |

**Verdict: small and consistent for 4 of 5, and the one exception is
economically explainable, not noise.** Four companies move by
0.2-0.8% relative - genuinely small, in the same direction (as it must
be, since the old proxy's `cash=0` can only ever overstate EV), and
bounded. Infosys is the outlier at +5.24%, but that's not an
unpredictable data-quality problem the way Other Liabilities was - it's
because Infosys, verified from its own primary filing, really does carry
~5% of its EV in literal cash, a directly observable and sensible fact
about the company (large IT services firm mid-buyback, per note 6 in its
own filing), not an artifact of which vendor's definition got used. The
rank consequence is real: **ITC and Infosys swap #1/#2** on EY once real
cash replaces the zero. TCS, Asian Paints, and Nestle India's relative
order is unaffected.

This does **not** clear the same bar Capital Employed/Other Liabilities
failed to clear. That case was disqualifying because the *direction and
size* of the Other Liabilities gap was unpredictable company-to-company -
there was no way to know how wrong any given number was. Here, the bias
is monotonic (always understates EY), small for ordinary companies, and
its one large exception is fully explained by an observable, real
company characteristic (how much cash that company actually holds) rather
than by inconsistent proxy behavior. That is a **known, bounded proxy
bias**, not a broken input.

**Recommendation: document and move on, not a backtest.py blocker.**
Record `cash_and_equivalents=0` as a known, monotonic, EY-*understating*
approximation whose error scales with a company's cash-to-EV ratio -
material (~5%) for cash-rich companies like Infosys, small (<1%) for most
others - rather than replacing it before the first real-data backtest
run. screener.in's free tier has no Cash field at all (confirmed
previously), and a real per-company fix means either its paid tier or
building a BSE-filing PDF parser at ~2,000-company scale - real effort,
each filing has a different table layout (compare the extraction above:
TCS/ITC/Asian Paints/Nestle each needed separately-read tables), not a
uniform machine-readable format.

**This recommendation was checked against a wider sample before being
trusted - see part 1b immediately below, which changes it for small cap
specifically.**

## 1b. Widening the check beyond large cap: does the bias stay small in mid/small cap?

The five ROCE fixture companies are all large cap. Before trusting
"document and move on" more broadly, checked whether the Cash-proxy bias
stays bounded outside large cap, where cash-to-EV ratios and the size of
EV itself behave very differently.

**Method.** Same primary-source method as part 1 (screener.in's Raw PDF
redirect -> BSE filing -> `pypdf`/`pdfplumber` extraction of the literal
"Cash and cash equivalents" line), applied to 10 more companies drawn by
random sample from AMFI's own Jun-2026 categorization (not hand-picked):
5 mid cap (Max Healthcare, Dalmia Bharat, AIA Engineering, Voltas, APL
Apollo) and 5 small cap (Nelcast, TIL, Xpro India, Excel Industries,
Orient Ceratech - TIL was dropped from the EY comparison, not because its
Cash data was bad, but because its own EBIT is negative (-1 Cr) and it
would be excluded from ranking entirely regardless of the Cash proxy).
The same Investments-bucket reconciliation from part 1 was checked for
every company here too and held exactly (e.g. Excel Industries:
999.38 Cr non-current + 126.51 Cr current = 1,125.89 Cr vs screener's
1,126 - matches to the rupee, as it did for all 5 original companies).

**Two things needed root-causing before the table below could be
trusted, not just folded in.**

*MAX Healthcare's apparent Investments mismatch - false alarm, my own
extraction error.* First pass through this company's filing (via
`pypdf`'s flat text extraction, which discards column structure and
requires manually counting positions to re-align labels to values)
mis-read the non-current "Loans" line (Rs. 74,645 Lakh) as "Investments,"
producing an apparent ~746 Cr vs. screener's 5 Cr - a mismatch large
enough to look like a real definitional or extraction problem, the same
shape as the earlier moneycontrol/stockanalysis Cash disagreement.
Re-extracted with `pdfplumber`'s table-aware parsing, which preserves the
label/value column correspondence instead of requiring manual recounting:
the real "(i) Investments" line is Rs. 546 Lakh = 5.46 Cr, matching
screener's 5.0 Cr closely. Not a screener bug, not a standalone/
consolidated mismatch (both readings came from the same page, explicitly
headed "CONSOLIDATED STATEMENT OF ASSETS AND LIABILITIES"), not a
genuine gap at all - a transcription error from using a text-extraction
method that doesn't preserve table structure. The EY figure reported for
Max Healthcare in the table below was already computed from screener's
correct 5 Cr figure throughout, so this doesn't change any number, only
resolves an apparent red flag. **Practical fix for future extractions:
use `pdfplumber`'s `extract_tables()`, not flat `pypdf` text, whenever a
filing's PDF is digitally typeset** - it removes this entire class of
error.

*ARVSMART and MMP's zero-candidate misses - a real, root-caused gap:
scanned-image filings, not missing or wrong documents.* Checked directly
(not assumed) by counting embedded images per page: both PDFs have real,
extractable text only on their first 1-2 pages (a covering letter), then
zero extractable text and exactly one large embedded image on every
subsequent page - rendered and visually inspected one such page (Arvind
SmartSpaces' actual Q4 FY26 results statement) to confirm it's a genuine,
legible, correctly-matched filing, just scanned rather than digitally
typeset. A third case, SRM Contractors, showed a related but distinct
pattern worth flagging separately: most pages are digitally typeset
(real extractable text), but the specific Balance Sheet page is embedded
as a scanned image with a tiled watermark texture (rendering as ~100+
tiny images on that one page) even though the surrounding pages are not
- visually confirmed this is SRM's real, legible standalone balance
sheet, likely scanned specifically because it carries the auditor's
physical signature/stamp. Neither pattern is a coverage gap in the sense
of "no primary source reachable" - the screener -> BSE redirect chain
found the correct filing in both cases. It's a gap in *this extraction
method* (text-based PDF parsing) specifically: recovering these numbers
would need OCR, not just a different text parser, and wasn't pursued
further here per the time-box.

**Rate at full-universe scale.** Across the 15 mid/small-cap candidates
attempted in this widened check (10 that succeeded, plus JARO - failed
earlier, at the screener-parsing stage, likely too recent an IPO to have
a full annual column yet - and TIINDIA, dropped for an ambiguous
consolidated/standalone page match under time pressure), **3 of 15
(20%) hit the scanned-image extraction gap** (ARVSMART, MMP, SRM). That's
a small, not-fully-random sample, so treat 20% as a rough order of
magnitude, not a precise rate - but it's a real, recurring friction
point specific to smaller/less digitally-mature filers, not a one-off.
Anyone leaning on this primary-source-verification approach beyond a
spot-check should expect roughly one in five mid/small-cap filings to
need OCR rather than being reachable through direct PDF text extraction.

| Symbol | bucket | EY old % | EY new % | relative shift | cash / EV(old) |
|---|---|---|---|---|---|
| Dalmia Bharat | mid | 5.136 | 5.169 | +0.63% | 0.63% |
| AIA Engineering | mid | 3.276 | 3.297 | +0.62% | 0.61% |
| Max Healthcare | mid | 1.798 | 1.810 | +0.67% | 0.67% |
| APL Apollo | mid | 2.541 | 2.567 | +1.05% | 1.04% |
| Voltas | mid | 1.169 | 1.194 | +2.09% | 2.04% |
| Orient Ceratech | small | 5.263 | 5.337 | +1.41% | 1.39% |
| Xpro India | small | 0.568 | 0.584 | +2.80% | 2.72% |
| Nelcast | small | 6.785 | 7.284 | **+7.35%** | 6.85% |
| Excel Industries | small | 48.993 | 53.710 | **+9.63%** | 8.78% |

**Mid cap: the bias stays small, consistent with large cap.** All five
mid-cap shifts land in a 0.6%-2.1% relative range - the same order of
magnitude as the large-cap check (0.2%-5.2%), no escalation.

**Small cap: meaningfully worse, and not by chance - the mechanism is
structural.** Two of the four small caps checked (Nelcast, Excel
Industries) show relative shifts of 7.35% and 9.63% - larger than the
worst large-cap case (Infosys, 5.24%). The reason is directly visible in
the "cash / EV(old)" column: both companies already have a small
*residual* EV after netting off their own large `Investments` balances
against market cap (Excel Industries' EV is just 149 Cr against a
1,267 Cr market cap - most of it is offset by its own 1,126 Cr
Investments bucket). Once EV is already thin, *any* omitted Cash term is
a much larger fraction of it - the same absolute Cash figure that would
be immaterial against a large company's EV becomes a double-digit
percentage swing against a small company's thin one. This is a
predictable, structural property of small/cash-rich companies, not
company-specific noise the way THOMASCOTT's data corruption was (0008
part 4) - it will recur for any small-cap value stock with a large
Investments-to-market-cap ratio, which is exactly the profile Magic
Formula's low-EV, high-quality screen tends to surface.

No rank flips occurred *within this specific 9-company sample* (the EY
gaps between neighbors happened to be wide enough to absorb even the
9.63% shift), but that's a property of this small illustrative sample,
not a general guarantee - in the full small-cap universe, where many
companies cluster closer together in EY, a shift of this size is easily
large enough to flip ranks between adjacent names, the same way it did
between ITC and Infosys in the large-cap check.

**Revised recommendation.** The original "document and proceed, fix
later" call stands for large and mid cap. It does **not** extend cleanly
to small cap: the bias there is not just noisier, it is systematically
larger for exactly the kind of cash-rich, low-EV small-cap value stock
this screen is designed to find. Backtest.py can proceed to real EY
ranks for large/mid cap on the documented bias. For the small-cap bucket
specifically, either flag small-cap EY ranks as carrying a materially
larger, known proxy-bias risk than large/mid cap in any output the
ranking produces, or prioritize the real Cash-source fix for small cap
before its ranks are used for anything beyond a first exploratory
backtest run.

## 2. AAKASH's "18x" share-count swing - verified against NSE's own corporate-actions record, not another scraper

**The question:** is AAKASH's ~18x get_shares_full() swing, cited in 0007
as headline evidence that constant-shares is unsafe even in
small/obscure names, real dilution or a parsing/units artifact?

**Checked against NSE's own corporate-actions API directly** (the
regulator-adjacent, primary record of what corporate actions actually
happened - not yfinance, not screener, not any scraped aggregator):
`nseindia.com/api/corporates-corporateActions`, queried for symbol
`AAKASH` across 2015-2026.

- **Mainboard equity segment** (`index=equities`) shows exactly one
  action ever: ex-date **3-Feb-2022**, subject `"Face Value Split
  (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"` - a
  literal 10:1 split, on the exact date yfinance's `.splits` reported one.
- Nothing else showed up in that segment before 2022 - because AAKASH
  wasn't on the mainboard yet. Queried the **SME segment**
  (`index=sme`) for the same symbol and found the earlier action there:
  ex-date **26-Mar-2020**, subject `"BONUS 1:2"` - a bonus issue of 1
  share for every 2 held, i.e. shares x1.5 - again the exact date
  yfinance's `.splits` reported a 1.5:1 event. AAKASH was trading on NSE
  Emerge (SME) until the Feb-2022 face-value split coincided with its
  migration to the mainboard, which also explains the shareholder-count
  jump visible in screener's shareholding pattern around that period.

**Verdict: fully confirmed, independent of any aggregator.** Both
corporate actions are real, dated, NSE-registered events, retrieved
directly from NSE's own corporate-actions database - not inferred from
yfinance, not cross-checked against another scraper. Combined ratio:
1.5 x 10 = **15x**, matching the yfinance-derived stable-baseline
calculation from the original review exactly. The "18x" figure in 0007
is confirmed to be an artifact of `get_shares_full()`'s single-day noise
(a 111,989,000 spike sandwiched between stable 101,250,000 values, per
the earlier check), not a real event - there is no third corporate action
of any kind in NSE's record that would produce it. 0007's underlying
conclusion (constant-shares is unsafe, even for an obscure small-cap)
stands, now on primary-source footing; its illustrative number should
read 15x, not 18x.

## 3. yfinance `get_shares_full()` reliability across cap buckets

**The hypothesis to check** (the user's own prior, stated explicitly):
large cap should be reliable, small cap should be the weak point.

**Method.** Pulled AMFI's official Jun-2026 categorization
(`AverageMarketCapitalization30Jun2026.xlsx` - the same authoritative
source `universe.py` already uses, not a guessed list) and drew a random
sample from each bucket: 7 large cap, 7 mid cap, 10 small cap (including
AAKASH). For each, pulled `get_shares_full()` and measured: whether data
exists at all, the min/max ratio, the fraction of the series sitting at
its single most common ("mode") value, and the count of single-day spikes
sandwiched between mode values on both sides (the same noise signature
found in AAKASH).

| Bucket | Symbol | n | min/max ratio | % at mode | single-day spikes |
|---|---|---|---|---|---|
| large | ASIANPAINT | 1429 | 1.10 | 13.4% | 45 |
| large | BAJAJFINSV | 1607 | 11.44 | 9.6% | 0 |
| large | TMPV | 21 | 2.00 | 23.8% | 1 |
| large | INDUSTOWER | 903 | 1.21 | 5.0% | 18 |
| large | DIVISLAB | 545 | 1.14 | 57.2% | **104** |
| large | KOTAKBANK | 888 | **5.42** | 4.6% | 17 |
| large | TATACONSUM | 1086 | 1.64 | 4.4% | 13 |
| mid | GODREJIND | 1595 | 1.15 | 4.8% | 24 |
| mid | COLPAL | 1632 | 1.18 | 17.3% | 125 |
| mid | NYKAA | 720 | 6.49 | 3.9% | 15 |
| mid | IDFCFIRSTB | 755 | 2.05 | 2.4% | 3 |
| mid | IRCTC | 1169 | 6.09 | 22.3% | 110 |
| mid | NMDC | 1499 | 3.26 | 15.1% | 105 |
| mid | SAIL | 1385 | 1.37 | 30.1% | **182** |
| small | AAKASH | 367 | 18.18 | 45.5% | 93 |
| small | RVTH | 284 | 1.12 | 17.6% | 7 |
| small | INFOBEAN | 517 | 4.34 | 31.9% | 54 |
| small | DREDGECORP | 1659 | 1.19 | 18.6% | 107 |
| small | INDBANK | 1605 | 1.23 | 19.9% | 113 |
| small | TIPSFILMS | 655 | 1.61 | 25.6% | 40 |
| small | ABSLAMC | 818 | 1.11 | 9.5% | 36 |
| small | THOMASCOTT | 1497 | **2258.3** | 2.9% | 15 |
| small | SANWARIA | 1220 | 3.80 | 16.0% | 55 |
| small | ONMOBILE | 1660 | 1.34 | 5.4% | 34 |

**Coverage (does data exist at all): 24/24, no exceptions.** The "small
cap will have sparse/missing data" half of the hypothesis is **not
confirmed** - every ticker in every bucket returned a real, non-empty
series, including thinly-traded small caps and a company (TMPV) that only
demerged and listed in late 2025.

**Reliability (is the data trustworthy at any given point-in-time lookup):
the "large cap is clean" half of the hypothesis is also not confirmed.**
Single-day noise is pervasive across all three buckets, not concentrated
in small cap:
- **DIVISLAB (large cap, pharma, highly liquid) has 104 single-day
  spikes** - more than most of the small caps sampled, including AAKASH.
- **SAIL and NMDC (mid cap, both well-covered PSUs) have 182 and 105
  spikes respectively** - the two noisiest series in the entire sample.
- **KOTAKBANK (large cap, top-20 by market cap) shows a 5.42x min/max
  ratio.** ~~Almost certainly a bad data point, not a stock split~~ -
  **correction, see part 4 below: this one is wrong.** KOTAKBANK did a
  real 5:1 split on 2026-01-14, confirmed via `.splits`; the 5.42x is
  correctly capturing a real event, not noise. This was an assumption
  made from the ratio alone, without checking `.splits` first - exactly
  the mistake part 4's investigation was set up to catch before any
  filter got built on top of it.
- **THOMASCOTT (small cap) shows a 2258x min/max ratio** - obviously
  corrupted/garbage data for at least one day, an extreme case but not an
  isolated one in kind.
- The best (lowest-noise) series in the whole sample, RVTH (small cap, 7
  spikes) and IDFCFIRSTB (mid cap, 3 spikes), are both *not* large cap.

**Verdict: reject the hypothesis as stated.** Coverage is universal
regardless of cap bucket. Noise/spike rate does not correlate cleanly
with cap bucket either - it looks more like a per-company/per-data-vendor
quirk (possibly provisional custodian/DP-level filings that get corrected)
than a liquidity-driven effect. Large cap is not a safe assumption of
"clean," and small cap is not uniquely bad.

**Design implication for `compute_point_in_time_market_cap` wiring.**
`nearest_value_lookup` (`src/magicformula/data_fetch/prices.py`) as built
does a plain "most recent value on-or-before target date" lookup with no
outlier handling. Given the above, that is not safe to wire to a live
`get_shares_full()` source as-is: a rebalance date landing on one of these
single-day spikes (up to 2258x in the worst case found here) would
silently produce a wildly wrong point-in-time market cap for that
stock at that rebalance, with no signal that anything went wrong. See
part 4 for the root-cause investigation this needed before any filter got
built, and the resulting design.

## 4. Outlier filtering: root cause first, then design, then tests

Before writing any filtering code: understand what's actually being
filtered. A filter built on the ratio/spike-count numbers alone (part 3)
would have been built on a wrong premise, as the KOTAKBANK correction
above shows directly.

**Method.** Pulled the full date-by-date `get_shares_full()` series (not
just summary statistics) for the worst offenders from part 3 -
THOMASCOTT (2258x), SAIL (182 spikes), KOTAKBANK (5.42x), BAJAJFINSV
(11.44x), DIVISLAB (104 spikes), AAKASH (18.18x) - and checked each
against `.splits` restricted to the exact window between that series' own
min-date and max-date (not the ticker's full history, which for names
like ASIANPAINT or KOTAKBANK stretches back to splits from the 1990s-2000s
that predate the 2015+ window entirely and would otherwise look like false
corroboration).

| Symbol | observed max/min ratio | real split(s) inside that window | ratio left unexplained after the split |
|---|---|---|---|
| KOTAKBANK | 5.42x | 5:1 (2026-01-14) | 1.08x |
| NMDC | 3.26x | 3:1 (2024-12-27) | 1.09x |
| NYKAA | 6.49x | 6:1 | 1.08x |
| AAKASH | 18.18x | 15x (2020 bonus 1:2 + 2022 face-value split 10:1) | 1.21x |
| IRCTC | 6.09x | 5:1 (2021-10-28) | 1.22x |
| SANWARIA | 3.80x | 2:1 | 1.90x |
| BAJAJFINSV | 11.44x | 5:1 (2022-09-13) | **2.29x** |
| ASIANPAINT, INDUSTOWER, TATACONSUM, GODREJIND, COLPAL, IDFCFIRSTB, SAIL, RVTH, INFOBEAN, DREDGECORP, INDBANK, TIPSFILMS, ABSLAMC, THOMASCOTT, ONMOBILE | 1.10x-2258x | **none in window** | entire ratio |

**Two distinct, independent phenomena, not one:**

1. **Real corporate actions, correctly captured.** Where a split exists in
   the window, `get_shares_full()` reflects it faithfully as a genuine,
   sustained level change. Confirmed for KOTAKBANK, NMDC, NYKAA, IRCTC,
   AAKASH, BAJAJFINSV, SANWARIA. This settles the earlier KOTAKBANK
   misreading above.
2. **Isolated garbage readings, unrelated to any real event, present
   whether or not a real split also happened in the same window.**
   Manually inspected the actual date-by-date values around the extreme
   points for THOMASCOTT, SAIL, DIVISLAB, and BAJAJFINSV's un-explained
   residual, and found the *same signature* every time: a value wildly
   different from its neighbors, on one calendar date (occasionally two,
   see THOMASCOTT below), with the reading immediately before and
   immediately after both back at the normal level. For example,
   THOMASCOTT's 2258x max: baseline ~11,295,200 for weeks, then on
   2024-09-17 two same-day readings of 3,826,259,968 and 3,827,820,032,
   then back to ~11,432,900 the very next day - not a sustained change,
   a two-entry glitch. SAIL's max (5,159,660,032 on 2024-06-05, ~25%
   above its ~4,130,530,048 baseline) shows the identical pattern: one
   day's outlier bracketed by matching normal readings on both sides. No
   consistent unit-conversion factor (not a clean x10, x100, or similar)
   explains these - they look like genuine data corruption somewhere in
   yfinance's pipeline, not a systematic, correctable units bug.

**This directly determines what a filter can and can't use as its
signal.** A large jump alone doesn't distinguish a real split from noise
- both produce large ratios, as the table above shows (KOTAKBANK's 5.42x
real split and THOMASCOTT's 2258x garbage value are the same *kind* of
event by that measure alone). What differs is **persistence**: a real
change is corroborated by many subsequent (and often preceding, once
established) readings at the new level; a garbage reading is not
corroborated by anything before or after it.

### Design: a persistence/confirmation filter, not a magnitude threshold

Implemented as `filter_persisted_values()` in
`src/magicformula/data_fetch/prices.py`. For each raw observation, look
at up to `confirmation_window` (default 3) neighboring raw observations
on *either* side and count how many fall within `tolerance_pct` (default
2%) of it. Keep the observation only if at least `min_confirmations`
(default 2) of those neighbors agree - from either side, so a reading
right after a real split is confirmed by what follows even though
everything before it legitimately disagrees. A reading with no
corroboration from either direction is dropped.

This is deliberately a *persistence* check, not a *magnitude* check:
- A real split's post-change readings are confirmed by the many
  subsequent observations at the same new level (this is why KOTAKBANK's
  actual post-split value is correctly **kept**, tested directly against
  its real series below).
- A single isolated garbage reading has no corroborating neighbor on
  either side and is **dropped**, regardless of how large or small the
  deviation is.
- A two-entry glitch (THOMASCOTT's actual pattern, two close-but-wrong
  values on adjacent readings) could in principle "confirm" each other -
  tested explicitly (see tests below) and confirmed this still gets
  rejected, because two mutually-agreeing bad readings still don't reach
  `min_confirmations` once real neighbors on both outer sides disagree
  with both of them.
- Small, sub-2% day-to-day jitter (SAIL's ongoing ~1% wobble, present
  even outside its one big spike) stays within `tolerance_pct` and is
  accepted rather than treated as an anomaly - it's immaterial for
  market-cap/EV purposes and not worth the false-positive risk of a
  tighter threshold.

**What happens to a dropped value: discarded, not interpolated, and the
company is not excluded.** A rejected observation is simply removed from
the returned series. A caller using the existing `nearest_value_lookup`
on the cleaned result then falls back to the nearest earlier *confirmed*
reading - exactly as if the noisy observation had never been recorded.
Two alternatives were considered and rejected:
- **Synthesizing an interpolated value** (e.g. averaging the neighbors)
  was rejected because it fabricates a number that was never actually
  observed, when the existing, real, nearest-confirmed value is already
  available and - since real share-count changes are infrequent step
  functions, not continuous drift - very likely close to correct anyway.
- **Excluding the company from that rebalance** was rejected because
  part 3 found noise rate doesn't correlate with any property of the
  company (cap bucket, liquidity) that would make exclusion a principled
  filter - it would silently bias the backtest toward whichever companies
  happen to have cleaner `get_shares_full()` data for reasons unrelated
  to investability, which is worse than just falling back to a known-good
  earlier value.

**Tests** (`tests/test_prices.py`), built as synthetic scenarios that
reproduce each real pattern found above, not just checked for crashing:
- `test_filter_persisted_values_drops_isolated_single_day_spike` -
  AAKASH's exact pattern (stable baseline, one wrong day, reverts).
- `test_filter_persisted_values_drops_two_day_spike_that_does_not_persist`
  - THOMASCOTT's harder case: two adjacent days share a similar wrong
    value (so they'd mutually "confirm" each other once) but neither
    persists - both must still be rejected.
- `test_filter_persisted_values_keeps_genuine_sustained_split` -
  KOTAKBANK's pattern: a real step-change that persists for every
  subsequent reading must be kept, including the transition reading
  itself.
- `test_filter_persisted_values_keeps_small_jitter_within_tolerance` -
  SAIL's ~1% wobble must not be flagged.
- `test_filter_persisted_values_composes_with_nearest_value_lookup` -
  confirms the intended end-to-end usage: a lookup dated on a dropped
  spike falls back to the last confirmed value via the existing
  `nearest_value_lookup`, unmodified.

**Validated against the real series, not just synthetic constructions**,
before considering this done: ran `filter_persisted_values` on the actual
`get_shares_full()` output for all four diagnosed tickers.

| Symbol | raw observations | kept | dropped | known spike/split date | result |
|---|---|---|---|---|---|
| AAKASH | 239 | 207 | 32 (13.4%) | 2022-11-02 (111,989,000 spike) | dropped; lookup returns 101,250,000 |
| THOMASCOTT | 1322 | 937 | 385 (29.1%) | 2024-09-17 (3,827,820,032 spike) | dropped; lookup returns 11,295,200 |
| SAIL | 1246 | 1191 | 55 (4.4%) | 2024-06-05 (5,159,660,032 spike) | dropped; lookup returns 4,130,530,048 |
| KOTAKBANK | 761 | 749 | 12 (1.6%) | 2026-01-14 real 5:1 split | **kept** - post-split value (9,947,656,028) preserved |

Exactly the intended behavior in all four cases: real noise dropped with
a safe fallback, the real split preserved. THOMASCOTT's 29% drop rate is
a striking number in its own right - almost a third of its raw
`get_shares_full()` history is unusable noise for this name specifically.

**This is the gate on live wiring, not a nice-to-have.**
`compute_point_in_time_market_cap` should not be wired to a live
`get_shares_full()` source until callers run new data through
`filter_persisted_values()` first. It's a data-cleaning pass ahead of the
existing `nearest_value_lookup`, not a change to that function's
contract - no other code needs to change.

## What this changes for backtest.py

Of the two explicit gating conditions, one clears with documentation, one
does not:

1. **Cash proxy**: clears for large and mid cap, does not clear as-is for
   small cap. Verified against primary-source audited filings (not
   aggregators, and not just the original 5 large caps - widened to 5 mid
   + 5 small cap in part 1b) that `cash_and_equivalents=0` is a small
   (<2.1% relative EY shift), monotonic, well-understood bias across
   large and mid cap. In small cap, two of four companies checked showed
   7.35% and 9.63% relative shifts - larger than any large-cap case - for
   a structural reason (thin residual EV after netting large Investments
   balances) that will recur for exactly the cash-rich, low-EV small-cap
   profile this screen targets, not a one-off. The Investments half of
   the proxy needed no change anywhere (re-confirmed against primary
   filings for all 15 companies checked across both passes). Real fix (a
   proper Cash data source) does not need to block large/mid-cap
   backtest runs; it should be prioritized before small-cap EY ranks are
   used for anything beyond a first exploratory run.
2. **Point-in-time shares**: gate cleared. `get_shares_full()` has full
   coverage across cap buckets (good news, simpler than feared). The
   pervasive noise found in part 3 turned out to be two different things
   (part 4): real, correctly-captured corporate actions in some cases
   (KOTAKBANK's ratio was wrongly flagged as bad data in part 3 - it
   isn't), and genuine isolated data-corruption spikes in others, up to
   2258x, unrelated to cap bucket or to whether a real split also
   happened. `filter_persisted_values()` is built, and tested against
   both synthetic scenarios and the real diagnosed series (AAKASH,
   THOMASCOTT, SAIL, KOTAKBANK) - noise dropped with a safe fallback to
   the last confirmed value, real splits preserved.

Backtest.py can proceed to real EY ranks for large and mid cap with the
Cash proxy documented as-is, and `compute_point_in_time_market_cap` can
be wired to a live `get_shares_full()` source provided callers run the
series through `filter_persisted_values()` first. Small-cap EY ranks
should carry the bias caveat from part 1b until a real Cash source is
built for that bucket.
