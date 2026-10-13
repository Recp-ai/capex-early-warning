import json
from types import SimpleNamespace as NS

import pytest

from cew import analyst, models, util

CFG = {"models": {"research": "claude-sonnet-5-5", "assessment": "claude-opus-5-5"}}
PRICES = models.load_prices()


# ---------------------------------------------------------------- model choice
def test_precedence_input_then_variable_then_config():
    env = {"INPUT_RESEARCH_MODEL": "claude-fable-5-1", "VAR_RESEARCH_MODEL": "claude-haiku-5-5",
           "VAR_ASSESSMENT_MODEL": "claude-sonnet-5-5"}
    sel = models.resolve(CFG, env)
    assert sel["research"] == "claude-fable-5-1" and sel["sources"]["research"] == "input"
    assert sel["assessment"] == "claude-sonnet-5-5" and sel["sources"]["assessment"] == "variable"
    sel = models.resolve(CFG, {})
    assert (sel["research"], sel["assessment"]) == ("claude-sonnet-5-5", "claude-opus-5-5")
    assert sel["sources"] == {"research": "config", "assessment": "config"}


def test_blank_and_invalid_values_fall_through():
    env = {"INPUT_RESEARCH_MODEL": "claude-x; rm -rf /", "VAR_RESEARCH_MODEL": "claude-haiku-5-5",
           "INPUT_ASSESSMENT_MODEL": "  ", "VAR_ASSESSMENT_MODEL": "gpt-5"}
    sel = models.resolve(CFG, env)
    assert sel["research"] == "claude-haiku-5-5" and sel["sources"]["research"] == "variable"
    assert sel["assessment"] == "claude-opus-5-5" and sel["sources"]["assessment"] == "config"


def test_invalid_config_uses_built_in_defaults():
    sel = models.resolve({"models": {"research": "Claude Sonnet", "assessment": ""}}, {})
    assert sel["research"] == models.DEFAULTS["research"] and sel["sources"]["research"] == "default"
    assert sel["assessment"] == models.DEFAULTS["assessment"]


def test_scheduled_defaults_ignore_dispatch_inputs():
    env = {"INPUT_RESEARCH_MODEL": "claude-fable-5-1", "VAR_RESEARCH_MODEL": "claude-opus-5-5"}
    assert models.resolve(CFG, env, use_inputs=False)["research"] == "claude-opus-5-5"


@pytest.mark.parametrize("model,ok", [
    ("claude-opus-5-5", True), ("claude-haiku-4-5-20251001", True), ("claude-3.5-sonnet", True),
    ("Claude-opus-5-5", False), ("claude-opus 5", False), ("claude-", False), ("gpt-5", False),
    ("claude-opus-5-5\n", False), ("claude-$(id)", False), (None, False),
])
def test_model_id_validation(model, ok):
    assert models.valid(model) is ok


# ---------------------------------------------------------------- cost math
def test_seeded_prices():
    assert models.price_of(PRICES, "claude-opus-5-5") == {"input": 4.0, "output": 20.0, "verify": False}
    assert models.price_of(PRICES, "claude-sonnet-5-5")["input"] == 2.0
    assert models.price_of(PRICES, "claude-fable-5-1")["output"] == 50.0
    assert models.price_of(PRICES, "claude-haiku-5-5")["verify"] is True
    assert PRICES["web_search_per_1k"] == 10.0


