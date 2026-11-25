"""Safety and behaviour tests. No network, no keys. Run: python -m unittest -v test_shachar"""
import os, json, time, tempfile, pathlib, threading, random, unittest
from unittest import mock

for _v in ("SHACHAR_LIVE", "SHACHAR_HALT", "GH_TOKEN", "REPO", "LLM_KEY", "IDLE_DEATH", "REPLAY_GATE", "ALLOW_HEURISTIC",
           "KRAKEN_KEY", "KRAKEN_SECRET", "TG_TOKEN", "TG_CHAT"):
    os.environ.pop(_v, None)

import store, notify, kraken, policy, brain, shachar, guard, scanner

INFO = {"XBTUSDC": {"ordermin": 0.0001, "costmin": 0.5, "lot_decimals": 8, "pair_decimals": 1, "base": "XXBT"},
        "ETHUSDC": {"ordermin": 0.001, "costmin": 0.5, "lot_decimals": 8, "pair_decimals": 2, "base": "XETH"},
        "SOLUSDC": {"ordermin": 0.01, "costmin": 0.5, "lot_decimals": 8, "pair_decimals": 2, "base": "SOL"}}
BUY = {"action": "buy", "pair": "XBTUSDC", "usdc": 10, "stop_pct": 3, "take_pct": 6, "reason": "test"}


def TK(px=100.0, spread=0.0005, **over):
    tk = {p: {"bid": px, "ask": px * (1 + spread), "last": px, "open": px, "vol24": 1e6} for p in INFO}
    for p, v in over.items():
        tk[p] = dict(tk[p], **v)
    return tk


class FakeK:
    """Stands in for kraken.Kraken in the ledger tests."""
    def __init__(self):
        self.calls, self.orders, self.bal = [], {}, {"USDC": 40.0}
        self.px, self.fill_frac, self.n = 100.0, 1.0, 0
        self.maker_exc = self.sell_exc = self.stop_exc = None
        self.lock_free_during_order = None

    def pairs(self):
        return INFO

    def order_maker(self, pair, side, volume, price, wait=90):
        self.calls.append(("maker", pair, volume, price))
        got = []
        t = threading.Thread(target=lambda: got.append(store.LOCK.acquire(timeout=1) and (store.LOCK.release() or True)))
        t.start(); t.join()
        self.lock_free_during_order = bool(got and got[0])
        if self.maker_exc:
            raise self.maker_exc
        v = volume * self.fill_frac
        return {"live": kraken.live_enabled(), "descr": "buy limit", "txid": "M1", "vol": v, "cost": v * price,
                "fee": v * price * 0.004, "status": "closed"}

    def order(self, pair, side, volume):
        self.calls.append(("sell", pair, volume))
        if self.sell_exc:
            raise self.sell_exc
        return {"live": kraken.live_enabled(), "descr": "sell market", "txid": "X1", "vol": volume, "cost": volume * self.px,
                "fee": volume * self.px * 0.008, "status": "closed"}

    def stop_order(self, pair, volume, stop_price):
        self.calls.append(("stop", pair, volume, stop_price))
        if self.stop_exc:
            raise self.stop_exc
        self.n += 1
        txid = "S%d" % self.n
        self.orders[txid] = {"status": "open", "vol": volume, "vol_exec": 0.0, "cost": 0.0, "fee": 0.0, "price": stop_price}
        return {"live": True, "descr": "stop", "txid": txid}

    def cancel_confirmed(self, txid, tries=5):
        self.calls.append(("cancel", txid))
        o = self.orders[txid]
        if o["status"] == "open":
            o["status"] = "canceled"
        return dict(o)

    def query_order(self, txid):
        return dict(self.orders[txid])

    def open_orders(self):
        return {t: o for t, o in self.orders.items() if o["status"] == "open"}

    def balance(self):
        return dict(self.bal)

    def usdc_peg(self):
        return 1.0

    def depth(self, pair, count=25):
        return {"bid_usd_0.5pct": 1000, "ask_usd_0.5pct": 1000, "imbalance": 0.0}

    def fire(self, txid, px):
        o = self.orders[txid]
        o.update(status="closed", vol_exec=o["vol"], cost=o["vol"] * px, fee=o["vol"] * px * 0.008)


class Base(unittest.TestCase):
    mode = "dry"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store.DATA = pathlib.Path(self.tmp.name)
        for v in ("SHACHAR_LIVE", "SHACHAR_HALT", "IDLE_DEATH", "REPLAY_GATE", "RR_GATE", "NEWS_HEADLINES", "LLM_KEY"):
            os.environ.pop(v, None)
        notify._flags.update(halt=False, flatten=False, ok_t=0.0, configured=False)
        notify._sent.clear()
        shachar._ALERTS.clear(); guard._below.clear(); guard._backoff.clear()
        self.alerts = []
        p = mock.patch.object(notify, "alert", side_effect=lambda t, b="", dedupe_s=0: self.alerts.append(t))
        p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.k = FakeK()
        if self.mode == "live":
            os.environ["SHACHAR_LIVE"] = kraken.LIVE_TOKEN
            self.addCleanup(lambda: os.environ.pop("SHACHAR_LIVE", None))
        store.save(self.mode, {"mode": self.mode, "bots": {"t": store.newbot("t", 40.0)}, "log": []})

    def bot(self, name="t"):
        return store.load(self.mode)["bots"][name]

    def edit(self, fn, name="t"):
        s = store.load(self.mode)
        fn(s["bots"][name])
        store.save(self.mode, s)

    def ex(self, d, tk=None, **kw):
        return shachar.execute(self.k, "t", d, tk or TK(), self.mode, **kw)

    def paper_buy(self, d=BUY, px=100.0):
        r = self.ex(d, TK(px))
        self.assertTrue(r.startswith("paper maker order working"), r)
        shachar.fill_pending(TK(px, XBTUSDC={"ask": px, "last": px * 0.999}, ETHUSDC={"ask": px, "last": px * 0.999}), "dry")
        return self.bot()


