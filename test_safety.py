"""Safety tests: spot only, never negative. Run: python test_safety.py"""
import kraken


def rejects(d):
    try:
        kraken.assert_spot(d)
    except ValueError:
        return True
    return False


base = {"pair": "XBTUSDC", "type": "buy", "ordertype": "market", "volume": "0.001"}
assert not rejects(base)
assert rejects(dict(base, leverage="2:1"))
assert rejects(dict(base, leverage="none"))
assert rejects(dict(base, oflags="fciq"))
assert rejects(dict(base, close="x"))
assert rejects(dict(base, type="short"))
assert rejects(dict(base, ordertype="limit"))
assert rejects(dict(base, volume="0"))
assert rejects(dict(base, volume="-1"))
assert rejects({"pair": "XBTUSDC", "type": "buy", "ordertype": "stop-loss", "volume": "1"})

import shachar, scanner


class FK:
    def pairs(self):
        return {"XBTUSDC": {"ordermin": 0.00005, "costmin": 0.5, "lot_decimals": 8, "pair_decimals": 1}}


tk = {"XBTUSDC": {"bid": 100.0, "ask": 100.0, "last": 100.0, "open": 100.0, "vol24": 1}}
b = {"name": "t", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
s = {"bots": {"t": b}}
r = shachar.execute(FK(), s, b, {"action": "sell", "pair": "XBTUSDC", "fraction": 1}, tk, "dry", False)
assert b["cash"] == 40.0 and not b["pos"], r          # nothing held: a sell (short) does nothing
for i in range(30):
    shachar.execute(FK(), s, b, {"action": "buy", "pair": "XBTUSDC", "usdc": 1000}, tk, "dry", False)
    assert b["cash"] >= 0, b["cash"]
    shachar.execute(FK(), s, b, {"action": "sell", "pair": "XBTUSDC", "fraction": 5}, tk, "dry", False)
    assert b["cash"] >= 0 and all(v >= 0 for v in b["pos"].values())
print("ALL SAFETY TESTS PASSED, final cash %.2f" % b["cash"])

# fee gate, cooldown, halt flag
b2 = {"name": "t2", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
s2 = {"bots": {"t2": b2}}
r = shachar.execute(FK(), s2, b2, {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 2}, tk, "dry", False)
assert r.startswith("refused: fee gate"), r
b2["cool"] = {"XBTUSDC": __import__("time").time()}
r = shachar.execute(FK(), s2, b2, {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 5}, tk, "dry", False)
assert "cooldown" in r, r
import os
os.environ["SHACHAR_HALT"] = "1"
r = shachar.execute(FK(), s2, b2, {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 5}, tk, "dry", False)
assert "halt" in r, r
print("FEE GATE / COOLDOWN / HALT TESTS PASSED")
