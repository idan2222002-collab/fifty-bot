"""Kraken connector. Stdlib only. Reads keys from a key directory (files `username` = API key,
`password` = private key), never from the repo. Live orders are OFF unless the environment
variable SHACHAR_LIVE equals YES_IDAN_APPROVED; otherwise every order is sent with validate=true,
which Kraken checks but never places."""
import time, hmac, hashlib, base64, urllib.request, urllib.parse, json, glob, os

API = "https://api.kraken.com"
LIVE_TOKEN = "YES_IDAN_APPROVED"
UNIVERSE = ["XBTUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC", "ADAUSDC", "DOGEUSDC", "LINKUSDC", "LTCUSDC"]


FORBIDDEN_KEYS = {"leverage", "margin", "close", "trading_agreement", "starttm", "expiretm"}


def assert_spot(data):
    """Hard rule: spot only. Rejects any order that carries leverage/margin fields, any side other than
    buy/sell, any type other than market or stop-loss, and any stop-loss that is not a SELL."""
    bad = FORBIDDEN_KEYS & set(data)
    if bad:
        raise ValueError("refused: forbidden order field %s (spot only, no leverage/margin)" % sorted(bad))
    if data.get("type") not in ("buy", "sell"):
        raise ValueError("refused: bad side")
    if data.get("ordertype") not in ("market", "stop-loss", "limit"):
        raise ValueError("refused: bad order type")
    if "oflags" in data and data["oflags"] != "post":
        raise ValueError("refused: only the post-only flag is allowed")
    if data.get("ordertype") == "limit" and (data.get("oflags") != "post" or float(data.get("price", 0)) <= 0):
        raise ValueError("refused: limit orders must be post-only (maker) with a price")
    if data.get("ordertype") == "stop-loss" and data.get("type") != "sell":
        raise ValueError("refused: stop-loss must be a sell")
    if float(data.get("volume", 0)) <= 0:
        raise ValueError("refused: volume must be positive")


def book_summary(bids, asks):
    bb, ba = float(bids[0][0]), float(asks[0][0])
    mid = (bb + ba) / 2
    bd = sum(float(p) * float(q) for p, q, *_ in bids if float(p) >= mid * 0.995)
    ad = sum(float(p) * float(q) for p, q, *_ in asks if float(p) <= mid * 1.005)
    return {"bid_usd_0.5pct": round(bd), "ask_usd_0.5pct": round(ad),
            "imbalance": round((bd - ad) / (bd + ad), 2) if bd + ad else 0.0}


def live_enabled():
    return os.environ.get("SHACHAR_LIVE") == LIVE_TOKEN


