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
# brain plumbing: an LLM error must not use up the 40-minute window, and the brain sees its own results
import tempfile as _tf, pathlib as _pl, time as _time, json as _json, brain, news, policy as _policy
shachar.HERE = _policy.HERE = _pl.Path(_tf.mkdtemp())


class FKC(FK):
    def tickers(self): return {"XBTUSDC": {"bid": 100.0, "ask": 100.05, "last": 100.0, "open": 100.0, "vol24": 1e6}}
    def ohlc(self, p, interval=60, n=48): return [[100.0] * 48]
    def ohlc_full(self, p, interval=60): return [100.0] * 300
    def usdc_peg(self): return 1.0
    def depth(self, p, count=25): return {"bid_usd_0.5pct": 1, "ask_usd_0.5pct": 1, "imbalance": 0.0}


bc = {"name": "tc", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0,
      "last_action": _time.time(), "children": []}
shachar._STATE["dry"] = {"bots": {"tc": bc}, "log": []}
shachar.INFLIGHT.clear()
_calls, _answer = [], [{"action": "hold", "reason": "llm error: http 429", "source": "llm_error"}]
_real_decide, _real_fg = brain.decide, news.fear_greed
brain.decide = lambda ctx: (_calls.append(ctx), dict(_answer[0]))[1]
news.fear_greed = lambda: None
shachar.cycle("dry", k=FKC())
assert len(_calls) == 1 and not bc.get("last_brain") and bc.get("last_brain_err"), bc      # the window was not used up
assert bc["recent"][-1]["result"] == "no trade"
shachar.cycle("dry", k=FKC())
assert len(_calls) == 1                                                                    # 10-minute backoff, no hammering
bc["last_brain_err"] = _time.time() - shachar.BRAIN_ERR_BACKOFF_S - 1
_answer[0] = {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 2, "stop_pct": 3, "source": "llm"}
shachar.cycle("dry", k=FKC())
assert len(_calls) == 2 and bc.get("last_brain") and "fee gate" in bc["recent"][-1]["result"], bc["recent"]
assert _calls[1]["me"]["recent_results"][-1]["result"] == "no trade"                       # the brain was shown its last result
for _ in range(9):
    bc["last_brain"] = 0
    shachar.cycle("dry", k=FKC())
assert len(bc["recent"]) == 6                                                              # bounded history
brain.decide, news.fear_greed = _real_decide, _real_fg
shachar._STATE.pop("dry")

# a model that answers without JSON does not end the round: the next model is asked
import os as _os, urllib.request as _ur
_replies = iter(["I would rather wait and see.", _json.dumps({"action": "hold", "reason": "ok"})])


class _Resp:
    def __init__(self, txt): self.txt = txt
    def read(self): return _json.dumps({"choices": [{"message": {"content": self.txt}}]}).encode()


_os.environ["LLM_KEY"] = "x"
_real_open, _real_models = _ur.urlopen, brain.LLM_MODELS
_ur.urlopen = lambda req, timeout=0: _Resp(next(_replies))
brain.LLM_MODELS = ["m1", "m2"]
d = brain.ask_llm({"x": 1})
_ur.urlopen, brain.LLM_MODELS = _real_open, _real_models
_os.environ.pop("LLM_KEY")
assert d["source"] == "llm:m2" and d["action"] == "hold", d
print("BRAIN WINDOW / RECENT RESULTS / MODEL FALLBACK TESTS PASSED")
# scanner warm start: the 1h window exists right after a (re)start instead of an hour later
import time as _t2


class FKS(FK):
    def __init__(self, fail=False):
        self.fail = fail
    def ohlc_rows(self, pair, interval=1):
        if self.fail:
            raise OSError("network down")
        now = int(_t2.time()) // 60 * 60
        return [(now - 60 * (90 - i), 100.0 + i * 0.01) for i in range(90)]       # 90 finished one-minute candles
    def tickers_fast(self):
        return {"XBTUSDC": {"bid": 101.0, "ask": 101.05, "last": 101.0, "open": 100.0, "vol24": 1e6}}


