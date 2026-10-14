"""config/stocks.yaml, the module's data paths, ticker validation and per-step model choice."""
from __future__ import annotations

import os
import re
from typing import Mapping

import yaml

from cew import models, util

CONFIG_PATH = util.ROOT / "config" / "stocks.yaml"
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
STEPS = ("research", "scoring")
DEFAULTS = {"research": "claude-sonnet-5-5", "scoring": "claude-opus-5-5"}


def load(path=CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# Paths are functions so tests can point util.DATA / util.SITE_DATA at a sandbox.
def data_dir():
    return util.DATA / "stocks"


def site_dir():
    return util.SITE_DATA / "stocks"


def normalize_ticker(raw: str | None) -> str | None:
    """Upper-cased ticker if it matches ^[A-Z][A-Z0-9.-]{0,9}$, else None."""
    t = (raw or "").strip().upper()
    return t if TICKER_RE.fullmatch(t) else None


def resolve_models(cfg: dict, environ: Mapping[str, str] | None = None) -> dict:
    """Each step's model: the workflow input (INPUT_RESEARCH_MODEL / INPUT_SCORING_MODEL), else
    config/stocks.yaml, else the default. Invalid values are skipped with a warning."""
    environ = os.environ if environ is None else environ
    configured = cfg.get("models") or {}
    out: dict = {"sources": {}}
    for step in STEPS:
        for source, value in (("input", environ.get(f"INPUT_{step.upper()}_MODEL")), ("config", configured.get(step))):
            value = (value or "").strip()
            if not value:
                continue
            if models.valid(value):
                out[step], out["sources"][step] = value, source
                break
            print(f"Ignoring invalid {step} model from {source}: {value[:60]!r}")
        else:
            out[step], out["sources"][step] = DEFAULTS[step], "default"
    return out
