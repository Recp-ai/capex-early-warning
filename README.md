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
| Credit markets | FRED high-yield and investment-grade spreads, plus Claude news adjustment (±1) | Rule-based |
| AI-lab financing | Claude web research (funding rounds, compute contracts, neocloud financing) | Claude, cited |
| Taiwan supply chain | TWSE monthly revenue: TSMC, Hon Hai, Quanta, Wistron, Wiwynn, Accton, Gigabyte | Rule-based |
| Hyperscaler filings | SEC EDGAR XBRL: capex, operating cash flow, new lease assets for MSFT, GOOGL, AMZN, META, ORCL; 10-Q/10-K text: leases signed but not yet commenced | Rule-based |
| Supplier signals | Claude web research (orders, backlog, earnings-call wording, insider selling), plus the hyperscaler earnings-release comparison on earnings days | Claude, cited |
| Physical build-out | Claude web research (cancellations, grid-connection withdrawals, GPU rental prices) | Claude, cited |

Macro context (CPI, core PCE, Fed funds, 2y/10y yields, Brent, VIX) and the S&P 500 / Nasdaq-100
levels are logged alongside. The composite is the weighted sum (max 18): **Green** < 5, **Amber** ≥ 5,
**Red** ≥ 9, and any single category at 3 triggers an alert. Red shows as **confirmed** only once two or
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
RUN_TRIGGER=event:MSFT-earnings python -m cew.run --no-email   # event run (reads MSFT's latest press release)
EVENT_NAME=schedule SCHEDULE="30 23 * * *" python -m cew.events plan   # would today's event check run?
python scripts/evaluate.py          # forward S&P / Nasdaq returns by alert level
```

## Layout

```
cew/            pipeline (fetch_fred, fetch_edgar, fetch_taiwan, edgar_text, leases, calls, scoring, analyst,
                sources, events, models, notify, publish, run)
config/         signals.yaml — thresholds, series, companies, models; models.yaml — prices; events.yaml — event days
data/           history (FRED CSVs, EDGAR metrics and lease footnotes, earnings releases in calls/, Taiwan
                revenue, daily reports, log.csv, every run in runs/ and runs.csv)
site/           dashboard (index.html + data/*.json written by the pipeline; api/run.js Vercel function)
scripts/        evaluate.py — did warnings lead price moves?
.github/        weekly workflow
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
- Costs are estimates at list prices from `config/models.yaml`; check the Claude Console for actual billing.
- Give the log a few quarters before trusting any threshold. `scripts/evaluate.py` shows whether
  Amber/Red weeks actually preceded weaker returns.