# ------------------------------------------------------------------ decision parsing (bug 1)

class TestParse(Base):
    def test_bad_buys_are_rejected(self):
        for bad in ({**BUY, "stop_pct": None}, {**BUY, "stop_pct": "three"}, {**BUY, "stop_pct": ""}, {**BUY, "take_pct": float("nan")},
                    {**BUY, "usdc": float("inf")}, {**BUY, "usdc": -5}, {**BUY, "usdc": True}, {**BUY, "pair": "FAKEUSDC"},
                    {**BUY, "pair": None}, {k: v for k, v in BUY.items() if k != "stop_pct"},
                    {k: v for k, v in BUY.items() if k != "take_pct"}, "buy", None):
            clean, err = shachar.parse_decision(bad)
            self.assertTrue(clean is None and err, bad)

    def test_clamps_and_defaults(self):
        c, _ = shachar.parse_decision({**BUY, "stop_pct": "3%", "take_pct": " 6.5 ", "usdc": "1,000"})
        self.assertEqual((c["stop_pct"], c["take_pct"], c["usdc"]), (3.0, 6.5, 1000.0))
        c, _ = shachar.parse_decision({**BUY, "stop_pct": 99, "take_pct": 0.1})
        self.assertEqual((c["stop_pct"], c["take_pct"]), (10.0, 2.0))
        c, _ = shachar.parse_decision({"action": "sell", "pair": "XBTUSDC", "fraction": 7})
        self.assertEqual(c["fraction"], 1.0)
        c, _ = shachar.parse_decision({"action": "sell", "pair": "XBTUSDC", "fraction": None})
        self.assertEqual(c["fraction"], 1.0)
        c, _ = shachar.parse_decision({"action": "dance"})
        self.assertEqual(c["action"], "hold")


class TestLiveBadDecision(Base):
    mode = "live"

    def test_null_stop_never_reaches_kraken(self):
        r = self.ex({**BUY, "stop_pct": None})
        self.assertTrue(r.startswith("refused: bad decision"), r)
        self.assertEqual(self.k.calls, [])
        self.assertEqual(self.bot()["cash"], 40.0)


# ------------------------------------------------------------------ hard gates

