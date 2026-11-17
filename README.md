# Undertow

A positioning desk over live Hyperliquid whale data that scores its own calls.

Every briefing reads the public Hyperliquid info API (read-only: no keys, no orders), builds
crowd metrics from the largest accounts' open positions, and files one stance per coin. Later
the stance is scored against the actual move, and the outcome feeds back into memory.

## Design

- **Two-pass briefing.** A draft pass proposes stances; an audit pass attacks each thesis
  against the numbers and may downgrade or contradict it. The audit's verdicts are kept.
- **Computed, not recalled.** Net tilt, gross notional, average leverage and liquidation
  clusters come from `crowd_metrics`; price structure (72h change, range position,
  realised vol, volume ratio) from `structure`. Every thesis must cite a number it was given.
- **Self-scoring.** `score_run` compares each stance with the move since publication.
  `flat` is correct inside a +-1.2% noise band; directional calls need a move beyond a third
  of that band in the right direction.
- **Memory with utility.** Lessons recalled before a good briefing gain utility, lessons
  recalled before a bad one lose it. Playbook retrieval ranks by relevance x utility.
- **RAG.** Cards on funding and crowding, liquidations and whales, and stance craft.

This is analysis tooling, not trading advice. It never places orders.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # needs git access to cacheycat/agentkit
python app.py                            # http://localhost:8813
```

Working on both repos at once? Link a sibling checkout instead of installing it:

```bash
pip install fastapi uvicorn numpy certifi pydantic
ln -s ../agentkit/agentkit agentkit      # ignored by git
```

The LLM layer uses `ANTHROPIC_API_KEY`, else a local headless `claude` CLI with tools
disabled, else a deterministic baseline briefing.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/meta` | Provider mode, coins, scoring horizon |
| POST | `/api/brief` | Generate a briefing |
| POST | `/api/score/{run_id}` | Score a filed briefing against the move since |
| GET | `/api/history` | Past briefings with their scores |
| GET | `/api/candles` | Hourly candles for a coin |
| GET | `/api/memory` | Playbook lessons with learned utility |
| GET | `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/events` | Run list, detail, SSE trace |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q && ruff check .
```
