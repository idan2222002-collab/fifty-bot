"""Shachar: one decision cycle for every living bot in the family.
  python shachar.py cycle dry        simulated fills at live prices, no Kraken key needed
  python shachar.py cycle dry --validate   also sends each order to Kraken with validate=true (no order is placed)
  python shachar.py cycle live       real orders. Refuses unless SHACHAR_LIVE=YES_IDAN_APPROVED
  python shachar.py status [dry|live]
Survival: equity < DEATH_FLOOR = dead (positions are kept and still protected, new buys stop).
Idle time is only informational (no death). Reproduction (dry only): equity >= 2x start and enough free cash.
Concurrency: one in-memory state per mode, shared by the guard thread and the brain thread. LOCK is held only for
short state reads/writes, never during exchange I/O. INFLIGHT blocks two orders on the same pair at once."""
import os, json, sys, time, pathlib, threading, zlib, urllib.request, urllib.error, math
import kraken, news, brain, policy

HERE = pathlib.Path(__file__).parent
FEE = 0.008               # Kraken Pro tier 1 spot taker 0.80% (official fee page)
DEATH_FLOOR = 3.0         # USDC. Below this no Kraken order is possible, so the bot is dead
IDLE_HOURS = 24           # informational only
SPAWN_AT, CHILD_STAKE, MIN_CHILD, MAX_CHILDREN = 2.0, 0.5, 6.0, 3
DRY_START = 47.87
LOCK = threading.RLock()
INFLIGHT = set()
LIVE_SIZE_CAP = float(os.environ.get("SHACHAR_LIVE_SIZE_CAP", "0.5"))   # first live: one buy <= 50% of cash. Idan can lift it.
DRY_SIZE_CAP = 0.9
MIN_RR = 1.5              # take_pct / stop_pct must be at least this
MAX_RISK_PCT = float(os.environ.get("SHACHAR_MAX_RISK_PCT", "2"))   # one stop-out (fees included) costs at most this % of equity
PAPER_PENDING = False     # paper mode: True = a maker entry waits for the market to trade through it (guard.py turns
                          # it on, because only the always-on loop can watch the order). False = instant fill.


def sfile(mode):
    return HERE / ("state_%s.json" % mode)


_STATE = {}


def load(mode, start):
    """The one shared in-memory state for this mode (loaded from disk once)."""
    with LOCK:
        if mode not in _STATE:
            f = sfile(mode)
            _STATE[mode] = json.loads(f.read_text()) if f.exists() else {"mode": mode, "bots": {"shachar": newbot("shachar", start, None)}, "log": []}
        return _STATE[mode]


def save(mode, s=None):
    """Atomic write: temp file then rename, so a crash never leaves half a file."""
    with LOCK:
        s = s if s is not None else _STATE[mode]
        f = sfile(mode)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1))
        os.replace(tmp, f)


def newbot(name, cash, parent):
    return {"name": name, "alive": True, "cash": cash, "start": cash, "pos": {}, "parent": parent, "children": [],
            "born": time.time(), "fees": 0.0, "last_action": time.time(), "idle_strikes": 0, "trades": 0}


def equity(b, tk):
    return b["cash"] + sum(q * tk[p]["bid"] for p, q in b["pos"].items() if p in tk)


def jlog(rec):
    rec["t"] = time.strftime("%F %T")
    with LOCK:
        with (HERE / "journal.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")


def write_halt(why):
    try:
        (HERE / "HALT").write_text("%s at %s\n" % (why, time.strftime("%F %T")))
    except Exception:
        pass
    jlog({"event": "halt", "why": why})


_RH = {"t": 0.0, "v": False}


