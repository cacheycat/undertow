import pytest

from lib import agent


def whale(*positions):
    return {"positions": [dict(p) for p in positions]}


def pos(coin, side, usd, lev=10, liq=None):
    return {"coin": coin, "side": side, "ntl_usd": usd, "leverage": lev, "liq_px": liq}


STATE = {"coins": [{"coin": "BTC"}, {"coin": "SOL"}]}


def row(rows, coin):
    return next(r for r in rows if r["coin"] == coin)


def test_net_tilt_and_gross():
    whales = [whale(pos("BTC", "long", 300, liq=90), pos("BTC", "short", 100, liq=130)),
              whale(pos("BTC", "long", 100, liq=95))]
    btc = row(agent.crowd_metrics(whales, STATE), "BTC")
    assert btc["long_usd"] == 400 and btc["short_usd"] == 100 and btc["gross_usd"] == 500
    assert btc["net_tilt"] == pytest.approx(0.6)
    assert btc["whales_long"] == 2 and btc["whales_short"] == 1


def test_liquidation_clusters_are_nearest_to_price():
    whales = [whale(pos("BTC", "long", 1, liq=90), pos("BTC", "long", 1, liq=95),
                    pos("BTC", "short", 1, liq=130), pos("BTC", "short", 1, liq=120))]
    btc = row(agent.crowd_metrics(whales, STATE), "BTC")
    assert btc["long_liq_cluster"] == 95 and btc["short_liq_cluster"] == 120


def test_coin_without_positions_is_flat_not_an_error():
    sol = row(agent.crowd_metrics([whale(pos("BTC", "long", 10))], STATE), "SOL")
    assert sol["net_tilt"] == 0.0 and sol["gross_usd"] == 0 and sol["avg_leverage"] is None


def test_average_leverage():
    whales = [whale(pos("BTC", "long", 1, lev=10), pos("BTC", "short", 1, lev=20))]
    assert row(agent.crowd_metrics(whales, STATE), "BTC")["avg_leverage"] == 15.0
