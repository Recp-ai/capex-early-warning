"""Optional email for a stock run, over the capex pipeline's SMTP secrets (ALERT_EMAIL_TO, SMTP_*)."""
from __future__ import annotations

import html
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from cew import notify as cew_notify
from cew import util

from . import publish, scoring

RATING_COLOR = {"Strong Buy": "#1a7f37", "Buy": "#2f9e44", "Hold": "#8a6d00", "Sell": "#c25e00", "Strong Sell": "#c62828"}
MUTED = cew_notify.MUTED


def _fmt(v, d=1, suffix=""):
    return "n/a" if v is None else f"{v:,.{d}f}{suffix}"


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
        ev.append(f"<p style='margin:10px 0 0;font-weight:600'>{html.escape(t.get('label') or k)}: {t.get('rating', 0):+d}"
                  f"{' (' + html.escape(t['cap_note']) + ')' if t.get('cap_note') else ''}</p>"
                  f"<p style='margin:2px 0'>{html.escape(t.get('summary') or '')}</p>" + (f"<ul>{items}</ul>" if items else ""))
    g = r.get("guidance") or {}
    quotes = "".join(f"<li>{html.escape(x['metric'])} · {html.escape(x['period'])}: “{html.escape(x['sentence'][:400])}”</li>"
                     for x in g.get("guidance") or [])
    li = lambda xs: "".join(f"<li>{html.escape(x)}</li>" for x in xs or [])  # noqa: E731
    price = r.get("price") or {}
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
<p style="font-size:13px;color:#555">Price {_fmt(price.get('close'), 2)} on {html.escape(str(price.get('date') or 'n/a'))} · cost {'n/a' if cost is None else f'${cost:.2f}'}</p>
{link}{"<p style='color:#777;font-size:12px'>Data issues: " + html.escape('; '.join(f'{k}: {v}' for k, v in errs.items())[:800]) + "</p>" if errs else ''}
<p style="color:#777;font-size:12px">{html.escape(publish.DISCLAIMER)}</p></div>"""
    body_text = (f"{r['ticker']} {r['rating']} {r['score']}/100 ({r['confidence']} confidence)\n{headline}\n\n"
                 f"{w.get('thesis') or ''}\n\n" + "\n".join(
                     f"{scoring.LABELS[k]}: {((r.get('factors') or {}).get(k) or {}).get('score', 'no data')}" for k in scoring.FACTORS)
                 + (f"\n\nCapex overlay: {ov['note']}" if ov.get("note") and ov.get("ai_exposed") else "")
                 + f"\n\n{publish.DISCLAIMER}")
    return subject, body_html, body_text


def send(r: dict) -> str:
    to, user, pwd = util.env("ALERT_EMAIL_TO"), util.env("SMTP_USERNAME"), util.env("SMTP_PASSWORD")
    if not (to and user and pwd):
        return "skipped: ALERT_EMAIL_TO / SMTP_USERNAME / SMTP_PASSWORD not set"
    host, port = util.env("SMTP_HOST", "smtp.gmail.com"), int(util.env("SMTP_PORT", "465"))
    subject, body_html, body_text = build_email(r, util.env("DASHBOARD_URL"))
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