def remote_halt():
    """Remote kill switch: a file named HALT on the main branch of the repo stops new buys. 404 = no halt.
    Fail-open on network errors (the real kill switch is revoking the Kraken API key). The CDN may cache ~5 min."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return False
    if time.time() - _RH["t"] < 60:
        return _RH["v"]
    _RH["t"] = time.time()
    try:
        urllib.request.urlopen(urllib.request.Request("https://raw.githubusercontent.com/%s/main/HALT" % repo, headers={"User-Agent": "shachar"}), timeout=10)
        _RH["v"] = True
    except urllib.error.HTTPError as e:
        _RH["v"] = False if e.code == 404 else _RH["v"]
    except Exception:
        pass
    return _RH["v"]


def halted():
    return os.environ.get("SHACHAR_HALT") == "1" or (HERE / "HALT").exists() or remote_halt()


ASSET = {"XXBT": "XBT", "XETH": "ETH", "XXRP": "XRP", "XXDG": "XDG", "XLTC": "LTC", "ZUSD": "USD"}
RECON_TOL = 0.03


def real_holdings(bal):
    """Kraken balance -> (usdc, {base: qty})."""
    qty = {}
    for a, v in bal.items():
        base = ASSET.get(a, a)
        qty[base] = qty.get(base, 0.0) + v
    return qty.pop("USDC", 0.0), qty


def reconcile(k, s, tk):
    """Live only: compare the real Kraken account with the ledger PER ASSET (USDC and every coin quantity).
    A mismatch over 3% (and over 1 USD) writes HALT (no new buys; guard exits still run). Returns (ok, real_equity, ledger_equity)."""
    usdc, held = real_holdings(k.balance())
    real = usdc
    bad = []
    led_usdc = sum(b["cash"] for b in s["bots"].values())
    led_eq = led_usdc
    for p in tk:
        base = p[:-4]
        r = held.get(base, 0.0)
        l = sum(b["pos"].get(p, 0.0) for b in s["bots"].values())
        real += r * tk[p]["bid"]
        led_eq += l * tk[p]["bid"]
        if abs(r - l) * tk[p]["bid"] > max(1.0, RECON_TOL * max(r, l) * tk[p]["bid"]):
            bad.append("%s kraken %.6f vs ledger %.6f" % (base, r, l))
    if abs(usdc - led_usdc) > max(0.5, RECON_TOL * max(led_eq, 1.0)):
        bad.append("USDC kraken %.2f vs ledger %.2f" % (usdc, led_usdc))
    ok = not bad
    jlog({"event": "reconcile", "ok": ok, "kraken_equity": round(real, 2), "ledger_equity": round(led_eq, 2), "bad": bad})
    if not ok:
        write_halt("reconcile mismatch: " + "; ".join(bad))
    return ok, real, led_eq


def boot_sync(k, s, tk, mode="live"):
    """Run once at start in live mode: Kraken is the truth. Cancel leftover non-stop orders, rebuild cash and
    positions from the real balances, keep known guards, adopt existing stop orders, place missing stops."""
    try:
        openo = k.open_orders()
        stops = {}
        for txid, o in openo.items():
            d = o.get("descr", {})
            if d.get("ordertype") == "stop-loss" and d.get("type") == "sell":
                stops[d.get("pair")] = txid
            else:
                k.cancel(txid)
        usdc, held = real_holdings(k.balance())
        with LOCK:
            b = s["bots"].setdefault("shachar", newbot("shachar", usdc, None))
            b["cash"] = usdc
            gd = b.setdefault("guards", {})
            b["pos"] = {}
            for p in tk:
                q = held.get(p[:-4], 0.0)
                if q * tk[p]["bid"] >= 1.0:
                    b["pos"][p] = q
                    px = tk[p]["bid"]
                    g = gd.get(p) or {"entry": px, "stop": px * 0.97, "take": px * 1.05, "high": px, "t": time.time()}
                    g["stop_txid"] = stops.get(p)
                    g["stop_vol"] = q if stops.get(p) else 0
                    if stops.get(p):
                        g["stop_px"] = g["stop"]
                    gd[p] = g
            for p in list(gd):
                if p not in b["pos"]:
                    gd.pop(p)
            if b.get("start", 0) <= 0:
                # The live state is first created with a stake of 0 (guard.main loads it with 0.0 before the real
                # balance is known), so the brain was told "start 0, profit = all your money". The stake is what the
                # account holds at the first live start. A stake that is already known is never changed.
                b["start"] = equity(b, tk)
            save(mode, s)
        jlog({"event": "boot_sync", "usdc": round(usdc, 2), "positions": list(b["pos"]), "stops": list(stops),
              "start": round(b["start"], 2)})
        for p in list(b["pos"]):
            ensure_stop(k, b, p, mode, False)
        return True
    except Exception as e:
        write_halt("boot_sync failed: %s" % str(e)[:120])
        return False


def lessons_tail(n=6):
    f = HERE / "lessons.md"
    return [l.strip() for l in f.read_text().splitlines() if l.startswith("- ")][-n:] if f.exists() else []


def market(k, tk):
    """Per-pair inputs for the brain. chg_24h_pct is a real 24h change from hourly closes (None if too little history);
    Kraken's Ticker `o` is only the UTC day open, so it is not used here."""
    out = {}
    for p, t in tk.items():
        c = None
        try:
            c = k.ohlc(p)[0]
        except Exception:
            pass
        ch6 = (c[-1] / c[-7] - 1) * 100 if c and len(c) >= 7 else 0.0
        ch24 = (c[-1] / c[-25] - 1) * 100 if c and len(c) >= 25 else None
        vol1h = 0.0
        try:
            rets = [c[i] / c[i - 1] - 1 for i in range(max(1, len(c) - 24), len(c))]
            m_ = sum(rets) / len(rets)
            vol1h = (sum((x - m_) ** 2 for x in rets) / len(rets)) ** 0.5 * 100
        except Exception:
            pass
        out[p] = {"vol_1h_pct": round(vol1h, 3), "price": t["last"], "chg_24h_pct": round(ch24, 2) if ch24 is not None else None,
                  "chg_6h_pct": round(ch6, 2), "min_order": k.pairs()[p]["ordermin"]}
    return out


