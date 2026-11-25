"""Always-on loop. Three threads:
- guard (this loop): every TICK seconds it reads all prices and protects open positions (stop, trailing stop,
  take-profit, time stop, hard equity stop, owner FLATTEN). It never opens a position.
- brain: every BRAIN_EVERY seconds one brain cycle (LLM). Slow, never blocks the guard.
- housekeeping: owner controls every minute; in live mode it mirrors Kraken's stop orders every 15s and
  reconciles the ledger with the real account every 10 minutes.
  python guard.py dry                 paper ledger, no orders at all
  python guard.py dry --validate      also validates orders with Kraken (nothing is placed)
  python guard.py live                refuses to place real orders unless SHACHAR_LIVE=YES_IDAN_APPROVED
Env: BRAIN_EVERY (default 300), TICK (default 2), MAX_TICKS (tests), NO_BRAIN=1.
SIGTERM/Ctrl-C: stops cleanly, working maker orders are cancelled before exit."""
import sys, os, time, threading, signal, traceback
import kraken, scanner, shachar, store, notify
from store import LOCK

TRAIL_START, TRAIL_PCT = 0.04, 0.02        # after +4% the stop trails 2% under the high
HARD_EQUITY_STOP = 0.5                     # equity below 50% of start: sell everything to USDC and HALT
MAX_HOLD_H = 48                            # time stop
STOP_CONFIRM_TICKS = 2                     # a stop needs two ticks in a row under the level (one bad print is ignored)
BRAIN_EVERY = int(os.environ.get("BRAIN_EVERY", "300"))
TICK = float(os.environ.get("TICK", "2"))

_below = {}        # (bot, pair) -> ticks in a row under the stop
_backoff = {}      # (bot, pair) -> do not retry an exit before this time


def protect(k, tk, mode, validate):
    """One guard pass. Decides under the state lock, sells outside it. Returns the actions taken."""
    exits, now = [], time.time()
    flat_all = notify.remote_flatten()
    with LOCK:
        s = store.load(mode)
        changed = False
        for b in s["bots"].values():
            if not b["pos"]:
                continue
            gd = b.setdefault("guards", {})
            eq = shachar.equity(b, tk)
            hard = eq < b["start"] * HARD_EQUITY_STOP
            for p in list(b["pos"]):
                if p not in tk:
                    continue
                px = tk[p]["bid"]
                g = gd.get(p)
                if g is None:                 # a position without a guard record (old state): protect it from here
                    g = gd[p] = {"entry": px, "basis": px, "stop": px * 0.97, "take": px * 1.07, "high": px,
                                 "stop_txid": None, "server_stop": None, "t": now}
                    changed = True
                if px > g["high"]:
                    g["high"] = px; changed = True
                if g["high"] >= g["entry"] * (1 + TRAIL_START):
                    ns = g["high"] * (1 - TRAIL_PCT)
                    if ns > g["stop"]:
                        g["stop"] = ns; changed = True
                key = (b["name"], p)
                _below[key] = _below.get(key, 0) + 1 if px <= g["stop"] else 0
                why = None
                if not b["alive"]:
                    why = "bot is dead, closing its position"
                elif flat_all:
                    why = "owner FLATTEN"
                elif hard:
                    why = "hard equity stop (equity %.2f < %d%% of start)" % (eq, HARD_EQUITY_STOP * 100)
                elif _below[key] >= STOP_CONFIRM_TICKS:
                    why = "stop hit (%.6f <= %.6f)" % (px, g["stop"])
                elif g.get("t") and now - g["t"] > MAX_HOLD_H * 3600:
                    why = "time stop (held more than %dh)" % MAX_HOLD_H
                elif px >= g["take"]:
                    why = "take-profit hit (%.6f >= %.6f)" % (px, g["take"])
                if why and now >= _backoff.get(key, 0):
                    exits.append((b["name"], p, why, hard))
        if changed:
            store.save(mode, s)
    acts = []
    for name, p, why, hard in exits:
        r = shachar.execute(k, name, {"action": "sell", "pair": p, "fraction": 1.0, "reason": "guard: " + why},
                            tk, mode, validate, source="guard")
        store.jlog({"bot": name, "guard_exit": why, "pair": p, "result": r})
        if r.startswith("sell"):
            _below.pop((name, p), None)
            notify.alert("guard exit %s: %s" % (p, why.split(" (")[0]), r, dedupe_s=60)
        else:
            _backoff[(name, p)] = time.time() + (300 if r.startswith("validated") else 20)
            if not r.startswith("validated"):
                notify.alert("guard exit FAILED on %s" % p, "%s\nreason: %s\nThe Kraken stop order (if armed) still protects." % (r, why))
        if hard and not store.halted():
            notify.halt("hard equity stop: equity fell under %d%% of start" % (HARD_EQUITY_STOP * 100))
        acts.append((name, p, why, r))
    return acts


