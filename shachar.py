"""Shachar: decision cycle, hard gates, order execution and ledger for every living bot in the family.
  python shachar.py cycle dry              one brain cycle on the paper ledger (no Kraken key needed)
  python shachar.py cycle dry --validate   also sends each order to Kraken with validate=true (nothing is placed)
  python shachar.py cycle live             real orders. Refuses unless SHACHAR_LIVE=YES_IDAN_APPROVED
  python shachar.py status [dry|live]
  python shachar.py reconcile live         compare the ledger with the real Kraken account
  python shachar.py adopt live             owner tool: rebuild the ledger from the real Kraken account
Normal operation is `python guard.py <mode>` (the scanner, the guard and the brain together).

Design rules:
- The brain proposes, code decides. parse_decision() validates the proposal BEFORE any order exists.
- execute() runs in three steps: plan (under the state lock, no network), place (network, no state lock),
  apply (under the state lock). A per-pair lock makes sure only one order per coin is in flight.
- Kraken is the source of truth: stops that fired on Kraken are mirrored (sync_stops), the ledger is compared
  per asset (reconcile), and anything that cannot be explained writes HALT and alerts the owner."""
import os, json, sys, time, math, collections, threading, traceback
import kraken, news, brain, policy, store, notify
from store import LOCK
from kraken import floor_to

FEE_TAKER = 0.008          # market order, per side (Kraken Pro entry tier; measure it on real fills)
FEE_MAKER = 0.004          # post-only limit order, per side
FEE_RT_PCT = (FEE_TAKER + FEE_MAKER) * 100      # maker entry + taker exit = 1.2% round trip
DEATH_FLOOR = 3.0          # USDC. Below this no Kraken order is possible
IDLE_HOURS, MAX_IDLE = 24, 3
SPAWN_AT, CHILD_STAKE, MIN_CHILD, MAX_CHILDREN = 2.0, 0.5, 6.0, 3
DRY_START = 47.87

BRAIN_MIN_BUSY_S = 2400    # with a setup or an open position: one brain call per 40 min
BRAIN_MIN_IDLE_S = 10800   # nothing to do: one per 3h
BRAIN_ERR_BACKOFF_S = 600  # after an LLM error: retry in 10 min (an error does not use up the window)
MIN_HOLD_H = 3             # the brain cannot sell a fresh position, unless there is a risk event (guard exits are exempt)
COOLDOWN_H = 6             # per-coin cooldown after a sell
MIN_TAKE_PCT = 3.0         # a target below this cannot clear fees with any margin
MIN_NET_RR = 1.0           # reward/risk check: (take - fees) >= MIN_NET_RR * (stop + fees). A warning; a gate with RR_GATE=1
MAX_RISK_PCT = 2.0         # a stop-out (including fees) may cost at most this % of equity
MAX_POSITIONS = 2
MAX_SPREAD_PCT = 0.2       # liquidity gate
MIN_VOL_USD = 500_000
DRAWDOWN_HALT = 0.85       # equity under 85% of its peak: HALT buys and alert the owner
REPLAY_MIN_N = 10          # replay check: a setup that lost money over >= 10 cases. A warning; a gate with REPLAY_GATE=1
MAKER_WAIT_S = 90
TK_MAX_AGE_S = 20          # a buy needs prices fresher than this
STOP_STEP = 0.005          # move the Kraken stop when the trailing stop is 0.5% above it
WARMUP_S = 3300            # no setups until the scanner has almost an hour of prices

_PAIR_LOCKS = collections.defaultdict(threading.Lock)
_ALERTS = []


def idle_death(mode):
    """Idle strikes kill in the paper game. With real money they are only counted, unless IDLE_DEATH=1:
    forcing trades to stay alive is the opposite of protecting the stake."""
    v = os.environ.get("IDLE_DEATH")
    return v == "1" if v in ("0", "1") else mode != "live"


def replay_gate():
    """Off by default: the brain is free to overrule the replay. It then gets a warning instead of a refusal."""
    return os.environ.get("REPLAY_GATE", "0") == "1"


def rr_gate():
    """Off by default: reward smaller than risk is the brain's call. It gets a warning instead of a refusal."""
    return os.environ.get("RR_GATE", "0") == "1"


def busy():
    return any(l.locked() for l in list(_PAIR_LOCKS.values()))


def _defer_alert(title, body=""):
    _ALERTS.append((title, body))


def _flush_alerts():
    while _ALERTS:
        t, b = _ALERTS.pop(0)
        notify.alert(t, b)


def _touch_peak(b, eq):
    pk = b.get("peak")
    b["peak"] = eq if pk is None else max(pk, eq)
    return b["peak"]


def _px(b, p, tk):
    if p in tk:
        return tk[p]["bid"]
    return (b.get("guards", {}).get(p) or {}).get("entry", 0.0)


def equity(b, tk):
    return (b["cash"] + sum(q * _px(b, p, tk) for p, q in b["pos"].items())
            + sum(q * _px(b, p, tk) for p, q in b.get("dust", {}).items()))


def lessons_tail(n=6):
    f = store.path("lessons.md")
    if not f.exists():
        return []
    return [l.strip()[:300] for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("- ")][-n:]


# ---------------------------------------------------------------- market data

def _pct(c, n):
    return (c[-1] / c[-1 - n] - 1) * 100 if len(c) > n and c[-1 - n] > 0 else None


