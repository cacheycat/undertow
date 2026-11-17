"""Undertow: a positioning desk over live Hyperliquid data. Run with `python app.py`."""
from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import HTTPException

from agentkit import LLM, Memory, Runner, Store
from agentkit.serve import make_app
from lib import agent, hyper

ROOT = Path(__file__).parent
store = Store(ROOT / "data" / "undertow.db")
memory = Memory(store)
runner = Runner(store)
app = make_app("Undertow", ROOT / "web", store, runner)


def _runs_with_scores(limit: int = 30) -> list[dict]:
    rows = [r for r in store.list_runs("brief", limit) if r["status"] == "done" and r.get("output")]
    for r in rows:
        r["scored"] = store.get(f"score:{r['id']}")
    return rows[::-1]  # oldest first for the history prompt


@app.get("/api/meta")
def meta():
    llm = LLM()
    return {"mode": llm.mode, "model": llm.model, "graph": agent.describe_graph(),
            "coins": hyper.COINS, "horizon_h": agent.HORIZON_H, "busy": runner.busy("brief"),
            "knowledge_passages": len(agent.CARDS.docs)}


@app.post("/api/brief")
def start_brief():
    if runner.busy("brief"):
        raise HTTPException(409, "a briefing is already being written")
    llm = LLM()
    history = _runs_with_scores()

    def job(run_id, emit):
        return agent.brief(emit, memory=memory, llm=llm, store=store, history=history)

    return {"run_id": runner.start("brief", "desk briefing", {}, job)}


@app.post("/api/score/{run_id}")
def score(run_id: int):
    run = store.get_run(run_id)
    if not run or run["status"] != "done":
        raise HTTPException(404, "no finished briefing with that id")
    scored = agent.score_run(run, memory)
    if not scored:
        raise HTTPException(422, "nothing to score in that briefing")
    prev = store.get(f"score:{run_id}") or {}
    store.put(f"score:{run_id}", scored)
    lesson = None
    if scored["final"] and not prev.get("lesson_written"):
        lesson = agent.learn_lesson(run, scored, memory, LLM())
        scored["lesson_written"] = bool(lesson)
        store.put(f"score:{run_id}", scored)
    return {**scored, "lesson": lesson}


@app.get("/api/history")
def history():
    rows = _runs_with_scores()
    out = []
    for r in rows:
        o = r["output"]
        out.append({"run_id": r["id"], "ts": o["ts"], "headline": o["headline"], "mode": o["mode"],
                    "stances": [{k: s.get(k) for k in ("coin", "stance", "conviction", "px_at_call")} for s in o["stances"]],
                    "scored": r.get("scored")})
    return out


@app.get("/api/candles")
def get_candles(coin: str, interval: str = "1h"):
    if coin not in hyper.COINS or interval not in ("15m", "1h", "4h"):
        raise HTTPException(422, "unknown coin or interval")
    return hyper.candles(coin, interval, 160)


@app.get("/api/memory")
def playbook():
    return memory.all("playbook")


app.finish()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8813, log_level="warning")
