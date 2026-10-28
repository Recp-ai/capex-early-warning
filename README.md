# Capex Early Warning

A weekly early-warning dashboard for the AI capex cycle. Every Saturday, and the morning after big
scheduled events (FOMC, CPI, AI-related earnings), a GitHub Actions job pulls public data, scores six
warning categories 0–3, asks Claude to research the news and write a short digest, checks the sources
it cites, emails you the result, and updates a dashboard you can open from any device.

The idea: a hyperscaler cutting capex on an earnings call is the *last* domino. Credit markets, AI-lab
financing, Taiwan server-maker revenue, filing footnotes, supplier commentary and physical build-out
data usually move first. This tracks all six in one place.

> Research tool, not investment advice. Leading indicators give false alarms; act on several
> categories confirming together, not one data point.

## The six categories

| Category | Source | How it's scored |
|---|---|---|
| Credit markets | FRED high-yield and investment-grade spreads, plus Claude news adjustment (±1); Baa spread for the contagion stage (context only) | Rule-based |
| AI-lab financing | Claude web research (funding rounds, compute contracts, neocloud financing) | Claude, cited |
| Taiwan supply chain | TWSE monthly revenue: TSMC, Hon Hai, Quanta, Wistron, Wiwynn, Accton, Gigabyte | Rule-based |
| Hyperscaler filings | SEC EDGAR XBRL: capex, operating cash flow, new lease assets for MSFT, GOOGL, AMZN, META, ORCL; 10-Q/10-K text: leases signed but not yet commenced | Rule-based |
| Supplier signals | Claude web research (orders, backlog, earnings-call wording, insider selling), plus the hyperscaler earnings-release comparison on earnings days | Claude, cited |
| Physical build-out | Claude web research (cancellations, grid-connection withdrawals, GPU rental prices) | Claude, cited |

Macro context (CPI, core PCE, Fed funds, 2y/10y yields, Brent, VIX) and the S&P 500 / Nasdaq-100
levels are logged alongside. The composite is the weighted sum (max 18): **Green** < 5, **Amber** ≥ 5,
**Red** ≥ 9, and any single category at 3 triggers an alert. Levels have hysteresis: once Amber, a run
stays Amber until the composite falls below 4 (Red holds down to 8), so a score bouncing between 4 and 5
doesn't flip the level or send event emails. Red shows as **confirmed** only once two or
more categories have scored 2+ in two consecutive runs (the same categories both times); until then it
shows as *unconfirmed*. A category that rose 2+ points since the previous run is flagged "worth a check".
All thresholds live in [`config/signals.yaml`](config/signals.yaml) — edit them there, no code changes needed.

Numbers always come from the official data via plain code. Claude only interprets them, scores the
three news categories, and must cite the sources it found. Where Claude reads numbers out of filing text
(lease commitments, capex guidance), a figure counts only if its sentence is found word for word in the
SEC filing and the amount is in that sentence; the value is then parsed by code.

## Version 2: event days, verified sources, every run kept

**Every run is kept.** Each run's full report is saved as `data/runs/<UTC timestamp>.json` (never
overwritten) and gets one row in `data/runs.csv`: timestamp, trigger (`schedule`, `manual` or
`event:<name>`), both models, tokens, web searches, cost, composite, level and the six scores.
`data/log.csv` stays one row per day for the running log. The dashboard's **Runs** table lists the last
20 runs so you can compare models side by side, with the month-to-date API spend.

**Event-day runs.** [`config/events.yaml`](config/events.yaml) lists dated events through the end of
2026: FOMC decisions, CPI releases, and earnings for MSFT, GOOGL, META, AMZN, ORCL, NVDA, AMD, AVGO and
TSMC, each marked `verified` or not (see the file header for what counts). A daily check at 23:30 UTC
(07:30 UTC+8) runs the full pipeline only if a listed event fell on the US Eastern calendar day that is
ending, and skips if a run already happened that day. The run is tagged `event:<name>` (several names
are joined with `+`), the news research focuses on the event, and the email goes out only if the
composite level changed or a category moved 2+ points versus the previous run. To re-run an event by
hand: Run workflow → `event` = e.g. `MSFT-earnings`. Add the 2027 dates before the year ends (the run
log warns 30 days ahead).

**Earnings press releases (free, from EDGAR).** On a hyperscaler earnings event the pipeline finds the
latest 8-K with Item 2.02 in `data.sec.gov/submissions/CIK##########.json`, fetches its Exhibit 99.1,
has the assessment model pull out capex guidance (amounts plus exact sentences), and counts phrases
such as *pacing, digestion, discipline, optimize, capacity constrained, fungible* (regex counts, listed in
`signals.yaml`). The result is stored in `data/calls/<TICKER>/<quarter>.json`, compared with the previous
quarter's release, and added as evidence to the hyperscaler filings and supplier signals categories.
The first time, the previous quarter's release is read too, so there is a comparison straight away.

**Leases signed but not yet started.** Each run checks EDGAR for a new 10-Q/10-K, finds the paragraphs
containing "not yet commenced" (and Oracle's wording, "additional lease commitments"; the list is
`filing_text.lease_phrases`), and has Claude extract the dollar amounts into
`data/edgar/<TICKER>_leases.json` with the source sentence for each (once per filing). Table rows such
as Amazon's are scaled by the table's "(in millions)" heading, found by code. Leases signed after the
quarter end are recorded but left out of the total. If the aggregate falls 25% or more quarter over
quarter, the filings score gets +1 (`hyperscaler_filings.leases_not_commenced_qoq`).

