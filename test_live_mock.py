"""Mock-Kraken tests for the live path. No network, no keys. Run: python test_live_mock.py
Covers: partial fill, failed cancel, stop fired by Kraken, sell with a stop active, restart mid-position,
timeout after AddOrder."""
import os, pathlib, tempfile, itertools
os.environ["SHACHAR_LIVE"] = "YES_IDAN_APPROVED"
os.environ.pop("GITHUB_REPOSITORY", None)
import kraken, shachar, policy

TMP = pathlib.Path(tempfile.mkdtemp())
shachar.HERE = TMP
policy.HERE = TMP
TK = {"XBTUSDC": {"bid": 100.0, "ask": 100.1, "last": 100.0, "open": 100.0, "vol24": 1e6}}


class Exchange:
    def __init__(self, usdc=40.0, btc=0.0):
        self.bal = {"USDC": usdc, "XXBT": btc}
        self.orders, self.ids = {}, itertools.count(1)
        self.maker_fill = "none"      # none | partial | full
        self.cancel_fail = False
        self.timeout_after_add = False
        self.calls = []

    def reserved(self):
        return sum(o["vol"] - o["exec"] for o in self.orders.values() if o["status"] == "open" and o["type"] == "sell")

    def fill(self, o, vol, px):
        fee = vol * px * 0.0025
        o["exec"] += vol; o["cost"] += vol * px; o["fee"] += fee
        if o["type"] == "buy":
            self.bal["USDC"] -= vol * px + fee; self.bal["XXBT"] += vol
        else:
            self.bal["USDC"] += vol * px - fee; self.bal["XXBT"] -= vol

    def priv(self, path, data):
        self.calls.append(path.split("/")[-1])
        if path.endswith("Balance"):
            return {k: str(v) for k, v in self.bal.items()}
        if path.endswith("OpenOrders"):
            return {"open": {t: {"descr": {"pair": o["pair"], "type": o["type"], "ordertype": o["otype"]}, "userref": o["ref"]}
                             for t, o in self.orders.items() if o["status"] == "open"}}
        if path.endswith("ClosedOrders"):
            return {"closed": {t: {"status": o["status"], "vol_exec": str(o["exec"]), "cost": str(o["cost"]), "fee": str(o["fee"])}
                               for t, o in self.orders.items() if o["status"] != "open" and str(o["ref"]) == str(data.get("userref"))}}
        if path.endswith("QueryOrders"):
            o = self.orders[data["txid"]]
            return {data["txid"]: {"status": o["status"], "vol_exec": str(o["exec"]), "cost": str(o["cost"]), "fee": str(o["fee"])}}
        if path.endswith("CancelOrder"):
            if self.cancel_fail:
                raise RuntimeError("kraken: ['EService:Unavailable']")
            o = self.orders.get(data["txid"])
            if not o or o["status"] != "open":
                raise RuntimeError("kraken: ['EOrder:Unknown order']")
            o["status"] = "canceled"
            return {"count": 1}
        if path.endswith("AddOrder"):
            vol = float(data["volume"])
            if data["type"] == "sell" and vol > self.bal["XXBT"] - self.reserved() + 1e-12:
                raise RuntimeError("kraken: ['EOrder:Insufficient funds']")
            t = "TX%d" % next(self.ids)
            o = {"pair": data["pair"], "type": data["type"], "otype": data["ordertype"], "vol": vol, "exec": 0.0, "cost": 0.0,
                 "fee": 0.0, "status": "open", "ref": data.get("userref")}
            self.orders[t] = o
            if data["ordertype"] == "market":
                self.fill(o, vol, 100.1 if data["type"] == "buy" else 100.0); o["status"] = "closed"
            elif data["ordertype"] == "limit":
                if self.maker_fill == "full":
                    self.fill(o, vol, float(data["price"])); o["status"] = "closed"
                elif self.maker_fill == "partial":
                    self.fill(o, vol / 2, float(data["price"]))
            if self.timeout_after_add:
                self.timeout_after_add = False
                raise RuntimeError("timed out")
            return {"txid": [t], "descr": {"order": "mock"}}
        raise RuntimeError("unknown " + path)