def market(k, tk):
    """Per pair: price, true 6h and 24h change (from hourly candles, not from the ticker's UTC-midnight open) and
    hourly volatility. Also returns the candles so the replay does not fetch them again."""
    out, candles = {}, {}
    info = k.pairs()
    for p, t in tk.items():
        try:
            rows = k.ohlc(p, 60)
        except Exception:
            rows = []
        candles[p] = rows
        c = [r["c"] for r in rows]
        vol1h = None
        if len(c) >= 25:
            rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 24, len(c))]
            m_ = sum(rets) / len(rets)
            vol1h = round((sum((x - m_) ** 2 for x in rets) / len(rets)) ** 0.5 * 100, 3)
        c6, c24 = _pct(c, 6), _pct(c, 24)
        out[p] = {"price": t["last"], "chg_6h_pct": None if c6 is None else round(c6, 2),
                  "chg_24h_pct": None if c24 is None else round(c24, 2), "vol_1h_pct": vol1h,
                  "min_order": info[p]["ordermin"]}
    return out, candles


def liquid(p, tk):
    t = tk[p]
    spread = (t["ask"] / t["bid"] - 1) * 100
    vol = t["vol24"] * t["last"]
    if spread > MAX_SPREAD_PCT:
        return False, "spread %.3f%% > %.1f%%" % (spread, MAX_SPREAD_PCT)
    if vol < MIN_VOL_USD:
        return False, "24h volume %.0f USD < %d" % (vol, MIN_VOL_USD)
    return True, ""


def sim_trade(rows, i, stop=0.03, take=0.05, hold=48, fee_pct=FEE_RT_PCT):
    """Enter at the close of candle i and walk forward using each candle's high and low. If a candle touches both
    levels, the stop wins (conservative). A candle that opens below the stop exits at its open (gap).
    Returns (net % after round-trip fees, index of the exit candle)."""
    px = rows[i]["c"]
    last = min(len(rows) - 1, i + hold)
    for j in range(i + 1, last + 1):
        r = rows[j]
        if r["l"] <= px * (1 - stop):
            return (min(r["o"], px * (1 - stop)) / px - 1) * 100 - fee_pct, j
        if r["h"] >= px * (1 + take):
            return take * 100 - fee_pct, j
    return (rows[last]["c"] / px - 1) * 100 - fee_pct, last


def setup_hit(c, i, kind):
    """Hourly-close version of the three setups (the live scanner also looks at 5m and 15m moves)."""
    c6, c24, c1 = c[i] / c[i - 6] - 1, c[i] / c[i - 24] - 1, c[i] / c[i - 1] - 1
    return ((kind == "breakout" and 0.03 <= c6 <= 0.08 and c1 > 0) or
            (kind == "trend" and 0.01 <= c6 < 0.05 and c24 > 0 and c1 > 0) or
            (kind == "rebound" and c24 <= -0.03 and c1 > 0))


def backtest_setup(rows, kind, stop=0.03, take=0.05, hold=48):
    """Replay on about 30 days of hourly candles. Returns {n, win_pct, avg_net_pct} or None with too little history.
    Rough: fixed 3%/5% levels, no trailing stop, taker-exit fees included."""
    c = [r["c"] for r in rows]
    nets, i = [], 24
    while i < len(rows) - 2:
        if not setup_hit(c, i, kind):
            i += 1
            continue
        net, j = sim_trade(rows, i, stop, take, hold)
        nets.append(net)
        i = j + 1
    if len(nets) < 3:
        return None
    return {"n": len(nets), "win_pct": round(100 * sum(1 for x in nets if x > 0) / len(nets)),
            "avg_net_pct": round(sum(nets) / len(nets), 2)}


def btc_moves(mk, scan):
    c1 = ((scan or {}).get("pairs", {}).get("XBTUSDC", {}) or {}).get("chg_1h_pct")
    c6 = mk.get("XBTUSDC", {}).get("chg_6h_pct")
    return c1, c6


def btc_alert(mk, scan):
    """Flag a big BTC move: 1h >= 2% or 6h >= 5% (either direction). BTC leads the market."""
    c1, c6 = btc_moves(mk, scan)
    msgs = []
    if c1 is not None and abs(c1) >= 2:
        msgs.append("BTC %+.1f%% in 1h" % c1)
    if c6 is not None and abs(c6) >= 5:
        msgs.append("BTC %+.1f%% in 6h" % c6)
    return msgs or "none"


def opportunities(mk, scan, tk):
    """Ranked list of liquid pairs showing a setup. Pure rules on free Kraken data; the brain still decides.
    breakout: 6h move 3-8% and the last hour still rising.
    trend: steady climb (1h up, 15m not falling, 6h 1-5%, 24h up).
    rebound: 24h <= -3% and the 5m and 15m have turned up.
    Missing data never counts as a confirmation: a pair without scanner history shows no setup."""
    out = []
    btc6 = mk.get("XBTUSDC", {}).get("chg_6h_pct")
    for p, m in mk.items():
        if p not in tk or not liquid(p, tk)[0]:
            continue
        sc = (scan or {}).get("pairs", {}).get(p, {})
        c5, c15, c1h = sc.get("chg_5m_pct"), sc.get("chg_15m_pct"), sc.get("chg_1h_pct")
        c6, c24 = m.get("chg_6h_pct"), m.get("chg_24h_pct")
        if c6 is None or c24 is None or c1h is None or c15 is None or c5 is None:
            continue
        kind, est = None, 0.0
        if 3 <= c6 <= 8 and c1h > 0:
            kind, est = "breakout", min(7.0, c6 * 1.2)
        elif 1 <= c6 < 5 and c24 > 0 and c1h > 0 and c15 >= 0:
            kind, est = "trend", min(5.0, 2.0 + c6)
        elif c24 <= -3 and c5 > 0 and c15 > 0:
            kind, est = "rebound", min(6.0, abs(c24) * 0.8)
        if kind and est >= MIN_TAKE_PCT:
            btc_ok = p == "XBTUSDC" or (btc6 is not None and btc6 > -1.5)
            out.append({"pair": p, "setup": kind, "est_move_pct": round(est, 1), "btc_ok": btc_ok,
                        "score": round(est + (1.0 if btc_ok else -2.0), 1), "chg_6h": c6, "chg_24h": c24,
                        "chg_1h": c1h, "chg_15m": c15})
    return sorted(out, key=lambda r: -r["score"])