class TestGates(Base):
    def test_paper_buy_books_position(self):
        b = self.paper_buy()
        self.assertIn("XBTUSDC", b["pos"])
        g = b["guards"]["XBTUSDC"]
        self.assertAlmostEqual(g["stop"], 97.0, places=6)
        self.assertAlmostEqual(g["take"], 106.0, places=6)
        self.assertLess(b["cash"], 40.0)
        self.assertEqual(b["trades"], 1)

    def test_fee_and_reward_risk_gate(self):
        self.assertIn("fee gate", self.ex({**BUY, "take_pct": 2.5, "stop_pct": 1.5}))
        weak = {**BUY, "take_pct": 5, "stop_pct": 3}                                         # 3.8 net win vs 4.2 net loss
        r = self.ex(weak)                                      # free mind: allowed, with a warning
        self.assertTrue(r.startswith("paper") and "warning: net win 3.8%" in r, r)
        self.edit(lambda b: b.pop("pending"))
        os.environ["RR_GATE"] = "1"                            # owner switch: the warning becomes a refusal
        self.assertIn("reward/risk gate", self.ex(weak))
        os.environ.pop("RR_GATE")
        self.assertTrue(self.ex({**BUY, "take_pct": 5.4, "stop_pct": 3}).startswith("paper"))

    def test_size_caps(self):
        self.ex({**BUY, "usdc": 1000})
        pd = self.bot()["pending"]
        spent = pd["vol"] * pd["price"] * (1 + shachar.FEE_MAKER)
        self.assertLessEqual(spent, 40.0 * policy.MAX_TRADE_FRAC + 1e-6)
        self.assertLessEqual(spent, 40.0 * shachar.MAX_RISK_PCT / (3 + shachar.FEE_RT_PCT) + 1e-6)

    def test_halt_cooldown_stale_holding_liquidity(self):
        os.environ["SHACHAR_HALT"] = "1"
        self.assertIn("halt", self.ex(BUY))
        os.environ.pop("SHACHAR_HALT")
        self.assertIn("old", self.ex(BUY, tk_age=60))
        self.edit(lambda b: b["cool"].update(XBTUSDC=time.time()))
        self.assertIn("cooldown", self.ex(BUY))
        self.edit(lambda b: b["cool"].clear())
        self.assertIn("not liquid", self.ex(BUY, TK(XBTUSDC={"vol24": 10.0})))
        self.assertIn("not liquid", self.ex(BUY, TK(spread=0.005)))
        self.paper_buy()
        self.assertIn("already holding", self.ex(BUY))

    def test_one_working_entry_and_max_positions(self):
        self.ex(BUY)
        self.assertIn("already working", self.ex({**BUY, "pair": "ETHUSDC"}))
        self.edit(lambda b: (b.pop("pending"), b["pos"].update(ETHUSDC=0.01, SOLUSDC=0.1)))
        self.assertIn("positions are open", self.ex(BUY))

    def test_remote_halt_blocks_buys(self):
        notify._flags.update(configured=True, halt=True, ok_t=time.time())
        self.assertIn("halt", self.ex(BUY))

    def test_drawdown_breaker_writes_halt(self):
        self.edit(lambda b: b.update(peak=100.0))
        r = self.ex(BUY)
        self.assertIn("drawdown", r)
        self.assertTrue(store.halted())
        self.assertTrue(any("drawdown" in a for a in self.alerts))

    def test_replay_gate(self):
        bad = {"n": 12, "win_pct": 20, "avg_net_pct": -1.4}
        r = self.ex(BUY, replay=bad)                           # free mind: allowed, with a warning
        self.assertTrue(r.startswith("paper") and "warning: this setup lost" in r, r)
        self.edit(lambda b: b.pop("pending"))
        os.environ["REPLAY_GATE"] = "1"                        # owner switch
        self.assertIn("replay gate", self.ex(BUY, replay=bad))
        self.assertTrue(self.ex(BUY, replay={"n": 4, "win_pct": 0, "avg_net_pct": -3}).startswith("paper"))   # too few cases
        self.edit(lambda b: b.pop("pending"))
        os.environ["REPLAY_GATE"] = "0"
        self.assertTrue(self.ex(BUY, replay=bad).startswith("paper"))

    def test_min_hold_cannot_be_talked_around(self):
        self.paper_buy()
        sell = {"action": "sell", "pair": "XBTUSDC", "fraction": 1, "reason": "guard: stop hit"}      # the brain imitates the guard
        self.assertIn("minimum hold", self.ex(sell))
        self.assertIn("XBTUSDC", self.bot()["pos"])
        self.assertTrue(self.ex(sell, risk_event=True).startswith("sell"))

    def test_guard_source_may_sell_at_once(self):
        self.paper_buy()
        self.assertTrue(self.ex({"action": "sell", "pair": "XBTUSDC"}, source="guard").startswith("sell"))
        b = self.bot()
        self.assertNotIn("XBTUSDC", b["pos"])
        self.assertIn("XBTUSDC", b["cool"])
        self.assertEqual(b["losses"], 1)                       # sold at the entry price: fees make it a loss

    def test_no_short_and_no_dust_left(self):
        self.assertIn("nothing to sell", self.ex({"action": "sell", "pair": "XBTUSDC"}, source="guard"))
        self.paper_buy()
        r = self.ex({"action": "sell", "pair": "XBTUSDC", "fraction": 0.9995}, source="guard")
        self.assertTrue(r.startswith("sell"), r)
        self.assertNotIn("XBTUSDC", self.bot()["pos"])        # a remainder below the minimum is sold with the rest

    def test_trade_caps_count_fills_not_attempts(self):
        for _ in range(20):
            self.assertIn("fee gate", self.ex({**BUY, "take_pct": 2.5}))       # refused attempts do not count
        self.edit(lambda b: b.update(trade_ts=[time.time()] * policy.MAX_TRADES_PER_DAY))
        self.assertIn("daily trade cap", self.ex(BUY))
        self.edit(lambda b: b["pos"].update(XBTUSDC=0.05))
        self.assertTrue(self.ex({"action": "sell", "pair": "XBTUSDC"}, source="guard").startswith("sell"))   # guard exit ignores the cap

    def test_never_negative_fuzz(self):
        rnd = random.Random(3)
        for i in range(300):
            px = 100 * (1 + rnd.uniform(-0.05, 0.05))
            tk = TK(px)
            if rnd.random() < 0.5:
                self.ex({"action": "buy", "pair": rnd.choice(list(INFO)), "usdc": rnd.uniform(1, 500),
                         "stop_pct": rnd.uniform(1, 12), "take_pct": rnd.uniform(3, 25)}, tk)
            else:
                self.ex({"action": "sell", "pair": rnd.choice(list(INFO)), "fraction": rnd.uniform(0, 5)}, tk,
                        source=rnd.choice(["brain", "guard"]))
            shachar.fill_pending(TK(px * rnd.choice([0.99, 1.0, 1.01])), "dry")
            self.edit(lambda b: (b["cool"].clear(), b.update(trade_ts=[])))
            b = self.bot()
            self.assertGreaterEqual(b["cash"], 0)
            self.assertTrue(all(q > 0 for q in b["pos"].values()))
            self.assertLessEqual(len(b["pos"]), shachar.MAX_POSITIONS)


class TestPaperFills(Base):
    def test_no_fill_without_a_trade_through(self):
        self.ex(BUY)
        self.assertEqual(shachar.fill_pending(TK(), "dry"), [])                   # same prices: still working
        self.assertEqual(shachar.fill_pending(TK(101.0), "dry"), [])              # price ran away: no fill
        self.assertIn("pending", self.bot())

    def test_fills_when_ask_reaches_our_bid(self):
        self.ex(BUY)
        out = shachar.fill_pending(TK(XBTUSDC={"bid": 99.9, "ask": 100.0}), "dry")
        self.assertTrue(out[0][1].startswith("buy"), out)
        self.assertNotIn("pending", self.bot())

    def test_expires(self):
        self.ex(BUY)
        self.edit(lambda b: b["pending"].update(t=time.time() - shachar.MAKER_WAIT_S - 1))
        out = shachar.fill_pending(TK(), "dry")
        self.assertIn("not filled", out[0][1])
        b = self.bot()
        self.assertEqual((b["cash"], b["pos"], b["trades"]), (40.0, {}, 0))


