"""1-second scanner. One public Kraken call per tick for all pairs. Keeps about 70 minutes of per-second
prices in memory and turns them into a short summary the brain can read."""
import time, json, collections, pathlib, os
import kraken

HERE = pathlib.Path(__file__).parent
WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


class Scanner:
    def __init__(self, k=None):
        self.k = k or kraken.Kraken()
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=4300))
        self.tk = {}
        self.t = 0.0

    def tick(self):
        self.tk = self.k.tickers_fast()
        self.t = time.time()
        for p, v in self.tk.items():
            self.hist[p].append((self.t, v["last"]))
        return self.tk

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

    def summary(self):
        out = {}
        for p, v in self.tk.items():
            row = {"price": v["last"], "spread_pct": round((v["ask"] / v["bid"] - 1) * 100, 3),
                   "chg_utc_day_pct": round((v["last"] / v["open"] - 1) * 100, 2), "vol24_base": round(v["vol24"], 1)}
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
        return {"as_of": time.strftime("%F %T"), "history_seconds": int(self.t - self.hist["XBTUSDC"][0][0]) if self.hist["XBTUSDC"] else 0,
                "pairs": out}

    def dump(self, path=None):
        f = pathlib.Path(path or HERE / "scan.json")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.summary()))
        os.replace(tmp, f)


if __name__ == "__main__":
    s = Scanner()
    for _ in range(3):
        s.tick(); time.sleep(1)
    print(json.dumps(s.summary(), indent=1)[:900])