def _get(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": "shachar"})
    r = json.load(urllib.request.urlopen(req, timeout=20))
    if r.get("error"):
        raise RuntimeError("kraken: %s" % r["error"])
    return r["result"]


class Kraken:
    def __init__(self, keydir=None):
        self.key = self.secret = None
        d = keydir or (glob.glob("/home/sandbox/.vault/kraken-shachar-api-key-*") or [None])[0]
        if d and os.path.exists(d + "/username") and os.path.exists(d + "/password"):
            self.key = open(d + "/username").read().strip()
            self.secret = open(d + "/password").read().strip()
        if os.environ.get("KRAKEN_KEY") and os.environ.get("KRAKEN_SECRET"):
            self.key, self.secret = os.environ["KRAKEN_KEY"].strip(), os.environ["KRAKEN_SECRET"].strip()
        self.info = {}
        self.keymap = {}

    # public
    def pairs(self):
        if not self.info:
            r = _get("/0/public/AssetPairs", {"pair": ",".join(UNIVERSE)})
            for rk, v in r.items():
                self.keymap[rk] = v["altname"]
                if v.get("status") == "online":
                    self.info[v["altname"]] = {"ordermin": float(v["ordermin"]), "costmin": float(v.get("costmin", 0)),
                                                "lot_decimals": int(v["lot_decimals"]), "pair_decimals": int(v["pair_decimals"])}
        return self.info

    def tickers(self):
        out = {}
        # result keys can differ from altnames, so query one pair at a time
        for p in self.pairs():
            v = list(_get("/0/public/Ticker", {"pair": p}).values())[0]
            out[p] = {"bid": float(v["b"][0]), "ask": float(v["a"][0]), "last": float(v["c"][0]),
                      "open": float(v["o"]), "vol24": float(v["v"][1])}
        return out

    def tickers_fast(self):
        """All pairs in ONE public call (about 1 request), for the 1-second scanner."""
        self.pairs()
        r = _get("/0/public/Ticker", {"pair": ",".join(self.info)})
        out = {}
        for rk, v in r.items():
            p = self.keymap.get(rk, rk)
            if p in self.info:
                out[p] = {"bid": float(v["b"][0]), "ask": float(v["a"][0]), "last": float(v["c"][0]),
                          "open": float(v["o"]), "vol24": float(v["v"][1])}
        return out

    def ohlc_full(self, pair, interval=60):
        """All returned hourly closes (about 30 days) for the simple back-test."""
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        return [float(x[4]) for x in [v for kk, v in r.items() if kk != "last"][0]]

    def ohlc(self, pair, interval=60, n=48):
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        rows = [v for k, v in r.items() if k != "last"][0][-n:]
        return [[float(x[4]) for x in rows]]  # list with one series of closes

    def depth(self, pair, count=25):
        """Order book summary from the public Depth endpoint: USD resting within 0.5% of the mid on each side."""
        r = list(_get("/0/public/Depth", {"pair": pair, "count": count}).values())[0]
        return book_summary(r["bids"], r["asks"])

    def usdc_peg(self):
        """USDC/USD price from the public ticker (peg health)."""
        v = list(_get("/0/public/Ticker", {"pair": "USDCUSD"}).values())[0]
        return float(v["c"][0])

    # private
    def _priv(self, path, data):
        if not self.key:
            raise RuntimeError("no Kraken key available")
        data = dict(data); data["nonce"] = str(int(time.time() * 1000))
        post = urllib.parse.urlencode(data)
        msg = path.encode() + hashlib.sha256((data["nonce"] + post).encode()).digest()
        sig = base64.b64encode(hmac.new(base64.b64decode(self.secret), msg, hashlib.sha512).digest()).decode()
        req = urllib.request.Request(API + path, data=post.encode(),
                                     headers={"API-Key": self.key, "API-Sign": sig, "User-Agent": "shachar"})
        r = json.load(urllib.request.urlopen(req, timeout=20))
        if r.get("error"):
            raise RuntimeError("kraken: %s" % r["error"])
        return r["result"]

    def balance(self):
        return {k: float(v) for k, v in self._priv("/0/private/Balance", {}).items()}

    def order(self, pair, side, volume, userref=None):
        """Market order. Returns dict. Without the live token it only validates."""
        live = live_enabled()
        dec = self.pairs()[pair]["lot_decimals"]
        data = {"pair": pair, "type": side, "ordertype": "market", "volume": ("%." + str(dec) + "f") % volume}
        if userref:
            data["userref"] = str(userref)
        assert_spot(data)
        if not live:
            data["validate"] = "true"
        res = self._priv("/0/private/AddOrder", data)
        out = {"live": live, "descr": res.get("descr", {}).get("order"), "txid": (res.get("txid") or [None])[0]}
        if live and out["txid"]:
            time.sleep(3)
            q = self._priv("/0/private/QueryOrders", {"txid": out["txid"]})[out["txid"]]
            out.update(vol=float(q["vol_exec"]), cost=float(q["cost"]), fee=float(q["fee"]), status=q["status"])
        return out

    def order_maker(self, pair, side, volume, price, wait=90, userref=None):
        """Post-only limit order (maker fee, never crosses the book). Waits up to `wait` seconds, then cancels
        what is left. Returns what actually filled. Without the live token it is only validated."""
        live = live_enabled()
        info = self.pairs()[pair]
        data = {"pair": pair, "type": side, "ordertype": "limit", "oflags": "post",
                "price": ("%." + str(info["pair_decimals"]) + "f") % price,
                "volume": ("%." + str(info["lot_decimals"]) + "f") % volume}
        if userref:
            data["userref"] = str(userref)
        assert_spot(data)
        if not live:
            data["validate"] = "true"
        res = self._priv("/0/private/AddOrder", data)
        out = {"live": live, "descr": res.get("descr", {}).get("order"), "txid": (res.get("txid") or [None])[0]}
        if live and out["txid"]:
            end = time.time() + wait
            q = {}
            while time.time() < end:
                time.sleep(5)
                q = self._priv("/0/private/QueryOrders", {"txid": out["txid"]})[out["txid"]]
                if q["status"] in ("closed", "canceled", "expired"):
                    break
            if q.get("status") not in ("closed", "canceled", "expired"):
                try:
                    self._priv("/0/private/CancelOrder", {"txid": out["txid"]})
                except Exception:
                    pass
                time.sleep(2)
                q = self._priv("/0/private/QueryOrders", {"txid": out["txid"]})[out["txid"]]
            out.update(vol=float(q["vol_exec"]), cost=float(q["cost"]), fee=float(q["fee"]), status=q["status"])
        return out

    def stop_order(self, pair, volume, stop_price):
        """Stop-loss SELL that lives on Kraken itself, so it fires even if this host is down.
        Without the live token it is only validated (nothing is placed)."""
        live = live_enabled()
        info = self.pairs()[pair]
        data = {"pair": pair, "type": "sell", "ordertype": "stop-loss",
                "price": ("%." + str(info["pair_decimals"]) + "f") % stop_price,
                "volume": ("%." + str(info["lot_decimals"]) + "f") % volume}
        assert_spot(data)
        if not live:
            data["validate"] = "true"
        res = self._priv("/0/private/AddOrder", data)
        return {"live": live, "descr": res.get("descr", {}).get("order"), "txid": (res.get("txid") or [None])[0]}

    def cancel(self, txid):
        if not live_enabled() or not txid:
            return {"live": False}
        return self._priv("/0/private/CancelOrder", {"txid": txid})