BRAIN_MIN_BUSY_S = 2400
BRAIN_MIN_IDLE_S = 10800
BRAIN_ERR_BACKOFF_S = 600   # after an LLM error: try again in 10 minutes (an error does not use up the window)
MAKER_FEE = 0.004       # Kraken Pro tier 1 spot maker 0.40% (taker 0.80% = FEE), official fee page, checked Nov 25 2026
MIN_HOLD_H = 3
COOLDOWN_H = 6
MIN_TAKE_PCT = 3.0


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


def _candles(rows):
    """Hourly closes (plain numbers) or candles ({o, h, l, c}). A close alone becomes a flat candle."""
    return [r if isinstance(r, dict) else {"o": r, "h": r, "l": r, "c": r} for r in rows]


def sim_trade(rows, i, stop=0.03, take=0.05, hold=48, fee_pct=0.0):
    """Enter at the close of candle i and walk forward on each candle's high and low. Conservative on purpose:
    a candle that touches both levels counts as the stop, and a candle that opens below the stop exits at its open
    (a gap fills worse than the stop price). Returns (net % after fee_pct, index of the exit candle)."""
    px = rows[i]["c"]
    last = min(len(rows) - 1, i + hold)
    for j in range(i + 1, last + 1):
        r = rows[j]
        if r["l"] <= px * (1 - stop):
            return (min(r["o"], px * (1 - stop)) / px - 1) * 100 - fee_pct, j
        if r["h"] >= px * (1 + take):
            return take * 100 - fee_pct, j
    return (rows[last]["c"] / px - 1) * 100 - fee_pct, last


def backtest_setup(closes, kind, stop=0.03, take=0.05, hold=48, fee_pct=None):
    """Replay on about 30 days of hourly data: every time this setup appeared, enter at the close, exit at -stop,
    +take or after `hold` hours, subtract round-trip fees (maker entry + taker exit, 1.2% by default).
    `closes` may be plain closes or candles; with candles the stop and take are checked on highs and lows, so a wick
    that would have stopped the trade is not missed. Returns {n, win_pct, avg_net_pct} or None with too little history."""
    rows = _candles(closes)
    c = [r["c"] for r in rows]
    fee_pct = (FEE + MAKER_FEE) * 100 if fee_pct is None else fee_pct
    nets, i = [], 24
    while i < len(c) - 2:
        c6, c24, c1 = c[i] / c[i - 6] - 1, c[i] / c[i - 24] - 1, c[i] / c[i - 1] - 1
        hit = ((kind == "breakout" and 0.03 <= c6 <= 0.08 and c1 > 0) or
               (kind == "trend" and 0.01 <= c6 < 0.05 and c24 > 0 and c1 > 0) or
               (kind == "rebound" and c24 <= -0.03 and c1 > 0))
        if not hit:
            i += 1
            continue
        net, j = sim_trade(rows, i, stop, take, hold, fee_pct)
        nets.append(net)
        i = j + 1
    if len(nets) < 3:
        return None
    return {"n": len(nets), "win_pct": round(100 * sum(1 for x in nets if x > 0) / len(nets)), "avg_net_pct": round(sum(nets) / len(nets), 2)}


def replay_rows(k, pair):
    """Candles when the connector has them (highs and lows matter), else plain closes."""
    try:
        return k.ohlc_candles(pair)
    except AttributeError:
        return k.ohlc_full(pair)


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
        c6, c24 = m["chg_6h_pct"], m.get("chg_24h_pct")
        kind, est = None, 0.0
        if c6 >= 3 and c6 <= 8 and c1h is not None and c1h > 0:
            kind, est = "breakout", min(7.0, c6 * 1.2)
        elif 1 <= c6 < 5 and c24 is not None and c24 > 0 and (c1h or 0) > 0 and (c15 or 0) >= 0:
            kind, est = "trend", min(5.0, 2.0 + c6)
        elif c24 is not None and c24 <= -3 and (c5 or 0) > 0 and (c15 or 0) > 0:
            kind, est = "rebound", min(6.0, abs(c24) * 0.8)
        if kind and est >= 3.0:
            btc_ok = btc.get("chg_6h_pct", 0) > -1.5 or p == "XBTUSDC"
            score = est + (1.0 if btc_ok else -2.0)
            out.append({"pair": p, "setup": kind, "est_move_pct": round(est, 1), "btc_ok": btc_ok,
                        "score": round(score, 1), "chg_6h": c6, "chg_24h": c24, "chg_1h": c1h, "chg_15m": c15})
    return sorted(out, key=lambda r: -r["score"])




