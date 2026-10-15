"""Optional email for a stock run, over the capex pipeline's SMTP secrets (ALERT_EMAIL_TO, SMTP_*).
A batch sends one summary email (a batch of one sends the full single-stock email)."""
from __future__ import annotations

import html
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from cew import notify as cew_notify
from cew import util

from . import prices, publish, scoring

RATING_COLOR = {"Strong Buy": "#1a7f37", "Buy": "#2f9e44", "Hold": "#8a6d00", "Sell": "#c25e00", "Strong Sell": "#c62828"}
MUTED = cew_notify.MUTED


def _fmt(v, d=1, suffix=""):
    return "n/a" if v is None else f"{v:,.{d}f}{suffix}"


def quote_text(q: dict | None) -> str | None:
    """"8.68 at 4:00 pm ET on 2026-10-14" for a quote, or None."""
    if not q or q.get("price") is None:
        return None
    try:
        t = datetime.fromisoformat(str(q.get("as_of")).replace("Z", "+00:00"))
    except ValueError:
        return f"{q['price']:,.2f}"
    return f"{q['price']:,.2f} at {prices.clock_et(t)} on {prices.et(t).date().isoformat()}"


def close_lines(r: dict) -> list[str]:
    """'Scored on the close of <date>', the stale-close note, the quote and the 16:00-18:00 ET flag."""
    price, cn = r.get("price") or {}, r.get("close_note") or {}
    if not price.get("date"):
        return ["No price data this run"]
    first = f"Scored on the close of {price['date']} ({_fmt(price.get('close'), 2)})"
    if cn.get("note"):
        first += f"; {cn['note']}"
    out = [first]
    qt = quote_text(r.get("quote"))
    if qt:
        out.append(f"Latest quote {qt} (display only, not scored)")
    if cn.get("close_window"):
        out.append(cn["close_window"])
    return out


def build_email(r: dict, dashboard_url: str | None) -> tuple[str, str, str]:
    w = r.get("writeup") or {}
    headline = w.get("headline") or f"{r['name']} rated {r['rating']}"
    subject = f"{r.get('subject_prefix', '[Stock]')} {r['ticker']} {r['rating']} {r['score']}/100 ({r['confidence']} confidence) — {headline}"
    ov = r.get("overlay") or {}
    rows = []
    for k in scoring.FACTORS:
        f = (r.get("factors") or {}).get(k) or {}
        val = "no data" if f.get("score") is None else f"{f['score']}/100"
        note = f.get("missing") or ""
        rows.append(f"<tr><td style='padding:3px 12px 3px 0'>{scoring.LABELS[k]}</td><td style='padding:3px 12px 3px 0;"
                    f"font-weight:600'>{val}</td><td style='{MUTED}'>{html.escape(note)}</td></tr>")
    year = int(r["run_id"][:4])
    ev = []
    for k, t in ((r.get("news") or {}).get("topics") or {}).items():
        items = "".join(f"<li>{cew_notify.when_html(e, year)} {html.escape(e.get('claim') or '')}{cew_notify._link(e)}</li>"
                        for e in t.get("evidence") or [])
        weak = (f" <span style='background:#fdecc8;border-radius:4px;padding:0 5px;font-size:12px'>{scoring.WEAK_SOURCES}</span>"
                if t.get("weak_sources_note") else "")
        notes = "; ".join(html.escape(t[n]) for n in ("cap_note", "weak_sources_note") if t.get(n))
        ev.append(f"<p style='margin:10px 0 0;font-weight:600'>{html.escape(t.get('label') or k)}: {t.get('rating', 0):+d}{weak}"
                  f"{' (' + notes + ')' if notes else ''}</p>"
                  f"<p style='margin:2px 0'>{html.escape(t.get('summary') or '')}</p>" + (f"<ul>{items}</ul>" if items else ""))
    g = r.get("guidance") or {}
    quotes = "".join(f"<li>{html.escape(x['metric'])} · {html.escape(x['period'])}: “{html.escape(x['sentence'][:400])}”</li>"
                     for x in g.get("guidance") or [])
    li = lambda xs: "".join(f"<li>{html.escape(x)}</li>" for x in xs or [])  # noqa: E731
    cost = (r.get("cost") or {}).get("total_usd")
    link = f"<p><a href='{html.escape(dashboard_url.rstrip('/') + '/stocks.html#' + r['ticker'])}'>Open on the stocks page</a></p>" if dashboard_url else ""
    errs = r.get("errors") or {}
    body_html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px">
