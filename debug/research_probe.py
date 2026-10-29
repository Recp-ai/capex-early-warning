"""One-off probe: run the production research call and save the raw reply's structure."""
import json, pathlib
from cew import analyst, followups, publish, util

captured = []
real = analyst._client

def wrapped():
    c = real()
    create = c.messages.create
    def cap(**kw):
        r = create(**kw); captured.append(r); return r
    c.messages.create = cap
    return c

analyst._client = wrapped
import yaml
cfg = yaml.safe_load(open("config/signals.yaml"))
pending = followups.pending(publish.last_assessed_run(), cfg)
out = analyst.research("claude-sonnet-5-5", cfg.get("max_web_searches", 16), util.today().isoformat(),
                       None, None, [f["item"] for f in pending])
dump = {"notes_len": len(out["notes"]), "urls": len(out["urls"]), "responses": []}
for r in captured:
    d = r.model_dump()
    blocks = []
    for b in d["content"]:
        t = b.get("type"); e = {"type": t}
        if t == "text": e["len"] = len(b.get("text") or ""); e["head"] = (b.get("text") or "")[:300]; e["citations"] = len(b.get("citations") or [])
        elif t == "web_search_tool_result":
            c = b.get("content"); e["content_type"] = type(c).__name__; e["n"] = len(c) if isinstance(c, list) else None
            if isinstance(c, dict): e["content"] = c
        else: e["keys"] = sorted(b.keys()); e["sample"] = json.dumps(b)[:800]
        blocks.append(e)
    dump["responses"].append({"stop_reason": d.get("stop_reason"), "stop_details": d.get("stop_details"),
                              "model": d.get("model"), "usage": d.get("usage"), "blocks": blocks,
                              "extra_top_keys": sorted(set(d) - {"content"})})
pathlib.Path("debug/out").mkdir(exist_ok=True)
pathlib.Path("debug/out/research_probe.json").write_text(json.dumps(dump, indent=1, default=str))
print(json.dumps({k: v for k, v in dump.items() if k != "responses"}))
