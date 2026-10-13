"""Email the weekly digest over SMTP (Gmail app password by default)."""
from __future__ import annotations

import html
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown as md

from . import util

LEVEL_COLOR = {"GREEN": "#1a7f37", "AMBER": "#b26b00", "RED": "#c62828"}


def build_email(report: dict, dashboard_url: str | None) -> tuple[str, str, str]:
    comp = report["composite"]
    level = comp["level"]
    a = (report.get("claude") or {}).get("assessment") or {}
    headline = a.get("headline") or "Weekly capex early-warning update"
    prefix = "ALERT " if comp["alert"] else ""
    subject = f"[Capex EW] {prefix}{level} {comp['total']:g}/{comp['max']:g} — {headline}"

    rows = []
    for key, label in report["categories"]:
        s = report["scores"][key]
        rows.append(f"<tr><td style='padding:4px 12px 4px 0'>{html.escape(label)}</td>"
                    f"<td style='padding:4px 0;font-weight:600'>{s['score']}/3</td></tr>")
    digest_html = md.markdown(a.get("digest_markdown") or "_Claude digest unavailable this week; scores below are rule-based only._")
    watch = "".join(f"<li>{html.escape(w)}</li>" for w in a.get("watch_next", []))
    link = f"<p><a href='{html.escape(dashboard_url)}'>Open the dashboard</a></p>" if dashboard_url else ""
    errors = report.get("errors") or {}
    err_html = ""
    if errors:
        err_html = "<p style='color:#777;font-size:12px'>Data issues this run: " + html.escape(
            "; ".join(f"{k}: {v}" for k, v in errors.items())[:800]) + "</p>"

    body_html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px">
<p style="font-size:13px;color:#555">Week of {report['date']}</p>
<h2 style="margin:0 0 4px;color:{LEVEL_COLOR.get(level, '#333')}">{level} — {comp['total']:g} / {comp['max']:g}</h2>
<p style="margin-top:0">{html.escape(headline)}</p>
{digest_html}
<h3>Category scores</h3><table>{''.join(rows)}</table>
{'<h3>Watch next</h3><ul>' + watch + '</ul>' if watch else ''}
{link}{err_html}
<p style="color:#777;font-size:12px">Automated research summary, not investment advice.</p></div>"""
    body_text = f"{level} {comp['total']:g}/{comp['max']:g}\n{headline}\n\n{a.get('digest_markdown', '')}"
    return subject, body_html, body_text


def send(report: dict) -> str:
    to = util.env("ALERT_EMAIL_TO")
    user = util.env("SMTP_USERNAME")
    pwd = util.env("SMTP_PASSWORD")
    if not (to and user and pwd):
        return "skipped: ALERT_EMAIL_TO / SMTP_USERNAME / SMTP_PASSWORD not set"
    host = util.env("SMTP_HOST", "smtp.gmail.com")
    port = int(util.env("SMTP_PORT", "465"))
    subject, body_html, body_text = build_email(report, util.env("DASHBOARD_URL"))

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, pwd)
            s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    return f"sent to {len(to.split(','))} recipient(s)"