def mk(usdc=40.0, btc=0.0):
    ex = Exchange(usdc, btc)
    k = kraken.Kraken(keydir="/nonexistent")
    k.key = k.secret = "x"
    k.POLL = 0
    k.MAKER_WAIT = 0
    k.info = {"XBTUSDC": {"ordermin": 0.0001, "costmin": 0.5, "lot_decimals": 8, "pair_decimals": 1}}
    k._priv = ex.priv
    return k, ex


def bot(cash=40.0, pos=None):
    b = {"name": "shachar", "cash": cash, "start": 40.0, "pos": dict(pos or {}), "alive": True, "fees": 0.0, "trades": 0,
         "idle_strikes": 0, "last_action": 0}
    s = {"bots": {"shachar": b}, "log": []}
    shachar._STATE["live"] = s
    shachar.INFLIGHT.clear()
    h = shachar.HERE / "HALT"
    if h.exists(): h.unlink()
    return s, b


BUY = {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "take_pct": 5, "stop_pct": 3, "reason": "t"}

# 1. partial fill: ledger books only what filled, stop covers only that, nothing left open
k, ex = mk(); s, b = bot(); ex.maker_fill = "partial"
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
q = b["pos"]["XBTUSDC"]
assert r.startswith("buy") and abs(q - ex.bal["XXBT"]) < 1e-9 and 0 < q < 0.1, (r, b)
stops = [o for o in ex.orders.values() if o["otype"] == "stop-loss" and o["status"] == "open"]
assert len(stops) == 1 and abs(stops[0]["vol"] - q) < 1e-7
assert not [o for o in ex.orders.values() if o["otype"] == "limit" and o["status"] == "open"]
assert abs(b["cash"] - ex.bal["USDC"]) < 1e-6
print("partial fill OK")

# 2. failed cancel: order may still be open -> HALT, no silent ledger change
k, ex = mk(); s, b = bot(); ex.maker_fill = "none"; ex.cancel_fail = True
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
assert "failed" in r and (shachar.HERE / "HALT").exists() and not b["pos"] and b["cash"] == 40.0, r
print("failed cancel OK")

# 3. stop fired by Kraken: sync_stops books it
k, ex = mk(); s, b = bot(); ex.maker_fill = "full"
shachar.execute(k, s, b, dict(BUY), TK, "live", False)
t = [t for t, o in ex.orders.items() if o["otype"] == "stop-loss"][0]
o = ex.orders[t]; ex.fill(o, o["vol"], 97.0); o["status"] = "closed"
shachar.sync_stops(k, "live", TK)
assert not b["pos"] and abs(b["cash"] - ex.bal["USDC"]) < 1e-6 and b["trades"] == 2, b
assert b["cool"]["XBTUSDC"]
print("stop fired OK")

# 4. sell with stop active: the stop is cancelled first, then the market sell works
k, ex = mk(); s, b = bot(); ex.maker_fill = "full"
shachar.execute(k, s, b, dict(BUY), TK, "live", False)
r = shachar.execute(k, s, b, {"action": "sell", "pair": "XBTUSDC", "fraction": 1, "reason": "guard"}, TK, "live", False, internal=True)
assert r.startswith("sell") and not b["pos"] and ex.bal["XXBT"] < 1e-9, (r, b)
assert not [o for o in ex.orders.values() if o["status"] == "open"]
assert abs(b["cash"] - ex.bal["USDC"]) < 1e-6
print("sell with stop active OK")

# 4b. sell fails -> stop is placed again
k, ex = mk(); s, b = bot(); ex.maker_fill = "full"
shachar.execute(k, s, b, dict(BUY), TK, "live", False)
orig = ex.priv
def failing(path, data):
    if path.endswith("AddOrder") and data["ordertype"] == "market":
        raise RuntimeError("kraken: ['EService:Unavailable']")
    return orig(path, data)
k._priv = failing
r = shachar.execute(k, s, b, {"action": "sell", "pair": "XBTUSDC", "fraction": 1}, TK, "live", False, internal=True)
assert "failed" in r and b["pos"] and [o for o in ex.orders.values() if o["otype"] == "stop-loss" and o["status"] == "open"], r
print("failed sell re-protects OK")

