"""Always-on loop. Fast layer: every second it reads prices (scanner) and protects open positions
(stop, trailing stop, take-profit, time stop, drawdown breaker). It never opens a position.
Slow layer: every BRAIN_EVERY seconds it runs one brain cycle (LLM) in a background thread.
  python guard.py dry                 simulated ledger, no orders at all
  python guard.py dry --validate      also validates orders with Kraken (nothing is placed)
  python guard.py live                refuses to place real orders unless SHACHAR_LIVE=YES_IDAN_APPROVED
Env: BRAIN_EVERY (seconds, default 300), TICK (seconds, default 2), MAX_TICKS (for tests)."""
import sys, os, time, threading, json, traceback, signal
import kraken, scanner, shachar

TRAIL_START, TRAIL_PCT = 0.04, 0.02        # after +4% the stop trails 2% under the high, so a trailing exit nets above the 1.6% fees
DRAWDOWN_HALT = 0.20                       # equity 20% below its peak: write HALT (no new buys). No sell-all: stops keep protecting.
BRAIN_EVERY = int(os.environ.get("BRAIN_EVERY", "300"))
MAX_HOLD_H = 48                            # time stop
TICK = float(os.environ.get("TICK", "2"))
RETRY_S = 10                               # wait after a failed exit before trying that pair again
SAVE_EVERY_S = 300


def protect(k, sc, mode, validate):
    """One guard pass. Decides exits under LOCK, then executes them WITHOUT holding LOCK. Returns actions taken."""
    acts, todo = [], []
    tk = sc.tk
    with shachar.LOCK:
        s = shachar.load(mode, shachar.DRY_START)
        changed = False
        for b in s["bots"].values():
            if not b["pos"]:
                continue                      # dead bots with positions are still protected
            gd = b.setdefault("guards", {})
            eq = shachar.equity(b, tk)
            if eq > b.get("peak", b["start"]):
                b["peak"] = eq
            if eq < b.get("peak", b["start"]) * (1 - DRAWDOWN_HALT) and not (shachar.HERE / "HALT").exists():
                shachar.write_halt("drawdown breaker: equity %.2f is >%d%% below peak %.2f" % (eq, DRAWDOWN_HALT * 100, b["peak"]))
            for p in list(b["pos"]):
                if p not in tk:
                    continue
                px = tk[p]["bid"]
                g = gd.setdefault(p, {"entry": px, "stop": px * 0.97, "take": px * 1.05, "high": px, "stop_txid": None})
                if px > g["high"]:
                    g["high"] = px; changed = True
                if g["high"] >= g["entry"] * (1 + TRAIL_START):
                    ns = g["high"] * (1 - TRAIL_PCT)
                    if ns > g["stop"]:
                        g["stop"] = ns; changed = True
                why = None
                if px <= g["stop"]:
                    why = "stop hit (%.4f <= %.4f)" % (px, g["stop"])
                elif g.get("t") and time.time() - g["t"] > MAX_HOLD_H * 3600:
                    why = "time stop (held more than %dh)" % MAX_HOLD_H
                elif px >= g["take"]:
                    why = "take-profit hit (%.4f >= %.4f)" % (px, g["take"])
                if why and time.time() - g.get("retry_t", 0) >= RETRY_S:
                    todo.append((b, p, why))
    for b, p, why in todo:
        r = shachar.execute(k, s, b, {"action": "sell", "pair": p, "fraction": 1.0, "reason": "guard: " + why},
                            tk, mode, validate, internal=True)
        if not r.startswith("sell"):
            with shachar.LOCK:
                g = b.get("guards", {}).get(p)
                if g is not None:
                    g["retry_t"] = time.time()
        shachar.jlog({"bot": b["name"], "guard_exit": why, "result": r})
        if r.startswith("sell"):
            shachar.alert("sold %s: %s" % (p, why.split(" (")[0]), "%s\nreason: %s" % (r, why), mode)
        elif not r.startswith("validated"):
            shachar.alert("exit FAILED on %s" % p, "%s\nreason: %s\nIf a Kraken stop is armed it still protects the position."
                          % (r, why), mode)
        acts.append((b["name"], p, why, r)); changed = True
    if changed:
        shachar.save(mode, s)
    return acts


def shutdown(mode, wait_s=120):
    """Clean stop. The host ends every run with a kill, and until now that could land in the middle of an order:
    a maker entry left resting on Kraken with nobody watching it, or a fill that was never written down. Here the
    resting entry is cancelled at once (kraken.ABORT), an order in flight gets time to finish and be booked, and the
    state is written one last time (in paper mode it was otherwise up to 5 minutes old).
    Returns True when nothing was left in flight."""
    kraken.ABORT.set()
    end = time.time() + wait_s
    while shachar.INFLIGHT and time.time() < end:
        time.sleep(0.2)
    try:
        shachar.save(mode)
    except Exception as e:
        print("final save failed", str(e)[:120])
    shachar.notify.drain(8)                   # let queued alerts go out before the process ends
    return not shachar.INFLIGHT


def brain_loop(sc, mode, validate, stop):
    while not stop.is_set():
        try:
            shachar.cycle(mode, validate, scan=sc.summary())
        except Exception:
            shachar.jlog({"event": "brain_error", "err": traceback.format_exc()[-300:]})
        stop.wait(BRAIN_EVERY)


def main(mode="dry", validate=False):
    k = kraken.get()
    sc = scanner.Scanner(k)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):       # `kill` from the workflow, Ctrl-C by hand
        try:
            signal.signal(sig, lambda *_: stop.set())
        except Exception:
            pass
    print("scanner seeded with 1-minute history for %d pairs" % sc.seed())
    sc.tick()
    if mode == "live" and kraken.live_enabled():
        shachar.boot_sync(k, shachar.load(mode, 0.0), sc.tk, mode)
    if mode != "live":
        shachar.PAPER_PENDING = True          # this loop watches paper entries, so they can fill honestly
    if os.environ.get("NO_BRAIN") != "1":
        threading.Thread(target=brain_loop, args=(sc, mode, validate, stop), daemon=True).start()
    n, errs, last_save = 0, 0, time.time()
    max_ticks = int(os.environ.get("MAX_TICKS", "0"))
    while not stop.is_set():
        t0 = time.time()
        try:
            sc.tick()
            if mode != "live":
                for a in shachar.fill_pending(sc.tk, mode):
                    print("PAPER", a)
            for a in protect(k, sc, mode, validate):
                print("GUARD", a)
            if n % 10 == 0:
                sc.dump()
            if mode == "live" and kraken.live_enabled() and n % 30 == 15:
                shachar.sync_stops(k, mode, sc.tk)
            if mode == "live" and kraken.live_enabled() and n % 300 == 299:
                shachar.reconcile(k, shachar.load(mode, 0.0), sc.tk)
            if time.time() - last_save > SAVE_EVERY_S:
                shachar.save(mode); last_save = time.time()
            errs = 0
        except Exception as e:
            errs += 1
            print("tick error", str(e)[:120])
            if errs in (5, 60):
                shachar.jlog({"event": "guard_tick_errors", "count": errs, "last": str(e)[:120]})
            if errs == 60:
                shachar.alert("guard is blind", "%d price reads in a row failed: %s\nKraken stop orders still protect open "
                              "positions." % (errs, str(e)[:120]), mode, always=True)
        n += 1
        if max_ticks and n >= max_ticks:
            stop.set(); break
        stop.wait(max(0.0, TICK - (time.time() - t0)))
    clean = shutdown(mode)
    print("guard stopped cleanly" if clean else "guard stopped with an order still in flight")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0] if a else "dry", "--validate" in a)
