"""Claude model choice per step, prices, per-run cost, and the dashboard's model list.

Each step's model comes from the first valid value of: the workflow_dispatch input
(INPUT_*_MODEL), the repo Actions variable (VAR_*_MODEL), then config/signals.yaml.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping

import yaml

from . import util

MODEL_RE = re.compile(r"^claude-[a-z0-9.-]+$")
STEPS = ("research", "assessment")
DEFAULTS = {"research": "claude-sonnet-5-5", "assessment": "claude-opus-5-5"}  # if config is unusable
PRICES_PATH = util.ROOT / "config" / "models.yaml"
CATALOG_NAME = "models.json"
# Prompt-cache tokens are billed relative to the input price (5-minute cache writes, cache reads).
CACHE_WRITE_X, CACHE_READ_X = 1.25, 0.1


def valid(model: Any) -> bool:
    return isinstance(model, str) and bool(MODEL_RE.fullmatch(model))  # fullmatch: "$" alone allows a trailing \n


def resolve(cfg: dict, environ: Mapping[str, str] | None = None, use_inputs: bool = True) -> dict:
    """Pick each step's model; invalid candidates are skipped with a warning."""
    environ = os.environ if environ is None else environ
    configured = cfg.get("models") or {}
    out: dict = {"sources": {}}
    for step in STEPS:
        key = step.upper()
        candidates = [("variable", environ.get(f"VAR_{key}_MODEL")), ("config", configured.get(step))]
        if use_inputs:
            candidates.insert(0, ("input", environ.get(f"INPUT_{key}_MODEL")))
        for source, value in candidates:
            value = (value or "").strip()
            if not value:
                continue
            if valid(value):
                out[step], out["sources"][step] = value, source
                break
            print(f"Ignoring invalid {step} model from {source}: {value[:60]!r}")
        else:
            out[step], out["sources"][step] = DEFAULTS[step], "default"
    return out