# ---------------------------------------------------------------- decisions and orders

def _num(v):
    """'3', 3, '3%', ' 2.5 ' -> float. Anything else raises (and the decision is refused before any order)."""
    if isinstance(v, bool) or v is None:
        raise ValueError("not a number")
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip().replace(",", "")
    return float(v)


def parse_decision(d):
    """Validate and clamp the brain's JSON BEFORE any order exists. Returns (clean, error).
    A buy must state usdc, stop_pct and take_pct as finite numbers: nothing is defaulted."""
    if not isinstance(d, dict):
        return None, "decision is not an object"
    act = d.get("action")
    reason = str(d.get("reason") or "")[:300]
    if act not in ("buy", "sell"):
        return {"action": act if act in ("hold", "idea") else "hold", "reason": reason}, None
    p = d.get("pair")
    if not isinstance(p, str) or p not in kraken.UNIVERSE:
        return None, "unknown pair"
    clean = {"action": act, "pair": p, "reason": reason}
    try:
        if act == "buy":
            vals = {x: _num(d[x]) for x in ("usdc", "stop_pct", "take_pct")}
            if not all(math.isfinite(v) for v in vals.values()) or vals["usdc"] <= 0:
                return None, "buy numbers must be finite and positive"
            clean["usdc"] = vals["usdc"]
            clean["stop_pct"] = min(10.0, max(1.5, vals["stop_pct"]))
            clean["take_pct"] = min(20.0, max(2.0, vals["take_pct"]))
        else:
            f = d.get("fraction")
            f = 1.0 if f is None else _num(f)
            if not math.isfinite(f):
                return None, "bad fraction"
            clean["fraction"] = min(1.0, max(0.0, f))
    except (KeyError, TypeError, ValueError):
        return None, "a buy needs numeric usdc, stop_pct and take_pct" if act == "buy" else "bad fraction"
    return clean, None


def _plan_buy(b, d, tk, info, mode, replay, tk_age):
    p, now = d["pair"], time.time()
    if not b["alive"]:
        return "refused: bot is dead"
    if store.halted() or notify.remote_halt(mode):
        return "refused: halt flag is set (no new buys)"
    if tk_age > TK_MAX_AGE_S:
        return "refused: prices are %.0fs old (need under %ds)" % (tk_age, TK_MAX_AGE_S)
    if b.get("pending"):
        return "refused: an entry order is already working"
    if p in b["pos"]:
        return "refused: already holding %s (one position per coin)" % p
    if len(b["pos"]) >= MAX_POSITIONS:
        return "refused: %d positions are open already" % MAX_POSITIONS
    if now - b.get("cool", {}).get(p, 0) < COOLDOWN_H * 3600:
        return "refused: %s is in cooldown after its last sell" % p
    eq = equity(b, tk)
    if eq < _touch_peak(b, eq) * DRAWDOWN_HALT:
        why = "drawdown circuit breaker: equity %.2f is under %d%% of its peak %.2f" % (eq, DRAWDOWN_HALT * 100, b["peak"])
        store.write_halt(why)
        _defer_alert("HALT: drawdown circuit breaker", why)
        return "refused: " + why
    stop, take = d["stop_pct"], d["take_pct"]
    if take < MIN_TAKE_PCT:
        return "refused: fee gate, take_pct must be at least %.1f (round trip costs ~%.1f%%)" % (MIN_TAKE_PCT, FEE_RT_PCT)
    warns = []
    if take - FEE_RT_PCT < MIN_NET_RR * (stop + FEE_RT_PCT) - 1e-9:
        msg = "net win %.1f%% is smaller than net loss %.1f%% after fees (you must win more than %d%% of such trades)" % (
            take - FEE_RT_PCT, stop + FEE_RT_PCT, round(100 * (stop + FEE_RT_PCT) / (take + stop)))
        if rr_gate():
            return "refused: reward/risk gate, " + msg
        warns.append(msg)
    if isinstance(replay, dict) and replay.get("n", 0) >= REPLAY_MIN_N and replay.get("avg_net_pct", 0) < 0:
        msg = "this setup lost %.2f%% per trade over %d cases in the last 30 days" % (replay["avg_net_pct"], replay["n"])
        if replay_gate():
            return "refused: replay gate, " + msg
        warns.append(msg)
    lq, lwhy = liquid(p, tk)
    if not lq:
        return "refused: not liquid enough (%s)" % lwhy
    usdc = min(d["usdc"], b["cash"] * policy.MAX_TRADE_FRAC, eq * MAX_RISK_PCT / (stop + FEE_RT_PCT))
    price = tk[p]["bid"]
    vol = floor_to(usdc / (price * (1 + FEE_MAKER)), info["lot_decimals"])
    if usdc < max(info["costmin"], 1) or vol < info["ordermin"]:
        return "refused: below Kraken minimum order (%.2f USDC after the size caps, min volume %s)" % (usdc, info["ordermin"])
    ok, why = policy.check(b, usdc, "trade", "kraken", d["reason"], side="buy", mode=mode)
    if not ok:
        return "blocked by policy: %s" % why
    return {"side": "buy", "pair": p, "vol": vol, "price": price, "usdc": usdc, "stop_pct": stop, "take_pct": take,
            "last0": tk[p]["last"], "reason": d["reason"], "warnings": warns}