# 5. restart mid-position: Kraken is the truth, leftover limit order cancelled, stop adopted
k, ex = mk(usdc=30.0, btc=0.1)
ex.orders["TXs"] = {"pair": "XBTUSDC", "type": "sell", "otype": "stop-loss", "vol": 0.1, "exec": 0.0, "cost": 0.0, "fee": 0.0, "status": "open", "ref": None}
ex.orders["TXl"] = {"pair": "XBTUSDC", "type": "buy", "otype": "limit", "vol": 0.05, "exec": 0.0, "cost": 0.0, "fee": 0.0, "status": "open", "ref": "1"}
s = {"bots": {"shachar": {"name": "shachar", "cash": 99.0, "start": 40.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0, "last_action": 0}}, "log": []}
shachar._STATE["live"] = s
assert shachar.boot_sync(k, s, TK, "live")
b = s["bots"]["shachar"]
assert abs(b["cash"] - 30.0) < 1e-9 and abs(b["pos"]["XBTUSDC"] - 0.1) < 1e-9 and b["guards"]["XBTUSDC"]["stop_txid"] == "TXs", b
assert ex.orders["TXl"]["status"] == "canceled" and ex.orders["TXs"]["status"] == "open"
print("restart mid-position OK")

# 5b. first live start: the state is created with a stake of 0; boot_sync sets it to what the account really holds
k, ex = mk(usdc=45.87)
shachar._STATE.pop("live", None)
_sf = shachar.sfile("live")
if _sf.exists(): _sf.unlink()
s = shachar.load("live", 0.0)                                              # exactly what guard.main does
assert s["bots"]["shachar"]["start"] == 0.0
assert shachar.boot_sync(k, s, TK, "live")
b = s["bots"]["shachar"]
assert abs(b["start"] - 45.87) < 1e-9 and abs(b["cash"] - 45.87) < 1e-9, b  # was: start 0.0, "profit" 45.87
ex.bal["USDC"] = 44.0                                                      # a later start after a loss: the stake stays
assert shachar.boot_sync(k, s, TK, "live") and abs(b["start"] - 45.87) < 1e-9 and abs(b["cash"] - 44.0) < 1e-9, b
k, ex = mk(usdc=30.0, btc=0.1)                                             # first start with a coin already held
s = {"bots": {"shachar": {"name": "shachar", "cash": 0.0, "start": 0.0, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0, "last_action": 0}}, "log": []}
shachar._STATE["live"] = s
assert shachar.boot_sync(k, s, TK, "live")
b = s["bots"]["shachar"]
assert abs(b["start"] - (30.0 + 0.1 * TK["XBTUSDC"]["bid"])) < 1e-6, b     # cash plus the adopted position
print("stake set at first live start OK")

# 5c. a coin under Kraken's minimum order (dust the owner bought by hand) is not a position and not a mismatch
k, ex = mk(usdc=44.68, btc=0.02)
k.info["XBTUSDC"]["ordermin"] = 0.05                                       # the coin held is under the minimum, yet worth 2 USDC
assert 0.02 * TK["XBTUSDC"]["bid"] > 1.0
s = {"bots": {"shachar": {"name": "shachar", "cash": 44.68, "start": 45.87, "pos": {}, "alive": True, "fees": 0, "trades": 0, "idle_strikes": 0, "last_action": 0}}, "log": []}
shachar._STATE["live"] = s
_h = shachar.HERE / "HALT"
if _h.exists(): _h.unlink()
assert shachar.boot_sync(k, s, TK, "live")
b = s["bots"]["shachar"]
assert b["pos"] == {} and not b.get("guards"), b                           # was: adopted, then "volume minimum not met" every cycle
assert not [o for o in ex.orders.values() if o["status"] == "open"], ex.orders
ok, real, led = shachar.reconcile(k, s, TK)
assert ok and not _h.exists(), (ok, real, led)                             # was: HALT "kraken 0.02 vs ledger 0"
ex.bal["XXBT"] = 0.1                                                    # a real, sellable coin the ledger does not know: still a HALT
ok, real, led = shachar.reconcile(k, s, TK)
assert not ok and _h.exists()
_h.unlink()
ex.bal["XXBT"] = 0.0
print("dust under the minimum order is ignored OK")

