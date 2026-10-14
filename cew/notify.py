"""Email the weekly digest over SMTP (Gmail app password by default)."""
from __future__ import annotations

import html
import re
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown as md

from . import util

LEVEL_COLOR = {"GREEN": "#1a7f37", "AMBER": "#b26b00", "RED": "#c62828"}
FOLLOW_UP_LABEL = {"resolved": "Resolved", "improved": "Improved", "worsened": "Worsened",
                   "still_open": "Still open", "no_news": "No news"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MUTED = "color:#777;font-size:12px"
BACKGROUND = ("font-size:11px;font-weight:600;color:#666;border:1px solid #ccc;border-radius:4px;"
              "padding:0 4px;margin-left:4px")


def _host(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else "link"


def day(d: str, year: int | None = None) -> str:
    """'2026-10-09' -> 'Oct 9', with the year when it isn't `year`."""
    try:
        x = date.fromisoformat(d)
    except ValueError:
        return d
    return f"{MONTHS[x.month - 1]} {x.day}" + ("" if x.year == year else f", {x.year}")


def when_text(e: dict, year: int | None = None) -> str:
    """Event date for the plain-text email: 'Oct 9', 'Sep 20 (background)' or 'undated'."""
    if not e.get("event_date"):
        return "undated"
    return day(e["event_date"], year) + (" (background)" if e.get("background") else "")


def when_html(e: dict, year: int | None = None) -> str:
    if not e.get("event_date"):
        return f"<span style='{MUTED}'>undated</span>"
    bg = f"<span style='{BACKGROUND}'>background</span>" if e.get("background") else ""
    return f"<span style='{MUTED}'>{html.escape(day(e['event_date'], year))}</span>{bg}"


def _link(src: dict) -> str:
    if not src.get("url"):
        return ""
    meta = ", ".join(x for x in (src.get("status"), src.get("tier")) if x)
    text = "SEC filing" if src.get("source") == "edgar" else _host(src["url"])
    return (f" <a href='{html.escape(src['url'])}'>{html.escape(text)}</a>"
            + (f" <span style='{MUTED}'>({html.escape(meta)})</span>" if meta else ""))


def follow_ups_html(items: list[dict], year: int | None = None) -> str:
    rows = []
    for f in items:
        src = f.get("source") or {}
        when = f" {when_html(f, year)}" if src.get("url") or f.get("event_date") else ""
        note = f": {html.escape(f['note'])}" if f.get("note") else ""
        rows.append(f"<li><b>{html.escape(FOLLOW_UP_LABEL.get(f.get('status'), f.get('status') or ''))}</b> · "
                    f"{html.escape(f['item'])}{note}{when}{_link(src)}</li>")
    return "<h3>Follow-ups from last run</h3><ul>" + "".join(rows) + "</ul>" if rows else ""


def evidence_html(report: dict, year: int | None = None) -> str:
    """Every category's evidence, each item with its event date (background when old)."""
    parts = []
    for key, label in report["categories"]:
        ev = report["scores"][key].get("evidence") or []
        if ev:
            items = "".join(f"<li>{when_html(e, year)} {html.escape(e.get('claim') or '')}{_link(e)}</li>" for e in ev)
            parts.append(f"<p style='margin:10px 0 0;font-weight:600'>{html.escape(label)}</p>"
                         f"<ul style='margin-top:4px'>{items}</ul>")
    return "<h3>Evidence</h3>" + "".join(parts) if parts else ""


def build_email(report: dict, dashboard_url: str | None) -> tuple[str, str, str]:
    comp = report["composite"]
    level = comp["level"]
    a = (report.get("claude") or {}).get("assessment") or {}
    headline = a.get("headline") or "Weekly capex early-warning update"
    prefix = "ALERT " if comp["alert"] else ""
    trigger = report.get("trigger") or ""
    if trigger.startswith("event:"):
        prefix = f"Event {trigger[6:].replace('+', ', ')} · " + prefix
    shown = level
    if level == "RED":
        shown = "RED (confirmed)" if comp.get("red_confirmed") else "RED (unconfirmed)"
    subject = f"[Capex EW] {prefix}{shown} {comp['total']:g}/{comp['max']:g} — {headline}"

    jumps = comp.get("jumps") or {}
    rows = []
    for key, label in report["categories"]:
        s = report["scores"][key]
        ev = s.get("evidence") or []
        checked = [e for e in ev if e.get("status")]
        src = ""
        if checked:
            v = sum(1 for e in checked if e["status"] == "verified")
            src = f"{v}/{len(checked)} sources verified"
        tiers = ", ".join(f"{n} {t}" for t in ("primary", "major", "other")
                          if (n := sum(1 for e in ev if e.get("tier") == t)))
        flag = f"rose {jumps[key]:+d} since last run, check it" if key in jumps else ""
        unstable = (f"unstable: scoring samples {', '.join(map(str, s['samples']))}" if s.get("unstable") else "")
        note = " · ".join(x for x in (src, tiers, flag, unstable) if x)
        rows.append(f"<tr><td style='padding:4px 12px 4px 0'>{html.escape(label)}</td>"
                    f"<td style='padding:4px 12px 4px 0;font-weight:600'>{s['score']}/3</td>"
                    f"<td style='padding:4px 0;color:#777;font-size:12px'>{html.escape(note)}</td></tr>")
    digest_html = md.markdown(a.get("digest_markdown") or "_Claude digest unavailable this week; scores below are rule-based only._")
    watch = "".join(f"<li>{html.escape(w)}</li>" for w in a.get("watch_next", []))
    year = int(report["date"][:4])
    follow = follow_ups_html(report.get("follow_ups") or [], year)
    evidence = evidence_html(report, year)
    stage = (report.get("credit_stage") or {}).get("line")
    stage_html = (f"<p style='font-size:13px;color:#555'>Credit contagion stage (context, not scored): "
                  f"{html.escape(stage)}</p>") if stage else ""
    held = comp.get("held")
    held_html = f"<p style='font-size:13px;color:#555'>{html.escape(held['note'])}</p>" if held else ""
    link = f"<p><a href='{html.escape(dashboard_url)}'>Open the dashboard</a></p>" if dashboard_url else ""
    errors = report.get("errors") or {}
    err_html = ""
    if errors:
        err_html = "<p style='color:#777;font-size:12px'>Data issues this run: " + html.escape(
            "; ".join(f"{k}: {v}" for k, v in errors.items())[:800]) + "</p>"

    conf = comp.get("confirmation") or {}
    conf_html = ""
    if level == "RED" or conf.get("categories"):
        cats = ", ".join(conf.get("categories") or []) or "none yet"
        conf_html = (f"<p style='font-size:13px;color:#555'>Confirmation: categories at "
                     f"{conf.get('min_score', 2)}+ in each of the last {conf.get('runs', 2)} runs: "
                     f"{html.escape(cats)} (RED counts as confirmed with {conf.get('min_categories', 2)}+).</p>")
    when = f"Run {report.get('run_id') or report['date']}"
    if trigger:
        when += f" · {html.escape(trigger)}"
    events_html = ""
    if report.get("events"):
        events_html = "<p style='font-size:13px;color:#555'>Event: " + html.escape(
            "; ".join(f"{e.get('title') or e['name']} ({e['date']})" for e in report["events"])) + "</p>"

    body_html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px">
<p style="font-size:13px;color:#555">{when}</p>{events_html}
<h2 style="margin:0 0 4px;color:{LEVEL_COLOR.get(level, '#333')}">{shown} — {comp['total']:g} / {comp['max']:g}</h2>
<p style="margin-top:0">{html.escape(headline)}</p>{held_html}{conf_html}
{digest_html}
<h3>Category scores</h3><table>{''.join(rows)}</table>{stage_html}
{follow}
{'<h3>Watch next</h3><ul>' + watch + '</ul>' if watch else ''}
{evidence}
{link}{err_html}
<p style="color:#777;font-size:12px">Automated research summary, not investment advice.</p></div>"""
    body_text = f"{shown} {comp['total']:g}/{comp['max']:g}\n{headline}\n\n{a.get('digest_markdown', '')}"
    if stage:
        body_text += f"\n\nCredit contagion stage: {stage}"
    if report.get("follow_ups"):
        body_text += "\n\nFollow-ups from last run:\n" + "\n".join(
            f"- {FOLLOW_UP_LABEL.get(f.get('status'), f.get('status'))}: {f['item']}"
            + (f" ({f['note']})" if f.get("note") else "")
            + (f" [{when_text(f, year)}]" if f.get("url") or f.get("event_date") else "") for f in report["follow_ups"])
    ev_lines = [f"- {label}: [{when_text(e, year)}] {e.get('claim') or ''} {e.get('url') or ''}".rstrip()
                for key, label in report["categories"] for e in report["scores"][key].get("evidence") or []]
    if ev_lines:
        body_text += "\n\nEvidence:\n" + "\n".join(ev_lines)
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
