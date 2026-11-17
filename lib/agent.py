"""Undertow: a desk-briefing loop over live Hyperliquid positioning.

observe -> crowd -> recall -> brief -> audit -> file

The harness fetches market state and large-account positions, computes crowd
metrics in code, and the model writes a per-coin stance with an invalidation
level. Every stance is filed with the mid at publication; a later scoring pass
marks it against what price actually did, and that verdict is the feedback
signal for the playbook memory. The model never sees an order book it could
act on - stances are paper, scored, and public data only.
"""
from __future__ import annotations

import math
import statistics
import time
from pathlib import Path

from agentkit import END, LLM, Index, Memory, StateGraph, load_cards

from . import hyper

CARDS = Index(load_cards(Path(__file__).resolve().parent.parent / "knowledge"))
HORIZON_H = 24  # a stance is judged against the move over this window

STANCE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["stances", "headline", "desk_note"],
  "properties": {
    "headline": {"type": "string"},
    "desk_note": {"type": "string"},
    "stances": {"type": "array", "items": {"type": "object", "additionalProperties": False,
      "required": ["coin", "stance", "conviction", "thesis", "invalidation"],
      "properties": {"coin": {"type": "string"}, "stance": {"type": "string", "enum": ["long", "short", "flat"]},
                      "conviction": {"type": "integer", "minimum": 1, "maximum": 5},
                      "thesis": {"type": "string"}, "invalidation": {"type": "string"}}}}}}

AUDIT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["checks", "revisions"],
  "properties": {"checks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
      "required": ["coin", "verdict", "note"],
      "properties": {"coin": {"type": "string"}, "verdict": {"type": "string", "enum": ["sound", "weak", "contradicted"]},
                      "note": {"type": "string"}}}},
    "revisions": {"type": "array", "items": {"type": "object", "additionalProperties": False,
      "required": ["coin", "stance", "conviction"],
      "properties": {"coin": {"type": "string"}, "stance": {"type": "string", "enum": ["long", "short", "flat"]},
                      "conviction": {"type": "integer", "minimum": 1, "maximum": 5}}}}}}

SYSTEM = f"""You are the analyst on a crypto perp desk writing the positioning briefing from live Hyperliquid data.

You are given, per coin: mark price, 24h change, funding (hourly and annualised), open interest, 24h volume, and crowd metrics computed by the harness from the current positions of large profitable accounts (whale net tilt, gross exposure, crowding score, average leverage, nearest liquidation cluster). You also see recent price/volume structure summaries, playbook lessons with a track record, and how your previous stances scored.

Write a stance per coin: long, short or flat, conviction 1-5, a thesis in one or two sentences, and a concrete invalidation (a price level or an observable change that would make the thesis wrong). Stances are paper positions scored against the {HORIZON_H}h move after publication; flat is scored as correct when the move stays inside a noise band, so flat is a real call, not a hedge.

Discipline:
- Funding and crowding are contrarian pressure, not direction: crowded longs paying high funding is fragile, not automatically short.
- Whale tilt is informative because these accounts are selected for recent profitability, but they can be wrong together; say when you are fading them and why.
- Every thesis must cite at least one number you were given. No vibes, no certainty theater, and never more than one conviction-5 call per briefing."""


def crowd_metrics(whales: list[dict], state: dict) -> list[dict]:
    rows = []
    for c in state["coins"]:
        coin = c["coin"]
        longs = [(w, p) for w in whales for p in w["positions"] if p["coin"] == coin and p["side"] == "long"]
        shorts = [(w, p) for w in whales for p in w["positions"] if p["coin"] == coin and p["side"] == "short"]
        l_usd = sum(p["ntl_usd"] for _, p in longs)
        s_usd = sum(p["ntl_usd"] for _, p in shorts)
        gross = l_usd + s_usd
        levs = [float(p["leverage"]) for _, p in longs + shorts if p.get("leverage")]
        liqs_below = [p["liq_px"] for _, p in longs if p.get("liq_px")]
        liqs_above = [p["liq_px"] for _, p in shorts if p.get("liq_px")]
        rows.append({"coin": coin,
                     "whales_long": len(longs), "whales_short": len(shorts),
                     "long_usd": l_usd, "short_usd": s_usd,
                     "net_tilt": round((l_usd - s_usd) / gross, 2) if gross else 0.0,
                     "gross_usd": gross,
                     "avg_leverage": round(statistics.mean(levs), 1) if levs else None,
                     "long_liq_cluster": round(max(liqs_below), 1) if liqs_below else None,
                     "short_liq_cluster": round(min(liqs_above), 1) if liqs_above else None})
    return rows


