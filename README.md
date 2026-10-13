# Capex Early Warning

A weekly early-warning dashboard for the AI capex cycle. Every Saturday a GitHub Actions job pulls
public data, scores six warning categories 0–3, asks Claude to research the week's news and write a
short digest, emails you the result, and updates a dashboard you can open from any device.

The idea: a hyperscaler cutting capex on an earnings call is the *last* domino. Credit markets, AI-lab
financing, Taiwan server-maker revenue, filing footnotes, supplier commentary and physical build-out
data usually move first. This tracks all six in one place.

> Research tool, not investment advice. Leading indicators give false alarms; act on several
> categories confirming together, not one data point.

## The six categories

| Category | Source | How it's scored |
|---|---|---|
| Credit markets | FRED high-yield and investment-grade spreads, plus Claude news adjustment (±1) | Rule-based |
| AI-lab financing | Claude web research (funding rounds, compute contracts, neocloud financing) | Claude, cited |
| Taiwan supply chain | TWSE monthly revenue: TSMC, Hon Hai, Quanta, Wistron, Wiwynn, Accton, Gigabyte | Rule-based |
| Hyperscaler filings | SEC EDGAR XBRL: capex, operating cash flow, new lease assets for MSFT, GOOGL, AMZN, META, ORCL | Rule-based |
| Supplier signals | Claude web research (orders, backlog, earnings-call wording, insider selling) | Claude, cited |
| Physical build-out | Claude web research (cancellations, grid-connection withdrawals, GPU rental prices) | Claude, cited |

Macro context (CPI, core PCE, Fed funds, 2y/10y yields, Brent, VIX) and the S&P 500 / Nasdaq-100
levels are logged alongside. The composite is the weighted sum (max 18): **Green** < 5, **Amber** ≥ 5,
**Red** ≥ 9, and any single category at 3 triggers an alert. All thresholds live in
[`config/signals.yaml`](config/signals.yaml) — edit them there, no code changes needed.

Numbers always come from the official data via plain code. Claude only interprets them, scores the
three news categories, and must cite the sources it found.

## Models and cost

The Claude work is two steps, each with its own model:

| Step | Default | What it does |
|---|---|---|
| Research | `claude-sonnet-5-5` | Web-searches the week's news (needs a model with web search) |
| Scoring & digest | `claude-opus-5-5` | Scores the news categories and writes the digest |

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

Optional repository **variables** (Variables tab):

| Variable | What it is |
|---|---|
| `DASHBOARD_URL` | Your Vercel URL, linked from the email |
| `RESEARCH_MODEL` / `ASSESSMENT_MODEL` | Default models for scheduled runs (the dashboard's "Save as default" sets these) |

### 2. First run
Actions → **Weekly capex early-warning** → **Run workflow**. It takes a few minutes, commits the new
data, and sends the email. After that it runs every Saturday at 09:17 (UTC+8).

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
   - **Variables**: Read and write (the "Save as default for weekly runs" checkbox)
   - **Metadata**: Read-only (added automatically)
5. Generate, and copy the token (it starts with `github_pat_`).

**b. Add it to Vercel** (your project → Settings → Environment Variables), for the Production
environment, marked Sensitive:

| Name | Value |
|---|---|
| `GITHUB_TOKEN` | the token from step a |
| `RUN_PASSCODE` | a long random passcode, e.g. the output of `openssl rand -base64 24` |
| `GITHUB_REPO` | optional, `owner/name`; only if the Vercel project isn't linked to this repo |

**c. Redeploy**: Deployments → the latest production deployment → ⋯ → **Redeploy**. Environment
variables only apply to new deployments.

Then open the dashboard, pick the models, enter the passcode and press **Run**. The panel follows
the run and reloads the data when the new commit has deployed (usually a few minutes after the run
finishes). Manual runs are capped at 5 per 24 hours (this counts every workflow_dispatch run,
including ones started from GitHub). The model dropdowns fill in after the first run with this
version; until then a Run uses the configured defaults.

## Running locally

```bash
pip install -r requirements.txt pytest
python -m pytest -q
export FRED_API_KEY=... ANTHROPIC_API_KEY=...
python -m cew.run --no-email        # full run without emailing
python -m cew.run --offline --no-llm --no-email   # re-score cached data only
INPUT_RESEARCH_MODEL=claude-haiku-5-5 python -m cew.run --no-email   # one-off model override
python scripts/evaluate.py          # forward S&P / Nasdaq returns by alert level
```

## Layout

```
cew/            pipeline (fetch_fred, fetch_edgar, fetch_taiwan, scoring, analyst, models, notify, publish, run)
config/         signals.yaml — thresholds, series, companies, models; models.yaml — prices
data/           history (FRED CSVs, EDGAR metrics, Taiwan revenue, weekly reports, log.csv)
site/           dashboard (index.html + data/*.json written by the pipeline; api/run.js Vercel function)
scripts/        evaluate.py — did warnings lead price moves?
.github/        weekly workflow
```

## Known limits (v1)

- Taiwan's deceleration check needs three months of history, which builds automatically from the first run.
- "Data-center leases signed but not yet started" lives in filing text, not XBRL; v1 uses new
  right-of-use lease assets (leases that *did* start) as the proxy. Reading the footnote text is a v2 item.
- Single-company CDS prices are paid data; credit uses index spreads plus Claude's news check.
- Costs are estimates at list prices from `config/models.yaml`; check the Claude Console for actual billing.
- Give the log a few quarters before trusting any threshold. `scripts/evaluate.py` shows whether
  Amber/Red weeks actually preceded weaker returns.