# ---------------------------------------------------------------- prices and cost
def load_prices(path=PRICES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data["models"] = data.get("models") or {}
    data["web_search_per_1k"] = float(data.get("web_search_per_1k", 10.0))
    return data


def price_of(prices: dict, model: str) -> dict | None:
    p = prices["models"].get(model) or {}
    if p.get("input") is None or p.get("output") is None:
        return None
    return {"input": float(p["input"]), "output": float(p["output"]), "verify": bool(p.get("verify"))}


def new_usage(model: str) -> dict:
    return {"model": model, "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "web_searches": 0}


def add_usage(usage: dict, resp: Any) -> None:
    """Add one Messages API response. Call it for every response, including pause_turn ones."""
    u = getattr(resp, "usage", None)
    usage["requests"] += 1
    if u is None:
        return
    usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    usage["cache_write_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    usage["cache_read_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    usage["web_searches"] += getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0


def step_cost(usage: dict, prices: dict) -> float | None:
    """Estimated USD for one step, or None when the model has no price in config/models.yaml."""
    p = price_of(prices, usage["model"])
    if p is None:
        return None
    input_equiv = (usage["input_tokens"] + usage["cache_write_tokens"] * CACHE_WRITE_X
                   + usage["cache_read_tokens"] * CACHE_READ_X)
    tokens = (input_equiv * p["input"] + usage["output_tokens"] * p["output"]) / 1e6
    return round(tokens + usage["web_searches"] * prices["web_search_per_1k"] / 1000, 4)


def cost_summary(usage_by_step: dict, prices: dict) -> dict:
    steps, total, unpriced = {}, 0.0, []
    for step, u in usage_by_step.items():
        c = step_cost(u, prices)
        steps[step] = {**u, "price": price_of(prices, u["model"]), "cost_usd": c}
        if c is None:
            unpriced.append(step)
        else:
            total += c
    return {"steps": steps, "total_usd": round(total, 4), "unpriced_steps": unpriced,
            "web_search_per_1k": prices["web_search_per_1k"]}


# ---------------------------------------------------------------- tiered prices (used by the stock module)
# Some models price a request by its prompt size: config/models.yaml `tiers: [{above_input_tokens, input,
# output}]` replace the base input / output price for a request whose prompt (input + cache write + cache
# read tokens) is above the threshold. That needs each request's tokens, so callers record them with
# `add_request` (or `record_request` around a call that adds usage itself). The functions above, which the
# capex pipeline uses, are unchanged and price at the base rate.
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "web_searches")


def tiers_of(prices: dict, model: str) -> list[dict]:
    """The model's prompt-size tiers, lowest threshold first ([] when it has none)."""
    out = []
    for t in (prices["models"].get(model) or {}).get("tiers") or []:
        try:
            out.append({"above_input_tokens": int(t["above_input_tokens"]), "input": float(t["input"]),
                        "output": float(t["output"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda t: t["above_input_tokens"])


def snapshot(usage: dict) -> dict:
    return {k: usage.get(k, 0) for k in TOKEN_KEYS}


def record_request(usage: dict, before: dict) -> None:
    """Log the tokens added to `usage` since `before` (one request) in usage["per_request"]."""
    usage.setdefault("per_request", []).append({k: usage.get(k, 0) - before.get(k, 0) for k in TOKEN_KEYS})


def add_request(usage: dict, resp: Any) -> None:
    """`add_usage` plus a per-request record, for tiered prices."""
    before = snapshot(usage)
    add_usage(usage, resp)
    record_request(usage, before)


def _rate(base: dict, tiers: list[dict], prompt_tokens: float) -> dict:
    rate = {"input": base["input"], "output": base["output"], "tier": None}
    for t in tiers:
        if prompt_tokens > t["above_input_tokens"]:
            rate = {"input": t["input"], "output": t["output"], "tier": t["above_input_tokens"]}
    return rate


def _tokens_cost(u: dict, rate: dict) -> float:
    input_equiv = (u["input_tokens"] + u["cache_write_tokens"] * CACHE_WRITE_X + u["cache_read_tokens"] * CACHE_READ_X)
    return (input_equiv * rate["input"] + u["output_tokens"] * rate["output"]) / 1e6


def step_cost_tiered(usage: dict, prices: dict) -> tuple[float | None, int]:
    """(estimated USD, requests priced at a higher tier). Each request in usage["per_request"] is priced by
    its own prompt size; tokens not logged per request (if any) at the average prompt size of the rest."""
    p = price_of(prices, usage["model"])
    if p is None:
        return None, 0
    tiers = tiers_of(prices, usage["model"])
    reqs = usage.get("per_request") or []
    if not tiers or not reqs:
        return step_cost(usage, prices), 0
    total, high = 0.0, 0
    for r in reqs:
        rate = _rate(p, tiers, r["input_tokens"] + r["cache_write_tokens"] + r["cache_read_tokens"])
        high += rate["tier"] is not None
        total += _tokens_cost(r, rate)
    rest = {k: usage[k] - sum(r[k] for r in reqs) for k in TOKEN_KEYS}
    n_rest = usage["requests"] - len(reqs)
    if n_rest > 0 and any(rest[k] for k in TOKEN_KEYS[:4]):
        rate = _rate(p, tiers, (rest["input_tokens"] + rest["cache_write_tokens"] + rest["cache_read_tokens"]) / n_rest)
        high += n_rest if rate["tier"] is not None else 0
        total += _tokens_cost(rest, rate)
    return round(total + usage["web_searches"] * prices["web_search_per_1k"] / 1000, 4), high


def cost_summary_tiered(usage_by_step: dict, prices: dict) -> dict:
    """`cost_summary` with prompt-size tiers: each step also gets "tiers" and "tiered_requests"."""
    out = cost_summary(usage_by_step, prices)
    total = 0.0
    for step, u in usage_by_step.items():
        c, high = step_cost_tiered(u, prices)
        out["steps"][step].update(cost_usd=c, tiers=tiers_of(prices, u["model"]), tiered_requests=high)
        total += c or 0.0
    out["total_usd"] = round(total, 4)
    return out


# ---------------------------------------------------------------- model list for the dashboard
def _client():
    import anthropic  # lazy, as in analyst
    return anthropic.Anthropic()


def _web_search_support(caps: Any) -> bool | None:
    try:
        v = caps["server_tools"]["web_search"]["supported"]
    except (KeyError, TypeError):
        return None
    return v if isinstance(v, bool) else None


def fetch_models(client: Any) -> list[dict]:
    """GET /v1/models; iterating the SDK page follows has_more / after_id across all pages."""
    out = []
    for m in client.models.list(limit=100):
        d = m if isinstance(m, dict) else m.to_dict(mode="json")
        if not valid(d.get("id")) or d.get("lifecycle") == "retired":
            continue
        out.append({"id": d["id"], "display_name": d.get("display_name") or d["id"],
                    "created_at": d.get("created_at"),
                    "supports_web_search": _web_search_support(d.get("capabilities"))})
    return out


def write_catalog(prices: dict, defaults: dict, use_api: bool,
                  client_factory: Callable[[], Any] | None = None) -> dict:
    """Write site/data/models.json from the Models API, falling back to config/models.yaml."""
    models, error = [], None
    if use_api:
        try:
            models = fetch_models((client_factory or _client)())
            if not models:
                error = "Models API returned no models"
        except Exception as e:
            error = f"Models API failed: {type(e).__name__}: {str(e)[:200]}"
    else:
        error = "Models API not called (offline, --no-llm, or no ANTHROPIC_API_KEY)"
    source = "api" if models else "config"
    if not models:
        models = [{"id": mid, "display_name": p.get("name") or mid, "created_at": None,
                   "supports_web_search": None}
                  for mid, p in prices["models"].items() if valid(mid)]
    for m in models:
        m["price"] = price_of(prices, m["id"])
    catalog = {"generated_at": util.now_iso(), "source": source, "error": error,
               "web_search_per_1k": prices["web_search_per_1k"],
               "defaults": {s: defaults[s] for s in STEPS}, "models": models}
    util.write_json(util.SITE_DATA / CATALOG_NAME, catalog)
    return catalog
