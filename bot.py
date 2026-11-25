"""fifty-bot: Phase 0 paper mode. Stdlib only. No real money, no keys.
Run: python bot.py tick   (one decision step)   python bot.py status
Each bot has a ledger in state.json. Bot dies at 0. At SPAWN_AT it funds a child
with PROFIT ONLY (never the original stake). Costs are simulated conservatively.
"""
import json, sys, time, urllib.request, pathlib

STATE = pathlib.Path(__file__).with_name("state.json")
START_ILS = 180.0        # about 50 USD; set to the real deposit in Phase 1
SPAWN_AT = 2.0           # equity multiple that triggers a child
CHILD_STAKE = 0.5        # fraction of the profit above start moved to the child
FEE = 0.005              # 0.5% per side, pessimistic (Bit2C Pro fees are lower)
COINS = {"bitcoin": "BTC", "ethereum": "ETH"}
MAX_HISTORY = 400


def prices():
    """ILS spot prices. Phase 0 uses CoinGecko; Phase 1 swaps in Bit2C order book."""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=%s&vs_currencies=ils" % ",".join(COINS)
    with urllib.request.urlopen(url, timeout=20) as r:
        d = json.load(r)
    return {COINS[k]: float(v["ils"]) for k, v in d.items()}


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"bots": {"gen0-a": newbot("gen0-a", START_ILS, "momentum", None)}, "hist": {}, "log": []}


def newbot(name, cash, strat, parent):
    return {"name": name, "alive": True, "cash": cash, "start": cash, "pos": {}, "strategy": strat,
            "parent": parent, "children": [], "born": time.time(), "fees": 0.0}


def equity(b, px):
    return b["cash"] + sum(q * px[c] for c, q in b["pos"].items())


def signal(strat, hist):
    """Return coin to hold, or None for cash. Simple on purpose; the bot may evolve this."""
    best, score = None, 0.0
    for c, h in hist.items():
        if len(h) < 12:
            continue
        short, long_ = sum(h[-4:]) / 4, sum(h[-12:]) / 12
        s = (short - long_) / long_
        s = s if strat == "momentum" else -s      # "revert" = fade moves
        if s > 0.002 and s > score:
            best, score = c, s
    return best


def trade(b, px, want):
    held = next(iter(b["pos"]), None)
    if held == want:
        return
    if held:                                      # sell
        v = b["pos"].pop(held) * px[held] * (1 - FEE)
        b["fees"] += v * FEE / (1 - FEE)
        b["cash"] += v
    if want and b["cash"] > 5:                    # buy
        spend = b["cash"]
        b["pos"][want] = spend * (1 - FEE) / px[want]
        b["fees"] += spend * FEE
        b["cash"] = 0.0


def tick():
    s, px = load(), prices()
    for c, p in px.items():
        h = s["hist"].setdefault(c, []); h.append(p); del h[:-MAX_HISTORY]
    for b in list(s["bots"].values()):
        if not b["alive"]:
            continue
        trade(b, px, signal(b["strategy"], s["hist"]))
        eq = equity(b, px)
        if eq <= 0.5:                             # death rule
            b["alive"] = False; b["pos"] = {}; b["cash"] = 0.0
            s["log"].append("%s DIED at %s" % (b["name"], time.strftime("%F %T")))
        elif eq >= b["start"] * SPAWN_AT and len(b["children"]) < 3:
            profit = (eq - b["start"]) * CHILD_STAKE
            if b["cash"] < profit:                # free up cash by selling
                trade(b, px, None)
            if b["cash"] >= profit:
                b["cash"] -= profit
                cn = "%s.%d" % (b["name"], len(b["children"]) + 1)
                strat = "revert" if b["strategy"] == "momentum" else "momentum"
                s["bots"][cn] = newbot(cn, profit, strat, b["name"])
                b["children"].append(cn)
                b["start"] = eq - profit           # raise the bar so it can't spawn every tick
                s["log"].append("%s spawned %s with %.2f ILS" % (b["name"], cn, profit))
    s["log"] = s["log"][-200:]
    STATE.write_text(json.dumps(s, indent=1))
    status(s, px)


def status(s=None, px=None):
    s = s or load(); px = px or prices()
    for b in s["bots"].values():
        print("%-10s %-8s %-9s equity %.2f ILS (stake %.2f) fees %.2f %s" % (
            b["name"], "alive" if b["alive"] else "DEAD", b["strategy"],
            equity(b, px), b["start"], b["fees"], list(b["pos"])))


if __name__ == "__main__":
    {"tick": tick, "status": status}.get(sys.argv[1] if len(sys.argv) > 1 else "status", status)()
