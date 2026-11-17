import time

import pytest

from agentkit import Memory, Store
from lib import agent


@pytest.fixture()
def memory(tmp_path):
    return Memory(Store(tmp_path / "t.db"))


def run_with(stances, age_h=2.0, recalled=()):
    return {"output": {"ts": time.time() - age_h * 3600, "horizon_h": 24, "recalled_ids": list(recalled),
                       "stances": [{"coin": c, "stance": s, "conviction": 3, "px_at_call": 100.0}
                                   for c, s in stances]}}


def score(monkeypatch, memory, stances, mids, **kw):
    monkeypatch.setattr(agent.hyper, "mids", lambda: mids)
    return agent.score_run(run_with(stances, **kw), memory)


@pytest.mark.parametrize("stance, px, verdict", [
    ("long", 103, "correct"),
    ("long", 97, "wrong"),
    ("short", 97, "correct"),
    ("short", 103, "wrong"),
    ("flat", 100.5, "correct"),   # inside the noise band
    ("flat", 104, "wrong"),
    ("long", 100.5, "correct"),   # moved past a third of the band (0.4%)
    ("long", 100.1, "wrong"),     # too small to count as follow-through
])
def test_verdicts(monkeypatch, memory, stance, px, verdict):
    out = score(monkeypatch, memory, [("BTC", stance)], {"BTC": px})
    assert out["stances"][0]["verdict"] == verdict


def test_hit_rate_and_final_flag(monkeypatch, memory):
    out = score(monkeypatch, memory, [("BTC", "long"), ("SOL", "short")], {"BTC": 105, "SOL": 105}, age_h=30)
    assert out["hit_rate"] == 0.5 and out["final"] is True


def test_missing_price_is_skipped_and_empty_returns_none(monkeypatch, memory):
    assert score(monkeypatch, memory, [("BTC", "long")], {}) is None


def test_recalled_lessons_get_credit_or_blame(monkeypatch, memory):
    good = memory.add("playbook", "crowded longs near liquidation clusters tend to flush")
    bad = memory.add("playbook", "fade every funding spike regardless of trend")
    score(monkeypatch, memory, [("BTC", "long")], {"BTC": 105}, recalled=[good])
    score(monkeypatch, memory, [("BTC", "long")], {"BTC": 90}, recalled=[bad])
    rows = {r["id"]: r for r in memory.all("playbook")}
    assert rows[good]["wins"] == 1 and rows[bad]["losses"] == 1


def test_no_feedback_before_one_hour(monkeypatch, memory):
    i = memory.add("playbook", "wait for confirmation before chasing a breakout move")
    score(monkeypatch, memory, [("BTC", "long")], {"BTC": 105}, age_h=0.5, recalled=[i])
    assert memory.all("playbook")[0]["wins"] == 0


def test_move_relative_to_btc_is_reported_but_verdict_stays_absolute(monkeypatch, memory):
    # market falls: BTC -4%, SOL -2%. A SOL long is wrong in absolute terms yet beat BTC.
    run = {"output": {"ts": time.time() - 7200, "horizon_h": 24, "recalled_ids": [],
                      "stances": [{"coin": "BTC", "stance": "flat", "conviction": 2, "px_at_call": 100.0},
                                  {"coin": "SOL", "stance": "long", "conviction": 3, "px_at_call": 100.0}]}}
    monkeypatch.setattr(agent.hyper, "mids", lambda: {"BTC": 96.0, "SOL": 98.0})
    by_coin = {s["coin"]: s for s in agent.score_run(run, memory)["stances"]}
    assert by_coin["SOL"]["verdict"] == "wrong"
    assert by_coin["SOL"]["vs_btc"] == pytest.approx(0.02)
    assert by_coin["BTC"]["vs_btc"] is None