class Refuse(Exception):
    pass


def num(v, default=None):
    """Parse a number from LLM output ('3', 3, '3%', ' 2.5 '). Missing -> default. Garbage/NaN/inf/negative -> Refuse."""
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise Refuse("bad number")
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip().replace(",", "")
    try:
        x = float(v)
    except Exception:
        raise Refuse("not a number: %r" % str(v)[:20])
    if math.isnan(x) or math.isinf(x) or x < 0:
        raise Refuse("number out of range")
    return x


def make_ref(name, p):
    return zlib.crc32(("%s:%s:%s" % (name, p, time.time())).encode()) & 0x7fffffff


def _plan(k, b, d, tk, mode, internal, risk):
    """Phase A (under LOCK): validate and size. Returns a plan dict or raises Refuse(message)."""
    act, p = d["action"], d["pair"]
    info = k.pairs()[p]
    if act == "buy":
        if not b["alive"]:
            raise Refuse("refused: bot is dead, no new buys")
        if b.get("pending"):
            raise Refuse("refused: an entry order is already working")
        usdc = num(d.get("usdc"))
        if not usdc:
            raise Refuse("refused: usdc amount missing or invalid")
        if d.get("take_pct") in (None, ""):
            raise Refuse("refused: take_pct is required for a buy")
        take = min(20.0, max(2.0, num(d.get("take_pct"))))
        stop = min(10.0, max(1.5, num(d.get("stop_pct"), 3.0)))
        cap = LIVE_SIZE_CAP if mode == "live" else DRY_SIZE_CAP
        usdc = min(usdc, b["cash"] * cap)
        # survival sizing: if this trade hits its stop, the loss (stop distance + round-trip fees) is at most
        # MAX_RISK_PCT of equity. The brain still chooses the coin, the stop and the target; a wider stop simply
        # buys a smaller position. Twenty stop-outs in a row then cost about a third of the stake, not all of it.
        risk_cap = equity(b, tk) * MAX_RISK_PCT / (stop + (FEE + MAKER_FEE) * 100)
        if usdc > risk_cap:
            usdc = risk_cap
        vol = kraken.floor_to(usdc * (1 - FEE) / tk[p]["ask"], info["lot_decimals"])
        if usdc < max(info["costmin"], 1) or vol < info["ordermin"]:
            raise Refuse("refused: below Kraken minimum order (%.2f USDC, min volume %s)" % (usdc, info["ordermin"]))
        if halted():
            raise Refuse("refused: halt flag is set (no new buys)")
        if time.time() - b.get("cool", {}).get(p, 0) < COOLDOWN_H * 3600:
            raise Refuse("refused: %s is in cooldown after its last sell" % p)
        if take < MIN_TAKE_PCT:
            raise Refuse("refused: fee gate, take-profit must be at least %.1f%% (round trip costs ~1.6%%)" % MIN_TAKE_PCT)
        if take / stop < MIN_RR:
            raise Refuse("refused: reward/risk %.2f below %.1f (take %.1f%% vs stop %.1f%%)" % (take / stop, MIN_RR, take, stop))
        lq, lwhy = liquid(p, tk)
        if not lq:
            raise Refuse("refused: not liquid enough (%s)" % lwhy)
        if usdc > b["cash"] * 0.99:
            raise Refuse("refused: would spend more than the account balance (spot only, never negative)")
        ok, why = policy.check(b, usdc, "trade", "kraken", d.get("reason", ""), side="buy", mode=mode, internal=internal)
        if not ok:
            raise Refuse("blocked by policy: %s" % why)
        return {"act": "buy", "p": p, "vol": vol, "usdc": usdc, "stop": stop, "take": take}
    frac = min(1.0, num(d.get("fraction"), 1.0))
    held = b["pos"].get(p, 0.0)
    q = held if frac >= 0.999 else kraken.floor_to(held * frac, info["lot_decimals"])
    g0 = b.get("guards", {}).get(p)
    if g0 and g0.get("t") and not internal and not risk and time.time() - g0["t"] < MIN_HOLD_H * 3600:
        raise Refuse("refused: minimum hold %dh not reached (guard stop and take-profit still protect)" % MIN_HOLD_H)
    if q > held + 1e-12:
        raise Refuse("refused: cannot sell more than held (no shorting)")
    if q < info["ordermin"]:
        raise Refuse("refused: nothing to sell or below minimum volume")
    ok, why = policy.check(b, q * tk[p]["bid"], "trade", "kraken", d.get("reason", ""), side="sell", mode=mode, internal=internal)
    if not ok:
        raise Refuse("blocked by policy: %s" % why)
    return {"act": "sell", "p": p, "vol": q}


