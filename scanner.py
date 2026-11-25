"""Price scanner. One public Kraken call per tick for all pairs. Keeps about 70+ minutes of prices in memory and turns
them into a short summary the brain can read. seed() loads the last ~75 one-minute candles at start, so the 1h window
is ready right after a restart instead of an hour later."""
import time, json, collections, os
import kraken, store

WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


class Scanner:
    def __init__(self, k=None):
        self.k = k or kraken.Kraken()
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=4400))
        self.tk = {}
        self.t = 0.0

    def seed(self, minutes=75):
        """Best effort. Returns the number of pairs seeded."""
        n = 0
        for p in self.k.pairs():
            try:
                rows = self.k.ohlc(p, 1)[-minutes:]
                self.hist[p].extend((r["t"] + 60.0, r["c"]) for r in rows[:-1])     # close time of finished candles
                n += 1
            except Exception:
                pass
        return n

    def tick(self):
        tk = self.k.tickers()
        self.tk, self.t = tk, time.time()
        for p, v in tk.items():
            self.hist[p].append((self.t, v["last"]))
        return tk

    def age(self):
        return time.time() - self.t if self.t else 1e9

    def _chg(self, p, secs):
        h = self.hist[p]
        if len(h) < 2:
            return None
        target = self.t - secs
        old = None
        for t, px in h:
            if t >= target:
                old = px
                break
        if old is None or h[-1][0] - h[0][0] < secs * 0.8:
            return None       # not enough history yet
        return round((h[-1][1] / old - 1) * 100, 3)

    def history_seconds(self):
        spans = [h[-1][0] - h[0][0] for h in self.hist.values() if len(h) > 1]
        return int(min(spans)) if spans else 0

    def summary(self):
        out = {}
        for p, v in self.tk.items():
            row = {"price": v["last"], "spread_pct": round((v["ask"] / v["bid"] - 1) * 100, 3),
                   "vol24_base": round(v["vol24"], 1)}
            flags = []
            for name, secs in WINDOWS.items():
                c = self._chg(p, secs)
                if c is not None:
                    row["chg_" + name + "_pct"] = c
            c1 = row.get("chg_1m_pct")
            if c1 is not None and abs(c1) >= 1.5:
                flags.append("spike_" + ("up" if c1 > 0 else "down") + "_1m")
            c5 = row.get("chg_5m_pct")
            if c5 is not None and abs(c5) >= 3:
                flags.append("big_move_5m")
            if row["spread_pct"] > 0.3:
                flags.append("wide_spread")
            row["flags"] = flags
            out[p] = row
        return {"as_of": time.strftime("%Y-%m-%d %H:%M:%S"), "history_seconds": self.history_seconds(), "pairs": out}

    def dump(self):
        store.atomic_write("scan.json", json.dumps(self.summary()))


if __name__ == "__main__":
    s = Scanner()
    print("seeded", s.seed())
    for _ in range(2):
        s.tick(); time.sleep(2)
    print(json.dumps(s.summary(), indent=1)[:1200])