def structure(coin: str) -> dict:
    ks = hyper.candles(coin, "1h", 160)
    if len(ks) < 30:
        return {"coin": coin}
    closes = [k["c"] for k in ks]
    last = closes[-1]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    vol = statistics.pstdev(rets[-72:]) * math.sqrt(24 * 365)
    hi, lo = max(k["h"] for k in ks[-72:]), min(k["l"] for k in ks[-72:])
    vols = [k["v"] for k in ks]
    v24, vprior = sum(vols[-24:]), sum(vols[-96:-24]) / 3 if len(vols) >= 96 else sum(vols[:-24]) / max(len(vols) - 24, 1) * 24
    return {"coin": coin, "px": last,
            "chg_72h": round(last / closes[-72] - 1, 4) if len(closes) >= 72 else None,
            "range_pos_72h": round((last - lo) / (hi - lo), 2) if hi > lo else None,
            "realised_vol_ann": round(vol, 2),
            "volume_vs_prior_3d": round(v24 / vprior, 2) if vprior else None}


def _memory_block(recalled: list[dict]) -> str | None:
    if not recalled:
        return None
    return "Playbook lessons (utility = share of recalls that preceded a correct briefing):\n" + \
        "\n".join(f"- [{m['utility']:.2f}] {m['text']}" for m in recalled)