cold = scanner.Scanner(FKS(fail=True))
assert cold.seed() == 0                                   # seeding is best effort: a failure never stops the guard
cold.tick()
assert "chg_1h_pct" not in cold.summary()["pairs"]["XBTUSDC"]             # before: blind until an hour has passed
warm = scanner.Scanner(FKS())
assert warm.seed() == 1
warm.tick()
row = warm.summary()["pairs"]["XBTUSDC"]
assert all(k_ in row for k_ in ("chg_5m_pct", "chg_15m_pct", "chg_1h_pct")), row
assert abs(row["chg_1h_pct"] - (101.0 / (100.0 + 0.01 * 30) - 1) * 100) < 0.05, row    # vs the close one hour ago
assert len(warm.hist["XBTUSDC"]) == 76 and warm.hist["XBTUSDC"][0][0] < warm.hist["XBTUSDC"][-1][0]
print("SCANNER WARM START TESTS PASSED")

# survival sizing: a stop-out, fees included, costs at most MAX_RISK_PCT of equity
import tempfile as _tf3, pathlib as _pl3, policy as _pol3
shachar.HERE = _pol3.HERE = _pl3.Path(_tf3.mkdtemp())
shachar.INFLIGHT.clear()
_rt = (shachar.FEE + shachar.MAKER_FEE) * 100
for _stop, _take in ((1.5, 5), (3, 5), (6, 10), (10, 20)):
    br = {"name": "tr%s" % _stop, "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
    r = shachar.execute(FK(), {"bots": {br["name"]: br}}, br, {"action": "buy", "pair": "XBTUSDC", "usdc": 1000,
                                                                "take_pct": _take, "stop_pct": _stop}, tk, "dry", False)
    assert r.startswith("buy"), r
    spent = 40.0 - br["cash"]
    worst = spent * (_stop + _rt) / 100                                       # loss if the stop is hit
    assert worst <= 40.0 * shachar.MAX_RISK_PCT / 100 + 0.02, (_stop, spent, worst)
    assert spent <= 40.0 * shachar.DRY_SIZE_CAP + 1e-6
small = {"name": "trs", "cash": 40.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0}
shachar.execute(FK(), {"bots": {"trs": small}}, small, {"action": "buy", "pair": "XBTUSDC", "usdc": 5, "take_pct": 5, "stop_pct": 3}, tk, "dry", False)
assert abs((40.0 - small["cash"]) - 5.0) < 0.1, small["cash"]                 # a request under the cap is not changed
print("RISK SIZE CAP TESTS PASSED")

# replay on highs and lows: wicks count, the stop wins a tie, a gap exits at the open, fees are the real 1.2%
def _cd(c, **kw): return dict({"o": c, "h": c, "l": c, "c": c}, **kw)
_rows = [_cd(100.0), _cd(100.0, h=106.0, l=96.0), _cd(100.0)]
_n, _j = shachar.sim_trade(_rows, 0); assert abs(_n + 3.0) < 1e-9 and _j == 1                              # touched both levels: counted as the stop
_rows[1] = _cd(94.0, o=94.0, h=95.0, l=93.0)
assert abs(shachar.sim_trade(_rows, 0)[0] - (-6.0)) < 1e-9                   # opened under the stop: exit at the open
_rows[1] = _cd(100.0, h=105.5, l=99.0)
assert abs(shachar.sim_trade(_rows, 0, fee_pct=1.2)[0] - 3.8) < 1e-9         # take hit, fees taken off
_rows[1] = _cd(100.0, h=101.0, l=99.0)
_n, _j = shachar.sim_trade(_rows, 0, hold=1); assert abs(_n) < 1e-9 and _j == 1                       # neither level: exit at the close on time
_up = [100 * (1.004 ** i) for i in range(300)]                               # steady climb: the trend setup wins
_closes_only = shachar.backtest_setup(_up, "trend")
assert _closes_only and _closes_only["win_pct"] == 100 and 3.5 < _closes_only["avg_net_pct"] <= 5 - 1.2, _closes_only   # 3.8 per take; the last trade ends with the data
_wicky = [_cd(c, l=c * 0.96) for c in _up]                                   # same closes, but every hour dips 4% first
_with_wicks = shachar.backtest_setup(_wicky, "trend")
assert _with_wicks["win_pct"] == 0 and _with_wicks["avg_net_pct"] < -4, _with_wicks   # closes alone hid every stop-out
assert shachar.backtest_setup(_up, "trend", fee_pct=1.6)["avg_net_pct"] < _closes_only["avg_net_pct"]


class _KC:
    def ohlc_candles(self, pair): return _wicky
class _KO:
    def ohlc_full(self, pair): return _up
assert shachar.replay_rows(_KC(), "XBTUSDC") is _wicky and shachar.replay_rows(_KO(), "XBTUSDC") is _up
print("REPLAY ON HIGHS AND LOWS TESTS PASSED")