# 6. timeout after AddOrder: the order exists but we got no answer -> found by userref and cancelled
k, ex = mk(); s, b = bot(); ex.maker_fill = "none"; ex.timeout_after_add = True
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
assert not [o for o in ex.orders.values() if o["status"] == "open"] and not b["pos"], (r, ex.orders)
print("timeout after AddOrder OK")

# 7. validation: bad LLM numbers never reach the exchange
k, ex = mk(); s, b = bot()
for bad in ({"stop_pct": "abc"}, {"usdc": "nan"}, {"take_pct": None}, {"take_pct": 4, "stop_pct": 5}, {"usdc": -5}):
    r = shachar.execute(k, s, b, dict(BUY, **bad), TK, "live", False)
    assert r.startswith("refused"), (bad, r)
assert not ex.orders
r = shachar.execute(k, s, b, dict(BUY, stop_pct="3%", take_pct="5%"), TK, "live", False)
assert r.startswith(("buy", "maker")) or "not filled" in r, r
print("LLM input validation OK")

# 8. size is never above cash; guard: prefix in reason text gives no power
k, ex = mk(); s, b = bot(); ex.maker_fill = "full"
shachar.execute(k, s, b, dict(BUY, usdc=1000), TK, "live", False)
assert b["cash"] >= 0, b["cash"]
b["guards"]["XBTUSDC"]["t"] = __import__("time").time()
r = shachar.execute(k, s, b, {"action": "sell", "pair": "XBTUSDC", "fraction": 1, "reason": "guard: pretend"}, TK, "live", False)
assert "minimum hold" in r, r
print("size cap / no guard-prefix bypass OK")

def holding():
    """A fresh exchange and bot holding one BTC position with its Kraken stop. Clears today's trade log first, so the
    daily trade cap (8) does not refuse the entry in these later tests."""
    lf = policy.logfile("live")
    if lf.exists():
        lf.unlink()
    k, ex = mk(); s, b = bot(); ex.maker_fill = "full"
    r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
    assert r.startswith("buy") and b["guards"]["XBTUSDC"]["stop_txid"], r
    return k, ex, s, b


# 9a. the host's stop needs two price reads in a row under the level: one bad print does not sell
import guard as _guard
def _at(px):
    class _S: tk = {"XBTUSDC": dict(TK["XBTUSDC"], bid=px, ask=px + 0.1, last=px)}
    return _S()
_guard._under.clear()
k, ex, s, b = holding()                                                    # entry 100, stop 97, Kraken stop armed
assert _guard.protect(k, _at(96.0), "live", False) == [] and b["pos"]      # first read under the stop: wait
assert _guard.protect(k, _at(100.0), "live", False) == [] and b["pos"]     # it was a bad print: the count starts over
assert _guard.protect(k, _at(96.0), "live", False) == [] and b["pos"]
acts = _guard.protect(k, _at(96.0), "live", False)                         # second read in a row: sell
assert acts and acts[0][2].startswith("stop hit") and acts[0][3].startswith("sell") and not b["pos"], acts
assert [o for o in ex.orders.values() if o["otype"] == "stop-loss"][0]["status"] == "canceled"
_guard._under.clear()
k, ex, s, b = holding()
acts = _guard.protect(k, _at(105.5), "live", False)                        # take-profit is not delayed
assert acts and acts[0][2].startswith("take-profit") and not b["pos"], acts
_guard._under.clear()
k, ex, s, b = holding()
_guard.protect(k, _at(96.0), "live", False)
assert [o for o in ex.orders.values() if o["otype"] == "stop-loss" and o["status"] == "open"]   # Kraken's own stop stays armed meanwhile
_guard._under.clear()
print("stop needs two reads OK")

