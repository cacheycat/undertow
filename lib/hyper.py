"""Hyperliquid public data: market state, leaderboard, whale positions, candles.

Read-only. Nothing in this project signs, submits or holds an order; the
exchange is used as a public data feed about *other people's* positioning.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from agentkit.http import fetch_json

INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
COINS = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP"]
WHALES = 10  # tracked accounts per snapshot
LB_TTL = 6 * 3600


def _info(body: dict):
    return fetch_json(INFO, body)


def market_state() -> dict:
    """Mark, funding, open interest and 24h move for the tracked coins."""
    meta, ctxs = _info({"type": "metaAndAssetCtxs"})
    by_coin = {}
    for asset, ctx in zip(meta["universe"], ctxs):
        name = asset["name"]
        if name not in COINS:
            continue
        mark, prev = float(ctx["markPx"]), float(ctx["prevDayPx"])
        by_coin[name] = {"coin": name, "mark": mark, "mid": float(ctx.get("midPx") or mark),
                        "funding_hr": float(ctx["funding"]), "funding_apr": round(float(ctx["funding"]) * 24 * 365, 4),
                        "oi_usd": round(float(ctx["openInterest"]) * mark),
                        "day_vlm_usd": round(float(ctx["dayNtlVlm"])),
                        "chg_24h": round(mark / prev - 1, 4) if prev else 0.0,
                        "max_leverage": asset.get("maxLeverage")}
    return {"ts": time.time(), "coins": [by_coin[c] for c in COINS if c in by_coin]}


def top_accounts() -> list[dict]:
    """Large, recently profitable accounts from the public leaderboard (cached)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / "leaderboard.json"
    if not path.exists() or time.time() - path.stat().st_mtime > LB_TTL:
        rows = fetch_json(LEADERBOARD, timeout=90, retries=1)["leaderboardRows"]
        slim = []
        for r in rows:
            acct = float(r["accountValue"])
            if acct < 2_000_000:
                continue
            perf = dict(r["windowPerformances"])
            slim.append({"address": r["ethAddress"], "account_usd": round(acct),
                         "display": r.get("displayName"),
                         "roi_week": round(float(perf.get("week", {}).get("roi", 0)), 4),
                         "roi_month": round(float(perf.get("month", {}).get("roi", 0)), 4),
                         "pnl_month": round(float(perf.get("month", {}).get("pnl", 0)))})
        slim.sort(key=lambda x: -(x["account_usd"] * max(x["roi_month"], 0.0001)))
        path.write_text(json.dumps(slim[:60]))
    return json.loads(path.read_text())


def positions(address: str) -> list[dict]:
    st = _info({"type": "clearinghouseState", "user": address})
    out = []
    for ap in st.get("assetPositions", []):
        p = ap.get("position") or {}
        coin, szi = p.get("coin"), float(p.get("szi") or 0)
        if coin not in COINS or szi == 0:
            continue
        lev = p.get("leverage") or {}
        out.append({"coin": coin, "side": "long" if szi > 0 else "short", "size": abs(szi),
                    "ntl_usd": round(abs(float(p.get("positionValue") or 0))),
                    "entry": float(p.get("entryPx") or 0), "upnl_usd": round(float(p.get("unrealizedPnl") or 0)),
                    "leverage": lev.get("value"), "liq_px": float(p["liquidationPx"]) if p.get("liquidationPx") else None})
    return out


def whale_snapshot(n: int = WHALES) -> list[dict]:
    out = []
    for acct in top_accounts():
        if len(out) >= n:
            break
        try:
            pos = positions(acct["address"])
        except RuntimeError:
            continue
        if pos:
            out.append({**acct, "positions": pos})
        time.sleep(0.12)
    return out


def candles(coin: str, interval: str = "1h", bars: int = 160) -> list[dict]:
    now = int(time.time() * 1000)
    ms = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[interval]
    rows = _info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                    "startTime": now - bars * ms, "endTime": now}})
    return [{"t": r["t"] / 1000, "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
             "c": float(r["c"]), "v": float(r["v"])} for r in rows]


def mids() -> dict:
    out = _info({"type": "allMids"})
    return {c: float(out[c]) for c in COINS if c in out}