# ------------------------------------------------------------------ live order flow (bugs 2, 3, 4, 6)

class TestLive(Base):
    mode = "live"

    def buy(self):
        r = self.ex(BUY)
        self.assertTrue(r.startswith("buy"), r)
        return self.bot()

    def test_buy_arms_server_stop_and_releases_state_lock(self):
        b = self.buy()
        g = b["guards"]["XBTUSDC"]
        self.assertEqual(g["stop_txid"], "S1")
        self.assertAlmostEqual(g["server_stop"], 97.0, places=6)
        self.assertAlmostEqual(self.k.orders["S1"]["vol"], b["pos"]["XBTUSDC"], places=8)
        self.assertTrue(self.k.lock_free_during_order)          # the guard is not frozen while the maker order waits

    def test_unfilled_maker_changes_nothing(self):
        self.k.fill_frac = 0.0
        self.assertIn("not filled", self.ex(BUY))
        b = self.bot()
        self.assertEqual((b["cash"], b["pos"], b["trades"]), (40.0, {}, 0))

    def test_partial_fill_is_booked_exactly(self):
        self.k.fill_frac = 0.4
        b = self.buy()
        self.assertAlmostEqual(b["pos"]["XBTUSDC"], self.k.calls[0][2] * 0.4, places=8)
        self.assertAlmostEqual(self.k.orders["S1"]["vol"], b["pos"]["XBTUSDC"], places=8)

    def test_ambiguous_entry_halts(self):
        self.k.maker_exc = kraken.AmbiguousOrder("cannot confirm cancel")
        self.assertIn("HALT", self.ex(BUY))
        self.assertTrue(store.halted())
        self.assertEqual(self.bot()["cash"], 40.0)

    def test_refused_entry_changes_nothing(self):
        self.k.maker_exc = kraken.KrakenError("kraken: ['EOrder:Insufficient funds']")
        self.assertIn("nothing changed", self.ex(BUY))
        self.assertFalse(store.halted())

    def test_stop_cancelled_before_market_sell(self):
        self.buy()
        self.k.calls.clear()
        r = self.ex({"action": "sell", "pair": "XBTUSDC"}, source="guard")
        self.assertTrue(r.startswith("sell"), r)
        self.assertEqual([c[0] for c in self.k.calls], ["cancel", "sell"])
        b = self.bot()
        self.assertEqual(b["pos"], {})
        self.assertGreater(b["cash"], 39.0)

    def test_stop_that_already_fired_is_not_sold_twice(self):
        self.buy()
        self.k.fire("S1", 97.0)
        self.k.calls.clear()
        r = self.ex({"action": "sell", "pair": "XBTUSDC"}, source="guard")
        self.assertTrue(r.startswith("sell"), r)
        self.assertNotIn("sell", [c[0] for c in self.k.calls])
        self.assertEqual(self.bot()["pos"], {})

    def test_failed_sell_keeps_position_and_rearms_stop(self):
        b0 = self.buy()
        self.k.sell_exc = kraken.KrakenError("kraken: ['EService:Unavailable']")
        r = self.ex({"action": "sell", "pair": "XBTUSDC"}, source="guard")
        self.assertIn("exit failed", r)
        b = self.bot()
        self.assertAlmostEqual(b["pos"]["XBTUSDC"], b0["pos"]["XBTUSDC"])
        self.assertEqual(b["guards"]["XBTUSDC"]["stop_txid"], "S2")
        self.assertEqual(self.k.orders["S2"]["status"], "open")

    def test_partial_sell_rearms_stop_for_the_rest(self):
        b0 = self.buy()
        r = self.ex({"action": "sell", "pair": "XBTUSDC", "fraction": 0.5}, source="guard")
        self.assertTrue(r.startswith("sell"), r)
        b = self.bot()
        self.assertAlmostEqual(b["pos"]["XBTUSDC"], b0["pos"]["XBTUSDC"] / 2, places=7)
        self.assertEqual(b["guards"]["XBTUSDC"]["stop_txid"], "S2")
        self.assertAlmostEqual(self.k.orders["S2"]["vol"], b["pos"]["XBTUSDC"], places=7)

    def test_live_token_off_means_validate_only(self):
        os.environ.pop("SHACHAR_LIVE")
        self.assertIn("validated only", self.ex(BUY))
        self.assertEqual(self.bot()["cash"], 40.0)

    def test_sync_stops_mirrors_a_fired_stop(self):
        self.buy()
        self.k.fire("S1", 97.0)
        out = shachar.sync_stops(self.k, "live")
        self.assertTrue(out and out[0][2].startswith("sell"), out)
        b = self.bot()
        self.assertEqual((b["pos"], b["guards"]), ({}, {}))
        self.assertEqual(b["losses"], 1)
        self.assertTrue(any("stop fired" in a for a in self.alerts))

    def test_sync_stops_rearms_a_vanished_stop(self):
        self.buy()
        self.k.orders["S1"]["status"] = "canceled"
        shachar.sync_stops(self.k, "live")
        self.assertEqual(self.bot()["guards"]["XBTUSDC"]["stop_txid"], "S2")
        self.edit(lambda b: b["guards"]["XBTUSDC"].update(stop_txid=None))
        shachar.sync_stops(self.k, "live")
        self.assertEqual(self.bot()["guards"]["XBTUSDC"]["stop_txid"], "S3")

    def test_trailing_stop_moves_the_kraken_order(self):
        self.buy()
        shachar.sync_stops(self.k, "live")
        self.assertEqual(self.bot()["guards"]["XBTUSDC"]["stop_txid"], "S1")       # nothing to do
        self.edit(lambda b: b["guards"]["XBTUSDC"].update(stop=102.0))
        shachar.sync_stops(self.k, "live")
        g = self.bot()["guards"]["XBTUSDC"]
        self.assertEqual((g["stop_txid"], g["server_stop"]), ("S2", 102.0))
        self.assertEqual(self.k.orders["S1"]["status"], "canceled")
        self.assertEqual(self.k.orders["S2"]["price"], 102.0)

    def test_stop_order_failure_alerts_and_is_retried(self):
        self.k.stop_exc = kraken.KrakenError("kraken: ['EOrder:Invalid price']")
        b = self.buy()
        self.assertIsNone(b["guards"]["XBTUSDC"]["stop_txid"])
        self.assertTrue(any("no server-side stop" in a for a in self.alerts))
        self.k.stop_exc = None
        shachar.sync_stops(self.k, "live")
        self.assertEqual(self.bot()["guards"]["XBTUSDC"]["stop_txid"], "S1")

    def test_reconcile_catches_a_lost_position_behind_an_equal_total(self):
        self.k.bal = {"USDC": 40.0}
        ok, _ = shachar.reconcile(self.k, "live", TK())
        self.assertTrue(ok and not store.halted())
        self.k.bal = {"USDC": 30.0, "XXBT": 0.1}                # same equity (30 + 0.1*100), different holdings
        ok, problems = shachar.reconcile(self.k, "live", TK())
        self.assertFalse(ok)
        self.assertEqual(len(problems), 2)
        self.assertTrue(store.halted())

    def test_boot_sync_cancels_unknown_orders(self):
        self.k.orders["GHOST"] = {"status": "open", "vol": 0.1, "vol_exec": 0.0, "cost": 0.0, "fee": 0.0, "price": 100.0}
        shachar.boot_sync(self.k, "live", TK())
        self.assertEqual(self.k.orders["GHOST"]["status"], "canceled")

    def test_death_sells_before_dying(self):
        self.edit(lambda b: b.update(cash=0.5, pos={"XBTUSDC": 0.02},
                                     guards={"XBTUSDC": {"entry": 100, "basis": 100, "stop": 97, "take": 106, "high": 100,
                                                         "stop_txid": None, "server_stop": None, "t": time.time()}}))
        shachar._bot_cycle(self.k, "t", "live", False, TK(), {}, {}, {}, None, None)
        b = self.bot()
        self.assertFalse(b["alive"])
        self.assertEqual(b["pos"], {})
        self.assertAlmostEqual(b["cash"], 0.5 + 2.0 * 0.992, places=6)

    def test_idle_strikes_are_not_fatal_with_real_money(self):
        self.edit(lambda b: b.update(idle_strikes=2, last_action=time.time() - 25 * 3600))
        shachar._bot_cycle(self.k, "t", "live", False, TK(), {}, {}, {}, None, None)
        b = self.bot()
        self.assertEqual(b["idle_strikes"], 3)
        self.assertTrue(b["alive"])


