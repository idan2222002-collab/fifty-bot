"""Kraken connector. Stdlib only. Reads keys from a key directory (files `username` = API key,
`password` = private key), never from the repo. Live orders are OFF unless the environment
variable SHACHAR_LIVE equals YES_IDAN_APPROVED; otherwise every order is sent with validate=true,
which Kraken checks but never places."""
import time, hmac, hashlib, base64, urllib.request, urllib.parse, json, glob, os

API = "https://api.kraken.com"
LIVE_TOKEN = "YES_IDAN_APPROVED"
UNIVERSE = ["XBTUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC", "ADAUSDC", "DOGEUSDC", "LINKUSDC", "LTCUSDC"]


FORBIDDEN_KEYS = {"leverage", "margin", "close", "oflags", "trading_agreement", "starttm", "expiretm"}


def assert_spot(data):
    """Hard rule: spot only. Rejects any order that carries leverage/margin fields, any side other than
    buy/sell, any type other than market or stop-loss, and any stop-loss that is not a SELL."""
    bad = FORBIDDEN_KEYS & set(data)
    if bad:
        raise ValueError("refused: forbidden order field %s (spot only, no leverage/margin)" % sorted(bad))
    if data.get("type") not in ("buy", "sell"):
        raise ValueError("refused: bad side")
    if data.get("ordertype") not in ("market", "stop-loss"):
        raise ValueError("refused: bad order type")
    if data.get("ordertype") == "stop-loss" and data.get("type") != "sell":
        raise ValueError("refused: stop-loss must be a sell")
    if float(data.get("volume", 0)) <= 0:
        raise ValueError("refused: volume must be positive")


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

    def ohlc(self, pair, interval=60, n=48):
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        rows = [v for k, v in r.items() if k != "last"][0][-n:]
        return [[float(x[4]) for x in rows]]  # list with one series of closes

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