def _plan_sell(b, d, tk, info, mode, source, risk_event):
    p = d["pair"]
    held = b["pos"].get(p, 0.0)
    if held <= 0:
        return "refused: nothing to sell (no shorting)"
    g = b.get("guards", {}).get(p) or {}
    if source != "guard" and not risk_event and g.get("t") and time.time() - g["t"] < MIN_HOLD_H * 3600:
        return "refused: minimum hold %dh not reached (guard stop and take-profit still protect)" % MIN_HOLD_H
    q = floor_to(held * d["fraction"], info["lot_decimals"])
    if held - q < info["ordermin"]:
        q = floor_to(held, info["lot_decimals"])          # never leave a remainder too small to sell
    if q < info["ordermin"]:
        return "refused: below Kraken minimum volume"
    ok, why = policy.check(b, q * tk[p]["bid"], "trade", "kraken", d["reason"], side="sell", guard=source == "guard", mode=mode)
    if not ok:
        return "blocked by policy: %s" % why
    return {"side": "sell", "pair": p, "vol": q, "held": held, "stop_txid": g.get("stop_txid"), "stop": g.get("stop"),
            "reason": d["reason"]}


def _arm_stop(k, p, vol, price):
    """Place the server-side stop. Returns its txid, or None after two failures (the owner is alerted; the host
    guard still watches the position and sync_stops() keeps trying)."""
    err = None
    for _ in range(2):
        try:
            so = k.stop_order(p, vol, price)
            if so.get("txid") or not so.get("live"):
                return so.get("txid")
            err = "no txid"
        except Exception as e:
            err = str(e)[:150]
    _defer_alert("no server-side stop on %s" % p, "Kraken refused the stop-loss order: %s. The host guard still protects "
                 "the position while it runs." % err)
    return None


def live_exit(k, plan, info):
    """Sell on Kraken. The server-side stop reserves the coins, so it is cancelled FIRST; whatever it already sold is
    counted. Never raises after money moved: partial progress comes back in the result with `error` set."""
    p, dec = plan["pair"], info["lot_decimals"]
    done = {"qty": 0.0, "cost": 0.0, "fee": 0.0, "stop_txid": None, "error": None, "stop_cancelled": False}
    if plan.get("stop_txid"):
        q = k.cancel_confirmed(plan["stop_txid"])        # AmbiguousOrder goes up: nothing changed yet
        done.update(qty=q["vol_exec"], cost=q["cost"], fee=q["fee"], stop_cancelled=True)
    rest = floor_to(plan["vol"] - done["qty"], dec)
    if rest >= info["ordermin"]:
        try:
            r = k.order(p, "sell", rest)
            done["qty"] += r["vol"]; done["cost"] += r["cost"]; done["fee"] += r["fee"]
        except Exception as e:
            done["error"] = str(e)[:150]
            done["ambiguous"] = isinstance(e, kraken.AmbiguousOrder)
    left = floor_to(plan["held"] - done["qty"], dec)
    if left >= info["ordermin"] and plan.get("stop") and not done.get("ambiguous"):
        done["stop_txid"] = _arm_stop(k, p, left, plan["stop"])
    return done


def _place(k, plan, tk, info, mode, validate):
    """Network step. Returns a fill dict, or a string when nothing happened. Does not touch the ledger."""
    p = plan["pair"]
    if plan["side"] == "buy":
        if mode != "live":
            if validate:
                r = k.order_maker(p, "buy", plan["vol"], plan["price"])
                store.jlog({"validated": r["descr"]})
            return {"pending": True}
        r = k.order_maker(p, "buy", plan["vol"], plan["price"], wait=MAKER_WAIT_S)
        if not r["live"]:
            return "validated only (live orders off): %s" % r["descr"]
        if r["vol"] <= 0:
            return "maker entry not filled in time, cancelled, no trade (no chasing)"
        fill = {"qty": r["vol"], "cost": r["cost"], "fee": r["fee"], "stop_txid": None}
        if r["vol"] >= info["ordermin"]:
            fill["stop_txid"] = _arm_stop(k, p, r["vol"], r["cost"] / r["vol"] * (1 - plan["stop_pct"] / 100))
        return fill
    if mode != "live":
        if validate:
            store.jlog({"validated": k.order(p, "sell", plan["vol"])["descr"]})
        cost = plan["vol"] * tk[p]["bid"]
        return {"qty": plan["vol"], "cost": cost, "fee": cost * FEE_TAKER, "stop_txid": None}
    if not kraken.live_enabled():
        return "validated only (live orders off): %s" % k.order(p, "sell", plan["vol"])["descr"]
    return live_exit(k, plan, info)


def _count_trade(b, fee):
    now = time.time()
    b["fees"] += fee
    b["trades"] += 1
    b["trade_ts"] = [t for t in b.get("trade_ts", []) if t > now - 7 * 86400] + [now]
    b["last_action"] = now
    b["idle_strikes"] = 0


def _book_buy(b, p, qty, cost, fee, stop_pct, take_pct, stop_txid, info):
    b["cash"] -= cost + fee
    _count_trade(b, fee)
    entry = cost / qty
    if qty < info["ordermin"]:                            # a partial fill too small to sell: keep it on the books as dust
        b.setdefault("dust", {})[p] = b.get("dust", {}).get(p, 0.0) + qty
        return "buy %s %.8f @ ~%.4f (fee %.3f): partial fill below the minimum, kept as dust" % (p, qty, entry, fee)
    stop = entry * (1 - stop_pct / 100)
    b["pos"][p] = qty
    b.setdefault("guards", {})[p] = {"entry": entry, "basis": (cost + fee) / qty, "stop": stop,
                                     "take": entry * (1 + take_pct / 100), "high": entry, "stop_txid": stop_txid,
                                     "server_stop": stop if stop_txid else None, "t": time.time()}
    return "buy %s %.8f @ ~%.4f (fee %.3f, stop %.4f, take %.4f)" % (p, qty, entry, fee, stop, entry * (1 + take_pct / 100))