class TestIdleDry(Base):
    def test_idle_death_in_the_paper_game(self):
        self.edit(lambda b: b.update(idle_strikes=2, last_action=time.time() - 25 * 3600))
        shachar._bot_cycle(self.k, "t", "dry", False, TK(), {}, {}, {}, None, None)
        self.assertFalse(self.bot()["alive"])

    def test_no_key_means_hold(self):
        shachar._bot_cycle(self.k, "t", "dry", False, TK(), {}, {}, {}, None, None)
        b = self.bot()
        self.assertEqual((b["trades"], b["cash"]), (0, 40.0))
        self.assertGreater(b.get("last_brain_err", 0), 0)       # an error does not use up the 40-minute window
        self.assertEqual(b["recent"][-1]["result"], "no trade")   # the brain can read what happened to its proposals
        self.assertEqual(b["last_brain"], 0)


# ------------------------------------------------------------------ the guard loop

class TestGuard(Base):
    def setUp(self):
        super().setUp()
        self.paper_buy()

    def test_take_profit(self):
        acts = guard.protect(self.k, TK(106.5), "dry", False)
        self.assertTrue(acts and acts[0][3].startswith("sell"), acts)
        self.assertEqual(self.bot()["pos"], {})

    def test_stop_needs_two_ticks(self):
        self.assertEqual(guard.protect(self.k, TK(96.0), "dry", False), [])
        self.assertEqual(guard.protect(self.k, TK(100.0), "dry", False), [])     # recovered: counter resets
        self.assertEqual(guard.protect(self.k, TK(96.0), "dry", False), [])
        acts = guard.protect(self.k, TK(96.0), "dry", False)
        self.assertIn("stop hit", acts[0][2])

    def test_trailing_stop_ratchets(self):
        guard.protect(self.k, TK(104.5), "dry", False)
        g = self.bot()["guards"]["XBTUSDC"]
        self.assertAlmostEqual(g["stop"], 104.5 * 0.98, places=6)
        guard.protect(self.k, TK(103.0), "dry", False)
        self.assertAlmostEqual(self.bot()["guards"]["XBTUSDC"]["stop"], 104.5 * 0.98, places=6)   # never moves down

    def test_time_stop(self):
        self.edit(lambda b: b["guards"]["XBTUSDC"].update(t=time.time() - 49 * 3600))
        self.assertIn("time stop", guard.protect(self.k, TK(), "dry", False)[0][2])

    def test_owner_flatten(self):
        notify._flags.update(configured=True, flatten=True, ok_t=time.time())
        self.assertIn("FLATTEN", guard.protect(self.k, TK(), "dry", False)[0][2])
        self.assertEqual(self.bot()["pos"], {})

    def test_dead_bot_positions_are_closed(self):
        self.edit(lambda b: b.update(alive=False))
        acts = guard.protect(self.k, TK(), "dry", False)
        self.assertTrue(acts[0][3].startswith("sell"), acts)

    def test_hard_equity_stop_halts(self):
        self.edit(lambda b: b.update(start=200.0))
        acts = guard.protect(self.k, TK(), "dry", False)
        self.assertIn("hard equity stop", acts[0][2])
        self.assertTrue(store.halted())