**Source checking.** After the assessment every evidence URL is fetched once (10-second timeout, an
honest bot user agent, public addresses only) and checked for the claim's key numbers and names. Each
source is marked **verified**, **unverified** (page loaded, claim not found) or **unreachable** (paywall,
block, timeout, PDF). A news category scoring 2 or more with fewer than 2 verified-or-unreachable
sources is capped at 1, and its reasons say so. The dashboard shows a badge on every source.

## Version 3: follow-ups, stronger sources, steadier scores

**Follow-ups.** Each run's "watch next" list becomes the next run's follow-ups. The research step is
asked to search for each one, and the assessment gives each a status (*resolved, improved, worsened,
still open, no news*) with a one-sentence note and a source, which is checked like any other evidence.
They appear as **Follow-ups from last run** above **Watch next** on the dashboard and in the email.
Unresolved items carry forward and are followed up in at most 3 runs, then drop off
(`follow_ups.max_runs`; at most `follow_ups.max_items` = 12 per run). The list and its counts are kept by
code from `data/runs/`; Claude only fills in status, note and URL. The web-search budget is 16 per run
(`max_web_searches`, was 12) so follow-ups don't crowd out the four regular topics.

**Source tiers.** The research step is told to prefer primary sources (filings, IR pages and press
releases, regulators, grid operators) and major outlets (Reuters, Bloomberg, CNBC, FT, WSJ) over
aggregators. Every evidence URL is tagged **primary**, **major** or **other** by code from its domain
(`source_tiers` in `signals.yaml`, editable), shown next to the verification badge. Tiers are labels
only; they don't change scores or caps.

**Steadier scores.** The scoring step runs `assessment_samples` times (default 2) on the same research
notes. Each news score is the average, rounded half down (1.5 → 1), and the spread is recorded; samples
2+ points apart on a category flag it **unstable** on the dashboard and in the email. The second sample
reads the prompt from Claude's prompt cache. Level hysteresis (`composite.level_hysteresis: 1`, above)
stops noise at a boundary from changing the level; event-run emails compare the held level.

**Credit contagion stage** (context only, never scored). From 4-week spread changes (`credit_stage` in
`signals.yaml`: HY widening at +0.40, IG at +0.10, Baa at +0.15, narrowing at the same sizes):
*Calm* (none widening), *Junk only: contained* (HY widening, IG and Baa not), *Spreading* (HY and IG or
Baa widening), *IG only: financing pressure, possibly bond supply* (IG or Baa widening, HY not), *Easing*
(HY and IG both narrowing). The 10-year's 4-week direction is added: rising (±0.10 pts) means rate or
inflation pressure; falling while spreads widen means a growth scare. One line on the Credit card and in
the email, e.g. "Junk only: contained · 10y rising (rate pressure)", plus a `credit_stage` column in
`data/runs.csv` and `data/log.csv` and a Baa spread chart (FRED `BAA10Y`) next to the HY and IG charts.

**Cost.** A weekly run cost about $0.63 for research and scoring before this version. Expect about
$0.90–1.05: up to 4 more searches and the search results they pull in (+$0.15–0.30 on research) and a
second scoring sample (+~$0.10, the shared prompt read from cache). Event runs that read filings add
the same lease and press-release costs as before.

## Version 4: dated evidence

**Event dates.** The research step writes each finding as "claim — event date — URL", and the assessment
gives every evidence item and follow-up an `event_date`: when it happened (deal priced, guidance given,
project cancelled), not when it was reported, as YYYY-MM-DD, or null when the source doesn't say. Code
keeps only real dates no later than tomorrow; press-release evidence from EDGAR uses the 8-K filing date.
The dashboard and the email show the date next to each evidence item and follow-up ("undated" when
unknown), and label items that happened more than 10 days before the run **background**
(`evidence_dates.background_after_days`). The email now lists every category's evidence at the end.
Dates are labels only; they don't change scores or caps.

**Conflicting figures.** When the same metric (a CDS level, a deal size) shows different values in
different sources, the research notes list each figure with its URL, and the assessment gives each as its
own evidence item, reports the range in the summary, digest and follow-up notes, and prefers the
primary- or major-tier figure. The assessment prompt lists the tier of every site in the notes (set by code
from `source_tiers`), and a follow-up's status may not rest on an "other"-tier number alone (it stays
*still open*, with the figure called unconfirmed). This is an instruction to the model, not a code check.

## Stock research (on demand)

A separate module rates stocks on demand: open **Stock research →** on the dashboard (`stocks.html`),
tick tickers in a list (or type one), pick the models and press **Run**. It runs its own workflow
(`.github/workflows/stocks.yml`, manual only, never scheduled), writes its own files, and never changes
how the capex pipeline runs; it only reads the capex dashboard's latest level for the overlay below.

