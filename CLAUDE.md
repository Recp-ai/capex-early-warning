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
  gives each follow-up a status, note and URL. The credit contagion stage is context, never a score input.
- **Never put secrets in `site/`.** Everything under `site/` is public (static files and the
  `site/api/run.js` source). Secrets live in GitHub Actions secrets or Vercel environment variables
  and are read at runtime; responses, errors and logs must never echo them.
- **Python 3.12** (3.13's stricter TLS rejects the TWSE certificate). Don't use 3.13-only features.
- **Tests must pass**: `python -m pytest -q`. The workflow runs them before every pipeline run.
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
cew/sources.py      source tier per URL, fetch evidence and follow-up URLs, mark verified/unverified/
                    unreachable, cap thin news scores
cew/models.py       per-step model choice, prices, token/cost accounting, site/data/models.json
cew/publish.py      data/reports/<date>.json, data/log.csv (one row per day), data/runs/<ts>.json +
                    data/runs.csv (every run), site/data/*.json
cew/notify.py       SMTP digest email
config/signals.yaml thresholds, series, companies, models, samples, follow_ups, credit_stage, confirmation,
                    source_check, source_tiers, filing_text, email
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

**Follow-ups.** `followups.pending` takes the latest `data/runs/*.json` with a Claude assessment: its
`watch_next` items (run 1) plus its unresolved `follow_ups` (run count + 1, dropped past
`follow_ups.max_runs`). Both Claude steps get the list; `followups.merge` matches the assessment's
`follow_ups` back by item text. Each follow-up's URL sits in `item["source"]` (evidence-shaped) so the
source check never overwrites the follow-up's own `status`.

**Samples.** `analyst.combine` averages the samples' news scores (rounded half down), records
`samples`/`spread`/`unstable` per category (`assessment_unstable_spread`), takes text from the sample
closest to the result, and rounds the credit adjustment half toward zero.

**Checks after the assessment.** EDGAR evidence (press-release comparison) is added to hyperscaler
filings and supplier signals with status `verified`; `sources.tag_tiers` labels every evidence and
follow-up URL primary/major/other; `sources.check_evidence` fetches every other evidence URL and the
follow-up URLs; `sources.apply_caps` caps news categories at `source_check.cap_to`. Then
`scoring.hysteresis` holds the previous run's level until the composite falls below the line minus
`composite.level_hysteresis` (`comp["level"]` is the held level everywhere, `comp["raw_level"]` the
unheld one), `scoring.confirmation` (RED is "confirmed" once the same 2+ categories scored 2+ in
consecutive runs) and `scoring.jumps` compare with the previous run in `data/runs.csv` (or the latest
daily report before the first v2 run).

**Run now.** The dashboard POSTs to `/api/run`, which checks `RUN_PASSCODE` (timingSafeEqual),
allows at most 5 workflow_dispatch runs per 24 h, validates models against `models.json`, optionally
saves them as the `RESEARCH_MODEL` / `ASSESSMENT_MODEL` repo variables, and dispatches `weekly.yml`
on `main` with the Vercel env var `GITHUB_TOKEN` (fine-grained, this repo only).

## Commands

```bash
pip install -r requirements.txt pytest
python -m pytest -q
python -m cew.run --offline --no-llm --no-email   # re-score cached data; writes data/ and site/data/
EVENT_NAME=schedule SCHEDULE="30 23 * * *" python -m cew.events plan   # today's event-day decision
node --check site/api/run.js
```

Local runs write into `data/` and `site/data/`; don't commit those results (`git checkout data site/data`).