def test_step_cost():
    u = dict(models.new_usage("claude-sonnet-5-5"), input_tokens=1_000_000, output_tokens=100_000, web_searches=10)
    assert models.step_cost(u, PRICES) == pytest.approx(2.0 + 1.0 + 0.10)
    u = dict(models.new_usage("claude-opus-5-5"), input_tokens=50_000, output_tokens=5_000)
    assert models.step_cost(u, PRICES) == pytest.approx(0.20 + 0.10)
    u = dict(models.new_usage("claude-opus-5-5"), cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert models.step_cost(u, PRICES) == pytest.approx(4.0 * 1.25 + 4.0 * 0.1)


def test_cost_summary_skips_unpriced_models():
    usage = {"research": dict(models.new_usage("claude-sonnet-5-5"), input_tokens=100_000, web_searches=5),
             "assessment": dict(models.new_usage("claude-unknown-1"), input_tokens=10_000)}
    c = models.cost_summary(usage, PRICES)
    assert c["steps"]["research"]["cost_usd"] == pytest.approx(0.2 + 0.05)
    assert c["steps"]["assessment"]["cost_usd"] is None and c["steps"]["assessment"]["price"] is None
    assert c["total_usd"] == pytest.approx(0.25) and c["unpriced_steps"] == ["assessment"]


def _usage(inp, out, searches=0):
    return NS(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=None, cache_read_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches) if searches else None)


def _text(text, urls=()):
    return NS(type="text", text=text, citations=[NS(url=u) for u in urls])


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def test_research_sums_every_pause_turn_response(monkeypatch):
    fake = FakeMessages([
        NS(stop_reason="pause_turn", usage=_usage(10_000, 500, 4),
           content=[_text("Part one", ["https://a.example"]),
                    NS(type="web_search_tool_result", content=[NS(url="https://b.example")])]),
        NS(stop_reason="end_turn", usage=_usage(25_000, 1_500, 3), content=[_text("Part two", ["https://c.example"])]),
    ])
    monkeypatch.setattr(analyst, "_client", lambda: NS(messages=fake))
    usage = models.new_usage("claude-sonnet-5-5")
    r = analyst.research("claude-sonnet-5-5", 12, "2026-10-13", usage)
    assert usage["requests"] == 2 and usage["input_tokens"] == 35_000 and usage["output_tokens"] == 2_000
    assert usage["web_searches"] == 7 and r["searches"] == 7
    assert "Part one" in r["notes"] and "Part two" in r["notes"]
    assert r["urls"] == ["https://a.example", "https://b.example", "https://c.example"]
    assert fake.calls[1]["messages"][-1]["role"] == "assistant"  # continued the paused turn


def test_run_uses_each_steps_model_and_records_usage(monkeypatch):
    assessment = {"news_scores": {k: {"score": 1, "summary": "s", "evidence": []}
                                  for k in ("ai_lab_financing", "supplier_signals", "physical_buildout")},
                  "credit_news_adjustment": {"delta": 0, "reason": ""}, "headline": "h",
                  "digest_markdown": "d", "watch_next": []}
    fake = FakeMessages([
        NS(stop_reason="end_turn", usage=_usage(20_000, 1_000, 5), content=[_text("notes")]),
        NS(stop_reason="end_turn", usage=_usage(8_000, 2_000), content=[_text(json.dumps(assessment))]),
    ])
    monkeypatch.setattr(analyst, "_client", lambda: NS(messages=fake))
    sel = models.resolve(CFG, {"INPUT_RESEARCH_MODEL": "claude-haiku-5-5"})
    out = analyst.run({**CFG, "max_web_searches": 5}, {}, None, True, sel)
    assert [c["model"] for c in fake.calls] == ["claude-haiku-5-5", "claude-opus-5-5"]
    assert out["models"] == {"research": "claude-haiku-5-5", "assessment": "claude-opus-5-5"}
    assert out["usage"]["research"]["web_searches"] == 5 and out["usage"]["assessment"]["output_tokens"] == 2_000
    assert out["assessment"]["headline"] == "h"


def test_assessment_usage_counts_even_when_it_stops_early(monkeypatch):
    fake = FakeMessages([NS(stop_reason="max_tokens", usage=_usage(9_000, 8_000), content=[])])
    monkeypatch.setattr(analyst, "_client", lambda: NS(messages=fake))
    usage = models.new_usage("claude-opus-5-5")
    with pytest.raises(RuntimeError):
        analyst.assess("claude-opus-5-5", "2026-10-13", {}, "", None, usage)
    assert usage["input_tokens"] == 9_000 and usage["output_tokens"] == 8_000