def brief(emit, *, memory: Memory, llm: LLM, store, history: list[dict]) -> dict:
    st: dict = {"state": None, "whales": [], "crowd": [], "structs": [], "recalled": [], "stances": [],
                "headline": None, "desk_note": None, "audit": [], "scored_feedback": None}
    log_call = lambda rec: emit("llm", rec)

    def observe(_):
        st["state"] = hyper.market_state()
        st["structs"] = [structure(c["coin"]) for c in st["state"]["coins"]]
        emit("market", st["state"])
        return {"_note": f"{len(st['state']['coins'])} coins: " + ", ".join(
            f"{c['coin']} {c['chg_24h']*100:+.1f}%" for c in st["state"]["coins"])}

    def crowd(_):
        st["whales"] = hyper.whale_snapshot()
        st["crowd"] = crowd_metrics(st["whales"], st["state"])
        emit("whales", {"accounts": st["whales"], "crowd": st["crowd"]})
        gross = sum(c["gross_usd"] for c in st["crowd"])
        return {"_note": f"{len(st['whales'])} large accounts, {fmtusd(gross)} gross in tracked coins"}

    def recall(_):
        skew = sorted(st["crowd"], key=lambda c: -abs(c["net_tilt"]))
        query = "funding crowding whale tilt " + " ".join(
            f"{c['coin']} {'long crowded' if c['net_tilt'] > 0.3 else 'short crowded' if c['net_tilt'] < -0.3 else 'balanced'}" for c in skew[:3])
        st["recalled"] = memory.search(query, k=4, kind="playbook")
        notes = CARDS.search(query, k=3)
        st["notes"] = notes
        emit("recall", {"query": query, "lessons": [{"id": m["id"], "text": m["text"], "utility": m["utility"]} for m in st["recalled"]],
                        "notes": [n["title"] for n in notes]})
        return {"_note": f"{len(st['recalled'])} playbook lessons, {len(notes)} reference notes"}

    def fallback_stances():
        out = []
        for c, cr in zip(st["state"]["coins"], st["crowd"]):
            crowded = abs(cr["net_tilt"]) > 0.5 and abs(c["funding_apr"]) > 0.15
            stance = "flat" if not crowded else ("short" if cr["net_tilt"] > 0 else "long")
            out.append({"coin": c["coin"], "stance": stance, "conviction": 2 if crowded else 1,
                        "thesis": f"Rule-based: net tilt {cr['net_tilt']:+.2f} with funding {c['funding_apr']*100:.0f}% APR"
                                  + (" marks a crowded trade to fade." if crowded else " shows no crowding edge."),
                        "invalidation": "tilt or funding normalises"})
        return {"stances": out, "headline": "Rule-based crowding scan (no model)", "desk_note": "Fallback briefing from the crowding heuristic only."}

    def write(_):
        scored = [r for r in history if r.get("scored")]
        feedback = None
        if scored:
            recent = scored[-3:]
            lines = []
            for r in recent:
                for s in r["scored"]["stances"]:
                    lines.append(f"- {r['scored']['when']}: {s['coin']} {s['stance']} (conv {s['conviction']}) -> move {s['move']*100:+.1f}% = {s['verdict']}")
            feedback = "How your previous calls scored against the subsequent move:\n" + "\n".join(lines[-12:])
        st["scored_feedback"] = feedback
        user = "\n\n".join(filter(None, [
            "Market state:\n" + "\n".join(
                f"- {c['coin']}: mark {c['mark']:g}, 24h {c['chg_24h']*100:+.1f}%, funding {c['funding_apr']*100:+.1f}% APR, "
                f"OI {fmtusd(c['oi_usd'])}, 24h volume {fmtusd(c['day_vlm_usd'])}" for c in st["state"]["coins"]),
            "Crowd metrics from the tracked accounts:\n" + "\n".join(
                f"- {c['coin']}: {c['whales_long']}L/{c['whales_short']}S, net tilt {c['net_tilt']:+.2f}, gross {fmtusd(c['gross_usd'])}, "
                f"avg leverage {c['avg_leverage'] or '?'}x, long liq cluster {c['long_liq_cluster'] or '-'}, short liq cluster {c['short_liq_cluster'] or '-'}"
                for c in st["crowd"]),
            "Price structure (1h bars):\n" + "\n".join(
                f"- {s['coin']}: 72h {s['chg_72h']*100:+.1f}%, range position {s['range_pos_72h']}, realised vol {s['realised_vol_ann']*100:.0f}%, "
                f"volume vs prior 3d {s['volume_vs_prior_3d']}x" for s in st["structs"] if s.get("px")),
            _memory_block(st["recalled"]),
            "Reference notes:\n" + "\n".join(f"- {n['title']}: {n['text'][:350]}" for n in st.get("notes", [])),
            feedback,
            "Write the briefing: a headline under 80 chars, a 2-4 sentence desk note on the overall tape, and one stance per coin listed above.",
        ]))
        out = llm.json("brief", SYSTEM, user, STANCE_SCHEMA, fallback_stances, on_call=log_call)
        coins = {c["coin"] for c in st["state"]["coins"]}
        st["stances"] = [s for s in out.get("stances", []) if s.get("coin") in coins][:len(coins)]
        st["headline"], st["desk_note"] = str(out.get("headline", ""))[:120], str(out.get("desk_note", ""))[:800]
        return {"_note": f"“{st['headline']}” · " + ", ".join(f"{s['coin']} {s['stance']}" for s in st["stances"])}

    def audit(_):
        """A second pass checks each thesis against the numbers before filing."""
        user = "\n\n".join([
            "You are auditing the briefing below before publication. For each stance, check the thesis against the data: "
            "does the cited number exist and point the way the thesis claims? Verdicts: sound, weak (thin but defensible), contradicted (the data says otherwise). "
            "For any contradicted stance, give a revision (stance and conviction).",
            "Data:\n" + "\n".join(
                f"- {c['coin']}: 24h {c['chg_24h']*100:+.1f}%, funding {c['funding_apr']*100:+.1f}% APR, tilt {cr['net_tilt']:+.2f}, "
                f"gross {fmtusd(cr['gross_usd'])}" for c, cr in zip(st["state"]["coins"], st["crowd"])),
            "Briefing:\n" + "\n".join(f"- {s['coin']} {s['stance']} (conv {s['conviction']}): {s['thesis']} | invalidation: {s['invalidation']}" for s in st["stances"]),
        ])
        out = llm.json("audit", SYSTEM, user, AUDIT_SCHEMA,
                       lambda: {"checks": [{"coin": s["coin"], "verdict": "sound", "note": "rule-based stance, no audit"} for s in st["stances"]],
                                "revisions": []}, on_call=log_call)
        st["audit"] = [c for c in out.get("checks", []) if c.get("coin")]
        revised = []
        for rev in out.get("revisions", []):
            s = next((x for x in st["stances"] if x["coin"] == rev.get("coin")), None)
            if s:
                s["stance"], s["conviction"], s["revised"] = rev["stance"], rev["conviction"], True
                revised.append(s["coin"])
        for c in st["audit"]:
            emit("audit", c)
        return {"_note": ("revised " + ", ".join(revised)) if revised else "all stances held up"}

    def file_(_):
        mids = {c["coin"]: c["mid"] for c in st["state"]["coins"]}
        for s in st["stances"]:
            s["px_at_call"] = mids.get(s["coin"])
        emit("briefing", {"headline": st["headline"], "desk_note": st["desk_note"], "stances": st["stances"],
                          "audit": st["audit"]})
        return {"_note": f"filed with mid prices; scored against the {HORIZON_H}h move"}

    g = StateGraph("undertow")
    g.node("observe", observe, "Observe", "tool").node("crowd", crowd, "Crowd", "tool")
    g.node("recall", recall, "Recall", "memory").node("write", write, "Brief", "llm")
    g.node("audit", audit, "Audit", "llm").node("file", file_, "File", "step")
    g.edge("observe", "crowd").edge("crowd", "recall").edge("recall", "write").edge("write", "audit").edge("audit", "file")
    emit("graph", g.describe())
    g.run({}, emit, max_steps=12)

    return {"ts": st["state"]["ts"], "mode": llm.mode, "model": llm.model,
            "market": st["state"], "crowd": st["crowd"], "whales": st["whales"], "structs": st["structs"],
            "headline": st["headline"], "desk_note": st["desk_note"], "stances": st["stances"], "audit": st["audit"],
            "recalled_ids": [m["id"] for m in st["recalled"]], "recalled": [{k: m[k] for k in ("id", "text", "utility")} for m in st["recalled"]],
            "horizon_h": HORIZON_H, "graph": g.describe(),
            "llm_calls": sum(1 for c in llm.calls if c["source"] != "baseline")}