def _live_buy(k, b, plan, tk):
    p = plan["p"]
    try:
        r = k.order_maker(p, "buy", plan["vol"], tk[p]["bid"], userref=make_ref(b["name"], p))
    except kraken.Rejected as e:
        # Kraken said no and nothing was placed (the usual case: the price moved and the post-only bid would have
        # crossed). An ordinary missed entry, not an unknown state: no HALT.
        return "maker entry refused by Kraken, no trade: %s" % str(e)[:100]
    except Exception as e:
        write_halt("maker entry error on %s: %s" % (p, str(e)[:100]))
        return "maker entry failed (HALT set until reconcile): %s" % str(e)[:100]
    if not r["live"]:
        return "validated only (live orders off): %s" % r["descr"]
    if r["vol"] <= 0:
        return "maker entry not filled in time, cancelled, no trade (no chasing)"
    return {"fills": [(r["vol"], r["cost"], r["fee"])], "side": "buy"}


def _live_sell(k, b, plan, tk):
    """Cancel the Kraken stop first (it locks the balance), verify, then market sell. If the stop already fired,
    book that fill. If the sell fails, put the stop back."""
    p, q = plan["p"], plan["vol"]
    g = b.get("guards", {}).get(p) or {}
    txid = g.get("stop_txid")
    fills, new_stop = [], "keep"
    if txid:
        try:
            k.cancel(txid)
            sq = k.query_order(txid)
        except Exception as e:
            return "sell refused: could not cancel/verify the Kraken stop (%s); position stays protected" % str(e)[:80]
        if sq["status"] not in kraken.DONE:
            return "sell refused: Kraken stop still open after cancel; position stays protected"
        new_stop = None
        if sq["vol_exec"] > 0:
            fills.append((sq["vol_exec"], sq["cost"], sq["fee"]))
            q = q - sq["vol_exec"]
    info = k.pairs()[p]
    if q >= info["ordermin"]:
        try:
            r = k.order(p, "sell", q, userref=make_ref(b["name"], p))
        except Exception as e:
            return {"fills": fills, "side": "sell", "stop_txid": None, "failed": "sell failed: %s" % str(e)[:100]}
        if not r["live"]:
            return {"fills": fills, "side": "sell", "stop_txid": None, "failed": "validated only (live orders off)"}
        if r.get("vol", 0) > 0:
            fills.append((r["vol"], r["cost"], r["fee"]))
        if r.get("status") not in kraken.DONE or r.get("vol", 0) <= 0:
            write_halt("sell %s did not confirm (status %s)" % (p, r.get("status")))
    return {"fills": fills, "side": "sell", "stop_txid": new_stop}


def apply_sell(b, p, qty, cost, fee, tk):
    b["cash"] += cost - fee
    b.setdefault("cool", {})[p] = time.time()
    b["pos"][p] = max(0.0, b["pos"].get(p, 0.0) - qty)
    if b["pos"][p] * tk[p]["bid"] < 1:
        b["pos"].pop(p, None); b.get("guards", {}).pop(p, None)
    b["fees"] += fee; b["trades"] += 1
    b["last_action"] = time.time(); b["idle_strikes"] = 0


def _apply_buy(b, plan, qty, cost, fee):
    p = plan["p"]
    gd = b.setdefault("guards", {})
    prev, g0 = b["pos"].get(p, 0.0), gd.get(p)
    b["cash"] -= cost + fee
    b["pos"][p] = prev + qty
    entry = (prev * g0["entry"] + cost) / (prev + qty) if g0 and prev else cost / qty
    gd[p] = {"entry": entry, "stop": entry * (1 - plan["stop"] / 100), "take": entry * (1 + plan["take"] / 100),
             "high": entry, "stop_txid": g0.get("stop_txid") if g0 else None, "stop_vol": g0.get("stop_vol", 0) if g0 else 0,
             "t": time.time()}
    b["fees"] += fee; b["trades"] += 1
    b["last_action"] = time.time(); b["idle_strikes"] = 0