# ---------------------------------------------------------------- models.json
@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "SITE_DATA", tmp_path)
    return tmp_path


def _model(mid, name, created, web=None, lifecycle="active"):
    d = {"type": "model", "id": mid, "display_name": name, "created_at": created, "lifecycle": lifecycle}
    if web is not None:
        d["capabilities"] = {"server_tools": {"supported": web, "web_search": {"supported": web},
                                              "code_execution": {"supported": False}}}
    return d


def test_write_catalog_pages_through_models_api(site):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    pages = {
        None: {"data": [_model("claude-opus-5-5", "Claude Opus 5.5", "2026-08-01T00:00:00Z", True),
                        _model("claude-new-6", "Claude New 6", "2026-10-01T00:00:00Z", False)],
               "has_more": True, "first_id": "claude-opus-5-5", "last_id": "claude-new-6"},
        "claude-new-6": {"data": [_model("claude-haiku-5-5", "Claude Haiku 5.5", "2026-07-01T00:00:00Z"),
                                  _model("claude-2-1", "Claude 2.1", "2023-11-21T00:00:00Z", lifecycle="retired"),
                                  _model("Not A Claude ID", "Bad", "2026-01-01T00:00:00Z")],
                         "has_more": False, "first_id": "claude-haiku-5-5", "last_id": "Not A Claude ID"},
    }
    seen = []

    def handler(request):
        assert request.url.path == "/v1/models" and request.headers["x-api-key"] == "test-key"
        seen.append(request.url.params.get("after_id"))
        return httpx2.Response(200, json=pages[request.url.params.get("after_id")])

    def factory():
        return anthropic.Anthropic(api_key="test-key", max_retries=0,
                                   http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))

    cat = models.write_catalog(PRICES, {"research": "claude-sonnet-5-5", "assessment": "claude-opus-5-5"},
                               True, factory)
    assert seen == [None, "claude-new-6"]
    written = json.loads((site / "models.json").read_text())
    assert written == json.loads(json.dumps(cat))
    assert written["source"] == "api" and written["error"] is None and written["web_search_per_1k"] == 10.0
    assert written["defaults"] == {"research": "claude-sonnet-5-5", "assessment": "claude-opus-5-5"}
    by_id = {m["id"]: m for m in written["models"]}
    assert list(by_id) == ["claude-opus-5-5", "claude-new-6", "claude-haiku-5-5"]  # retired + bad id dropped
    assert by_id["claude-opus-5-5"] == {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                                        "created_at": "2026-08-01T00:00:00Z", "supports_web_search": True,
                                        "price": {"input": 4.0, "output": 20.0, "verify": False}}
    assert by_id["claude-new-6"]["supports_web_search"] is False and by_id["claude-new-6"]["price"] is None
    assert by_id["claude-haiku-5-5"]["supports_web_search"] is None and by_id["claude-haiku-5-5"]["price"]["verify"]


def test_write_catalog_falls_back_to_config_list(site):
    def broken():
        raise ConnectionError("network down")

    cat = models.write_catalog(PRICES, {"research": "claude-sonnet-5-5", "assessment": "claude-opus-5-5"},
                               True, broken)
    assert cat["source"] == "config" and "network down" in cat["error"]
    assert [m["id"] for m in cat["models"]] == list(PRICES["models"])
    opus = next(m for m in cat["models"] if m["id"] == "claude-opus-5-5")
    assert opus["display_name"] == "Claude Opus 5.5" and opus["supports_web_search"] is None
    assert opus["price"]["output"] == 20.0
    assert json.loads((site / "models.json").read_text())["source"] == "config"


def test_write_catalog_without_api_access(site):
    called = []
    cat = models.write_catalog(PRICES, {"research": "claude-sonnet-5-5", "assessment": "claude-opus-5-5"},
                               False, lambda: called.append(1))
    assert cat["source"] == "config" and not called
