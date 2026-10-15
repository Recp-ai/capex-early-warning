# CLAUDE.md

Weekly AI-capex early-warning pipeline. A GitHub Actions job (Saturdays, the morning after listed
events, or on demand) fetches public data, scores six warning categories 0–3, asks Claude to research
the news and write a digest, checks the cited sources, emails it, and commits the results. A static dashboard in `site/` is served by Vercel
(Vercel Root Directory = `site`), which redeploys on every data commit.

## Rules

- **Numbers come only from the deterministic fetchers** (`fetch_fred`, `fetch_edgar`, `fetch_taiwan`)
  and the rule-based scoring in `scoring.py`. Claude only interprets them: it scores the three news
  categories, may nudge credit by ±1, and writes the digest, citing URLs from its own research.
  Never let a Claude output become a data value or override a rule-based score.
  One exception, by design: figures Claude extracts from SEC filing text (`leases.py`, `calls.py`) count
  only after `edgar_text.found_in` finds the sentence verbatim in the filing and the amount in that
  sentence; the value is then parsed by `edgar_text.parse_usd*`, never taken from Claude. Keep it that way.
  Phrase counts in press releases are regex counts, not model output. Source tiers come from the
  `source_tiers` domain lists, and the follow-up list and its run counts from `followups.py`; Claude only
  gives each follow-up a status, note, URL and event date. Event dates (`event_date` on evidence and
  follow-ups) are labels, checked by `sources.tag_dates`, never score inputs. The credit contagion stage is
  context, never a score input.
- **Never put secrets in `site/`.** Everything under `site/` is public (static files and the
  `site/api/run.js` source). Secrets live in GitHub Actions secrets or Vercel environment variables
  and are read at runtime; responses, errors and logs must never echo them.