# ------------------------------------------------------------------ market data, replay, scanner

def rows_from(closes, wig=0.0):
    return [{"t": i * 3600, "o": c, "h": c * (1 + wig), "l": c * (1 - wig), "c": c} for i, c in enumerate(closes)]


class TestMarket(Base):
    def test_24h_change_comes_from_candles_not_from_the_ticker_open(self):
        k = FakeK()
        k.ohlc = lambda p, interval=60: rows_from([100.0] * 30 + [110.0])
        mk, candles = shachar.market(k, TK(110.0, XBTUSDC={"open": 109.9}))       # ticker says +0.1% "today"
        self.assertEqual(mk["XBTUSDC"]["chg_24h_pct"], 10.0)
        self.assertEqual(mk["XBTUSDC"]["chg_6h_pct"], 10.0)
        self.assertEqual(len(candles["XBTUSDC"]), 31)

    def test_missing_data_is_never_a_confirmation(self):
        mk = {"XBTUSDC": {"chg_6h_pct": 4.0, "chg_24h_pct": 2.0}}
        self.assertEqual(shachar.opportunities(mk, None, TK()), [])
        self.assertEqual(shachar.opportunities(mk, {"pairs": {"XBTUSDC": {"chg_15m_pct": 0.2, "chg_5m_pct": 0.1}}}, TK()), [])
        full = {"pairs": {"XBTUSDC": {"chg_1h_pct": 0.8, "chg_15m_pct": 0.2, "chg_5m_pct": 0.1}}}
        ops = shachar.opportunities(mk, full, TK())
        self.assertEqual((len(ops), ops[0]["setup"]), (1, "breakout"))
        self.assertEqual(shachar.opportunities(mk, full, TK(XBTUSDC={"vol24": 1.0})), [])   # not liquid

    def test_sim_trade_is_conservative(self):
        rows = rows_from([100, 100, 100])
        rows[1].update(h=106, l=96)                           # touches both levels in one candle: the stop wins
        net, j = shachar.sim_trade(rows, 0)
        self.assertAlmostEqual(net, -3 - shachar.FEE_RT_PCT)
        rows[1].update(o=94, h=95, l=93)                      # gap below the stop: exit at the open
        self.assertAlmostEqual(shachar.sim_trade(rows, 0)[0], -6 - shachar.FEE_RT_PCT)
        rows[1].update(o=100, h=105.5, l=99)
        self.assertAlmostEqual(shachar.sim_trade(rows, 0)[0], 5 - shachar.FEE_RT_PCT)

    def test_backtest_shapes(self):
        self.assertIsNone(shachar.backtest_setup(rows_from([100.0] * 300), "breakout"))
        up = rows_from([100 * (1.004 ** i) for i in range(300)], wig=0.001)
        r = shachar.backtest_setup(up, "trend")
        self.assertTrue(r and r["n"] >= 3 and r["avg_net_pct"] > 0, r)

    def test_scanner_seed_gives_an_hour_of_history(self):
        k = FakeK()
        now = int(time.time())
        k.ohlc = lambda p, interval=1: [{"t": now - 60 * (80 - i), "o": 100, "h": 100, "l": 100, "c": 100 + i * 0.01} for i in range(80)]
        k.tickers = lambda: TK(101.0)
        sc = scanner.Scanner(k)
        self.assertEqual(sc.seed(), 3)
        sc.tick()
        s = sc.summary()
        self.assertGreaterEqual(s["history_seconds"], 3600)
        self.assertIn("chg_1h_pct", s["pairs"]["XBTUSDC"])


# ------------------------------------------------------------------ the Kraken connector (bugs 4, 7)

class KStub(kraken.Kraken):
    def __init__(self, script):
        super().__init__(key="k", secret="cw==")
        self.info = dict(INFO)
        self.script, self.calls, self.sent = script, [], []

    def _priv(self, path, data):
        name = path.rsplit("/", 1)[1]
        self.calls.append(name)
        self.sent.append((name, dict(data)))
        r = self.script(name, data, self)
        if isinstance(r, Exception):
            raise r
        return r


