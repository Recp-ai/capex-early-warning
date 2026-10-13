# CLAUDE.md

Weekly AI-capex early-warning pipeline. A GitHub Actions job (Saturdays, or on demand) fetches public
data, scores six warning categories 0–3, asks Claude to research the week's news and write a digest,
emails it, and commits the results. A static dashboard in `site/` is served by Vercel
(Vercel Root Directory = `site`), which redeploys on every data commit.

## Rules

- **Numbers come only from the deterministic fetchers** (`fetch_fred`, `fetch_edgar`, `fetch_taiwan`)
  and the rule-based scoring in `scoring.py`. Claude only interprets them: it scores the three news
  categories, may nudge credit by ±1, and writes the digest, citing URLs from its own research.
  Never let a Claude output become a data value or override a rule-based score.
- **Never put secrets in `site/`.** Everything under `site/` is public (static files and the
  `site/api/run.js` source). Secrets live in GitHub Actions secrets or Vercel environment variables
  and are read at runtime; responses, errors and logs must never echo them.
- **Python 3.12** (3.13's stricter TLS rejects the TWSE certificate). Don't use 3.13-only features.
- **Tests must pass**: `python -m pytest -q`. The workflow runs them before every pipeline run.
- **Never hand-edit `data/` or `site/data/`.** The workflow writes them (FRED CSVs, EDGAR metrics,
  Taiwan revenue, reports, `log.csv`, and the dashboard's `latest.json`, `charts.json`, `log.json`,
  `models.json`). Change the code that writes them instead.
- Thresholds and weights live in `config/signals.yaml`, not in code. Don't change scoring logic
  unless asked.
- In `.github/workflows/weekly.yml`, pass every `inputs.*` / `vars.*` value to scripts through
  `env:`; never put an expression directly inside a `run:` script (script injection).

## Architecture

```
cew/run.py          orchestrates one run: fetch -> score -> Claude -> cost -> publish -> email
cew/fetch_fred.py   FRED series -> data/fred/*.csv + summaries (latest, 4w/52w changes)
cew/fetch_edgar.py  SEC XBRL capex, cash flow, lease assets -> data/edgar/*.json
cew/fetch_taiwan.py TWSE monthly revenue -> data/taiwan/monthly_revenue.csv
cew/scoring.py      rule-based scores (credit, Taiwan, filings) and the weighted composite
cew/analyst.py      Claude step 1 "research" (web search, loops through pause_turn) and
                    step 2 "assessment" (structured-output JSON: news scores + digest)
cew/models.py       per-step model choice, prices, token/cost accounting, site/data/models.json
cew/publish.py      data/reports/<date>.json, data/log.csv, site/data/*.json
cew/notify.py       SMTP digest email
config/signals.yaml thresholds, series, companies, models.research / models.assessment
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
separately) per step; `models.cost_summary` prices it with `config/models.yaml`. The result is
`report["cost"]`, and the `research_model`, `assessment_model`, `cost_usd` columns in `data/log.csv`.

**Model list.** Each run calls the Models API (`client.models.list()`, which pages through
`has_more`/`after_id`) and writes `site/data/models.json` (id, display_name, created_at,
supports_web_search from `capabilities.server_tools.web_search.supported` or null, price). If the call
fails it writes the `config/models.yaml` list instead. The dashboard dropdowns and `api/run.js`
validation both read this file.

**Run now.** The dashboard POSTs to `/api/run`, which checks `RUN_PASSCODE` (timingSafeEqual),
allows at most 5 workflow_dispatch runs per 24 h, validates models against `models.json`, optionally
saves them as the `RESEARCH_MODEL` / `ASSESSMENT_MODEL` repo variables, and dispatches `weekly.yml`
on `main` with the Vercel env var `GITHUB_TOKEN` (fine-grained, this repo only).

## Commands

```bash
pip install -r requirements.txt pytest
python -m pytest -q
python -m cew.run --offline --no-llm --no-email   # re-score cached data; writes data/ and site/data/
node --check site/api/run.js
```

Local runs write into `data/` and `site/data/`; don't commit those results (`git checkout data site/data`).