def ensure_stop(k, b, p, mode, validate):
    """Keep one Kraken stop-loss order matching the position and the guard's stop level (replace if the quantity changed
    or a trailing stop rose >= 1%). No lock during exchange I/O."""
    live = mode == "live" and kraken.live_enabled()
    if not (live or validate):
        return
    with LOCK:
        g, qty = b.get("guards", {}).get(p), b["pos"].get(p, 0.0)
        if not g or qty <= 0 or time.time() - g.get("stop_err_t", 0) < 300:
            return
        old, stop = g.get("stop_txid"), g["stop"]
        need = old is None or abs(qty - g.get("stop_vol", 0)) > qty * 0.01 or stop >= g.get("stop_px", 0) * 1.01
    if not need:
        return
    gone = not (old and live)        # True once the old stop is known not to be working on Kraken any more
    try:
        if old and live:
            k.cancel(old)
            q = k.query_order(old)
            if q["vol_exec"] > 0:
                return           # it fired; sync_stops books it
            if q["status"] not in ("canceled", "expired"):
                # Kraken has not confirmed the cancel: the old stop still protects the position and still holds the
                # coins, so a new one would be refused. Keep the old id (sync_stops must go on watching it) and retry.
                with LOCK:
                    g["stop_err_t"] = time.time()
                jlog({"bot": b["name"], "stop_order_error": "old stop %s is still %s, not replaced" % (old, q["status"])})
                return
            gone = True
        so = k.stop_order(p, qty, stop)
        with LOCK:
            g["stop_txid"] = so["txid"] if live else None
            g["stop_vol"], g["stop_px"] = qty, stop
        jlog({"bot": b["name"], "stop_order": so["descr"], "live": so["live"]})
    except Exception as e:
        with LOCK:
            g["stop_err_t"] = time.time()
            if old and live and gone:        # forget the old id only when Kraken confirmed it is gone
                g["stop_txid"] = None
        jlog({"bot": b["name"], "stop_order_error": str(e)[:150]})


def sync_stops(k, mode, tk):
    """Live: book stops that Kraken fired, clear dead stop ids, re-place missing stops."""
    if not (mode == "live" and kraken.live_enabled()):
        return
    with LOCK:
        s = load(mode, 0.0)
        todo = [(b, p, b["guards"][p]["stop_txid"]) for b in s["bots"].values() for p in list(b["pos"])
                if b.get("guards", {}).get(p, {}).get("stop_txid")]
    for b, p, txid in todo:
        try:
            q = k.query_order(txid)
        except Exception as e:
            jlog({"event": "stop_query_error", "err": str(e)[:100]})
            continue
        if q["status"] in kraken.DONE:
            with LOCK:
                g = b.get("guards", {}).get(p)
                if g and g.get("stop_txid") == txid:
                    g["stop_txid"] = None
                    if q["vol_exec"] > 0:
                        apply_sell(b, p, q["vol_exec"], q["cost"], q["fee"], tk)
                        jlog({"bot": b["name"], "event": "stop_fired_by_kraken", "pair": p, "qty": q["vol_exec"], "cost": q["cost"]})
                    save(mode, s)
    with LOCK:
        pairs = [(b, p) for b in s["bots"].values() for p in list(b["pos"])]
    for b, p in pairs:
        ensure_stop(k, b, p, mode, False)


def execute(k, s, b, d, tk, mode, validate, internal=False, risk=False):
    """Apply one decision. internal=True is set only by the guard in code (never by LLM text). risk=True lets a risk
    event (BTC alert, USDC depeg) release the min-hold. Phase A validate (LOCK), B exchange I/O (no LOCK), C book fill (LOCK)."""
    if not isinstance(d, dict):
        return "no trade"
    act, p = d.get("action"), d.get("pair")
    if act not in ("buy", "sell") or p not in tk or p not in kraken.UNIVERSE:
        return "no trade"
    key = (b["name"], p)
    with LOCK:
        if key in INFLIGHT:
            return "refused: an order for %s is already in flight" % p
        try:
            plan = _plan(k, b, d, tk, mode, internal, risk)
        except Refuse as e:
            m = str(e)
            return m if m.startswith(("refused", "blocked")) else "refused: " + m
        INFLIGHT.add(key)
    try:
        info = k.pairs()[p]
        if mode == "live":
            res = _live_buy(k, b, plan, tk) if act == "buy" else _live_sell(k, b, plan, tk)
            if isinstance(res, str):
                return res
        else:
            vol = plan["vol"]
            if validate:
                try:
                    r = k.order_maker(p, "buy", vol, tk[p]["bid"]) if act == "buy" else k.order(p, "sell", vol)
                    jlog({"bot": b["name"], "validated": r["descr"]})
                except Exception as e:
                    jlog({"bot": b["name"], "validate_error": str(e)[:120]})
            if act == "buy" and PAPER_PENDING:
                with LOCK:          # like the real post-only bid: it rests, and fill_pending() decides its fate
                    b["pending"] = {"p": p, "vol": vol, "price": tk[p]["bid"], "last0": tk[p]["last"],
                                    "stop": plan["stop"], "take": plan["take"], "t": time.time()}
                return "paper maker order working: buy %s %.6f @ %.4f (fills only if the market trades through it within %ds)" % (
                    p, vol, tk[p]["bid"], kraken.Kraken.MAKER_WAIT)
            maker = act == "buy"
            cost = vol * tk[p]["bid"]       # instant paper fill (single `cycle` runs and tests): optimistic
            res = {"fills": [(vol, cost, cost * (MAKER_FEE if maker else FEE))], "side": act}
        with LOCK:
            msg = ""
            if act == "buy":
                for qty, cost, fee in res["fills"]:
                    _apply_buy(b, plan, qty, cost, fee)
                    policy.record(b["name"], mode, cost, "buy")
                    msg = "buy %s %.6f @ ~%.4f (fee %.3f)" % (p, qty, cost / qty, fee)
            else:
                if "stop_txid" in res and b.get("guards", {}).get(p) is not None:
                    b["guards"][p]["stop_txid"] = res["stop_txid"]
                    b["guards"][p]["stop_vol"] = 0
                for qty, cost, fee in res["fills"]:
                    apply_sell(b, p, qty, cost, fee, tk)
                    policy.record(b["name"], mode, cost, "sell")
                    msg = "sell %s %.6f @ ~%.4f (fee %.3f)" % (p, qty, cost / qty, fee)
                if res.get("failed"):
                    msg = (msg + " | " if msg else "") + res["failed"]
            if mode == "live":
                save(mode, s)
        if mode == "live" or validate:
            if p in b["pos"]:
                ensure_stop(k, b, p, mode, validate)
        return msg or "no trade"
    finally:
        with LOCK:
            INFLIGHT.discard(key)