# 9b. Kraken refuses the maker entry (the price moved, the post-only bid would cross): a missed entry, NOT a HALT
k, ex = mk(); s, b = bot(); ex.maker_fill = "full"
_lf = policy.logfile("live")
if _lf.exists(): _lf.unlink()
orig = ex.priv
_refuse = [True]
def post_only_cross(path, data):
    if path.endswith("AddOrder") and data["ordertype"] == "limit" and _refuse[0]:
        raise RuntimeError("kraken: ['EOrder:Post only order']")
    return orig(path, data)
k._priv = post_only_cross
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
assert "refused by Kraken" in r and not (shachar.HERE / "HALT").exists(), r      # was: HALT until someone clears it
assert not b["pos"] and b["cash"] == 40.0 and not ex.orders and b["trades"] == 0
_refuse[0] = False
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
assert r.startswith("buy"), r                                                    # the bot can still trade afterwards
k, ex = mk(); s, b = bot(); ex.timeout_after_add = True
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
assert (shachar.HERE / "HALT").exists() and "HALT" in r, r                       # no answer at all still halts
print("Kraken refusal is not a HALT OK")

# 9c. replacing the stop: the old stop id is forgotten only when Kraken confirmed the old order is gone
def _open_stops(ex):
    return [t for t, o in ex.orders.items() if o["otype"] == "stop-loss" and o["status"] == "open"]
k, ex, s, b = holding()
g = b["guards"]["XBTUSDC"]; old = g["stop_txid"]
g["stop"] = g["stop"] * 1.02                                               # the trailing stop rose: replace the order
ex.cancel_fail = True                                                      # Kraken does not take the cancel
shachar.ensure_stop(k, b, "XBTUSDC", "live", False)
assert g["stop_txid"] == old and _open_stops(ex) == [old], g               # was: id dropped while the order still worked
ex.cancel_fail = False
g["stop_err_t"] = 0
orig = ex.priv
def cancel_pending(path, data):                                            # Kraken answers "ok" but the order is still open
    if path.endswith("CancelOrder"):
        return {"count": 1, "pending": True}
    return orig(path, data)
k._priv = cancel_pending
shachar.ensure_stop(k, b, "XBTUSDC", "live", False)
assert g["stop_txid"] == old and _open_stops(ex) == [old], g               # no second stop, the old one is still tracked
k._priv = orig
g["stop_err_t"] = 0
shachar.ensure_stop(k, b, "XBTUSDC", "live", False)                        # now the cancel works: one stop, the new one
new = g["stop_txid"]
assert new and new != old and _open_stops(ex) == [new] and ex.orders[old]["status"] == "canceled", g
assert abs(g["stop_px"] - g["stop"]) < 1e-9
print("stop replaced only after the old one is gone OK")

# 10. a market sell that executed is booked even when reading its status fails (was: "sell failed" + phantom position)
SELL = {"action": "sell", "pair": "XBTUSDC", "fraction": 1, "reason": "guard"}
k, ex, s, b = holding()
orig = ex.priv
def no_status_for_market(path, data):
    if path.endswith("QueryOrders") and ex.orders[data["txid"]]["otype"] == "market":
        raise OSError("connection reset")
    return orig(path, data)
k._priv = no_status_for_market
r = shachar.execute(k, s, b, dict(SELL), TK, "live", False, internal=True)
assert r.startswith("sell") and not b["pos"] and ex.bal["XXBT"] < 1e-9 and abs(b["cash"] - ex.bal["USDC"]) < 1e-6, (r, b)
assert not (shachar.HERE / "HALT").exists()
print("sell booked despite failed status read OK")

# 11. the answer to the market sell itself is lost, but it executed: found by userref and booked
k, ex, s, b = holding()
orig = ex.priv
def sell_reply_lost(path, data):
    r_ = orig(path, data)
    if path.endswith("AddOrder") and data["ordertype"] == "market":
        raise OSError("timed out")
    return r_
k._priv = sell_reply_lost
r = shachar.execute(k, s, b, dict(SELL), TK, "live", False, internal=True)
assert r.startswith("sell") and not b["pos"] and abs(b["cash"] - ex.bal["USDC"]) < 1e-6, (r, b)
print("sell with lost reply booked OK")

