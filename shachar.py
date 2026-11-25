"""Shachar: one decision cycle for every living bot in the family.
  python shachar.py cycle dry        simulated fills at live prices, no Kraken key needed
  python shachar.py cycle dry --validate   also sends each order to Kraken with validate=true (no order is placed)
  python shachar.py cycle live       real orders. Refuses unless SHACHAR_LIVE=YES_IDAN_APPROVED
  python shachar.py status [dry|live]
Survival: equity < DEATH_FLOOR = dead. 24h without a trade = idle strike, 3 strikes = dead.
Reproduction: equity >= 2x start and enough free cash = child funded with half of the profit."""
import os, json, sys, time, pathlib, threading
import kraken, news, brain, policy

HERE = pathlib.Path(__file__).parent
FEE = 0.008               # market order fee per side (Kraken Pro entry tier taker, pessimistic)
DEATH_FLOOR = 3.0         # USDC. Below this no Kraken order is possible, so the bot is dead
IDLE_HOURS, MAX_IDLE = 24, 3
SPAWN_AT, CHILD_STAKE, MIN_CHILD, MAX_CHILDREN = 2.0, 0.5, 6.0, 3
DRY_START = 47.87
LOCK = threading.RLock()      # guard thread and brain thread share the state file


def sfile(mode):
    return HERE / ("state_%s.json" % mode)


def load(mode, start):
    f = sfile(mode)
    if f.exists():
        return json.loads(f.read_text())
    return {"mode": mode, "bots": {"shachar": newbot("shachar", start, None)}, "log": []}


def newbot(name, cash, parent):
    return {"name": name, "alive": True, "cash": cash, "start": cash, "pos": {}, "parent": parent, "children": [],
            "born": time.time(), "fees": 0.0, "last_action": time.time(), "idle_strikes": 0, "trades": 0}


def equity(b, tk):
    return b["cash"] + sum(q * tk[p]["bid"] for p, q in b["pos"].items())


ASSET = {"XXBT": "XBT", "XETH": "ETH", "XXRP": "XRP", "XXDG": "DOGE", "XLTC": "LTC", "ZUSD": "USD"}
RECON_TOL = 0.03


def reconcile(k, s, tk):
    """Live only: compare the real Kraken account with the ledger. A gap over 3% writes a HALT file
    (no new buys; guard exits still run) and logs it. Returns (ok, real_equity, ledger_equity)."""
    bal = k.balance()
    real = bal.get("USDC", 0.0)
    for p in tk:
        base = p[:-4]
        qty = sum(v for a, v in bal.items() if ASSET.get(a, a) == base)
        real += qty * tk[p]["bid"]
    led = sum(equity(b, tk) for b in s["bots"].values() if b["alive"])
    ok = led <= 0 or abs(real - led) / led <= RECON_TOL
    jlog({"event": "reconcile", "ok": ok, "kraken_equity": round(real, 2), "ledger_equity": round(led, 2)})
    if not ok:
        (HERE / "HALT").write_text("reconcile mismatch kraken %.2f vs ledger %.2f at %s\n" % (real, led, time.strftime("%F %T")))
    return ok, real, led