<p style="font-size:13px;color:#555">Stock research · {html.escape(r['run_id'])} · {html.escape(r['name'])} ({html.escape(r['ticker'])})</p>
<h2 style="margin:0 0 4px;color:{RATING_COLOR.get(r['rating'], '#333')}">{html.escape(r['rating'])} — {r['score']} / 100</h2>
<p style="margin-top:0">{html.escape(headline)} · {html.escape(r['confidence'])} confidence ({len((r.get('composite') or {}).get('available') or [])} of 6 factors had data)</p>
{f"<p style='font-size:13px;color:#555'><b>Capex overlay:</b> {html.escape(ov['note'])}</p>" if ov.get('note') and ov.get('ai_exposed') else ''}
<p>{html.escape(w.get('thesis') or '')}</p>
<h3>Factors</h3><table>{''.join(rows)}</table>
{'<h3>Key risks</h3><ul>' + li(w.get('key_risks')) + '</ul>' if w.get('key_risks') else ''}
{'<h3>What would change this rating</h3><ul>' + li(w.get('what_would_change')) + '</ul>' if w.get('what_would_change') else ''}
{'<h3>Guidance in the latest earnings release</h3><ul>' + quotes + '</ul>' if quotes else ''}
{'<h3>News</h3>' + ''.join(ev) if ev else ''}
<p style="font-size:14px">{'<br>'.join(html.escape(x) for x in close_lines(r))}</p>
<p style="font-size:13px;color:#555">Cost {'n/a' if cost is None else f'${cost:.2f}'}</p>
{link}{"<p style='color:#777;font-size:12px'>Data issues: " + html.escape('; '.join(f'{k}: {v}' for k, v in errs.items())[:800]) + "</p>" if errs else ''}
<p style="color:#777;font-size:12px">{html.escape(publish.DISCLAIMER)}</p></div>"""
    body_text = (f"{r['ticker']} {r['rating']} {r['score']}/100 ({r['confidence']} confidence)\n{headline}\n"
                 + "".join(f"{x}\n" for x in close_lines(r)) + "\n"
                 f"{w.get('thesis') or ''}\n\n" + "\n".join(
                     f"{scoring.LABELS[k]}: {((r.get('factors') or {}).get(k) or {}).get('score', 'no data')}" for k in scoring.FACTORS)
                 + (f"\n\nCapex overlay: {ov['note']}" if ov.get("note") and ov.get("ai_exposed") else "")
                 + "".join(f"\n{t.get('label') or k}: {t.get('rating', 0):+d}, {scoring.WEAK_SOURCES} ({t['weak_sources_note']})"
                           for k, t in ((r.get("news") or {}).get("topics") or {}).items() if t.get("weak_sources_note"))
                 + f"\n\n{publish.DISCLAIMER}")
    return subject, body_html, body_text


def build_batch_email(b: dict, dashboard_url: str | None, prefix: str = "[Stock]") -> tuple[str, str, str]:
    """One email for a batch: each ticker's rating (and the one before), price and quote, the failures."""
    done = [x for x in b["tickers"] if x["status"] == "done"]
    failed = [x for x in b["tickers"] if x["status"] != "done"]
    head = ", ".join(f"{x['ticker']} {x['rating']} {x['score']}" for x in done[:8]) + (" …" if len(done) > 8 else "")
    subject = (f"{prefix} Batch of {len(b['tickers'])}: {head or 'nothing rated'}"
               + (f" ({len(failed)} failed)" if failed else ""))
    base = dashboard_url.rstrip("/") + "/stocks.html#" if dashboard_url else None
    td = "padding:4px 10px 4px 0;border-bottom:1px solid #eee;vertical-align:top"

    def was(x):
        p = x.get("previous")
        return "new" if not p else f"{p.get('rating')} {'' if p.get('score') is None else int(p['score'])}".strip()

    def row(x):
        tk = html.escape(x["ticker"])
        tk = f"<a href='{html.escape(base + x['ticker'])}'>{tk}</a>" if base else tk
        scored = f"{_fmt(x.get('price'), 2)} ({html.escape(str(x.get('scored_on') or 'n/a'))})"
        note = f"<br><span style='{MUTED}'>{html.escape(x['close_note'])}</span>" if x.get("close_note") else ""
        cost = "n/a" if x.get("cost_usd") is None else f"${x['cost_usd']:.2f}"
        return (f"<tr><td style='{td};font-weight:600'>{tk}</td>"
                f"<td style='{td};color:{RATING_COLOR.get(x['rating'], '#333')};font-weight:600'>{html.escape(x['rating'])}</td>"
                f"<td style='{td}'>{x['score']}</td><td style='{td}'>{html.escape(x['confidence'])}</td>"
                f"<td style='{td}'>{html.escape(was(x))}</td><td style='{td}'>{scored}{note}</td>"
                f"<td style='{td}'>{html.escape(quote_text(x.get('quote')) or '—')}</td>"
                f"<td style='{td}'>{cost}</td></tr>")
    th = "".join(f"<th style='{td};text-align:left'>{h}</th>" for h in
                 ("Ticker", "Rating", "Score", "Confidence", "Before", "Scored on close", "Latest quote", "Cost"))
    fails = "".join(f"<li><b>{html.escape(x['ticker'])}</b>: {html.escape(str(x.get('error') or 'failed'))}</li>" for x in failed)
    total = b.get("cost_usd")
    total_text = "n/a" if total is None else f"${total:.2f}"
    run_link = f" · <a href='{html.escape(b['run_url'])}'>workflow run</a>" if b.get("run_url") else ""
    page_link = (f" · <a href='{html.escape(dashboard_url.rstrip('/') + '/stocks.html')}'>Open the stocks page</a>"
                 if dashboard_url else "")
    table = (f"<table style='border-collapse:collapse;font-size:14px'><tr>{th}</tr>{''.join(row(x) for x in done)}</table>"
             if done else "<p>No ticker was rated.</p>")
    window = f"<p style='font-size:13px'>{html.escape(b['close_window'])}.</p>" if b.get("close_window") else ""
    body_html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px">