**Lists.** The page's Lists panel keeps your watch lists in one repo Actions variable, `STOCK_LISTS`
(`{version, revision, updated_at, lists: [{id, name, tickers}]}`, compact JSON, at most 30 lists of
100 tickers and 40,000 bytes, well under GitHub's 48 KB per variable; a ticker can be in several lists).
Create, rename, delete (asks first) and reorder lists; add, remove, reorder, copy or move tickers, with
up/down buttons rather than drag and drop. Edits stay in a local draft marked **Unsaved changes** until
**Save changes** (passcode) writes the whole document once; **Discard** reverts. If another tab or
device saved in between, the save is refused and you choose to reload or overwrite. **All rated** shows
every ticker ever run. Nothing is seeded.

**List view.** Each row shows the rating, score and confidence, the last run in your timezone (UTC on
hover) and its age, a **stale** tag past `page.stale_after_days` (14), the close the rating was scored
on, the latest price with its time (e.g. `8.68 · 4:00 pm ET`; a quote timestamped outside the regular
session, 9:30 am–4:00 pm ET on weekdays, adds **pre-market** or **after hours**, here and on the detail
card), the change since the rating, and the number of runs (or "not run yet"). Sort by list order, rating,
score, last run or change; click a row for the detail, which ends with a **History** table of every past
rating (time, rating, score, the six factor scores Growth / Quality / Balance sheet / Valuation / Trend /
News from `ratings.csv`, confidence, price, models, cost). On a phone the table scrolls sideways inside
its card with the time column pinned.

**Batch runs.** Tick rows (or **Select all in this list**); the Run panel shows the count and an
estimate (count × the average cost of the last 10 stock runs) and asks before starting. The workflow
has three jobs: **plan** (runs the stock tests, validates and de-duplicates the tickers, fetches
SPY/QQQ once and shares them as an artifact), **rate** (one job per ticker, `batch.max_parallel` = 5 at
a time, fail-fast off; each uploads its result and never commits) and **collect** (writes the
append-only files, refreshes the latest prices in one request, commits once with pull-rebase retries,
and sends one summary email if email is on; a batch of one gets the full single-stock email). The page
shows each ticker as queued / running / done / failed from the jobs, then reloads the list when the
data is deployed; a failed ticker shows its error (from `site/data/stocks/batch.json`) and never blocks
the others. Limits (`config/stocks.yaml` `batch`, enforced by `site/api/stock.js`): up to 256 tickers per run
(GitHub's limit of 256 jobs in one matrix) and, by default, no daily limit (`max_tickers_per_day` and
`max_rescore_tickers_per_day` are 0; set a number to cap tickers in any 24 hours, full runs and re-scores
counted apart). Tiingo's free tier allows about 50 requests an hour and a batch of n costs n + 3, so the
plan and rate jobs set `STOCK_PRICE_RATE_WAIT_MIN` (75): on HTTP 429 a fetch pauses 5 minutes and retries
until that budget is used, and a large batch simply takes longer. Stock runs share the concurrency group
`stocks`, so two batches never overlap; the capex workflow is unaffected. Once the results are on the
page, the rows' done tags go away; failed tickers keep theirs, and the Run panel keeps the batch summary,
until you dismiss it (×).

**Re-score without new research.** Tick **Re-score only** in the Run panel, or Run workflow with
**reuse_news** ticked (or `python -m stocks.run MSFT --reuse-news`): SEC data and prices are fetched again and every code factor is
re-scored, but the news topic ratings and source checks are the latest run's, as they were (labelled
"news from <time>" on the page and in the email), and the only Claude call is the write-up. The ratings
stay Claude's, but the caps are code, so they are applied again (with the evidence dated as of the news
run): a cap added since, like the date rule below, lowers a reused rating, and the page and email say
which ones changed. Runs from before the direction labels (below) have none: their evidence counts as
pointing the rating's way, so nothing changes, and every non-zero topic resting on unlabelled evidence is
listed as **direction not checked** (`news.direction_unchecked`; page, email, batch email) until a fresh
run labels it. The one check a re-score runs again: archived Form 4 XML items left unverified are re-read
on the normalised text (below; `news.rechecked`), so a rating capped only because they failed gets its
caps re-applied on the new result. The row is
appended as usual with `news_from` set to the run whose news was used (blank for a run's own news). A
re-score costs about $0.03 a ticker (the write-up), so it is titled "Stock research (rescore) <tickers>"
and counted against its own daily cap (`batch.max_rescore_tickers_per_day`, 0 = none), not the full
runs'; the page estimates it from recent re-scores and leaves it out of the full-run estimate.

**Six factors, each 0–100.** Five are computed by code; Claude never sets a number or the rating.

| Factor | Default weight | From |
|---|---|---|
| Growth | 20% | Revenue YoY for the latest quarter and the trailing twelve months (TTM), and acceleration (latest quarter's YoY minus the previous quarter's) |
| Quality | 20% | TTM gross, operating and free-cash-flow margins (FCF ex-prepayments or normalised FCF when that applies, below), stock-based comp as % of revenue, diluted share count YoY |
| Balance sheet | 10% | Net debt / TTM FCF (net cash scores 100; cash runway if FCF is negative), cash + short-term investments / debt |
| Valuation | 20% | P/S, P/FCF, EV/EBIT, FCF yield minus the 10-year Treasury (FRED `DGS10`), and where each stands versus the company's own month-end history over 5 years (ranked on yields, below) |
| Trend | 15% | Price vs 50/200-day averages, 12-1 month momentum, 6-month relative strength vs SPY and QQQ, distance from the 52-week high |
| News & risk | 15% | Claude's research of the last 30 days (max 8 web searches): guidance changes, analyst actions, litigation/regulation, customer concentration, insider activity, product/competitive news |

Fundamentals come from SEC XBRL companyfacts. 10-Q cash-flow figures are year-to-date, so quarters are
derived by differencing as the capex pipeline does, and Q4 is the 10-K year minus nine months. Each
figure keeps the date it was first filed, so the valuation history only uses what was public at each
month end. A quarter (or year) with no diluted share count tagged gets one derived as net income /
diluted EPS (`NetIncomeLoss` / `EarningsPerShareDiluted`, both undimensioned, same period, from the first
filing that gave both; skipped when |EPS| is under $0.10, where cent rounding alone is over 5%), marked
"net income / diluted EPS" (an asterisk in the quarterly table). Alphabet tags its diluted count only from
2023-06-30, first filed in July 2024, so without it its history started in mid-2024. Dilution year on year
still compares reported counts only. Historical share counts are put in today's units using the split
factors in the price data; a count more than 10x off the median of its neighbours on both sides (a filing
that gave 29,452 for 29.5 million) is dropped, with a note (`valuation.share_outlier_x`), derived ones too. Foreign filers (20-F / 40-F, IFRS)
report annually: growth, quality and balance sheet use the annual figures, and valuation is marked
missing (reporting currency and ADR ratios aren't reconciled).

**Balance sheet tags.** Cash, short-term investments (face-statement tags, then the current
available-for-sale / held-to-maturity totals, then cash-and-investments reported as one line) and debt
come from tag lists in `stocks/fundamentals.py`. Debt is read by family: `LongTermDebt` and its parts,
notes (`LongTermNotesPayable`, `NotesPayableCurrent`), convertibles, other long-term debt and loans,
`DebtCurrent` for all current borrowings, `DebtLongtermAndShorttermCombinedAmount` when given; leases are
excluded. A figure is **unknown**, never zero, when a tag of its group stopped reporting (a non-zero value
in the last 4 quarters, 8 for debt, and nothing covering the latest date), when only tags the screen
doesn't read carry a value on the latest balance sheet, or when one such tag is larger than everything the
screen read (Oracle's `LongTermNotesAndLoans` next to its `NotesPayableCurrent`). Then the balance-sheet factor and EV/EBIT count
as missing, with the reason on the page. "No debt" means no debt tag had a value in the last 8 quarters.
Key metrics shows the tags behind each figure on hover; `python -m stocks.fundamentals tags NVDA ORCL`
prints them from the cache, with any debt or investment tags on the latest balance sheet that the
screen doesn't read.

**Margins and prepayments.** Without a `GrossProfit` tag, gross profit is revenue minus cost of revenue
(`CostOfRevenue`, `CostOfGoodsAndServicesSold`, `CostOfServices`, ...); without `OperatingIncomeLoss`,
operating income is revenue minus `CostsAndExpenses`, else gross profit minus `OperatingExpenses`, period
by period. A margin still missing shows as **missing** in the factor's inputs.

Two adjustments decide which FCF quality and valuation are scored on (the P/FCF history uses the same
basis; the reported FCF is always shown, the page and email flag the adjustment, and confidence steps
down; if both apply, the lower FCF is scored):
- **Excess prepayments.** Excess = the TTM increase in contract liabilities, deferred revenue or customer
  advances (from the cash-flow statement, else the balance sheet) minus their balance a year earlier ×
  TTM revenue growth. Deferred revenue that grows with sales (Rubrik, Zscaler) leaves no excess; a jump
  in advances (Oracle) does. When the excess is above 25% of TTM operating cash flow
  (`prepayments.max_share_of_ocf`), **FCF ex-prepayments** = FCF minus the excess. Without a balance a
  year earlier the excess can't be measured and nothing is subtracted.
- **FCF spike guard** (needs no tag). When any of the last 4 quarters has FCF above that quarter's revenue,
  or the TTM FCF margin is more than 2× the one a year earlier and above 30%, FCF is **normalised**: the
  median quarterly FCF margin of the last 8 quarters × TTM revenue, flagged "FCF spike (cause not in tagged
  data)". Innodata (Q2 2026 FCF $124M on $92M of revenue, from customer advances under its own tag)
  triggers it. Thresholds in `fcf_spike`.

**Valuation history on yields.** Each multiple's place in its own history is computed on the yield
behind it (sales, FCF or EBIT over market cap or EV): the share of month-ends that were cheaper than now.
That equals the multiple's percentile when everything is positive, and the few months with near-zero or
negative FCF or EBIT rank as the most expensive instead of producing multiples like 36,569x. The page
still shows today's multiples; median, low and high are the multiples the yields imply ("neg." when there
is none).

**Not comparable.** Against a history of losses that ranking means nothing: every loss-making month counts
as "most expensive", so any company that turned profitable reads "cheaper than every month-end" (Innodata's
P/FCF, negative in 36 of 61 month-ends; GitLab's EV/EBIT, negative in all 55 and today). So P/FCF and
EV/EBIT are compared with their history only when FCF / EBIT is positive now and was negative in at most
`valuation.history_max_negative_share` (20%) of the month-ends. Otherwise the comparison is **not
comparable (negative in 36 of 61 month-ends)**: it leaves the valuation score (its weight is 0 and the
other inputs' weights re-normalise), the cheap / middle / expensive label and the percentile column, and
the write-up gets that phrase instead. P/S is always compared.

**Valuation history in words.** The write-up never sees that percentile (given "cheaper than now in 0% of
61 month-ends", it wrote that NVIDIA "has not been more expensive than now at any of the last 61
month-ends"). Code turns the month-end counts into a phrase per multiple, "cheaper than at 58 of the last
61 month-ends", "more expensive than at 57 of the last 61 month-ends" or "near the middle of the last 61
month-ends (...)", and an overall label, **cheap / middle / expensive** against its own history: cheap
when fewer than 33% of month-ends were cheaper than now, expensive above 67% (`valuation.history_label`),
the overall label from the compared multiples' shares weighted like their sub-scores (not-comparable ones
named after it). The page shows the label under the history table. The absolute measures (the multiples, FCF yield vs the 10-year) go to the write-up
labelled absolute, and the prompt says to use the history phrases as given. A post-check then reads the
note: a clause about the company's own history (history, month-ends, five-year, its usual range) that
calls valuation expensive, rich, stretched or high while the label is cheap, or cheap while it is
expensive, or calls a not-comparable multiple either, gets the write-up regenerated once with the conflict
named; a sentence still in conflict is dropped, and Run details on the page (and the email) say so.

**Write-up precision.** The write-up's input is rounded (the stored data is not): multiples to one decimal
(8.5x), percentages and percentage points to one decimal (12.3%, 4.1 pp), as the page shows them;
dollar amounts and the balance-sheet ratios as stored. Signed inputs go as words plus the size, never a
signed number (`run.SIGNED`): growth levels as a noun ("TTM revenue growth of 12.2%", "a TTM revenue decline
of 3.2%"), changes and gaps with their subject ("growth slowed by 1.1 pp from the previous quarter",
"diluted share count up 5.8% year on year", "FCF yield 3.9 pp below the 10-year Treasury yield", "price
58.3% below its 52-week high"), so the note can't say "slowed by −1.1 pp" or NetApp's "growth of up 12.2%".
Levels such as margins stay numbers. The prompt tells Claude to keep that precision and that
form, and that the key risks and "what would change" are about the company and its stock: caveats about
the screen's own data (reused news, unchecked directions, a short history) are shown elsewhere and stay out.

News & risk is scored on a fixed rubric. Claude rates each topic −2…+2 (±2 = clear, material news from
two credible sources; ±1 = modest or single-source; 0 = nothing material). Every cited URL is dated,
tiered (primary / major / other) and fetched as in the capex pipeline. A company's own sites are primary for
its own stock only (`stocks/domains.py`): SEC's website / investorWebsite (empty for all 14 rated so far),
the curated `company_domains.tickers` in `config/stocks.yaml` (zscaler.com, shopify.com, msgsports.com, ...),
and a strict match on cited sites whose registered domain is the ticker (3+ characters) or the company name's
first word (4+ letters, not generic), except `company_domains.exclude` (shop.com, alphabet.com, madison.com
belong to others). A re-score re-tags the archived evidence the same way. The exception to the capex check
is a raw SEC Form 4 XML file
(sec.gov `.../ownership.xml`, `wk-form4_*.xml`): read as plain text its prices are "219.7200" and its roles a
bare "1", so `stocks/form4.py` reads it raw and checks the claim on a normalised text (trailing decimal
zeros trimmed, in the claim too; prices also rounded to the cent, since a weighted average of 219.7166 is
reported as $219.72; Director / Officer + title / 10% owner from the role flags; sale /
purchase / exercise / tax withholding for codes S / P / M / F; written dates also as YYYY-MM-DD). Only in-window evidence counts toward
the caps: a source that was verified or unreachable and whose event date is no more than
`evidence_dates.background_after_days` (30) before the run. Undated items and older ones (Innodata's
insider sales of Sep 4 and 14, Oracle's Sep 10 results, Palantir's Aug 3 guidance, in an Oct 15 run)
support nothing (both prompts tell Claude to date a Form 4 or other insider item by its transaction date,
which the form always gives, so those aren't left undated). Claude also labels each evidence item's **direction**, positive, negative or neutral
(what that item alone says for the company: a downgrade or a reiterated Sell is negative, a delay is
negative, guidance reiterated unchanged is neutral), a label like the event date, never a score. An item
supports a rating only if it is in-window **and** points the rating's way (positive for +1/+2, negative for
−1/−2; neutral supports neither), and the counts use only those items. A topic's rating can't exceed, in
size, the number of its supporting sources (one: ±1); ±2 needs a verified supporting primary or major
source ("capped: weak sources" otherwise); with no in-window source at all the rating is capped at 0, shown
as **capped: only older evidence**; and with in-window sources that all point the other way (or neither
way) it is capped at 0, shown as **capped: no recent evidence in this direction** (Oracle's guidance +1
whose only in-window item was "Project Jupiter at risk of delay"; Rubrik's analyst actions +1 resting on
"Weiss Ratings reiterated a Sell"). `scoring.cap_all` runs the four caps in that order. Code turns the
ratings into 50 + Σ weight × rating × 25.

The **composite** is the weighted average of the factors that had data, with weights re-normalised when one is
missing. A factor needs at least half of its inputs (by weight) to count. **Data confidence** (stored as
`confidence`) is how complete and clean the inputs are, not the chance the rating is right, as the page's
hover and note and the email say: it starts at High and each issue adds a point (`confidence` in the config): each factor with no
data, missing inputs of a scored factor, unknown cash or debt, FCF ex-prepayments, an FCF spike, a valuation history
under 24 month-ends, a score within 2 points of a band edge, news topics capped for weak sources. 1–2
points is Medium, 3+ Low (so 4–5 factors alone still read Medium and 3 Low, as before). The reasons are
listed under the label on the page and in the email. **Rating:** ≥80 Strong Buy, 65–79 Buy, 45–64 Hold, 30–44 Sell, <30 Strong Sell.
**Capex overlay:** tickers in `overlay.ai_exposed` (NVDA, AMD, AVGO, MRVL, MU, TSM, MSFT, GOOGL, AMZN, META,
ORCL, CRDO, ALAB, ANET, VRT, SMCI, DELL, CRWV) are capped at Hold while the capex dashboard is RED
(confirmed). AMBER, or an unconfirmed RED, only adds a note. The page and email show the overlay
whenever it applies. Everything is in [`config/stocks.yaml`](config/stocks.yaml).

**Also on the page:** the latest earnings release (8-K Item 2.02, Exhibit 99.1). Guidance sentences
count only if found word for word in the release, as with the capex press releases. The period it covers
is the release's first "quarter ended ..." date only when that is 20–120 days before the filing and no
companyfacts quarter end falls in between (Rubrik's Aug 27 release opens with Jan 31, TIC's Aug 6 one with
Dec 31, 2025, Zscaler's May 26 one with Jan 31); otherwise the latest companyfacts quarter end before the
filing (Jul 31, Jun 30, Apr 30), unless companyfacts doesn't have that quarter yet, and else the fiscal
calendar's last quarter end. Cached releases are corrected the same way. The page also shows
cautious-phrase counts versus the previous release (the latest earlier release covering an earlier
period; regex counts, with the forward-looking-statements section left out). Claude writes a short thesis, the key risks and "what would change this rating" only after code
has set the rating, and is told the points to the next band.

**Output.** Every run is kept: `data/stocks/runs/<TICKER>/<UTC timestamp>.json` (never overwritten) and
a row in `data/stocks/ratings.csv` (timestamp, ticker, rating, score, confidence, each factor, price at
rating time, models, cost, overlay, then `quote_price`, `quote_as_of` and `news_from`; append-only).
`site/data/stocks/index.json` lists the latest rating and latest price per ticker, the page settings and
the recent average cost; `site/data/stocks/<TICKER>.json` holds the latest detail and the full history;
`site/data/stocks/batch.json` the last batch's per-ticker status. The email is optional per run and uses
the same SMTP secrets. `python scripts/evaluate_stocks.py [--refresh]` prints forward 1/3/6-month
returns by rating band, and versus SPY, from `ratings.csv`.

**Near-live prices (display only).** Scores always use the latest official daily close. Tiingo
publishes a day's close some time after 4 pm ET, so a run at 5 pm can still score on the previous
day's close. The page therefore says **Scored on the close of <date>**, adds "today's close not yet
published; scored on <date>" only when a newer close existed at run time: the latest completed session
(the most recent weekday whose 4:00 pm ET close had passed; holidays not known), or the session a quote
proves closed when it is stamped at or after 4:00 pm ET, is after the scored close. A pre-market or
regular-session quote proves no new close, so Oracle's 8:49 am ET run, scored on the previous day's close,
has no note. A run between 16:00 and 18:00 ET says so in its summary and email. The latest price comes from Tiingo's
IEX endpoint: `tngoLast` (the official close once it prints; `last` needs an IEX entitlement and is
ignored), `prevClose` and the timestamp, all tickers in **one request** (`iex/?tickers=a,b,c`; the
all-tickers endpoint, filtered in code, only if that form fails). Each run stores
`quote: {price, as_of, prev_close}`, never a score input. The page always asks one fixed URL,
`api/stock?quotes=all`, once on load and every 10 minutes while the tab is visible and US markets are
open (9:30 am–4:15 pm ET), and picks out the tickers it shows. The function quotes every ticker that is
rated (`site/data/stocks/index.json`) or in `STOCK_LISTS`, so no other ticker can ever reach Tiingo: one
`?tickers=` request up to 100 tickers, two up to 200, the all-tickers endpoint beyond. Any other query
(`?quotes=MSFT`, extra parameters) is refused with 400 before any upstream call. The response is cached
at the edge for 5 minutes (errors for 60 s), so a ticker added to a list since the last cache fill shows
its stored close until the next refresh.

**Cost.** A run makes four Claude calls: research (Sonnet, up to 8 searches), topic ratings, the
write-up, and guidance extraction (Opus; once per earnings release, then cached). The first live run
(MSFT) cost $0.45 at list prices, $0.36 of it research: 8 searches and the ~118K tokens of search results
they pulled in. The page estimates a run as the number of tickers × the average cost of the last 10 full stock
runs. A `--reuse-news` re-score makes only the write-up call (two when the post-check asks for a rewrite,
as can a full run). Haiku 5.5 is priced by tier: a request whose
prompt (input + cache write + cache read tokens) is over 100,000 tokens pays $0.50 / $2.50 per MTok instead
of $0.10 / $0.50 (`tiers` in `config/models.yaml`); stock runs log every request's tokens and price each by
its tier. The capex pipeline's estimate still uses the base price.

### Price data: Tiingo

Prices come from [Tiingo](https://www.tiingo.com)'s end-of-day API. Why Tiingo:
- a free key gives full daily history with split- and dividend-adjusted closes;
- it reports split factors, which the valuation history needs;
- it is plain JSON over HTTPS with the key in a header, so it works from GitHub Actions runners.

Alternatives looked at: Alpha Vantage's free key allows 25 requests a day and keeps adjusted daily
series on paid plans. Yahoo Finance has no official API and often blocks cloud IPs.

**Free-tier limits** (Tiingo's "Starter" plan, for personal use; check
[tiingo.com/about/pricing](https://www.tiingo.com/about/pricing) for the current numbers): about 50
requests an hour, 1,000 a day and 500 unique symbols a month, shared by runs and the page's quotes.
A batch of n tickers uses n + 3 requests: SPY and QQQ once (plan), one daily history per ticker, and one
quote request (collect); a full batch of 15 uses 18, and the 30-tickers-a-day cap at most 36 in a day.
The page's quotes come from one fixed, edge-cached URL, so Tiingo sees at most one cache fill per
5 minutes however many tabs or devices are open: at most 12 requests an hour (24 past 100 rated or
listed tickers; Vercel caches per region, so a second region could add as many). One open tab while
markets are open is 6 an hour. A heavy hour (one full batch plus a polling tab) is about 24 requests. If a
key is missing, rejected or rate-limited, valuation and trend are marked missing, confidence drops,
and the run still completes; a failed quote only leaves the latest price at the stored close.

**Setup.**
1. Sign up at tiingo.com (free), then copy your API token from Account → API → Token.
2. Add it as a repository secret: Settings → Secrets and variables → Actions → New repository secret,
   name `STOCK_PRICE_API_KEY`.
3. The stocks page uses the same Vercel variables as Run now (`GITHUB_TOKEN`, `RUN_PASSCODE`) through
   its own function, [`site/api/stock.js`](site/api/stock.js). The token's **Actions: Read and write**
   permission covers `stocks.yml`; its **Variables: Read and write** permission covers the
   `STOCK_LISTS` variable (lists). Runs take up to 256 tickers each, with no daily cap
   unless one is set in `config/stocks.yaml` (`batch`).
4. For live prices on the page, also add the Tiingo token in Vercel (Settings → Environment Variables)
   as `STOCK_PRICE_API_KEY`, then redeploy. Without it the page shows the stored latest close.

**Use it** from the page, or Actions → **Stock research** → Run workflow (comma-separated tickers,
models, email), or locally:

```bash
python -m pytest -q tests_stocks
export ANTHROPIC_API_KEY=... STOCK_PRICE_API_KEY=...
python -m stocks.run MSFT              # full run, no email
python -m stocks.run MSFT --email      # and email it
python -m stocks.run MSFT --no-llm     # code factors only
python -m stocks.run MSFT --offline --no-llm   # re-score cached data
python -m stocks.run MSFT --reuse-news # new SEC data and prices, the latest news, a new write-up
python -m stocks.fundamentals tags MSFT NVDA   # which XBRL tags the cached balance sheet and margins use
INPUT_TICKERS=MSFT,NVDA python -m stocks.batch plan --shared shared   # what the plan job does
python scripts/evaluate_stocks.py --refresh    # forward returns by rating band
```

Tickers must be SEC-registered (resolved through SEC's `company_tickers.json`; `BRK.B` and `BRK-B`
both work). An unknown ticker stops the run with a clear message and writes nothing.

**Limits.** Ratings are a screen, not advice: thresholds are starting points (see `scripts/evaluate_stocks.py`
once there is history). XBRL tags vary by company: a figure no listed tag covers is left out as unknown rather
than counted as zero (the page says which tag stopped), and a company using only tags outside the lists shows
as unknown until they are added. Leases are excluded from debt. Banks and insurers
fit the factors poorly. Guidance in press releases is rare (most companies give it on the call).

## Models and cost

The Claude work is two steps, each with its own model:

| Step | Default | What it does |
|---|---|---|
| Research | `claude-sonnet-5-5` | Web-searches the week's news (needs a model with web search) |
| Scoring & digest | `claude-opus-5-5` | Scores the news categories and writes the digest (`assessment_samples` passes, default 2) |

A run uses the first valid model ID from: the **Run workflow** inputs (or the dashboard's Run now
panel) → the repository variables `RESEARCH_MODEL` / `ASSESSMENT_MODEL` → `models:` in
[`config/signals.yaml`](config/signals.yaml). IDs must match `^claude-[a-z0-9.-]+$`; anything else
is ignored with a warning in the run log.

Every run records, per step, the model, input and output tokens, web searches and an estimated cost
(list prices from [`config/models.yaml`](config/models.yaml): $ per million tokens, plus $10 per
1,000 web searches). It's in the weekly report JSON, in the `research_model`, `assessment_model`,
`cost_usd` columns of `data/log.csv`, and on the dashboard. Each run also refreshes the dashboard's
model list from the Anthropic Models API, so new models show up in the dropdowns without code
changes; add a price line to `config/models.yaml` for any new model you want costed.

## Setup

### 1. Repository secrets
Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Required | What it is |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Claude API key (Claude Console) |
| `FRED_API_KEY` | yes | Free key from the St. Louis Fed |
| `ALERT_EMAIL_TO` | yes | Where the digest goes (comma-separate several) |
| `SMTP_USERNAME` | yes | Sending email account (e.g. a Gmail address) |
| `SMTP_PASSWORD` | yes | That account's app password (Gmail: myaccount.google.com/apppasswords) |
| `SEC_USER_AGENT` | recommended | SEC asks for a contact, e.g. `CapexEarlyWarning you@example.com`. Falls back to `SMTP_USERNAME`. |
| `SMTP_HOST` / `SMTP_PORT` | only if not Gmail | Defaults: `smtp.gmail.com` / `465` |
| `STOCK_PRICE_API_KEY` | for stock research | Free Tiingo API token (see [Stock research](#stock-research-on-demand)); without it valuation and trend are left out. Add it in Vercel too for the page's live prices. |

Optional repository **variables** (Variables tab):

| Variable | What it is |
|---|---|
| `DASHBOARD_URL` | Your Vercel URL, linked from the email |
| `RESEARCH_MODEL` / `ASSESSMENT_MODEL` | Default models for scheduled runs (the dashboard's "Save as default" sets these) |
| `STOCK_LISTS` | The stocks page's lists (written by the page; don't edit by hand) |

### 2. First run
Actions → **Weekly capex early-warning** → **Run workflow**. It takes a few minutes, commits the new
data, and sends the email. After that it runs every Saturday at 09:17 (UTC+8), and checks every morning
at 07:30 (UTC+8) for an event day. The first run with version 2 also reads the latest two 10-Q/10-K
lease footnotes for each hyperscaler (a little extra Claude usage, once).

### 3. Dashboard on Vercel
Add New → Project → import this repo → set **Root Directory** to `site`, Framework Preset **Other**,
no build command → Deploy. Each weekly data commit redeploys it automatically.
On the free Hobby plan the production URL is publicly reachable; it only shows public market data,
so don't add personal positions to it.

### 4. "Run now" from the dashboard (optional)
The dashboard's Run now panel starts the workflow through a small Vercel function
([`site/api/run.js`](site/api/run.js)), behind a passcode. It needs a GitHub token and the passcode
as Vercel environment variables. Neither is ever sent to the browser.

**a. Create a fine-grained GitHub token** (GitHub → your avatar → Settings → Developer settings →
Personal access tokens → **Fine-grained tokens** → Generate new token):

1. Token name, e.g. `capex-dashboard-run`; pick an expiration and note the date (renew it before then).
2. Resource owner: the account or organization that owns this repository. (For an organization, it
   must allow fine-grained tokens, and an owner may need to approve the request.)
3. Repository access: **Only select repositories** → this repository only.
4. Repository permissions:
   - **Actions**: Read and write (start the workflow, read run status, count recent runs)
   - **Variables**: Read and write (the "Save as default for weekly runs" checkbox and the
     Automatic runs switch)
   - **Metadata**: Read-only (added automatically)
5. Generate, and copy the token (it starts with `github_pat_`).

**b. Add it to Vercel** (your project → Settings → Environment Variables), for the Production
environment, marked Sensitive:

| Name | Value |
|---|---|
| `GITHUB_TOKEN` | the token from step a |
| `RUN_PASSCODE` | a long random passcode, e.g. the output of `openssl rand -base64 24` |
| `GITHUB_REPO` | optional, `owner/name`; only if the Vercel project isn't linked to this repo |
| `STOCK_PRICE_API_KEY` | optional, the Tiingo token: live prices on the stocks page (`api/stock?quotes=all`) |

**c. Redeploy**: Deployments → the latest production deployment → ⋯ → **Redeploy**. Environment
variables only apply to new deployments.

Then open the dashboard, pick the models, enter the passcode and press **Run**. The panel follows
the run and reloads the data when the new commit has deployed (usually a few minutes after the run
finishes). Manual runs are capped at 5 per 24 hours (this counts every workflow_dispatch run,
including ones started from GitHub). The model dropdowns fill in after the first run with this
version; until then a Run uses the configured defaults.

**Automatic runs switch.** Below Run now, "Automatic runs" turns the scheduled runs on or off without
touching the code: **On** (Saturdays and event days, the default), **Saturdays only**, **Event days
only**, or **Paused** (manual runs only). It needs the passcode and saves the repo Actions variable
`AUTO_RUNS` (`all` / `weekly` / `events` / `off`); the workflow's "Decide whether to run" step reads it,
so a switched-off run stops there in about a minute and makes no Claude API calls. Manual runs always
work. You can also set `AUTO_RUNS` directly under repo Settings → Secrets and variables → Actions →
Variables; unset or unknown values mean `all`.

## Running locally

```bash
pip install -r requirements.txt pytest
python -m pytest -q
export FRED_API_KEY=... ANTHROPIC_API_KEY=...
python -m cew.run --no-email        # full run without emailing
python -m cew.run --offline --no-llm --no-email   # re-score cached data only
INPUT_RESEARCH_MODEL=claude-haiku-5-5 python -m cew.run --no-email   # one-off model override
RUN_TRIGGER=event:MSFT-earnings python -m cew.run --no-email   # event run (reads MSFT's latest press release)
EVENT_NAME=schedule SCHEDULE="30 23 * * *" python -m cew.events plan   # would today's event check run?
python scripts/evaluate.py          # forward S&P / Nasdaq returns by alert level
python -m pytest -q tests_stocks && python -m stocks.run MSFT   # stock research (see above)
```

## Layout

```
cew/            pipeline (fetch_fred, fetch_edgar, fetch_taiwan, edgar_text, leases, calls, scoring, analyst,
                sources, events, models, notify, publish, run)
stocks/         stock research (resolve, fundamentals, prices and quotes, guidance, metrics, scoring, research,
                publish, notify, run; batch = plan and collect for batch runs)
config/         signals.yaml — thresholds, series, companies, models; models.yaml — prices; events.yaml — event days;
                stocks.yaml — stock factors, weights, bands, overlay
data/           history (FRED CSVs, EDGAR metrics and lease footnotes, earnings releases in calls/, Taiwan
                revenue, daily reports, log.csv, every run in runs/ and runs.csv); data/stocks/ for stock runs
site/           dashboard (index.html, stocks.html + data/*.json written by the pipelines; api/run.js and
                api/stock.js Vercel functions)
scripts/        evaluate.py — did warnings lead price moves? evaluate_stocks.py — did ratings?
tests/, tests_stocks/   capex tests (run by the weekly workflow) and stock tests (run by stocks.yml)
.github/        weekly workflow, stock research workflow
```

## Known limits

- Taiwan's deceleration check needs three months of history, which builds automatically from the first run.
- Event dates marked `verified: false` in `config/events.yaml` are estimates or were seen only in a
  search summary; re-check them when the companies announce. A wrong date means a missed or extra run.
- Earnings press releases rarely contain capex guidance (most companies give it on the call); the
  phrase counts still compare tone quarter to quarter. Call transcripts are not free and not used.
- The lease footnote comparison needs two filings per company; the first v2 run reads both. Companies
  word the footnote differently, so check `data/edgar/<TICKER>_leases.json` after each new 10-Q.
- Source checks are literal: a claim that rounds a number differently from the page is "unverified",
  and paywalled or bot-blocking sites are "unreachable" (which counts in the claim's favour).
- Single-company CDS prices are paid data; credit uses index spreads plus Claude's news check.
- Source tiers come from a domain list, so a major outlet's syndicated copy on an aggregator counts as
  "other", and a new company newsroom is "other" until its domain is added to `source_tiers`.
- Follow-up items are matched to Claude's answers by their text; an item Claude skips shows "no news".
- Event dates come from Claude's reading of the sources; code checks only that each is a real date and not
  in the future. A range in a follow-up note (e.g. "145-160 bp") usually shows "unverified", since the
  cited page has only one of the two figures.
- Costs are estimates at list prices from `config/models.yaml`; check the Claude Console for actual billing.
- Give the log a few quarters before trusting any threshold. `scripts/evaluate.py` shows whether
  Amber/Red weeks actually preceded weaker returns.