def _book_sell(b, p, qty, cost, fee, stop_txid, info):
    g = b.setdefault("guards", {}).get(p) or {}
    b["cash"] += cost - fee
    pnl = cost - fee - qty * g.get("basis", g.get("entry", cost / qty))
    b["realized"] = b.get("realized", 0.0) + pnl
    b["wins" if pnl > 0 else "losses"] = b.get("wins" if pnl > 0 else "losses", 0) + 1
    _count_trade(b, fee)
    b.setdefault("cool", {})[p] = time.time()
    rem = b["pos"].get(p, 0.0) - qty
    if rem < info["ordermin"]:
        b["pos"].pop(p, None)
        b["guards"].pop(p, None)
        if rem > 1e-12:
            b.setdefault("dust", {})[p] = b.get("dust", {}).get(p, 0.0) + rem
    else:
        b["pos"][p] = rem
        if g:
            g["stop_txid"] = stop_txid
            g["server_stop"] = g["stop"] if stop_txid else None
    return "sell %s %.8f @ ~%.4f (fee %.3f, pnl %+.3f)" % (p, qty, cost / qty, fee, pnl)


def _apply(b, plan, fill, info):
    p = plan["pair"]
    if plan["side"] == "buy":
        if fill.get("pending"):
            b["pending"] = {"pair": p, "price": plan["price"], "vol": plan["vol"], "stop_pct": plan["stop_pct"],
                            "take_pct": plan["take_pct"], "last0": plan["last0"], "t": time.time()}
            return "paper maker order working: buy %s %.8f @ %.4f (fills only if the market trades through it within %ds)" % (
                p, plan["vol"], plan["price"], MAKER_WAIT_S)
        return _book_buy(b, p, fill["qty"], fill["cost"], fill["fee"], plan["stop_pct"], plan["take_pct"], fill["stop_txid"], info)
    if fill["qty"] > 0:
        res = _book_sell(b, p, fill["qty"], fill["cost"], fill["fee"], fill.get("stop_txid"), info)
        return res + (" BUT: %s" % fill["error"] if fill.get("error") else "")
    g = b.get("guards", {}).get(p)
    if g and fill.get("stop_cancelled"):
        g["stop_txid"] = fill.get("stop_txid")
        g["server_stop"] = g["stop"] if fill.get("stop_txid") else None
    return "exit failed, position kept: %s" % fill.get("error")


def execute(k, name, d, tk, mode, validate=False, source="brain", risk_event=False, replay=None, tk_age=0.0):
    """Apply one decision for bot `name`. Returns a short result string ("buy ..."/"sell ..." on success).
    `source` is set by code ("brain" or "guard"); text written by the brain can never unlock a guard privilege."""
    d, err = parse_decision(d)
    if err:
        return "refused: bad decision (%s)" % err
    if d["action"] not in ("buy", "sell"):
        return "no trade"
    p = d["pair"]
    info = k.pairs().get(p)
    if not info or p not in tk:
        return "refused: pair is offline or has no price"
    try:
        with _PAIR_LOCKS[p]:
            with LOCK:
                s = store.load(mode)
                b = s["bots"].get(name)
                if b is None:
                    return "refused: unknown bot"
                plan = (_plan_buy(b, d, tk, info, mode, replay, tk_age) if d["action"] == "buy"
                        else _plan_sell(b, d, tk, info, mode, source, risk_event))
                if d["action"] == "buy":
                    store.save(mode, s)                    # keeps the equity peak
            if isinstance(plan, str):
                return plan
            try:
                fill = _place(k, plan, tk, info, mode, validate)
            except kraken.AmbiguousOrder as e:
                notify.halt("order state unknown on %s: %s" % (p, str(e)[:120]))
                store.jlog({"bot": name, "event": "ambiguous_order", "pair": p, "err": str(e)[:200]})
                return "order state unknown, HALT written: %s" % str(e)[:100]
            except Exception as e:
                store.jlog({"bot": name, "event": "order_failed", "pair": p, "side": plan["side"], "err": str(e)[:200]})
                return "order failed, nothing changed: %s" % str(e)[:100]
            if isinstance(fill, str):
                return fill
            with LOCK:
                s = store.load(mode)
                res = _apply(s["bots"][name], plan, fill, info)
                if plan.get("warnings"):
                    res += " | warning: " + "; ".join(plan["warnings"])
                store.save(mode, s)
            if fill.get("ambiguous"):
                notify.halt("market sell on %s has an unknown state: %s" % (p, fill.get("error")))
            store.request_save()
            return res
    finally:
        _flush_alerts()


def fill_pending(tk, mode):
    """Paper ledger only: an entry is a post-only bid. It fills only if the market really trades through it
    (the ask comes down to our price, or a NEW trade prints below it) within MAKER_WAIT_S; otherwise it expires.
    This keeps the paper results honest about unfilled and adversely-selected maker orders."""
    out = []
    with LOCK:
        s = store.load(mode)
        changed = False
        for b in s["bots"].values():
            pd = b.get("pending")
            if not pd:
                continue
            t = tk.get(pd["pair"])
            traded_through = t and (t["ask"] <= pd["price"] or (t["last"] != pd.get("last0") and t["last"] < pd["price"]))
            if traded_through and b["alive"]:
                cost = pd["vol"] * pd["price"]
                fee = cost * FEE_MAKER
                info = {"ordermin": 0.0}
                res = _book_buy(b, pd["pair"], pd["vol"], cost, fee, pd["stop_pct"], pd["take_pct"], None, info) \
                    if cost + fee <= b["cash"] else "paper entry dropped: not enough cash"
                b.pop("pending"); changed = True
                out.append((b["name"], res))
            elif time.time() - pd["t"] > MAKER_WAIT_S or not b["alive"]:
                b.pop("pending"); changed = True
                out.append((b["name"], "paper maker entry not filled in time, cancelled, no trade (no chasing)"))
        if changed:
            store.save(mode, s)
    for name, res in out:
        store.jlog({"bot": name, "paper_entry": res})
    return out