<p style="font-size:13px;color:#555">Stock research batch · {len(done)} rated, {len(failed)} failed{run_link}</p>
{table}
{f"<h3>Failed</h3><ul>{fails}</ul>" if fails else ""}
{window}
<p style="font-size:13px;color:#555">Total cost {total_text}{page_link}</p>
<p style="color:#777;font-size:12px">{html.escape(publish.DISCLAIMER)}</p></div>"""
    lines = [f"{x['ticker']}: {x['rating']} {x['score']}/100 ({x['confidence']}), before: {was(x)}; scored on the close of "
             f"{x.get('scored_on') or 'n/a'}" + (f" ({x['close_note']})" if x.get("close_note") else "")
             + (f"; latest quote {quote_text(x.get('quote'))}" if quote_text(x.get("quote")) else "") for x in done]
    lines += [f"{x['ticker']}: FAILED: {x.get('error') or 'failed'}" for x in failed]
    body_text = ("\n".join(lines) + (f"\n\n{b['close_window']}." if b.get("close_window") else "")
                 + f"\n\nTotal cost: {total_text}\n\n{publish.DISCLAIMER}")
    return subject, body_html, body_text


def _deliver(subject: str, body_html: str, body_text: str) -> str:
    to, user, pwd = util.env("ALERT_EMAIL_TO"), util.env("SMTP_USERNAME"), util.env("SMTP_PASSWORD")
    if not (to and user and pwd):
        return "skipped: ALERT_EMAIL_TO / SMTP_USERNAME / SMTP_PASSWORD not set"
    host, port = util.env("SMTP_HOST", "smtp.gmail.com"), int(util.env("SMTP_PORT", "465"))
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    rcpt = [x.strip() for x in to.split(",")]
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, pwd)
            s.sendmail(user, rcpt, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.sendmail(user, rcpt, msg.as_string())
    return f"sent to {len(rcpt)} recipient(s)"


def send(r: dict) -> str:
    return _deliver(*build_email(r, util.env("DASHBOARD_URL")))


def send_batch(b: dict, prefix: str = "[Stock]") -> str:
    return _deliver(*build_batch_email(b, util.env("DASHBOARD_URL"), prefix))
