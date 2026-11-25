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
assert not rejects(dict(base, ordertype="limit", oflags="post", price="99.5"))
assert rejects(dict(base, ordertype="limit", price="99.5"))
assert rejects(dict(base, ordertype="limit", oflags="post"))
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


tk = {"XBTUSDC": {"bid": 100.0, "ask": 100.0, "last": 100.0, "open": 100.0, "vol24": 1e6}}
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


# reconcile: matching account passes, mismatch writes HALT
class RK:
    def __init__(self, usdc): self.u = usdc
    def balance(self): return {"USDC": self.u}
b3 = {"name": "t3", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True}
ok, _, _ = shachar.reconcile(RK(40.5), {"bots": {"t3": b3}}, tk)
assert ok and not (shachar.HERE / "HALT").exists()
ok, _, _ = shachar.reconcile(RK(30.0), {"bots": {"t3": b3}}, tk)
assert not ok and (shachar.HERE / "HALT").exists()
(shachar.HERE / "HALT").unlink()
print("RECONCILE TESTS PASSED")

b4 = {"name": "t4", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
s4 = {"bots": {"t4": b4}}
shachar.execute(FK(), s4, b4, {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 5}, tk, "dry", False) if False else None
os.environ.pop("SHACHAR_HALT", None)
r = shachar.execute(FK(), s4, b4, {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 5}, tk, "dry", False)
assert r.startswith("buy"), r
r = shachar.execute(FK(), s4, b4, {"action": "sell", "pair": "XBTUSDC", "fraction": 1, "reason": "brain"}, tk, "dry", False)
assert "minimum hold" in r, r
print("MIN HOLD TEST PASSED")

tkl = {"XBTUSDC": {"bid": 100.0, "ask": 100.05, "last": 100.0, "open": 100.0, "vol24": 1e6},
       "ADAUSDC": {"bid": 1.0, "ask": 1.0005, "last": 1.0, "open": 1.0, "vol24": 1000.0},
       "SOLUSDC": {"bid": 10.0, "ask": 10.05, "last": 10.0, "open": 10.0, "vol24": 1e6}}
assert shachar.liquid("XBTUSDC", tkl)[0]
assert not shachar.liquid("ADAUSDC", tkl)[0]      # thin volume
assert not shachar.liquid("SOLUSDC", tkl)[0]      # spread 0.5%
mkx = {"XBTUSDC": {"chg_6h_pct": 4.0, "chg_24h_pct": 2.0, "price": 100, "min_order": 0.0001},
       "ADAUSDC": {"chg_6h_pct": 6.0, "chg_24h_pct": 5.0, "price": 1, "min_order": 1}}
ops = shachar.opportunities(mkx, {"pairs": {"XBTUSDC": {"chg_1h_pct": 0.8, "chg_15m_pct": 0.2}}}, tkl)
assert len(ops) == 1 and ops[0]["pair"] == "XBTUSDC" and ops[0]["setup"] == "breakout", ops
assert shachar.opportunities({"XBTUSDC": {"chg_6h_pct": 0.2, "chg_24h_pct": 0.1}}, {}, tkl) == []
os.environ.pop("SHACHAR_HALT", None)
bb = {"name": "t5", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
class FK2(FK):
    def pairs(self):
        return {"ADAUSDC": {"ordermin": 1, "costmin": 0.5, "lot_decimals": 1, "pair_decimals": 4}}
r = shachar.execute(FK2(), {"bots": {"t5": bb}}, bb, {"action": "buy", "pair": "ADAUSDC", "usdc": 10, "take_pct": 5}, tkl, "dry", False)
assert "not liquid" in r, r
print("LIQUIDITY / OPPORTUNITY TESTS PASSED")

bs = kraken.book_summary([["99.9", "100"], ["99.0", "50"]], [["100.1", "40"], ["101.0", "10"]])
assert bs["bid_usd_0.5pct"] == round(99.9 * 100) and bs["ask_usd_0.5pct"] == round(100.1 * 40), bs
assert shachar.btc_alert({"XBTUSDC": {"chg_6h_pct": 6.0}}, {}) == ["BTC +6.0% in 6h"]
assert shachar.btc_alert({"XBTUSDC": {"chg_6h_pct": 0.5}}, {"pairs": {"XBTUSDC": {"chg_1h_pct": 0.1}}}) == "none"
print("INPUT TESTS PASSED")

import math
up = [100 * (1 + 0.004 * i) + (3 if i % 7 == 0 else 0) for i in range(300)]
r = shachar.backtest_setup(up, "trend")
assert r is None or ("n" in r and r["n"] >= 3), r
flat = [100.0] * 300
assert shachar.backtest_setup(flat, "breakout") is None
# dry maker buy fills at the bid with maker fee
import os
os.environ.pop("SHACHAR_HALT", None)
bm = {"name": "t6", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
r = shachar.execute(FK(), {"bots": {"t6": bm}}, bm, {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 5}, tk, "dry", False)
assert r.startswith("buy") and abs(bm["fees"] - 10 * 0.995 * 0.005 / 0.995) < 0.5 and bm["cash"] >= 0, (r, bm)
print("MAKER / BACKTEST TESTS PASSED")

# paper maker entries (guard.py turns PAPER_PENDING on): a bid fills only when the market trades through it
import tempfile, pathlib, time, policy
_tmp = pathlib.Path(tempfile.mkdtemp())
shachar.HERE = policy.HERE = _tmp                         # keep this block's journal and trade log out of the repo
shachar.PAPER_PENDING = True
bp = {"name": "tp", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
shachar._STATE["dry"] = {"bots": {"tp": bp}, "log": []}
shachar.INFLIGHT.clear()
tkp = {"XBTUSDC": {"bid": 100.0, "ask": 100.05, "last": 100.0, "open": 100.0, "vol24": 1e6}}
PB = {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 5, "stop_pct": 3}
def ptk(**kw): return {"XBTUSDC": dict(tkp["XBTUSDC"], **kw)}
r = shachar.execute(FK(), shachar._STATE["dry"], bp, dict(PB), tkp, "dry", False)
assert r.startswith("paper maker order working") and not bp["pos"] and bp["cash"] == 40.0 and bp["trades"] == 0, (r, bp)
r = shachar.execute(FK(), shachar._STATE["dry"], bp, dict(PB), tkp, "dry", False)
assert "already working" in r, r                                           # one resting entry at a time
assert shachar.fill_pending(tkp, "dry") == [] and bp.get("pending")        # nothing traded: still resting
assert shachar.fill_pending(ptk(bid=101.0, ask=101.05, last=101.0), "dry") == []   # price ran away: no fill, no chase
out = shachar.fill_pending(ptk(bid=99.95, ask=100.0, last=100.0), "dry")           # the ask came down to our bid
vol = bp["pos"]["XBTUSDC"]
assert out[0][1].startswith("buy") and "pending" not in bp and bp["trades"] == 1, (out, bp)
assert abs(bp["cash"] - (40.0 - vol * 100.0 * (1 + shachar.MAKER_FEE))) < 1e-9 and bp["cash"] >= 0, bp
assert abs(bp["guards"]["XBTUSDC"]["stop"] - 97.0) < 1e-9 and abs(bp["guards"]["XBTUSDC"]["take"] - 105.0) < 1e-9
bp["pos"].clear(); bp["guards"].clear(); cash0 = bp["cash"]
shachar.execute(FK(), shachar._STATE["dry"], bp, dict(PB), tkp, "dry", False)
out = shachar.fill_pending(ptk(last=99.9), "dry")                          # a new trade printed below our bid: filled
assert out[0][1].startswith("buy") and bp["pos"], out
bp["pos"].clear(); bp["guards"].clear(); cash0 = bp["cash"]
shachar.execute(FK(), shachar._STATE["dry"], bp, dict(PB), tkp, "dry", False)
bp["pending"]["t"] = time.time() - kraken.Kraken.MAKER_WAIT - 1
out = shachar.fill_pending(tkp, "dry")                                     # 90s passed with no trade through: expired
assert "not filled" in out[0][1] and "pending" not in bp and not bp["pos"] and bp["cash"] == cash0, (out, bp)
shachar.PAPER_PENDING = False
r = shachar.execute(FK(), shachar._STATE["dry"], bp, dict(PB), tkp, "dry", False)
assert r.startswith("buy"), r                                              # default (single cycle, tests): instant fill
shachar._STATE.pop("dry")
print("PAPER MAKER FILL TESTS PASSED")
