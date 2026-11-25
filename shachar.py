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
        out[p] = {"price": t["last"], "chg_24h_pct": round((t["last"] / t["open"] - 1) * 100, 2),
                  "chg_6h_pct": round(ch6, 2), "min_order": k.pairs()[p]["ordermin"]}
    return out


COOLDOWN_H = 6          # per-coin cooldown after a sell
MIN_TAKE_PCT = 3.0      # fee gate: ~1.6% round trip plus margin


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
        if usdc > b["cash"] * 0.99:
            return "refused: would spend more than the account balance (spot only, never negative)"
        ok, why = policy.check(b, usdc, "trade", "kraken", d.get("reason", ""), side="buy")
    else:
        q = b["pos"].get(p, 0.0) * max(0.0, min(1.0, float(d.get("fraction", 1))))
        if q > b["pos"].get(p, 0.0) + 1e-12:
            return "refused: cannot sell more than held (no shorting)"
        if q < info["ordermin"]:
            return "refused: nothing to sell or below minimum volume"
        usdc, vol = q * tk[p]["bid"], q
        ok, why = policy.check(b, usdc, "trade", "kraken", d.get("reason", ""), side="sell")
    if not ok:
        return "blocked by policy: %s" % why
    vol = round(vol, info["lot_decimals"])
    if mode == "live":
        r = k.order(p, act, vol, userref=abs(hash(b["name"])) % 10**6)
        if not r["live"]:
            return "validated only (live orders off): %s" % r["descr"]
        qty, cost, fee = r["vol"], r["cost"], r["fee"]
    else:
        if validate:
            r = k.order(p, act, vol)
            jlog({"bot": b["name"], "validated": r["descr"]})
        qty = vol
        cost = vol * (tk[p]["ask"] if act == "buy" else tk[p]["bid"])
        fee = cost * FEE
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