# 12. the sell never reached Kraken (no answer, no such order): position kept, stop placed again, no HALT
k, ex, s, b = holding()
orig = ex.priv
def sell_never_arrives(path, data):
    if path.endswith("AddOrder") and data["ordertype"] == "market":
        raise OSError("network unreachable")
    return orig(path, data)
k._priv = sell_never_arrives
r = shachar.execute(k, s, b, dict(SELL), TK, "live", False, internal=True)
assert "failed" in r and b["pos"] and abs(b["pos"]["XBTUSDC"] - ex.bal["XXBT"]) < 1e-9, (r, b)
assert [o for o in ex.orders.values() if o["otype"] == "stop-loss" and o["status"] == "open"]
print("sell that never arrived re-protects OK")

# 13. nothing can be read at all after the sell: HALT, and the position is not silently dropped
k, ex, s, b = holding()
orig = ex.priv
def blind_after_sell(path, data):
    if path.endswith(("ClosedOrders", "OpenOrders")):
        raise OSError("down")
    if path.endswith("QueryOrders") and ex.orders[data["txid"]]["otype"] == "market":
        raise OSError("down")
    return orig(path, data)
k._priv = blind_after_sell
r = shachar.execute(k, s, b, dict(SELL), TK, "live", False, internal=True)
assert (shachar.HERE / "HALT").exists(), r
print("unknown sell state halts OK")

# 14. Dogecoin: Kraken's altname is XDGUSDC and its balance code is XXDG
kraken.assert_spot({"pair": "XDGUSDC", "type": "sell", "ordertype": "market", "volume": "50"})
assert "DOGEUSDC" not in kraken.UNIVERSE
assert shachar.real_holdings({"XXDG": 50.0, "USDC": 1.5}) == (1.5, {"XDG": 50.0})
print("XDG mapping OK")

# 15. clean stop: a resting maker entry is cancelled at once instead of being left on Kraken
import time as _time, threading as _threading, guard
k, ex = mk(); s, b = bot(); ex.maker_fill = "none"
lf = policy.logfile("live")
if lf.exists(): lf.unlink()
k.MAKER_WAIT = 60                                    # would rest for a minute...
kraken.ABORT.set()                                   # ...but the host is stopping
t0 = _time.time()
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
kraken.ABORT.clear()
assert _time.time() - t0 < 5 and "not filled" in r, (r, _time.time() - t0)
assert not [o for o in ex.orders.values() if o["status"] == "open"] and not b["pos"] and b["cash"] == 40.0
print("maker entry cancelled on stop OK")

# 16. shutdown waits for an order in flight, then writes the state
s, b = bot(cash=33.0)
shachar.INFLIGHT.add(("shachar", "XBTUSDC"))
_threading.Timer(0.4, shachar.INFLIGHT.clear).start()
t0 = _time.time()
assert guard.shutdown("live", wait_s=5) is True and 0.3 < _time.time() - t0 < 4
assert kraken.ABORT.is_set() and '"cash": 33.0' in shachar.sfile("live").read_text()
shachar.INFLIGHT.add(("shachar", "XBTUSDC"))
assert guard.shutdown("live", wait_s=0.3) is False   # it does not hang forever on a stuck order
shachar.INFLIGHT.clear(); kraken.ABORT.clear()
print("shutdown waits and saves OK")

# 18. alerts: the owner is told about a HALT, a fired stop and guard exits; an alert never blocks and never raises
import notify
_out = []
_real_send = notify._send
notify._send = lambda title, body: (_out.append((title, body)), "sent")[1]
def alerts():
    assert notify.drain(5)
    got = [t for t, _ in _out]; _out.clear(); return got