class TestKraken(Base):
    mode = "live"

    def setUp(self):
        super().setUp()
        p = mock.patch("time.sleep", lambda s: None)
        p.start(); self.addCleanup(p.stop)

    def test_assert_spot_allow_list(self):
        ok = {"pair": "XBTUSDC", "type": "sell", "ordertype": "market", "volume": "0.001"}
        kraken.assert_spot(ok)
        kraken.assert_spot({"pair": "XBTUSDC", "type": "buy", "ordertype": "limit", "oflags": "post", "price": "99.5", "volume": "1"})
        kraken.assert_spot({"pair": "XBTUSDC", "type": "sell", "ordertype": "stop-loss", "price": "90", "volume": "1"})
        for bad in (dict(ok, leverage="2:1"), dict(ok, leverage="none"), dict(ok, oflags="fciq"), dict(ok, close="x"),
                    {**ok, "close[ordertype]": "limit"}, dict(ok, reduce_only="true"), dict(ok, type="short"),
                    dict(ok, pair="XBTUSD"), dict(ok, pair="PEPEUSDC"), dict(ok, volume="0"), dict(ok, volume="-1"),
                    dict(ok, volume="nan"), dict(ok, ordertype="limit"), dict(ok, ordertype="limit", price="99"),
                    dict(ok, ordertype="limit", oflags="post"), dict(ok, ordertype="limit", oflags="post", price="99"),
                    dict(ok, ordertype="stop-loss", type="buy", price="90"), dict(ok, ordertype="trailing-stop", price="+2%"),
                    dict(ok, ordertype="settle-position"), dict(ok, price="100")):
            with self.assertRaises(ValueError, msg=bad):
                kraken.assert_spot(bad)

    def test_nonce_is_strictly_increasing_across_threads(self):
        got = []
        ts = [threading.Thread(target=lambda: got.extend(int(kraken.next_nonce()) for _ in range(300))) for _ in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(set(got)), len(got))

    def test_maker_always_cancels_even_when_polling_fails(self):
        def script(name, data, k):
            if name == "AddOrder":
                return {"descr": {"order": "x"}, "txid": ["T1"]}
            if name == "QueryOrders":
                return TimeoutError("timeout")
            return {}
        k = KStub(script)
        with self.assertRaises(kraken.AmbiguousOrder):
            k.order_maker("XBTUSDC", "buy", 0.001, 100.0, wait=0.05)
        self.assertIn("CancelOrder", k.calls)

    def test_maker_unfilled_and_filled(self):
        state = {"status": "open", "exec": "0"}

        def script(name, data, k):
            if name == "AddOrder":
                return {"descr": {"order": "x"}, "txid": ["T1"]}
            if name == "CancelOrder":
                if state["status"] == "open":
                    state["status"] = "canceled"
                return {"count": 1}
            return {"T1": {"status": state["status"], "vol": "0.001", "vol_exec": state["exec"], "cost": "0.1", "fee": "0.0004"}}
        k = KStub(script)
        r = k.order_maker("XBTUSDC", "buy", 0.001, 100.0, wait=0.05)
        self.assertEqual((r["vol"], r["status"]), (0.0, "canceled"))
        self.assertEqual(k.sent[0][1]["oflags"], "post")
        state.update(status="closed", exec="0.001")
        r = KStub(script).order_maker("XBTUSDC", "buy", 0.001, 100.0, wait=0.05)
        self.assertEqual((r["vol"], r["status"]), (0.001, "closed"))

    def test_lost_reply_is_recovered_by_userref(self):
        def script(name, data, k):
            if name == "AddOrder":
                return TimeoutError("read timed out")
            if name == "CancelOrder":
                return kraken.KrakenError("kraken: ['EOrder:Unknown order']")
            if name == "OpenOrders":
                return {"open": {}}
            if name == "ClosedOrders":
                return {"closed": {"T9": {"vol_exec": "0.0005", "cost": "0.05", "fee": "0.0002", "status": "canceled"}}}
        k = KStub(script)
        r = k.order_maker("XBTUSDC", "buy", 0.001, 100.0, wait=0.05)
        self.assertEqual((r["vol"], r["status"]), (0.0005, "recovered"))
        ref = k.sent[0][1]["userref"]
        self.assertTrue(all(d.get("txid", d.get("userref")) == ref for n, d in k.sent[1:]))

    def test_kraken_refusal_is_not_treated_as_ambiguous(self):
        k = KStub(lambda name, data, k: kraken.KrakenError("kraken: ['EOrder:Insufficient funds']"))
        with self.assertRaises(kraken.KrakenError):
            k.order("XBTUSDC", "sell", 0.001)
        self.assertEqual(k.calls, ["AddOrder"])

    def test_without_live_token_every_order_is_validate_only(self):
        os.environ.pop("SHACHAR_LIVE")
        k = KStub(lambda name, data, k: {"descr": {"order": "x"}})
        k.order("XBTUSDC", "sell", 0.001); k.order_maker("XBTUSDC", "buy", 0.001, 100.0); k.stop_order("XBTUSDC", 0.001, 90.0)
        self.assertTrue(all(d.get("validate") == "true" for n, d in k.sent))
        self.assertEqual(k.calls, ["AddOrder"] * 3)

    def test_floor_to_never_rounds_up(self):
        self.assertEqual(kraken.floor_to(0.123456789, 8), 0.12345678)
        self.assertEqual(kraken.floor_to(1.0, 8), 1.0)
        self.assertEqual(kraken.floor_to(0.2999, 2), 0.29)