def fill_pending(tk, mode):
    """Paper mode only. A maker entry is a resting bid: it fills only if the market really trades through it within
    MAKER_WAIT seconds (the ask comes down to our price, or a NEW trade prints below it); otherwise it expires with no
    trade, exactly like the live order that is cancelled and never chased. Without this the paper ledger fills every
    entry at once and in full, which flatters the results. Returns [(bot, message)]."""
    out = []
    with LOCK:
        s = load(mode, DRY_START)
        for b in s["bots"].values():
            pd = b.get("pending")
            if not pd:
                continue
            t = tk.get(pd["p"])
            through = bool(t) and (t["ask"] <= pd["price"] or (t["last"] != pd["last0"] and t["last"] < pd["price"]))
            if through and b["alive"]:
                b.pop("pending")
                cost = pd["vol"] * pd["price"]
                fee = cost * MAKER_FEE
                if cost + fee > b["cash"]:
                    msg = "paper entry dropped: not enough cash"
                else:
                    _apply_buy(b, pd, pd["vol"], cost, fee)
                    policy.record(b["name"], mode, cost, "buy")
                    msg = "buy %s %.6f @ ~%.4f (fee %.3f)" % (pd["p"], pd["vol"], pd["price"], fee)
            elif time.time() - pd["t"] > kraken.Kraken.MAKER_WAIT or not b["alive"]:
                b.pop("pending")
                msg = "paper maker entry not filled in time, cancelled, no trade (no chasing)"
            else:
                continue
            out.append((b["name"], msg))
            jlog({"bot": b["name"], "paper_entry": msg})
        if out:
            save(mode, s)
    return out


