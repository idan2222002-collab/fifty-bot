"""Always-on loop. Fast layer: every second it reads prices (scanner) and protects open positions
(stop, trailing stop, take-profit, hard equity stop, survival check). It never opens a position.
Slow layer: every BRAIN_EVERY seconds it runs one brain cycle (LLM) in a background thread.
  python guard.py dry                 simulated ledger, no orders at all
  python guard.py dry --validate      also validates orders with Kraken (nothing is placed)
  python guard.py live                refuses to place real orders unless SHACHAR_LIVE=YES_IDAN_APPROVED
Env: BRAIN_EVERY (seconds, default 300), TICK (seconds, default 1), MAX_TICKS (for tests)."""
import sys, os, time, threading, json, traceback
import kraken, scanner, shachar

TRAIL_START, TRAIL_PCT = 0.04, 0.02        # after +4% the stop trails 2% under the high, so a trailing exit nets above the 1.6% fees
HARD_EQUITY_STOP = 0.4                     # equity below 40% of start: sell everything to USDC
BRAIN_EVERY = int(os.environ.get("BRAIN_EVERY", "300"))
MAX_HOLD_H = 48                            # time stop
TICK = float(os.environ.get("TICK", "1"))


def protect(k, sc, mode, validate):
    """One guard pass. Returns a list of actions taken."""
    acts = []
    tk = sc.tk
    with shachar.LOCK:
        s = shachar.load(mode, shachar.DRY_START)
        changed = False
        for b in s["bots"].values():
            if not b["alive"] or not b["pos"]:
                continue
            gd = b.setdefault("guards", {})
            eq = shachar.equity(b, tk)
            hard = eq < b["start"] * HARD_EQUITY_STOP
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
                if hard:
                    why = "hard equity stop (equity %.2f < %d%% of start)" % (eq, HARD_EQUITY_STOP * 100)
                elif px <= g["stop"]:
                    why = "stop hit (%.4f <= %.4f)" % (px, g["stop"])
                elif g.get("t") and time.time() - g["t"] > MAX_HOLD_H * 3600:
                    why = "time stop (held more than %dh)" % MAX_HOLD_H
                elif px >= g["take"]:
                    why = "take-profit hit (%.4f >= %.4f)" % (px, g["take"])
                if why:
                    r = shachar.execute(k, s, b, {"action": "sell", "pair": p, "fraction": 1.0, "reason": "guard: " + why},
                                        tk, mode, validate)
                    shachar.jlog({"bot": b["name"], "guard_exit": why, "result": r})
                    acts.append((b["name"], p, why, r)); changed = True
        if changed:
            shachar.sfile(mode).write_text(json.dumps(s, indent=1))
    return acts


def brain_loop(sc, mode, validate, stop):
    while not stop.is_set():
        try:
            shachar.cycle(mode, validate, scan=sc.summary())
        except Exception:
            shachar.jlog({"event": "brain_error", "err": traceback.format_exc()[-300:]})
        stop.wait(BRAIN_EVERY)


def main(mode="dry", validate=False):
    k = kraken.Kraken()
    sc = scanner.Scanner(k)
    stop = threading.Event()
    sc.tick()
    if os.environ.get("NO_BRAIN") != "1":
        threading.Thread(target=brain_loop, args=(sc, mode, validate, stop), daemon=True).start()
    n, errs = 0, 0
    max_ticks = int(os.environ.get("MAX_TICKS", "0"))
    while True:
        t0 = time.time()
        try:
            sc.tick()
            for a in protect(k, sc, mode, validate):
                print("GUARD", a)
            if n % 10 == 0:
                sc.dump()
            errs = 0
        except Exception as e:
            errs += 1
            print("tick error", str(e)[:120])
            if errs in (5, 60):
                shachar.jlog({"event": "guard_tick_errors", "count": errs, "last": str(e)[:120]})
        n += 1
        if max_ticks and n >= max_ticks:
            stop.set(); break
        time.sleep(max(0.0, TICK - (time.time() - t0)))


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0] if a else "dry", "--validate" in a)