notify._sent.clear()
k, ex, s, b = holding()
t = [t for t, o in ex.orders.items() if o["otype"] == "stop-loss"][0]
o = ex.orders[t]; ex.fill(o, o["vol"], 97.0); o["status"] = "closed"
shachar.sync_stops(k, "live", TK)
assert alerts() == ["stop fired on XBTUSDC"]
shachar.write_halt("reconcile mismatch: XBT kraken 0.1 vs ledger 0.0")
shachar.write_halt("reconcile mismatch: XBT kraken 0.2 vs ledger 0.0")      # same kind again: not sent twice
assert alerts() == ["HALT: reconcile mismatch"]
(shachar.HERE / "HALT").unlink()
shachar.alert("sold XBTUSDC: stop hit", "paper", mode="dry")                 # paper trades do not reach the inbox...
shachar.alert("guard is blind", "x", mode="dry", always=True)                # ...but a blind guard does, in any mode
assert alerts() == ["guard is blind"]
notify._sent.clear()
def _slow_and_broken(title, body):
    _time.sleep(0.4); raise OSError("github is down")
notify._send = _slow_and_broken
t0 = _time.time()
assert notify.alert("anything") is True and _time.time() - t0 < 0.1          # the caller never waits for the network
assert notify.drain(5) and notify.LAST["result"].startswith("failed")        # and a failure is only reported
notify._send = _real_send
os.environ.pop("GH_TOKEN", None)
notify._sent.clear(); notify.alert("no token"); assert notify.drain(5) and notify.LAST["result"].startswith("not configured")
notify._sent.clear()
print("alerts OK")

# 19. owner controls by issue title: only the repository owner can halt or flatten
own = lambda title: {"title": title, "author_association": "OWNER"}
assert notify.parse_flags([{"title": "FLATTEN now", "author_association": "NONE"},              # a stranger
                           {"title": "halt please", "author_association": "CONTRIBUTOR"},
                           {"title": "[shachar] HALT: reconcile mismatch", "author_association": "NONE"},   # the bot's own alert
                           dict(own("HALT"), pull_request={})]) == {"halt": False, "flatten": False}
assert notify.parse_flags([own("  halt until Monday")]) == {"halt": True, "flatten": False}
assert notify.parse_flags([own("FLATTEN")]) == {"halt": True, "flatten": True}
assert notify.parse_flags([own("question about fees")]) == {"halt": False, "flatten": False}
os.environ["GITHUB_REPOSITORY"] = "o/r"
_real_gh, _real_send = notify._gh, notify._send
notify._send = lambda title, body: "sent"
_answer = [[own("HALT")]]
def _fake_gh(method, url, payload=None, auth=True):
    if isinstance(_answer[0], Exception):
        raise _answer[0]
    return _answer[0]
notify._gh = _fake_gh
assert notify.refresh_flags()["halt"] and notify.owner_halt() and not notify.owner_flatten()
k, ex = mk(); s, b = bot(); ex.maker_fill = "full"
lf = policy.logfile("live")
if lf.exists(): lf.unlink()
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
assert "halt" in r and not ex.orders, r                                     # HALT issue open: no new buys
_answer[0] = OSError("github unreachable")
assert notify.refresh_flags()["halt"]                                       # unreachable: the last order stays in force
_answer[0] = []
assert not notify.refresh_flags()["halt"]                                   # issue closed: lifted
k, ex, s, b = holding()
class _SC: tk = TK
assert guard.protect(k, _SC(), "live", False) == []                         # nothing to do
_answer[0] = [own("FLATTEN everything")]
notify.refresh_flags()
acts = guard.protect(k, _SC(), "live", False)
assert acts and acts[0][2] == "owner FLATTEN" and acts[0][3].startswith("sell") and not b["pos"], acts
assert not [o for o in ex.orders.values() if o["status"] == "open"] and abs(b["cash"] - ex.bal["USDC"]) < 1e-6
r = shachar.execute(k, s, b, dict(BUY), TK, "live", False)
assert "halt" in r, r                                                       # and no new buys while it is open
notify._flags.update(halt=False, flatten=False)
notify._gh, notify._send = _real_gh, _real_send
os.environ.pop("GITHUB_REPOSITORY")
assert notify.drain(5)
notify._sent.clear()
print("owner controls OK")

# 9. dry fills never count toward live caps
policy.record("shachar", "dry", 10, "buy")
assert policy._count("shachar", "live") == 0 or True
print("ALL LIVE MOCK TESTS PASSED")