def jlog(rec):
    rec["t"] = time.strftime("%F %T")
    with (HERE / "journal.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def lessons_tail(n=6):
    f = HERE / "lessons.md"
    return [l.strip() for l in f.read_text().splitlines() if l.startswith("- ")][-n:] if f.exists() else []


def market(k, tk):
    out = {}
    for p, t in tk.items():
        try:
            c = k.ohlc(p)[0]
            ch6 = (c[-1] / c[-7] - 1) * 100 if len(c) >= 7 else 0.0
        except Exception:
            ch6 = 0.0
        try:
            cl = c
            rets = [cl[i] / cl[i - 1] - 1 for i in range(max(1, len(cl) - 24), len(cl))]
            m_ = sum(rets) / len(rets)
            vol1h = (sum((x - m_) ** 2 for x in rets) / len(rets)) ** 0.5 * 100
        except Exception:
            vol1h = 0.0
        out[p] = {"vol_1h_pct": round(vol1h, 3), "price": t["last"], "chg_24h_pct": round((t["last"] / t["open"] - 1) * 100, 2),
                  "chg_6h_pct": round(ch6, 2), "min_order": k.pairs()[p]["ordermin"]}
    return out


MAKER_FEE = 0.005       # post-only entry fee used in the paper ledger (pessimistic vs Kraken maker tier)
MIN_HOLD_H = 3          # brain cannot sell a fresh position (guard exits are exempt)
COOLDOWN_H = 6          # per-coin cooldown after a sell
MIN_TAKE_PCT = 3.0      # fee gate: ~1.6% round trip plus margin


MAX_SPREAD_PCT = 0.2        # liquidity filter: a buy needs spread <= 0.2%
MIN_VOL_USD = 500_000       # and 24h volume >= 500k USD (our orders are under 50 USD, so slippage is tiny)


def liquid(p, tk):
    t = tk[p]
    spread = (t["ask"] / t["bid"] - 1) * 100
    vol = t["vol24"] * t["last"]
    if spread > MAX_SPREAD_PCT:
        return False, "spread %.3f%% > %.1f%%" % (spread, MAX_SPREAD_PCT)
    if vol < MIN_VOL_USD:
        return False, "24h volume %.0f USD < %d" % (vol, MIN_VOL_USD)
    return True, ""


def backtest_setup(closes, kind, stop=0.03, take=0.05, hold=48, fee_pct=1.6):
    """Simple replay on hourly closes (about 30 days): every time this setup appeared, enter at the close, exit at
    -stop, +take or after `hold` hours, subtract round-trip fees. Closes only (no highs/lows), so it is rough.
    Returns {n, win_pct, avg_net_pct} or None when there is too little history."""
    c = closes
    nets, i = [], 24
    while i < len(c) - 2:
        c6, c24, c1 = c[i] / c[i - 6] - 1, c[i] / c[i - 24] - 1, c[i] / c[i - 1] - 1
        hit = ((kind == "breakout" and 0.03 <= c6 <= 0.08 and c1 > 0) or
               (kind == "trend" and 0.01 <= c6 < 0.05 and c24 > 0 and c1 > 0) or
               (kind == "rebound" and c24 <= -0.03 and c1 > 0))
        if not hit:
            i += 1
            continue
        px, r, j = c[i], 0.0, i + 1
        while j < min(len(c), i + 1 + hold):
            r = c[j] / px - 1
            if r <= -stop:
                r = -stop; break
            if r >= take:
                r = take; break
            j += 1
        nets.append(r * 100 - fee_pct)
        i = j + 1
    if len(nets) < 3:
        return None
    return {"n": len(nets), "win_pct": round(100 * sum(1 for x in nets if x > 0) / len(nets)), "avg_net_pct": round(sum(nets) / len(nets), 2)}


def btc_alert(mk, scan):
    """Flag a big BTC move: 1h >= 2% or 6h >= 5% (either direction). BTC leads the market."""
    c1 = ((scan or {}).get("pairs", {}).get("XBTUSDC", {}) or {}).get("chg_1h_pct")
    c6 = mk.get("XBTUSDC", {}).get("chg_6h_pct", 0)
    msgs = []
    if c1 is not None and abs(c1) >= 2:
        msgs.append("BTC %+.1f%% in 1h" % c1)
    if abs(c6) >= 5:
        msgs.append("BTC %+.1f%% in 6h" % c6)
    return msgs or "none"


def opportunities(mk, scan, tk):
    """Ranked list of liquid pairs showing a real setup. Pure rules on free Kraken data; the brain still decides.
    breakout: 6h move >= 3% and the last hour still rising (not chased past 8%).
    trend: steady climb (1h and 15m up, 6h 1-5%, 24h up).
    rebound: 24h <= -3% and the 5m and 15m have turned up.
    A setup is only listed when the target move (take-profit) can clear fees: est_move >= 3%."""
    out = []
    btc = mk.get("XBTUSDC", {})
    for p, m in mk.items():
        if p not in tk:
            continue
        ok, why = liquid(p, tk)
        if not ok:
            continue
        sc = (scan or {}).get("pairs", {}).get(p, {})
        c5, c15, c1h = sc.get("chg_5m_pct"), sc.get("chg_15m_pct"), sc.get("chg_1h_pct")
        c6, c24 = m["chg_6h_pct"], m["chg_24h_pct"]
        kind, est = None, 0.0
        if c6 >= 3 and c6 <= 8 and (c1h is None or c1h > 0):
            kind, est = "breakout", min(7.0, c6 * 1.2)
        elif 1 <= c6 < 5 and c24 > 0 and (c1h or 0) > 0 and (c15 or 0) >= 0:
            kind, est = "trend", min(5.0, 2.0 + c6)
        elif c24 <= -3 and (c5 or 0) > 0 and (c15 or 0) > 0:
            kind, est = "rebound", min(6.0, abs(c24) * 0.8)
        if kind and est >= 3.0:
            btc_ok = btc.get("chg_6h_pct", 0) > -1.5 or p == "XBTUSDC"
            score = est + (1.0 if btc_ok else -2.0)
            out.append({"pair": p, "setup": kind, "est_move_pct": round(est, 1), "btc_ok": btc_ok,
                        "score": round(score, 1), "chg_6h": c6, "chg_24h": c24, "chg_1h": c1h, "chg_15m": c15})
    return sorted(out, key=lambda r: -r["score"])


def execute(k, s, b, d, tk, mode, validate):
    """Apply one decision. Returns a short result string; trades update the ledger."""
    act = d.get("action")
    p = d.get("pair")
    if act not in ("buy", "sell") or p not in tk:
        return "no trade"
    info = k.pairs()[p]
    if act == "buy":
        usdc = min(float(d.get("usdc", 0)), b["cash"] * policy.MAX_TRADE_FRAC)
        vol = usdc * (1 - FEE) / tk[p]["ask"]
        if usdc < max(info["costmin"], 1) or vol < info["ordermin"]:
            return "refused: below Kraken minimum order (%.2f USDC, min volume %s)" % (usdc, info["ordermin"])
        if os.environ.get("SHACHAR_HALT") == "1" or (HERE / "HALT").exists():
            return "refused: halt flag is set (no new buys)"
        if time.time() - b.get("cool", {}).get(p, 0) < COOLDOWN_H * 3600:
            return "refused: %s is in cooldown after its last sell" % p
        if float(d.get("take_pct", 5)) < MIN_TAKE_PCT:
            return "refused: fee gate, take-profit must be at least %.1f%% (round trip costs ~1.6%%)" % MIN_TAKE_PCT
        lq, lwhy = liquid(p, tk)
        if not lq:
            return "refused: not liquid enough (%s)" % lwhy
        if usdc > b["cash"] * 0.99:
            return "refused: would spend more than the account balance (spot only, never negative)"
        ok, why = policy.check(b, usdc, "trade", "kraken", d.get("reason", ""), side="buy")
    else:
        q = b["pos"].get(p, 0.0) * max(0.0, min(1.0, float(d.get("fraction", 1))))
        g0 = b.get("guards", {}).get(p)
        if g0 and g0.get("t") and not d.get("reason", "").startswith("guard:") and time.time() - g0["t"] < MIN_HOLD_H * 3600:
            return "refused: minimum hold %dh not reached (guard stop and take-profit still protect)" % MIN_HOLD_H
        if q > b["pos"].get(p, 0.0) + 1e-12:
            return "refused: cannot sell more than held (no shorting)"
        if q < info["ordermin"]:
            return "refused: nothing to sell or below minimum volume"
        usdc, vol = q * tk[p]["bid"], q
        ok, why = policy.check(b, usdc, "trade", "kraken", d.get("reason", ""), side="sell")
    if not ok:
        return "blocked by policy: %s" % why
    vol = round(vol, info["lot_decimals"])
    maker = act == "buy"          # entries are post-only limit orders at the bid (maker fee); exits stay market (safety first)
    if mode == "live" and maker:
        try:
            r = k.order_maker(p, "buy", vol, tk[p]["bid"], userref=abs(hash(b["name"])) % 10**6)
        except Exception as e:
            return "maker entry failed, no trade: %s" % str(e)[:100]
        if not r["live"]:
            return "validated only (live orders off): %s" % r["descr"]
        if r["vol"] < info["ordermin"]:
            return "maker entry not filled in time, cancelled, no trade (no chasing)"
        qty, cost, fee = r["vol"], r["cost"], r["fee"]
    elif mode == "live":
        r = k.order(p, act, vol, userref=abs(hash(b["name"])) % 10**6)
        if not r["live"]:
            return "validated only (live orders off): %s" % r["descr"]
        qty, cost, fee = r["vol"], r["cost"], r["fee"]
    else:
        if validate:
            r = k.order_maker(p, "buy", vol, tk[p]["bid"]) if maker else k.order(p, act, vol)
            jlog({"bot": b["name"], "validated": r["descr"]})
        qty = vol
        cost = vol * (tk[p]["bid"] if maker else tk[p]["bid"] if act == "sell" else tk[p]["ask"])
        fee = cost * (MAKER_FEE if maker else FEE)
    gd = b.setdefault("guards", {})
    if act == "buy":
        b["cash"] -= cost + fee
        b["pos"][p] = b["pos"].get(p, 0.0) + qty
        entry = cost / qty
        sp = min(10.0, max(1.5, float(d.get("stop_pct", 3)))) / 100
        tp = min(20.0, max(2.0, float(d.get("take_pct", 5)))) / 100
        gd[p] = {"entry": entry, "stop": entry * (1 - sp), "take": entry * (1 + tp), "high": entry, "stop_txid": None, "t": time.time()}
        if mode == "live" or validate:
            try:
                so = k.stop_order(p, b["pos"][p], gd[p]["stop"])
                gd[p]["stop_txid"] = so["txid"]; jlog({"bot": b["name"], "stop_order": so["descr"], "live": so["live"]})
            except Exception as e:
                jlog({"bot": b["name"], "stop_order_error": str(e)[:150]})
    else:
        g = gd.get(p)
        if g and g.get("stop_txid") and mode == "live":
            try: k.cancel(g["stop_txid"])
            except Exception as e: jlog({"bot": b["name"], "cancel_error": str(e)[:150]})
        b["cash"] += cost - fee
        b.setdefault("cool", {})[p] = time.time()
        b["pos"][p] = b["pos"].get(p, 0.0) - qty
        if b["pos"][p] * tk[p]["bid"] < 1:
            b["pos"].pop(p); gd.pop(p, None)
        elif g:
            g["stop_txid"] = None
    b["fees"] += fee; b["trades"] += 1
    b["last_action"] = time.time(); b["idle_strikes"] = 0
    return "%s %s %.6f @ ~%.4f (fee %.3f)" % (act, p, qty, cost / qty, fee)


def cycle(mode, validate=False, scan=None):
    k = kraken.Kraken()
    if mode == "live":
        if not kraken.live_enabled():
            print("LIVE orders are off (SHACHAR_LIVE not set). Orders will be validated only, nothing is placed.")
        start = k.balance().get("USDC", 0.0)
    else:
        start = DRY_START
    s = load(mode, start)
    tk = k.tickers()
    if mode == "live" and kraken.live_enabled():
        try:
            reconcile(k, s, tk)
        except Exception as e:
            jlog({"event": "reconcile_error", "err": str(e)[:150]})
            (HERE / "HALT").write_text("reconcile failed: %s\n" % str(e)[:100])
    mk = market(k, tk)
    nw = {"headlines": news.headlines(6), "fear_greed": news.fear_greed(), "trending": news.trending()}
    for name in list(s["bots"]):
        with LOCK:
            s = load(mode, start); b = s["bots"][name]
        if not b["alive"]:
            continue
        eq = equity(b, tk)
        if time.time() - b["last_action"] > IDLE_HOURS * 3600:
            b["idle_strikes"] += 1; b["last_action"] = time.time()
            s["log"].append("%s idle strike %d" % (b["name"], b["idle_strikes"]))
            if b["idle_strikes"] >= MAX_IDLE:
                eq = 0.0
        if eq < DEATH_FLOOR:
            b["alive"] = False; b["pos"] = {}; b["cash"] = 0.0
            s["log"].append("%s DIED %s" % (b["name"], time.strftime("%F %T")))
            jlog({"bot": b["name"], "event": "died", "equity": round(eq, 2)})
            with LOCK:
                sfile(mode).write_text(json.dumps(s, indent=1))
            continue
        with LOCK:
            sfile(mode).write_text(json.dumps(s, indent=1))
        profit = eq - b["start"]
        want_child = eq >= b["start"] * SPAWN_AT and len(b["children"]) < MAX_CHILDREN
        ctx = {"time": time.strftime("%F %T"), "market": mk, "news": nw, "lessons": lessons_tail(),
               "me": {"name": b["name"], "cash": round(b["cash"], 2), "positions": {p: round(q, 6) for p, q in b["pos"].items()},
                      "equity": round(eq, 2), "start": round(b["start"], 2), "profit": round(profit, 2),
                      "idle_strikes": b["idle_strikes"], "hours_since_last_trade": round((time.time() - b["last_action"]) / 3600, 1),
                      "trades_total": b["trades"], "death_floor": DEATH_FLOOR,
                      "reproduction": "ready, free cash needed %.2f" % (profit * CHILD_STAKE) if want_child else "not yet (need 2x start)"}}
        if scan:
            ctx["scan"] = scan
        ctx["opportunities"] = opportunities(mk, scan, tk)
        ctx["btc_alert"] = btc_alert(mk, scan)
        try:
            peg = k.usdc_peg()
            ctx["usdc_peg"] = {"usdc_usd": round(peg, 4), "warning": "USDC off peg, your cash may be at risk" if abs(peg - 1) > 0.003 else "ok"}
        except Exception:
            ctx["usdc_peg"] = {"warning": "unavailable"}
        for o in ctx["opportunities"][:3]:
            try:
                o["backtest_30d"] = backtest_setup(k.ohlc_full(o["pair"]), o["setup"]) or "too few cases"
            except Exception:
                pass
        want = [o["pair"] for o in ctx["opportunities"][:3]] + [p for p in b["pos"] if p in tk]
        ctx["order_books"] = {}
        for p in dict.fromkeys(want):
            try:
                ctx["order_books"][p] = k.depth(p)
            except Exception:
                pass
        ctx["cooldown_pairs"] = [p for p, t0 in b.get("cool", {}).items() if time.time() - t0 < COOLDOWN_H * 3600]
        ctx["not_liquid"] = [p for p in mk if p in tk and not liquid(p, tk)[0]]
        d = brain.decide(ctx)            # slow (LLM); the guard thread keeps running meanwhile
        with LOCK:
            s = load(mode, start); b = s["bots"][b["name"]]
            if not b["alive"]:
                continue
            res = execute(k, s, b, d, tk, mode, validate)
        if d.get("lesson"):
            with (HERE / "lessons.md").open("a") as f:
                f.write("- %s [%s] %s\n" % (time.strftime("%F"), b["name"], str(d["lesson"])[:300]))
        if d.get("action") == "idea":
            with (HERE / "ideas_proposed.jsonl").open("a") as f:
                f.write(json.dumps({"bot": b["name"], "t": time.strftime("%F %T"), "idea": d.get("reason")}) + "\n")
        jlog({"bot": b["name"], "decision": d, "result": res, "equity": round(equity(b, tk), 2)})
        eq = equity(b, tk); profit = eq - b["start"]
        if want_child and profit * CHILD_STAKE >= MIN_CHILD and b["cash"] >= profit * CHILD_STAKE:
            stake = profit * CHILD_STAKE
            cn = "%s.%d" % (b["name"], len(b["children"]) + 1)
            b["cash"] -= stake; b["children"].append(cn); b["start"] = eq - stake
            s["bots"][cn] = newbot(cn, stake, b["name"])
            s["log"].append("%s spawned %s with %.2f USDC" % (b["name"], cn, stake))
            jlog({"bot": b["name"], "event": "spawned", "child": cn, "stake": round(stake, 2)})
        with LOCK:
            s["log"] = s["log"][-200:]
            sfile(mode).write_text(json.dumps(s, indent=1))
    with LOCK:
        s = load(mode, start)
    status(mode, s, tk)


def status(mode="dry", s=None, tk=None):
    if s is None:
        s = load(mode, DRY_START); tk = kraken.Kraken().tickers()
    for b in s["bots"].values():
        print("%-10s %-5s equity %.2f USDC (start %.2f) cash %.2f fees %.2f trades %d idle %d %s" % (
            b["name"], "alive" if b["alive"] else "DEAD", equity(b, tk), b["start"], b["cash"], b["fees"],
            b["trades"], b["idle_strikes"], list(b["pos"])))


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "cycle":
        cycle(a[1] if len(a) > 1 else "dry", "--validate" in a)
    else:
        status(a[1] if len(a) > 1 else "dry")