# ---------------------------------------------------------------- Kraken is the source of truth (live only)

def _sync_one(k, mode, name, p):
    info = k.pairs()[p]
    with LOCK:
        b = store.load(mode)["bots"][name]
        g = dict(b.get("guards", {}).get(p) or {})
        qty = b["pos"].get(p, 0.0)
    if not g or qty < info["ordermin"]:
        return None
    txid, fired, new, rearm = g.get("stop_txid"), None, None, False
    if txid:
        q = k.query_order(txid)
        if q["status"] in kraken.FINAL:
            if q["vol_exec"] > 0:
                fired = q
            else:
                rearm = True
        elif g["stop"] > (g.get("server_stop") or 0) * (1 + STOP_STEP):      # the trailing stop moved up
            q = k.cancel_confirmed(txid)
            if q["vol_exec"] > 0:
                fired = q
            else:
                rearm = True
    else:
        rearm = True
    if not fired and not rearm:
        return None                                        # the stop is open on Kraken at the right level
    if rearm:
        new = _arm_stop(k, p, qty, g["stop"])
    with LOCK:
        s = store.load(mode)
        b = s["bots"][name]
        if p not in b["pos"]:
            return None
        if fired:
            res = _book_sell(b, p, min(fired["vol_exec"], b["pos"][p]), fired["cost"], fired["fee"], None, info)
            store.jlog({"bot": name, "event": "server_stop_fired", "pair": p, "result": res})
            _defer_alert("stop fired on %s" % p, "Kraken executed the stop-loss: %s" % res, )
        else:
            gg = b["guards"][p]
            gg["stop_txid"] = new
            gg["server_stop"] = gg["stop"] if new else None
            res = "stop re-armed" if new else "stop NOT armed"
        store.save(mode, s)
    store.request_save()
    return res


def sync_stops(k, mode):
    """Mirror Kraken's stop orders into the ledger: a stop that fired closes the position, a stop that vanished is
    re-armed, a trailing stop that moved up replaces the order on Kraken. Call every ~15s (live only)."""
    with LOCK:
        todo = [(b["name"], p) for b in store.load(mode)["bots"].values() for p in b.get("guards", {}) if p in b["pos"]]
    out = []
    for name, p in todo:
        lock = _PAIR_LOCKS[p]
        if not lock.acquire(blocking=False):
            continue                                       # an order is in flight on this coin: next pass
        try:
            r = _sync_one(k, mode, name, p)
            if r:
                out.append((name, p, r))
        except Exception as e:
            store.jlog({"bot": name, "event": "sync_stops_error", "pair": p, "err": str(e)[:200]})
        finally:
            lock.release()
            _flush_alerts()
    return out


def reconcile(k, mode, tk):
    """Compare the real Kraken account with the ledger, asset by asset (a missing position cannot hide behind an
    equal total). A gap writes HALT and alerts. Returns (ok, problems)."""
    if busy():
        return True, ["skipped: an order is in flight"]
    bal = k.balance()
    if busy():
        return True, ["skipped: an order is in flight"]
    info = k.pairs()
    with LOCK:
        bots = list(store.load(mode)["bots"].values())
    led_eq = sum(equity(b, tk) for b in bots)
    tol = max(1.0, 0.02 * led_eq)
    problems = []
    for p in info:
        real = bal.get(info[p]["base"], 0.0)
        led = sum(b["pos"].get(p, 0.0) + b.get("dust", {}).get(p, 0.0) for b in bots)
        px = tk.get(p, {}).get("bid", 0.0)
        if abs(real - led) * px > tol:
            problems.append("%s: kraken %.8f vs ledger %.8f" % (p, real, led))
    cash = sum(b["cash"] for b in bots)
    if abs(bal.get(kraken.QUOTE, 0.0) - cash) > tol:
        problems.append("%s: kraken %.2f vs ledger %.2f" % (kraken.QUOTE, bal.get(kraken.QUOTE, 0.0), cash))
    ok = not problems
    store.jlog({"event": "reconcile", "ok": ok, "problems": problems, "ledger_equity": round(led_eq, 2)})
    if not ok:
        notify.halt("reconcile mismatch: " + "; ".join(problems))
    return ok, problems


def boot_sync(k, mode, tk):
    """Live start-up: cancel every open order the ledger does not know (a maker entry left by a killed run),
    mirror the stops, then reconcile."""
    with LOCK:
        known = {g.get("stop_txid") for b in store.load(mode)["bots"].values() for g in b.get("guards", {}).values()}
    for txid in list(k.open_orders()):
        if txid not in known:
            q = k.cancel_confirmed(txid)
            store.jlog({"event": "boot_cancelled_unknown_order", "txid": txid, "vol_exec": q["vol_exec"]})
    sync_stops(k, mode)
    return reconcile(k, mode, tk)


