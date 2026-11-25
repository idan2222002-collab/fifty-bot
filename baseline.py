"""Is there any edge? Compare, with the SAME exit rules and the SAME fees:
  (a) daily trend following (in the coin above its 50-day average, in USDC below) vs buy and hold,
  (b) entries on the three setups, (c) entries at random moments.
If the setups do not beat random entries by a clear margin, the entries add nothing and only the fees are certain.
Run: python baseline.py [PAIR ...]      (public Kraken data only, no key)"""
import sys, random, statistics
import kraken, shachar

FEE_SIDE = shachar.FEE_TAKER          # conservative for the trend test: taker on both sides


def trend_vs_hold(days, n=50):
    cl = [d["c"] for d in days[:-1]]                        # drop the candle still forming
    if len(cl) < n + 30:
        return None
    eq, pos, entries = 1.0, False, 0
    for i in range(n, len(cl)):
        want = cl[i - 1] > sum(cl[i - n:i]) / n             # decided on yesterday's close, held through today
        if want != pos:
            eq *= 1 - FEE_SIDE
            entries += want
            pos = want
        if pos:
            eq *= cl[i] / cl[i - 1]
    peak, dd, e, p2 = 1.0, 0.0, 1.0, False
    for i in range(n, len(cl)):                              # max drawdown of the trend curve
        w = cl[i - 1] > sum(cl[i - n:i]) / n
        if w != p2:
            e *= 1 - FEE_SIDE; p2 = w
        if p2:
            e *= cl[i] / cl[i - 1]
        peak = max(peak, e); dd = min(dd, e / peak - 1)
    hpeak, hdd = cl[n - 1], 0.0
    for x in cl[n - 1:]:
        hpeak = max(hpeak, x); hdd = min(hdd, x / hpeak - 1)
    return {"days": len(cl) - n, "trend_return_pct": round((eq - 1) * 100, 1), "trend_max_drawdown_pct": round(dd * 100, 1),
            "hold_return_pct": round((cl[-1] / cl[n - 1] - 1) * 100, 1), "hold_max_drawdown_pct": round(hdd * 100, 1),
            "entries": entries}


def stats(xs):
    if len(xs) < 3:
        return {"n": len(xs)}
    return {"n": len(xs), "win_pct": round(100 * sum(1 for x in xs if x > 0) / len(xs)),
            "avg_net_pct": round(statistics.mean(xs), 2), "stderr_pct": round(statistics.stdev(xs) / len(xs) ** 0.5, 2)}


def setup_trades(rows, kind):
    c = [r["c"] for r in rows]
    xs, i = [], 24
    while i < len(rows) - 2:
        if shachar.setup_hit(c, i, kind):
            net, j = shachar.sim_trade(rows, i)
            xs.append(net); i = j + 1
        else:
            i += 1
    return xs


def random_trades(rows, n=4000, seed=7):
    rnd = random.Random(seed)
    return [shachar.sim_trade(rows, rnd.randrange(24, len(rows) - 2))[0] for _ in range(n)]


def report(pair, k):
    hourly = k.ohlc(pair, 60)[:-1]
    daily = k.ohlc(pair, 1440)
    out = {"pair": pair, "hours": len(hourly), "fee_round_trip_pct": shachar.FEE_RT_PCT,
           "daily_trend_sma50": trend_vs_hold(daily), "random_entry": stats(random_trades(hourly))}
    for kind in ("breakout", "trend", "rebound"):
        out["setup_" + kind] = stats(setup_trades(hourly, kind))
    return out


if __name__ == "__main__":
    kk = kraken.Kraken()
    for pr in (sys.argv[1:] or ["XBTUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC"]):
        r = report(pr, kk)
        print("\n== %s  (%d hourly candles, round-trip fee %.1f%%, stop 3%% / take 5%% / 48h)" % (pr, r["hours"], r["fee_round_trip_pct"]))
        for key in ("random_entry", "setup_breakout", "setup_trend", "setup_rebound", "daily_trend_sma50"):
            print("  %-18s %s" % (key, r[key]))