def cycle(mode, validate=False, scan=None, k=None, fresh_tk=None):
    k = k or kraken.get()
    if mode == "live":
        if not kraken.live_enabled():
            print("LIVE orders are off (SHACHAR_LIVE not set). Orders will be validated only, nothing is placed.")
        start = k.balance().get("USDC", 0.0)
    else:
        start = DRY_START
    s = load(mode, start)
    tk = k.tickers()
    if mode == "live" and kraken.live_enabled():
        sync_stops(k, mode, tk)
        try:
            reconcile(k, s, tk)
        except Exception as e:
            write_halt("reconcile failed: %s" % str(e)[:100])
    mk = market(k, tk)
    nw = {"fear_greed": news.fear_greed()}
    for name in list(s["bots"]):
        with LOCK:
            b = s["bots"][name]
            if not b["alive"]:
                continue
            eq = equity(b, tk)
            if time.time() - b["last_action"] > IDLE_HOURS * 3600:
                b["idle_strikes"] += 1; b["last_action"] = time.time()
                s["log"].append("%s idle note %d (informational, no penalty)" % (b["name"], b["idle_strikes"]))
            if eq < DEATH_FLOOR:
                b["alive"] = False
                s["log"].append("%s DIED %s (positions kept and still protected)" % (b["name"], time.strftime("%F %T")))
                jlog({"bot": b["name"], "event": "died", "equity": round(eq, 2)})
                write_halt("bot %s equity %.2f below death floor" % (b["name"], eq))
                save(mode, s)
                continue
            save(mode, s)
            profit = eq - b["start"]
            want_child = mode != "live" and eq >= b["start"] * SPAWN_AT and len(b["children"]) < MAX_CHILDREN
            ctx = {"time": time.strftime("%F %T"), "market": mk, "news": nw, "lessons": lessons_tail(),
                   "me": {"name": b["name"], "cash": round(b["cash"], 2), "positions": {p: round(q, 6) for p, q in b["pos"].items()},
                          "equity": round(eq, 2), "start": round(b["start"], 2), "profit": round(profit, 2),
                          "hours_since_last_trade": round((time.time() - b["last_action"]) / 3600, 1),
                          "trades_total": b["trades"], "death_floor": DEATH_FLOOR, "working_entry": b.get("pending"),
                          "reproduction": ("ready, free cash needed %.2f" % (profit * CHILD_STAKE) if want_child else "not yet (need 2x start)") if mode != "live" else "off in live"},
                   "cooldown_pairs": [p for p, t0 in b.get("cool", {}).items() if time.time() - t0 < COOLDOWN_H * 3600]}
            positions = [p for p in b["pos"] if p in tk]
        ctx["me"]["recent_results"] = list(b.get("recent", []))
        if scan:
            ctx["scan"] = scan
        ctx["opportunities"] = opportunities(mk, scan, tk)
        ctx["btc_alert"] = btc_alert(mk, scan)
        risk = ctx["btc_alert"] != "none"
        try:
            peg = k.usdc_peg()
            ctx["usdc_peg"] = {"usdc_usd": round(peg, 4), "warning": "USDC off peg, your cash may be at risk" if abs(peg - 1) > 0.003 else "ok"}
            risk = risk or abs(peg - 1) > 0.003
        except Exception:
            ctx["usdc_peg"] = {"warning": "unavailable"}
        for o in ctx["opportunities"][:3]:
            try:
                o["backtest_30d"] = backtest_setup(replay_rows(k, o["pair"]), o["setup"]) or "too few cases"
            except Exception:
                pass
        ctx["order_books"] = {}
        for p in dict.fromkeys([o["pair"] for o in ctx["opportunities"][:3]] + positions):
            try:
                ctx["order_books"][p] = k.depth(p)
            except Exception:
                pass
        ctx["not_liquid"] = [p for p in mk if p in tk and not liquid(p, tk)[0]]
        busy = bool(ctx["opportunities"]) or bool(positions) or risk
        wait_s = BRAIN_MIN_BUSY_S if busy else BRAIN_MIN_IDLE_S
        if time.time() - b.get("last_brain", 0) < wait_s:
            d = {"action": "hold", "reason": "gate: no new setup, brain not called (free quota is 200k tokens/day)", "source": "gate"}
        elif time.time() - b.get("last_brain_err", 0) < BRAIN_ERR_BACKOFF_S:
            d = {"action": "hold", "reason": "gate: waiting a few minutes after an LLM error", "source": "gate"}
        else:
            d = brain.decide(ctx)        # slow (LLM), no lock held; the guard thread keeps running meanwhile
        src = d.get("source") if isinstance(d, dict) else "llm_error"
        if not isinstance(d, dict):
            d = {"action": "hold", "reason": "brain returned no object", "source": src}
        if src in ("llm_error", "no_llm"):
            b["last_brain_err"] = time.time()     # the brain did not really think: do not make it wait 40 minutes
        elif src != "gate":
            b["last_brain"] = time.time()
        # the prices in `tk` were read before the brain was asked (it can take a minute): order at the newest ones
        res = execute(k, s, b, d, (fresh_tk() if fresh_tk else None) or tk, mode, validate, risk=risk)
        if src != "gate":                         # the brain reads what happened to its last proposals, refusals included
            with LOCK:
                b["recent"] = (b.get("recent") or [])[-5:] + [{"t": time.strftime("%F %H:%M"), "action": d.get("action"),
                                                               "pair": d.get("pair"), "result": str(res)[:220]}]
        if d.get("lesson"):
            with (HERE / "lessons.md").open("a") as f:
                f.write("- %s [%s] %s\n" % (time.strftime("%F"), b["name"], str(d["lesson"])[:300]))
        if d.get("action") == "idea":
            with (HERE / "ideas_proposed.jsonl").open("a") as f:
                f.write(json.dumps({"bot": b["name"], "t": time.strftime("%F %T"), "idea": d.get("reason")}) + "\n")
        jlog({"bot": b["name"], "decision": d, "result": res, "equity": round(equity(b, tk), 2)})
        with LOCK:
            eq = equity(b, tk); profit = eq - b["start"]
            if want_child and profit * CHILD_STAKE >= MIN_CHILD and b["cash"] >= profit * CHILD_STAKE:
                stake = profit * CHILD_STAKE
                cn = "%s.%d" % (b["name"], len(b["children"]) + 1)
                b["cash"] -= stake; b["children"].append(cn); b["start"] = eq - stake
                s["bots"][cn] = newbot(cn, stake, b["name"])
                s["log"].append("%s spawned %s with %.2f USDC" % (b["name"], cn, stake))
                jlog({"bot": b["name"], "event": "spawned", "child": cn, "stake": round(stake, 2)})
            s["log"] = s["log"][-200:]
            save(mode, s)
    status(mode, s, tk)


def status(mode="dry", s=None, tk=None):
    if s is None:
        s = load(mode, DRY_START); tk = kraken.get().tickers()
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