def adopt(k, mode="live"):
    """Owner tool, run by hand after a HALT you understand: cancel all open orders and rebuild the ledger from the
    real account (cash and coins), with a fresh 3% stop on every position. Does not remove the HALT file."""
    for txid in list(k.open_orders()):
        k.cancel_confirmed(txid)
    bal, info, tk = k.balance(), k.pairs(), k.tickers()
    with LOCK:
        s = store.load(mode, bal.get(kraken.QUOTE, 0.0))
        b = s["bots"]["shachar"]
        for other in s["bots"].values():
            if other is not b:
                other.update(cash=0.0, pos={}, dust={}, guards={}, alive=False)
        b.update(cash=bal.get(kraken.QUOTE, 0.0), pos={}, dust={}, guards={})
        b.pop("pending", None)
        for p, i in info.items():
            qty = bal.get(i["base"], 0.0)
            if qty <= 0 or p not in tk:
                continue
            if qty < i["ordermin"]:
                b["dust"][p] = qty
                continue
            px = tk[p]["bid"]
            b["pos"][p] = qty
            b["guards"][p] = {"entry": px, "basis": px, "stop": px * 0.97, "take": px * 1.07, "high": px,
                              "stop_txid": None, "server_stop": None, "t": time.time()}
        if not store.exists(mode):
            b["start"] = b["peak"] = equity(b, tk)
        store.save(mode, s)
    if kraken.live_enabled():
        sync_stops(k, mode)
    status(mode)


# ---------------------------------------------------------------- the brain cycle

def ensure_state(k, mode):
    if store.exists(mode):
        return
    start = k.balance().get(kraken.QUOTE, 0.0) if mode == "live" else DRY_START
    with LOCK:
        if not store.exists(mode):
            store.save(mode, store.load(mode, start))


def flatten(k, name, tk, mode, validate, why):
    with LOCK:
        pairs = list(store.load(mode)["bots"][name]["pos"])
    return [(p, execute(k, name, {"action": "sell", "pair": p, "fraction": 1.0, "reason": "flatten: " + why},
                        tk, mode, validate, source="guard")) for p in pairs]