NOISE_BAND = 0.012  # within +-1.2%, "flat" was the right call


def score_run(run: dict, memory: Memory) -> dict | None:
    """Score a filed briefing against the move since publication (or the full
    horizon once elapsed). Correct calls credit the recalled lessons."""
    out = run.get("output")
    if not out or not out.get("stances"):
        return None
    mids = hyper.mids()
    age_h = (time.time() - out["ts"]) / 3600
    stances, correct = [], 0
    for s in out["stances"]:
        px0, px1 = s.get("px_at_call"), mids.get(s["coin"])
        if not px0 or not px1:
            continue
        move = px1 / px0 - 1
        if s["stance"] == "flat":
            ok = abs(move) <= NOISE_BAND
        else:
            ok = move > NOISE_BAND / 3 if s["stance"] == "long" else move < -NOISE_BAND / 3
        correct += ok
        stances.append({"coin": s["coin"], "stance": s["stance"], "conviction": s["conviction"],
                        "move": round(move, 4), "verdict": "correct" if ok else "wrong"})
    if not stances:
        return None
    scored = {"when": f"{age_h:.1f}h ago", "age_h": round(age_h, 2), "final": age_h >= out["horizon_h"],
              "stances": stances, "hit_rate": round(correct / len(stances), 2)}
    if out.get("recalled_ids") and age_h >= 1:
        memory.feedback(out["recalled_ids"], scored["hit_rate"] >= 0.5)
    return scored


def learn_lesson(run: dict, scored: dict, memory: Memory, llm: LLM) -> dict | None:
    """After a horizon elapses, distil one playbook lesson from the outcome."""
    out = run.get("output")
    if not out or llm.mode == "baseline":
        return None
    lines = "\n".join(f"- {s['coin']} {s['stance']} conv {s['conviction']}: moved {s['move']*100:+.1f}% -> {s['verdict']}" for s in scored["stances"])
    user = (f"A briefing you wrote {scored['when']} (“{out['headline']}”) has now been scored against the {out['horizon_h']}h move:\n{lines}\n\n"
            "Crowd metrics at the time:\n" + "\n".join(f"- {c['coin']}: tilt {c['net_tilt']:+.2f}" for c in out["crowd"]) +
            "\n\nWrite at most one transferable playbook lesson (one sentence, specific: name the signal pattern and what to do), or an empty list if the outcome was noise.")
    res = llm.json("learn", SYSTEM, user,
                   {"type": "object", "additionalProperties": False, "required": ["lessons"],
                    "properties": {"lessons": {"type": "array", "items": {"type": "string"}}}},
                   lambda: {"lessons": []})
    for text in res.get("lessons", [])[:1]:
        if isinstance(text, str) and 20 < len(text) < 300:
            mid = memory.add("playbook", text, {"run_ts": out["ts"]})
            return {"id": mid, "text": text}
    return None


def fmtusd(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"${a/1e9:.2f}B"
    if a >= 1e6:
        return f"${a/1e6:.1f}M"
    if a >= 1e3:
        return f"${a/1e3:.0f}k"
    return f"${a:.0f}"


def describe_graph() -> dict:
    g = StateGraph("undertow")
    for n, l, k in [("observe", "Observe", "tool"), ("crowd", "Crowd", "tool"), ("recall", "Recall", "memory"),
                    ("write", "Brief", "llm"), ("audit", "Audit", "llm"), ("file", "File", "step")]:
        g.node(n, lambda s: None, l, k)
    g.edge("observe", "crowd").edge("crowd", "recall").edge("recall", "write").edge("write", "audit").edge("audit", "file")
    return g.describe()