# ------------------------------------------------------------------ brain, controls, storage

class TestBrain(Base):
    def test_no_key_holds(self):
        d = brain.decide({"market": {"XBTUSDC": {"chg_6h_pct": 9.0}}, "me": {"positions": {}, "cash": 40}})
        self.assertEqual((d["action"], d["source"]), ("hold", "nokey"))

    def test_extract_json(self):
        self.assertEqual(brain.extract_json('thinking {not json} then {"action":"hold","reason":"a } brace"} bye')["action"], "hold")
        self.assertEqual(brain.extract_json('```json\n{"action":"buy","x":{"y":1}}\n```')["x"], {"y": 1})
        self.assertIsNone(brain.extract_json("no json here"))
        self.assertIsNone(brain.extract_json(None))

    def test_bad_answer_falls_through_to_the_next_model(self):
        os.environ["LLM_KEY"] = "x"
        replies = iter(["I think we should wait.", json.dumps({"action": "hold", "reason": "ok"})])

        class R:
            def __init__(self, txt): self.txt = txt
            def read(self): return json.dumps({"choices": [{"message": {"content": self.txt}}]}).encode()
        with mock.patch.object(brain, "LLM_MODELS", ["m1", "m2"]), \
                mock.patch("urllib.request.urlopen", lambda req, timeout=0: R(next(replies))):
            d = brain.ask_llm({"x": 1})
        self.assertEqual(d["source"], "llm:m2")


class TestControls(Base):
    def test_only_the_owner_can_halt_or_flatten(self):
        f = notify.parse_flags([{"title": "FLATTEN now", "author_association": "NONE"},
                                {"title": "halt please", "author_association": "CONTRIBUTOR"},
                                {"title": "[shachar] HALT: something", "author_association": "NONE"},
                                {"title": "HALT", "author_association": "OWNER", "pull_request": {}}])
        self.assertEqual(f, {"halt": False, "flatten": False, "resume": []})
        self.assertEqual(notify.parse_flags([{"title": " halt for the weekend", "author_association": "OWNER"}]),
                         {"halt": True, "flatten": False, "resume": []})
        own = lambda t, n=1: {"title": t, "author_association": "OWNER", "number": n}
        self.assertEqual(notify.parse_flags([own("FLATTEN")]), {"halt": True, "flatten": True, "resume": []})
        self.assertEqual(notify.parse_flags([own("resume", 7)])["resume"], [7])
        self.assertEqual(notify.parse_flags([own("resume", 7), own("HALT", 8)])["resume"], [])     # a halt wins over a resume
        self.assertEqual(notify.parse_flags([{"title": "RESUME", "author_association": "NONE", "number": 9}])["resume"], [])

    def test_resume_lifts_a_self_halt_and_restarts_the_peak(self):
        self.edit(lambda b: b.update(peak=100.0))
        self.assertIn("drawdown", self.ex(BUY))
        self.assertTrue(store.halted())
        with mock.patch.object(notify, "_gh", return_value={}) as gh:
            notify._resume("o/r", [7])
        self.assertFalse(store.halted())
        self.assertIsNone(self.bot()["peak"])
        self.assertEqual([c.args[0] for c in gh.call_args_list], ["POST", "PATCH"])
        self.assertTrue(self.ex(BUY).startswith("paper"))
        self.assertEqual(self.bot()["peak"], 40.0)

    def test_live_fails_closed_when_controls_are_unreachable(self):
        notify._flags.update(configured=True, halt=False, flatten=False, ok_t=time.time() - notify.STALE_S - 5)
        self.assertTrue(notify.remote_halt("live"))
        self.assertFalse(notify.remote_halt("dry"))
        notify._flags.update(ok_t=time.time())
        self.assertFalse(notify.remote_halt("live"))


class TestStore(Base):
    def test_old_state_files_are_upgraded(self):
        old = {"mode": "dry", "bots": {"shachar": {"name": "shachar", "alive": True, "cash": 30.0, "start": 47.87,
                                                   "pos": {"XBTUSDC": 0.1}, "parent": None, "children": [], "born": 1.0,
                                                   "fees": 0.2, "last_action": 1.0, "idle_strikes": 0, "trades": 3}}}
        store.sfile("dry").write_text(json.dumps(old), encoding="utf-8")
        b = store.load("dry")["bots"]["shachar"]
        self.assertEqual((b["peak"], b["dust"], b["trade_ts"], b["cash"]), (47.87, {}, [], 30.0))
        guard.protect(FakeK(), TK(), "dry", False)             # a position without a guard record gets one
        self.assertIn("XBTUSDC", store.load("dry")["bots"]["shachar"]["guards"])

    def test_atomic_save_leaves_no_temp_file(self):
        store.save("dry", store.load("dry"))
        self.assertEqual([f.name for f in store.DATA.iterdir() if f.name.endswith(".tmp")], [])

    def test_policy_non_trade_limits(self):
        b = store.newbot("p", 40.0)
        self.assertFalse(policy.check(b, 30, "service", "kraken", "x")[0])           # over half the balance
        self.assertFalse(policy.check(b, 5, "service", "someone", "x")[0])           # payee not approved
        self.assertFalse(policy.check(b, 5, "gambling", "kraken", "x")[0])
        self.assertTrue(policy.check(b, 5, "service", "kraken", "x")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