def _bot_cycle(k, name, mode, validate, tk, mk, candles, nw, scan, live_tk):
    now, die = time.time(), None
    with LOCK:
        s = store.load(mode)
        b = s["bots"][name]
        if not b["alive"]:
            return
        eq = equity(b, tk)
        _touch_peak(b, eq)
        if now - b["last_action"] > IDLE_HOURS * 3600:
            b["idle_strikes"] += 1
            b["last_action"] = now
            s["log"].append("%s idle strike %d" % (name, b["idle_strikes"]))
            if b["idle_strikes"] >= MAX_IDLE and idle_death(mode):
                die = "%d idle strikes" % b["idle_strikes"]
        if eq < DEATH_FLOOR:
            die = "equity %.2f is below the death floor" % eq
        store.save(mode, s)
    if die:
        sold = flatten(k, name, tk, mode, validate, die)          # sell first: never abandon coins on the exchange
        with LOCK:
            s = store.load(mode)
            s["bots"][name]["alive"] = False
            s["bots"][name].pop("pending", None)
            s["log"].append("%s DIED %s (%s)" % (name, time.strftime("%Y-%m-%d %H:%M:%S"), die))
            store.save(mode, s)
        store.jlog({"bot": name, "event": "died", "why": die, "flatten": sold})
        notify.alert("%s died: %s" % (name, die), json.dumps(sold))
        return

    profit = eq - b["start"]
    want_child = mode != "live" and eq >= b["start"] * SPAWN_AT and len(b["children"]) < MAX_CHILDREN
    ctx = {"time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "market": mk, "news": nw, "lessons": lessons_tail(),
           "me": {"name": name, "cash": round(b["cash"], 2), "positions": {p: round(q, 8) for p, q in b["pos"].items()},
                  "guards": {p: {"entry": round(g["entry"], 6), "stop": round(g["stop"], 6), "take": round(g["take"], 6),
                                 "held_h": round((now - g.get("t", now)) / 3600, 1)} for p, g in b.get("guards", {}).items()},
                  "equity": round(eq, 2), "start": round(b["start"], 2), "peak": round(b["peak"], 2),
                  "profit": round(profit, 2), "realized": round(b.get("realized", 0.0), 2), "wins": b.get("wins", 0),
                  "losses": b.get("losses", 0), "fees_paid": round(b["fees"], 2), "idle_strikes": b["idle_strikes"],
                  "idle_strikes_are_fatal": idle_death(mode),
                  "hours_since_last_trade": round((now - b["last_action"]) / 3600, 1), "trades_total": b["trades"],
                  "death_floor": DEATH_FLOOR, "recent_results": b.get("recent", []), "halted": store.halted() or notify.remote_halt(mode)}}
    warm = scan is not None and scan.get("history_seconds", 0) >= WARMUP_S
    if scan:
        ctx["scan"] = scan
    opps = opportunities(mk, scan, tk) if warm else []
    if not warm:
        ctx["note"] = "scanner has less than an hour of prices: no setups are listed yet"
    replays = {}
    for o in opps[:3]:
        bt = backtest_setup(candles.get(o["pair"]) or [], o["setup"])
        o["backtest_30d"] = bt or "too few cases"
        if bt:
            replays[o["pair"]] = bt
    ctx["opportunities"] = opps
    ctx["btc_alert"] = btc_alert(mk, scan)
    peg_warn = False
    try:
        peg = k.usdc_peg()
        peg_warn = abs(peg - 1) > 0.003
        ctx["usdc_peg"] = {"usdc_usd": round(peg, 4), "warning": "USDC off peg, your cash may be at risk" if peg_warn else "ok"}
    except Exception:
        ctx["usdc_peg"] = {"warning": "unavailable"}
    c1, c6 = btc_moves(mk, scan)
    risk_event = peg_warn or (c1 is not None and c1 <= -2) or (c6 is not None and c6 <= -5)
    ctx["risk_event"] = risk_event
    ctx["order_books"] = {}
    for p in dict.fromkeys([o["pair"] for o in opps[:3]] + [p for p in b["pos"] if p in tk]):
        try:
            ctx["order_books"][p] = k.depth(p)
        except Exception:
            pass
    ctx["cooldown_pairs"] = [p for p, t0 in b.get("cool", {}).items() if now - t0 < COOLDOWN_H * 3600]
    ctx["not_liquid"] = [p for p in mk if p in tk and not liquid(p, tk)[0]]

    is_busy = bool(opps) or bool(b["pos"]) or ctx["btc_alert"] != "none"
    wait_s = BRAIN_MIN_BUSY_S if is_busy else BRAIN_MIN_IDLE_S
    if now - b.get("last_brain", 0) < wait_s:
        d = {"action": "hold", "reason": "gate: brain not called (free quota is limited)", "source": "gate"}
    elif now - b.get("last_brain_err", 0) < BRAIN_ERR_BACKOFF_S:
        d = {"action": "hold", "reason": "gate: waiting after an LLM error", "source": "gate"}
    else:
        d = brain.decide(ctx)                 # slow (LLM). No lock is held; the guard keeps running
    if not isinstance(d, dict):
        d = {"action": "hold", "reason": "brain returned no object", "source": "llm_error"}
    src = d.get("source")

    tk2, age = live_tk() if live_tk else (tk, 0.0)
    res = execute(k, name, d, tk2 or tk, mode, validate, source="brain", risk_event=risk_event,
                  replay=replays.get(d.get("pair")), tk_age=age)

    with LOCK:
        s = store.load(mode)
        b = s["bots"][name]
        if src != "gate":                     # the brain sees what happened to its last proposals, refusals included
            b["recent"] = (b.get("recent") or [])[-5:] + [{"t": time.strftime("%Y-%m-%d %H:%M"), "action": d.get("action"),
                                                           "pair": d.get("pair"), "result": str(res)[:220]}]
        if src in ("llm_error", "nokey"):
            b["last_brain_err"] = time.time()
        elif src != "gate":
            b["last_brain"] = time.time()
        eq = equity(b, tk)
        profit = eq - b["start"]
        if want_child and profit * CHILD_STAKE >= MIN_CHILD and b["cash"] >= profit * CHILD_STAKE and b["alive"]:
            stake = profit * CHILD_STAKE
            cn = "%s.%d" % (name, len(b["children"]) + 1)
            b["cash"] -= stake
            b["children"].append(cn)
            b["start"] = eq - stake
            b["peak"] = b["start"]
            s["bots"][cn] = store.newbot(cn, stake, name)
            s["log"].append("%s spawned %s with %.2f USDC" % (name, cn, stake))
            store.jlog({"bot": name, "event": "spawned", "child": cn, "stake": round(stake, 2)})
        s["log"] = s["log"][-200:]
        store.save(mode, s)
    if d.get("lesson") and src != "gate":
        with store.path("lessons.md").open("a", encoding="utf-8") as f:
            f.write("- %s [%s] %s\n" % (time.strftime("%Y-%m-%d"), name, " ".join(str(d["lesson"]).split())[:300]))
    if d.get("action") == "idea":
        with store.path("ideas_proposed.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"bot": name, "t": time.strftime("%Y-%m-%d %H:%M:%S"), "idea": str(d.get("reason"))[:1000]}) + "\n")
    if src != "gate" or res != "no trade":
        store.jlog({"bot": name, "decision": d, "result": res, "equity": round(eq, 2)})


def cycle(mode, validate=False, scan=None, k=None, live_tk=None):
    k = k or kraken.Kraken()
    ensure_state(k, mode)
    if mode == "live" and not kraken.live_enabled():
        print("LIVE orders are off (SHACHAR_LIVE not set). Orders will be validated only, nothing is placed.")
    tk = k.tickers()
    mk, candles = market(k, tk)
    nw = {"fear_greed": news.fear_greed()}
    hl = news.headlines()
    if hl:
        nw["headlines_untrusted"] = hl
    with LOCK:
        names = list(store.load(mode)["bots"])
    for name in names:
        try:
            _bot_cycle(k, name, mode, validate, tk, mk, candles, nw, scan, live_tk)
        except Exception:
            store.jlog({"bot": name, "event": "cycle_error", "err": traceback.format_exc()[-400:]})
    status(mode, tk=tk)


def status(mode="dry", tk=None):
    tk = tk or kraken.Kraken().tickers()
    with LOCK:
        s = store.load(mode, DRY_START)
    for b in s["bots"].values():
        print("%-10s %-5s equity %.2f USDC (start %.2f, peak %.2f) cash %.2f realized %+.2f fees %.2f trades %d (W%d/L%d) idle %d %s%s" % (
            b["name"], "alive" if b["alive"] else "DEAD", equity(b, tk), b["start"], b.get("peak") or b["start"], b["cash"],
            b.get("realized", 0.0), b["fees"], b["trades"], b.get("wins", 0), b.get("losses", 0), b["idle_strikes"],
            list(b["pos"]), " HALT" if store.halted() else ""))


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd, mode = (a[0] if a else "status"), (a[1] if len(a) > 1 else "dry")
    if cmd == "cycle":
        cycle(mode, "--validate" in a)
    elif cmd == "reconcile":
        kk = kraken.Kraken()
        print(reconcile(kk, mode, kk.tickers()))
    elif cmd == "adopt":
        adopt(kraken.Kraken(), mode)
    else:
        status(mode)
