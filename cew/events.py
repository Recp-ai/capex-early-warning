"""Dated events (config/events.yaml) and the daily event-day check.

The workflow's daily cron (EVENT_CHECK_CRON, 23:30 UTC = 07:30 UTC+8) runs `python -m cew.events plan`.
It starts the full pipeline only if a listed event fell on the US Eastern calendar day that is just
ending (after the US close, so after-hours earnings are out) and no run has happened on that day yet.

Run triggers, as recorded in data/runs.csv: "schedule" (weekly cron), "manual" (workflow_dispatch or
local), "event:<name>+<name>" (event-day run, or a manual run with the `event` input).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Mapping

import yaml

from . import util

EVENTS_PATH = util.ROOT / "config" / "events.yaml"
EVENT_CHECK_CRON = "30 23 * * *"  # must match the second schedule in .github/workflows/weekly.yml
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,40}$")
TYPES = ("fomc", "cpi", "earnings")

try:
    from zoneinfo import ZoneInfo
    US_TZ = ZoneInfo("America/New_York")
except Exception:  # no tz database: fixed EST is close enough for a 23:30 UTC check
    US_TZ = timezone(timedelta(hours=-5))


def load(path=None) -> list[dict]:
    """Events sorted by date; dates as ISO strings."""
    with open(path or EVENTS_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out = []
    for e in raw.get("events") or []:
        d = e.get("date")
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        out.append({**e, "date": d.isoformat(), "verified": bool(e.get("verified"))})
    return sorted(out, key=lambda e: (e["date"], e["name"]))


def us_date(moment: datetime) -> date:
    """US Eastern calendar date of a (timezone-aware) moment."""
    return moment.astimezone(US_TZ).date()


def on(events: list[dict], day: date) -> list[dict]:
    return [e for e in events if e["date"] == day.isoformat()]


def parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def ran_on(day: date, runs: list[dict]) -> bool:
    """Whether any run in data/runs.csv started on this US Eastern calendar day."""
    for r in runs:
        t = parse_ts(r.get("timestamp", ""))
        if t is not None and us_date(t if t.tzinfo else t.replace(tzinfo=timezone.utc)) == day:
            return True
    return False


def trigger_for(names: list[str]) -> str:
    return "event:" + "+".join(dict.fromkeys(names))


def names_in(trigger: str | None) -> list[str]:
    """Event names in an 'event:a+b' trigger (empty for schedule/manual)."""
    if not trigger or not trigger.startswith("event:"):
        return []
    return [n for n in trigger[len("event:"):].split("+") if NAME_RE.fullmatch(n)]


def requested(value: str | None, events: list[dict]) -> list[str]:
    """Validate a workflow_dispatch `event` input: known event names, separated by , + or spaces."""
    known = {e["name"] for e in events}
    out = []
    for n in re.split(r"[\s,+]+", (value or "").strip()):
        if not n:
            continue
        if NAME_RE.fullmatch(n) and n in known:
            out.append(n)
        else:
            print(f"Ignoring unknown event name: {n[:60]!r}")
    return list(dict.fromkeys(out))


def plan(environ: Mapping[str, str], now: datetime, events: list[dict], runs: list[dict]) -> dict:
    """Decide whether this workflow run should run the pipeline, and with which trigger tag."""
    kind = environ.get("EVENT_NAME", "")
    if kind == "schedule" and (environ.get("SCHEDULE") or "").strip() == EVENT_CHECK_CRON:
        day = us_date(now)
        todays = on(events, day)
        if not todays:
            return {"run": False, "trigger": "", "reason": f"No listed event on {day} (US Eastern)"}
        names = [e["name"] for e in todays]
        if ran_on(day, runs):
            return {"run": False, "trigger": "",
                    "reason": f"{', '.join(names)} on {day}, but a run already happened that day"}
        return {"run": True, "trigger": trigger_for(names), "reason": f"Event day {day}: {', '.join(names)}"}
    if kind == "schedule":
        return {"run": True, "trigger": "schedule", "reason": "Weekly schedule"}
    names = requested(environ.get("INPUT_EVENT"), events)
    if names:
        return {"run": True, "trigger": trigger_for(names), "reason": "Manual run tagged as an event run"}
    return {"run": True, "trigger": "manual", "reason": "Manual run"}


def for_trigger(trigger: str, events: list[dict], day: date) -> list[dict]:
    """The events behind an event trigger: that day's listing, else the nearest date per name."""
    names = names_in(trigger)
    if not names:
        return []
    todays = {e["name"]: e for e in on(events, day)}
    out = []
    for n in names:
        if n in todays:
            out.append(todays[n])
            continue
        same = [e for e in events if e["name"] == n]
        if same:
            out.append(min(same, key=lambda e: abs((date.fromisoformat(e["date"]) - day).days)))
    return out


def upcoming(events: list[dict], day: date, horizon_days: int = 120) -> list[dict]:
    end = (day + timedelta(days=horizon_days)).isoformat()
    keys = ("date", "name", "type", "ticker", "title", "time_et", "verified")
    return [{k: e.get(k) for k in keys} for e in events if day.isoformat() <= e["date"] <= end]


def horizon_warning(events: list[dict], day: date, days: int = 30) -> str | None:
    """A note when config/events.yaml is about to run out of dates."""
    last = max((e["date"] for e in events), default=None)
    if last is None:
        return "config/events.yaml lists no events"
    if date.fromisoformat(last) < day + timedelta(days=days):
        return f"config/events.yaml lists no events after {last}; add the next dates"
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["plan"]:
        print("usage: python -m cew.events plan", file=sys.stderr)
        return 2
    p = plan(os.environ, util.now_utc(), load(), util.read_csv(util.DATA / "runs.csv"))
    print(f"run={str(p['run']).lower()} trigger={p['trigger'] or '-'}: {p['reason']}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:  # values are validated names, no newlines
            f.write(f"run={str(p['run']).lower()}\ntrigger={p['trigger']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