def brain_loop(k, sc, mode, validate, stop):
    while not stop.is_set():
        try:
            shachar.cycle(mode, validate, scan=sc.summary(), k=k, live_tk=lambda: (sc.tk, sc.age()))
        except Exception:
            store.jlog({"event": "brain_error", "err": traceback.format_exc()[-400:]})
        stop.wait(BRAIN_EVERY)


def housekeeping(k, sc, mode, stop):
    n = 0
    live = mode == "live" and kraken.live_enabled()
    while not stop.is_set():
        try:
            if n % 4 == 0:
                notify.refresh_flags()
            if live:
                shachar.sync_stops(k, mode)
                if n % 40 == 39 and sc.tk:
                    shachar.reconcile(k, mode, sc.tk)
        except Exception:
            store.jlog({"event": "housekeeping_error", "err": traceback.format_exc()[-300:]})
        n += 1
        stop.wait(15)


def main(mode="dry", validate=False):
    k = kraken.Kraken()
    sc = scanner.Scanner(k)
    stop = threading.Event()

    def _term(*_):
        stop.set(); kraken.ABORT.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _term)
        except Exception:
            pass

    shachar.ensure_state(k, mode)
    print("seeded 1m history for %d pairs" % sc.seed())
    sc.tick()
    notify.refresh_flags()
    if mode == "live" and kraken.live_enabled():
        try:
            ok, problems = shachar.boot_sync(k, mode, sc.tk)
            print("boot reconcile:", "ok" if ok else problems)
        except Exception as e:
            notify.halt("boot sync failed: %s" % str(e)[:120])
    threads = [threading.Thread(target=housekeeping, args=(k, sc, mode, stop), daemon=True)]
    if os.environ.get("NO_BRAIN") != "1":
        threads.append(threading.Thread(target=brain_loop, args=(k, sc, mode, validate, stop), daemon=True))
    for t in threads:
        t.start()

    n, errs = 0, 0
    max_ticks = int(os.environ.get("MAX_TICKS", "0"))
    while not stop.is_set():
        t0 = time.time()
        try:
            sc.tick()
            if mode != "live":
                for a in shachar.fill_pending(sc.tk, mode):
                    print("PAPER", a)
            for a in protect(k, sc.tk, mode, validate):
                print("GUARD", a)
            if n % 10 == 0:
                sc.dump()
            errs = 0
        except Exception as e:
            errs += 1
            print("tick error", str(e)[:120])
            if errs in (5, 30):
                store.jlog({"event": "guard_tick_errors", "count": errs, "last": str(e)[:120]})
            if errs == 30:
                notify.alert("guard is blind", "%d price reads in a row failed: %s\nKraken stop orders still protect open positions." % (errs, str(e)[:120]))
        n += 1
        if max_ticks and n >= max_ticks:
            _term()
            break
        stop.wait(max(0.0, TICK - (time.time() - t0)))

    deadline = time.time() + 150           # let an order in flight finish (a working maker order cancels itself on ABORT)
    while shachar.busy() and time.time() < deadline:
        time.sleep(1)
    print("guard stopped cleanly" if not shachar.busy() else "guard stopped with an order still in flight")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0] if a else "dry", "--validate" in a)