- **Python 3.12** (3.13's stricter TLS rejects the TWSE certificate). Don't use 3.13-only features.
- **Tests must pass**: `python -m pytest -q` (both `tests/` and `tests_stocks/`). The weekly workflow runs
  `tests/` before every pipeline run; the stock workflow runs `tests_stocks/`.
- **Never hand-edit `data/` or `site/data/`.** The workflow writes them (FRED CSVs, EDGAR metrics and
  `<TICKER>_leases.json`, `calls/`, Taiwan revenue, reports, `log.csv`, `runs/`, `runs.csv`, and the
  dashboard's `latest.json`, `charts.json`, `log.json`, `models.json`, `runs.json`). Change the code
  that writes them instead.
- **`data/runs/` and `data/runs.csv` are append-only.** Never delete, rewrite or overwrite an earlier run
  file or row (`publish.archive_run` opens files with mode "x"; `append_run_row` only appends). A new
  column goes on the end of `RUN_FIELDS`; `append_run_row` then appends its name to the header line and
  leaves every earlier row byte for byte as it was (they read as blank in that column).
- **`config/events.yaml`**: `verified: true` only when the date was seen on the official source
  (federalreserve.gov, bls.gov, the company's IR site) on a page opened directly or on two or more
  official pages; otherwise `verified: false` with a `note` saying where the date came from.
  `events.EVENT_CHECK_CRON` must equal the daily cron in `weekly.yml` (a test checks this).
- Thresholds and weights live in `config/signals.yaml`, not in code. Don't change scoring logic
  unless asked.
- In `.github/workflows/weekly.yml`, pass every `inputs.*` / `vars.*` value to scripts through
  `env:`; never put an expression directly inside a `run:` script (script injection).

## Architecture

```
cew/run.py          orchestrates one run: fetch -> filing text -> score -> follow-ups -> Claude -> source
                    tiers/checks -> hysteresis/confirmation/jumps -> publish -> email -> archive
cew/events.py       config/events.yaml, run triggers, `python -m cew.events plan` (daily event check)
cew/fetch_fred.py   FRED series -> data/fred/*.csv + summaries (latest, 4w/52w changes)
cew/fetch_edgar.py  SEC XBRL capex, cash flow, lease assets -> data/edgar/*.json
cew/edgar_text.py   EDGAR submissions index, filing documents, Exhibit 99.1 lookup, verbatim/amount checks
cew/leases.py       10-Q/10-K lease-commitment paragraphs (filing_text.lease_phrases) -> Claude ->
                    data/edgar/<TICKER>_leases.json; bump leases.VERSION to re-read filings that gave no total
cew/calls.py        earnings 8-K Exhibit 99.1 -> capex guidance + phrase counts -> data/calls/<TICKER>/<quarter>.json
cew/fetch_taiwan.py TWSE monthly revenue -> data/taiwan/monthly_revenue.csv
cew/scoring.py      rule-based scores (credit, Taiwan, filings incl. leases), composite, level hysteresis,
                    credit contagion stage (context only), confirmation, jumps, event-email rule
cew/analyst.py      Claude step 1 "research" (web search, loops through pause_turn), step 2
                    "assessment" (structured-output JSON: news scores, digest, follow-up statuses), run
                    `assessment_samples` times and merged by `combine`; and `extract` for filing text
cew/followups.py    previous run's watch_next -> this run's follow-ups (carried up to follow_ups.max_runs)
cew/sources.py      source tier per URL, event dates (background when old), fetch evidence and follow-up
                    URLs, mark verified/unverified/unreachable, cap thin news scores
cew/models.py       per-step model choice, prices, token/cost accounting (plus per-request tiered pricing used by
                    the stock module), site/data/models.json
cew/publish.py      data/reports/<date>.json, data/log.csv (one row per day), data/runs/<ts>.json +
                    data/runs.csv (every run), site/data/*.json
cew/notify.py       SMTP digest email
config/signals.yaml thresholds, series, companies, models, samples, follow_ups, credit_stage, confirmation,
                    source_check, source_tiers, evidence_dates, filing_text, email
config/events.yaml  dated events (FOMC, CPI, earnings) with verified flags and sources
config/models.yaml  $ per MTok (input/output) per model, web search $ per 1,000; fallback model list
site/index.html     the dashboard (vanilla JS, no build step) incl. the "Run now" panel
site/api/run.js     Vercel function: POST dispatches the workflow (passcode), GET latest run status
site/vercel.json    bundles site/data/*.json with the function
scripts/evaluate.py forward index returns by alert level, from data/log.csv
```

**Models.** Each step's model is the first valid ID (`^claude-[a-z0-9.-]+$`) from: the
workflow_dispatch input (`INPUT_RESEARCH_MODEL` / `INPUT_ASSESSMENT_MODEL`), the repo Actions
variable (`VAR_RESEARCH_MODEL` / `VAR_ASSESSMENT_MODEL`, from `RESEARCH_MODEL` / `ASSESSMENT_MODEL`),
then `config/signals.yaml`. Invalid values are skipped with a warning.

**Cost.** `analyst` adds the `usage` of every API response (each pause_turn continuation is billed
separately) per step: `research`, `assessment` (every sample; samples after the first set
`cache_control` and read the prompt from the cache), and `leases` / `earnings` when filing text was read
(those use the assessment model); `models.cost_summary` prices it with `config/models.yaml`. The result
is `report["cost"]`, the `research_model`, `assessment_model`, `cost_usd` columns in `data/log.csv`, and
the `tokens`, `searches`, `cost_usd` columns in `data/runs.csv` (the dashboard's month-to-date spend).

**Model list.** Each run calls the Models API (`client.models.list()`, which pages through
`has_more`/`after_id`) and writes `site/data/models.json` (id, display_name, created_at,
supports_web_search from `capabilities.server_tools.web_search.supported` or null, price). If the call
fails it writes the `config/models.yaml` list instead. The dashboard dropdowns and `api/run.js`
validation both read this file.

**Triggers and event days.** The workflow's first step, `python -m cew.events plan`, decides whether to
run and sets `RUN_TRIGGER`: `schedule` (Saturday cron), `manual` (workflow_dispatch), or
`event:<name>+<name>` (the daily 23:30 UTC cron when a listed event fell on the US Eastern day just ending
and `data/runs.csv` has no run that day; or a manual run with the `event` input). Event runs focus the
research on the event, read hyperscaler press releases (`calls.py`, only for tickers in `hyperscalers`),
and email only on a level change or a category move of `email.event_category_move`.
The repo Actions variable `AUTO_RUNS` (`all` | `weekly` | `events` | `off`, passed to the plan step as
env `AUTO_RUNS`; unset/unknown = `all`) switches the Saturday run and/or the daily event check off;
`events.plan` applies it to scheduled runs only, never to manual runs. The dashboard sets it.

**Follow-ups.** `followups.pending` takes the latest `data/runs/*.json` with a Claude assessment: its
`watch_next` items (run 1) plus its unresolved `follow_ups` (run count + 1, dropped past
`follow_ups.max_runs`). Both Claude steps get the list; `followups.merge` matches the assessment's
`follow_ups` back by item text. Each follow-up's URL sits in `item["source"]` (evidence-shaped) so the
source check never overwrites the follow-up's own `status`.

**Samples.** `analyst.combine` averages the samples' news scores (rounded half down), records
`samples`/`spread`/`unstable` per category (`assessment_unstable_spread`), takes text from the sample
closest to the result, and rounds the credit adjustment half toward zero.

**Dates and conflicting figures.** Both Claude steps must give an event date (when it happened, not when
it was reported) for every evidence item and follow-up; the schema's `event_date` is a date or null. The
assessment prompt lists each site in the research notes by tier (`sources.hosts_by_tier`) and tells the
model to report the range when sources disagree on a metric, prefer the primary/major figure, and never
base a follow-up status on an "other"-tier number alone (a prompt rule, not a code check).

**Checks after the assessment.** EDGAR evidence (press-release comparison) is added to hyperscaler
filings and supplier signals with status `verified` (its `event_date` is the 8-K filing date);
`sources.tag_tiers` labels every evidence and follow-up URL primary/major/other; `sources.tag_dates` keeps
each `event_date` only if it is a real YYYY-MM-DD no later than tomorrow and sets `age_days` and
`background` (more than `evidence_dates.background_after_days` before the run), shown next to each item on
the dashboard and in the email; `sources.check_evidence` fetches every other evidence URL and the
follow-up URLs; `sources.apply_caps` caps news categories at `source_check.cap_to`. Then
`scoring.hysteresis` holds the previous run's level until the composite falls below the line minus
`composite.level_hysteresis` (`comp["level"]` is the held level everywhere, `comp["raw_level"]` the
unheld one), `scoring.confirmation` (RED is "confirmed" once the same 2+ categories scored 2+ in
consecutive runs) and `scoring.jumps` compare with the previous run in `data/runs.csv` (or the latest
daily report before the first v2 run).

**Run now.** The dashboard POSTs to `/api/run`, which checks `RUN_PASSCODE` (timingSafeEqual),
allows at most 5 workflow_dispatch runs per 24 h, validates models against `models.json`, optionally
saves them as the `RESEARCH_MODEL` / `ASSESSMENT_MODEL` repo variables, and dispatches `weekly.yml`
on `main` with the Vercel env var `GITHUB_TOKEN` (fine-grained, this repo only). The same endpoint
handles the Automatic runs switch: `POST {passcode, action: "set_auto", auto_runs}` saves `AUTO_RUNS`
(no dispatch, not counted in the 5/24 h cap) and `GET /api/run?auto` reads it. Keep `AUTO_MODES` in
`run.js` and `cew/events.py` the same.

## Stock research (separate module)

An on-demand stock rating module lives beside the capex pipeline and must never change how it behaves.

- **Isolation.** Code in `stocks/`, config in `config/stocks.yaml`, data in `data/stocks/` and
  `site/data/stocks/`, tests in `tests_stocks/`, workflow `.github/workflows/stocks.yml` (workflow_dispatch
  only, never a schedule; concurrency group `stocks`, never the capex group), page `site/stocks.html`,
  function `site/api/stock.js`, lists in the `STOCK_LISTS` repo variable. `stocks/` may import cew
  helpers (`util`, `edgar_text`, `fetch_edgar`, `models`, `sources`, `analyst`, `calls`, `publish`, `notify`);
  `cew/` must never import `stocks` (a test checks). If a shared helper needs more, add a new function;
  don't change an existing cew function's behaviour. `weekly.yml` runs `python -m pytest -q tests` and
  `stocks.yml` runs `python -m pytest -q tests_stocks`, so stock tests can never block a capex run; both
  suites must pass. Don't touch `site/api/run.js` or the Run now / Automatic runs panels; the two pages
  only link to each other.
- **Numbers only from code.** Growth, quality, balance sheet, valuation and trend (0-100 each) are
  computed from SEC companyfacts, Tiingo prices and `data/fred/DGS10.csv` by `stocks/scoring.py` with
  `config/stocks.yaml`. Claude rates six news topics -2..+2 (the rubric in the config and prompt); the
  source check caps each rating at its number of verified-or-unreachable sources, then by source quality
  (`news_risk.source_quality`: ±2 needs a verified primary- or major-tier source, else ±1; only other-tier
  sources cap at ±1, or 0 if none verified; `scoring.cap_weak_sources` sets `weak_sources_note`, shown as
  "capped: weak sources" on the page and in the email); code turns the ratings into the news & risk score.
  Claude then writes the thesis, risks and "what would change" after the rating is set, and never sets a
  rating, score or number. Guidance sentences from the 8-K count only
  when `edgar_text.found_in` finds them verbatim (as in `cew/calls.py`); they are display and context,
  not score inputs. The capex overlay (cap at Hold for `overlay.ai_exposed` tickers when
  `site/data/latest.json` is RED and confirmed; AMBER only a note) reads the capex result, never writes it.
  Quotes (`prices.quotes`, Tiingo IEX `tngoLast`) are display only, never a score input: factors use the
  latest official daily close, and the page says which close was scored (`prices.close_note`).
- **Data quality, not silent zeros.** Balance-sheet tag lists live in `stocks/fundamentals.py` (debt by
  family; leases excluded). A cash / investment / debt figure is unknown (None), never zero, when a tag of
  its group stopped reporting (`balance_sheet.stopped_tag_quarters` / `no_debt_quarters`), only tags we
  don't read carry a value, or one unread tag is larger than all we read (the record's `scan` of the latest
  balance sheet); the balance-sheet factor and
  EV then count as missing with the reason. Missing margins show as "missing" in the factor inputs (after
  the revenue-minus-costs fallbacks). Quality and valuation score FCF on `summary["fcf_basis"]`: reported;
  "ex-prepayments" (FCF minus the excess prepayments, i.e. the increase beyond balance a year earlier x
  revenue growth, when that excess exceeds `prepayments.max_share_of_ocf` of operating cash flow); or
  "normalised" (the `fcf_spike` guard: a quarter's FCF above its revenue, or a TTM margin jump; median
  quarterly margin x TTM revenue); the lower when both apply. Share counts off by more than
  `valuation.share_outlier_x` from both neighbours are dropped. History percentiles rank yields
  (`metrics.cheaper_share`), not multiples. Confidence is `scoring.confidence_detail` (points per issue,
  thresholds in `confidence`), with its reasons shown.
- **Re-score (`--reuse-news`, stocks.yml input `reuse_news`).** Fetches SEC data and prices, re-scores the
  code factors, keeps the latest archived run's topics and source checks unchanged (`publish.latest_news`;
  a reuse passes the original `news_from` on), skips guidance extraction, and calls Claude for the
  write-up only. `ratings.csv` gets `news_from` (last column; blank = the run's own news);
  `publish.recent_cost` averages full runs and re-scores apart. A re-score run is titled
  "Stock research (rescore) <tickers>" and `api/stock.js` counts it against `batch.max_rescore_tickers_per_day`,
  never the full-run cap (and full runs never against the re-score cap).
- **Cost.** Stock steps log every request (`cew.models.add_request` / `record_request`) and are priced by
  `cew.models.cost_summary_tiered` (prompt-size `tiers` in `config/models.yaml`, e.g. Haiku 5.5 above
  100K tokens). The capex pipeline keeps `cost_summary` at base prices; don't change it.
- **Batches.** `stocks.yml`: plan (`python -m stocks.batch plan`: validate, de-duplicate, max `batch.max_batch`,
  fetch SPY/QQQ once as an artifact) -> one `rate <TICKER>` matrix job per ticker
  (`python -m stocks.run --batch-out out --shared shared`; never commits, never fetches other tickers'
  prices) -> collect (`python -m stocks.batch collect`: one quote request, the append-only files, one
  email, one commit). Keep the job names, the run-name `Stock research <tickers>` / `Stock research (rescore)
  <tickers>` (api/stock.js counts tickers per 24 h from run titles and reads per-ticker status from job names) and the limits in
  `api/stock.js` equal to `config/stocks.yaml` (a test checks).
- **Tiingo budget** (free tier ~50 requests/hour, shared): a batch of n costs n + 3; quotes are always ONE
  request for any number of tickers (`iex/?tickers=`, else the all-tickers endpoint), never one per ticker.
  The page's quotes use one fixed URL, `GET api/stock?quotes=all` (anything else is a 400 before any
  upstream call), cached `public, s-maxage=300` (errors 60 s): the function quotes only tickers that are
  rated (`site/data/stocks/index.json`) or in `STOCK_LISTS` (one request up to 100, two up to 200, the
  all-tickers endpoint beyond), so unknown tickers never reach Tiingo. Keep it that way.
- **Append-only.** `data/stocks/runs/<TICKER>/<UTC timestamp>.json` is opened with mode "x";
  `data/stocks/ratings.csv` is only appended to (a new column goes on the end of `publish.RATING_FIELDS`,
  earlier rows stay byte for byte). Never hand-edit `data/stocks/` or `site/data/stocks/`.
- **Secrets.** `STOCK_PRICE_API_KEY` (GitHub secret, and a Vercel env var for the page's quotes) goes in
  the Tiingo Authorization header, never a URL or an error message. `api/stock.js` uses the same
  `RUN_PASSCODE` (timingSafeEqual) and `GITHUB_TOKEN` as `api/run.js`, validates every ticker
  (`^[A-Z][A-Z0-9.\-]{0,9}$` after uppercasing) and models, caps runs at 15 tickers and 30 tickers per 24 h
  counted on `stocks.yml` (re-scores: their own 30 per 24 h), saves lists only with the passcode (validated: 30 lists, 100 tickers each,
  40,000 bytes), and scrubs all three secrets from every response.

```
stocks/run.py          one run: resolve -> companyfacts -> prices -> 8-K -> factors -> Claude news + source checks
                       -> composite / rating / overlay -> write-up -> email -> publish
stocks/resolve.py      ticker -> CIK (SEC company_tickers.json); TickerNotFound stops the run (exit 2, nothing written)
stocks/fundamentals.py companyfacts -> quarterly/annual flows (YTD differencing, first-filed dates), margin
                       fallbacks, prepayments, balance sheet (tag families, unknown guard, tag scan), share
                       counts (outliers dropped); IFRS / 20-F handled; cache data/stocks/fundamentals/<TICKER>.json;
                       `python -m stocks.fundamentals tags <TICKER>` prints the tags used
stocks/prices.py       Tiingo EOD (ticker, SPY, QQQ, 5 years) -> data/stocks/prices/<TICKER>.csv; failures recorded;
                       quotes() one IEX request for all tickers; close_note() which close was scored
stocks/guidance.py     latest earnings 8-K Exhibit 99.1: verbatim-checked guidance, phrase counts vs previous release
stocks/metrics.py      valuation (now and point-in-time month-end history, percentiles on yields), trend, DGS10,
                       capex context, chart
stocks/scoring.py      sub-metric points, factors, news & risk, composite, confidence, bands, overlay
stocks/research.py     Claude research (8 searches, 30 days), topic ratings, write-up
stocks/publish.py      archive, ratings.csv, site/data/stocks/<TICKER>.json (full history) and index.json
stocks/batch.py        batch plan (validate, benchmarks once) and collect (quotes, publish, one email, batch.json)
stocks/notify.py       optional email (same SMTP secrets); one summary email per batch
scripts/evaluate_stocks.py  forward 1/3/6-month returns by rating band and vs SPY
```

```bash
python -m pytest -q tests_stocks
python -m stocks.run MSFT --offline --no-llm    # re-score cached data (writes data/stocks/, site/data/stocks/)
python -m stocks.run MSFT --reuse-news          # new SEC data / prices, the latest run's news, a new write-up
python -m stocks.fundamentals tags MSFT         # the XBRL tags behind the cached balance sheet and margins
node --check site/api/stock.js
```

## Commands

```bash
pip install -r requirements.txt pytest
python -m pytest -q
python -m cew.run --offline --no-llm --no-email   # re-score cached data; writes data/ and site/data/
EVENT_NAME=schedule SCHEDULE="30 23 * * *" python -m cew.events plan   # today's event-day decision
node --check site/api/run.js
```

Local runs write into `data/` and `site/data/`; don't commit those results (`git checkout data site/data`,
and delete new untracked files under `data/stocks/` and `site/data/stocks/`).
