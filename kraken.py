"""Kraken connector (spot only). Stdlib only.
Keys come from the environment (KRAKEN_KEY, KRAKEN_SECRET) or from KRAKEN_KEY_DIR (files `username` = API key,
`password` = private key), never from the repo. Live orders are OFF unless SHACHAR_LIVE equals YES_IDAN_APPROVED;
otherwise every order is sent with validate=true, which Kraken checks but never places.

Safety properties of this file:
- assert_spot() is an allow-list: any field, pair, side or order type that is not explicitly allowed is refused.
- One process-wide lock and a strictly increasing nonce for private calls (two threads can never race the nonce).
- An order whose reply was lost is never assumed dead: it is cancelled by its userref and its fills are read back.
  If its fate cannot be established, AmbiguousOrder is raised and the caller must HALT.
- order_maker() always cancels what is left, also when polling fails or the process is asked to stop."""
import time, hmac, hashlib, base64, urllib.request, urllib.parse, json, os, threading, math

API = "https://api.kraken.com"
LIVE_TOKEN = "YES_IDAN_APPROVED"
QUOTE = "USDC"
# Kraken altnames. Note: Dogecoin is XDG on Kraken ("DOGEUSDC" resolves to the altname "XDGUSDC").
UNIVERSE = ["XBTUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC", "ADAUSDC", "XDGUSDC", "LINKUSDC", "LTCUSDC"]
ALLOWED_KEYS = {"pair", "type", "ordertype", "volume", "price", "oflags", "userref", "validate", "nonce"}
FINAL = ("closed", "canceled", "expired")
PUB_GAP = float(os.environ.get("KRAKEN_PUB_GAP", "0.35"))     # min seconds between public calls (shared by all threads)
ABORT = threading.Event()                                      # set on shutdown: working maker orders cancel at once

_PRIV_LOCK = threading.Lock()
_PUB_LOCK = threading.Lock()
_REF_LOCK = threading.Lock()
_state = {"nonce": 0, "pub": 0.0, "ref": 0}


class KrakenError(RuntimeError):
    """Kraken answered with an error: the request was definitely NOT executed."""


class AmbiguousOrder(RuntimeError):
    """The fate of an order could not be established. The caller must stop opening positions (HALT)."""


def live_enabled():
    return os.environ.get("SHACHAR_LIVE") == LIVE_TOKEN


def floor_to(x, dec):
    """Round DOWN to `dec` decimals (never sell or buy more than intended)."""
    m = 10 ** dec
    return math.floor(x * m + 1e-9) / m


def _fmt(x, dec):
    return "%.*f" % (dec, x)


def next_nonce():
    _state["nonce"] = max(int(time.time() * 1000), _state["nonce"] + 1)
    return str(_state["nonce"])


def new_userref():
    """Unique positive int32 per order, so a lost reply can be traced and cancelled."""
    with _REF_LOCK:
        _state["ref"] = max(int(time.time() * 10) % 2_000_000_000, _state["ref"] + 1)
        return _state["ref"]


def assert_spot(data):
    """Hard rule: plain spot orders only. Allow-list, not deny-list."""
    extra = set(data) - ALLOWED_KEYS
    if extra:
        raise ValueError("refused: order field not allowed %s (spot only, no leverage/margin/conditional close)" % sorted(extra))
    if data.get("pair") not in UNIVERSE:
        raise ValueError("refused: pair is not in the universe")
    if data.get("type") not in ("buy", "sell"):
        raise ValueError("refused: bad side")
    ot = data.get("ordertype")
    if ot not in ("market", "stop-loss", "limit"):
        raise ValueError("refused: bad order type")
    if "oflags" in data and data["oflags"] != "post":
        raise ValueError("refused: only the post-only flag is allowed")
    if ot == "limit" and (data.get("oflags") != "post" or data.get("type") != "buy" or float(data.get("price", 0)) <= 0):
        raise ValueError("refused: limit orders must be post-only (maker) buys with a price")
    if ot == "stop-loss" and (data.get("type") != "sell" or float(data.get("price", 0)) <= 0):
        raise ValueError("refused: stop-loss must be a sell with a price")
    if ot == "market" and "price" in data:
        raise ValueError("refused: market orders carry no price")
    v = float(data.get("volume", 0))
    if not (v > 0 and math.isfinite(v)):
        raise ValueError("refused: volume must be positive")


def book_summary(bids, asks):
    bb, ba = float(bids[0][0]), float(asks[0][0])
    mid = (bb + ba) / 2
    bd = sum(float(p) * float(q) for p, q, *_ in bids if float(p) >= mid * 0.995)
    ad = sum(float(p) * float(q) for p, q, *_ in asks if float(p) <= mid * 1.005)
    return {"bid_usd_0.5pct": round(bd), "ask_usd_0.5pct": round(ad),
            "imbalance": round((bd - ad) / (bd + ad), 2) if bd + ad else 0.0}


def _get(path, params=None, tries=2):
    """Public call. Throttled process-wide and retried once."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    err = None
    for i in range(tries):
        with _PUB_LOCK:
            wait = PUB_GAP - (time.time() - _state["pub"])
            if wait > 0:
                time.sleep(wait)
            _state["pub"] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "shachar"})
            r = json.load(urllib.request.urlopen(req, timeout=15))
            if r.get("error"):
                raise KrakenError("kraken: %s" % r["error"])
            return r["result"]
        except Exception as e:
            err = e
            if i + 1 < tries:
                time.sleep(1.5)
    raise err


class Kraken:
    def __init__(self, key=None, secret=None):
        self.key = key or (os.environ.get("KRAKEN_KEY") or "").strip() or None
        self.secret = secret or (os.environ.get("KRAKEN_SECRET") or "").strip() or None
        d = os.environ.get("KRAKEN_KEY_DIR")
        if not self.key and d and os.path.exists(os.path.join(d, "username")) and os.path.exists(os.path.join(d, "password")):
            self.key = open(os.path.join(d, "username"), encoding="utf-8").read().strip()
            self.secret = open(os.path.join(d, "password"), encoding="utf-8").read().strip()
        self.info = {}
        self.keymap = {}
        self._ilock = threading.Lock()

    # ---------- public ----------
    def pairs(self):
        with self._ilock:
            if not self.info:
                r = _get("/0/public/AssetPairs", {"pair": ",".join(UNIVERSE)})
                info = {}
                for rk, v in r.items():
                    self.keymap[rk] = v["altname"]
                    if v.get("status") == "online" and v["altname"] in UNIVERSE and v.get("quote") == QUOTE:
                        info[v["altname"]] = {"ordermin": float(v["ordermin"]), "costmin": float(v.get("costmin", 0)),
                                              "lot_decimals": int(v["lot_decimals"]), "pair_decimals": int(v["pair_decimals"]),
                                              "base": v["base"]}       # `base` is the code used in Balance (XXBT, SOL, XXDG...)
                self.info = info
            return self.info

    def tickers(self):
        """All pairs in ONE public call. `open` is today's UTC-midnight open (NOT 24h ago): do not use it as a 24h change."""
        info = self.pairs()
        r = _get("/0/public/Ticker", {"pair": ",".join(info)})
        out = {}
        for rk, v in r.items():
            p = self.keymap.get(rk, rk)
            if p in info:
                out[p] = {"bid": float(v["b"][0]), "ask": float(v["a"][0]), "last": float(v["c"][0]),
                          "open": float(v["o"]), "vol24": float(v["v"][1])}
        return out

    tickers_fast = tickers

    def ohlc(self, pair, interval=60):
        """Up to 720 candles as dicts {t,o,h,l,c}. The last one is the candle still forming."""
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        rows = [v for kk, v in r.items() if kk != "last"][0]
        return [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]

    def depth(self, pair, count=25):
        r = list(_get("/0/public/Depth", {"pair": pair, "count": count}).values())[0]
        return book_summary(r["bids"], r["asks"])

    def usdc_peg(self):
        v = list(_get("/0/public/Ticker", {"pair": "USDCUSD"}).values())[0]
        return float(v["c"][0])

    # ---------- private ----------
    def _priv(self, path, data):
        if not self.key:
            raise KrakenError("no Kraken key available")
        with _PRIV_LOCK:                      # nonce and send as one step: no two threads can reorder nonces
            data = dict(data)
            data["nonce"] = next_nonce()
            post = urllib.parse.urlencode(data)
            msg = path.encode() + hashlib.sha256((data["nonce"] + post).encode()).digest()
            sig = base64.b64encode(hmac.new(base64.b64decode(self.secret), msg, hashlib.sha512).digest()).decode()
            req = urllib.request.Request(API + path, data=post.encode(),
                                         headers={"API-Key": self.key, "API-Sign": sig, "User-Agent": "shachar"})
            r = json.load(urllib.request.urlopen(req, timeout=20))
        if r.get("error"):
            raise KrakenError("kraken: %s" % r["error"])
        return r["result"]

    def balance(self):
        return {k: float(v) for k, v in self._priv("/0/private/Balance", {}).items()}

    def open_orders(self):
        return self._priv("/0/private/OpenOrders", {}).get("open", {})

    def query_order(self, txid):
        q = self._priv("/0/private/QueryOrders", {"txid": txid})[txid]
        return {"status": q["status"], "vol": float(q["vol"]), "vol_exec": float(q["vol_exec"]),
                "cost": float(q["cost"]), "fee": float(q["fee"])}

    def cancel_confirmed(self, txid, tries=5):
        """Cancel and return only when Kraken reports a final status (with whatever was executed)."""
        last = None
        for _ in range(tries):
            try:
                self._priv("/0/private/CancelOrder", {"txid": txid})
            except Exception as e:           # already closed / unknown / network: the query below tells the truth
                last = e
            time.sleep(1.5)
            try:
                q = self.query_order(txid)
                if q["status"] in FINAL:
                    return q
            except Exception as e:
                last = e
        raise AmbiguousOrder("cannot confirm cancel of %s (%s)" % (txid, str(last)[:80]))

    def _recover(self, userref):
        """The AddOrder reply was lost. Cancel anything open under this userref and report what was executed."""
        last = None
        for _ in range(3):
            time.sleep(2)
            try:
                try:
                    self._priv("/0/private/CancelOrder", {"txid": str(userref)})
                except KrakenError:
                    pass                      # nothing open under this userref
                if self._priv("/0/private/OpenOrders", {"userref": str(userref)}).get("open"):
                    continue
                closed = self._priv("/0/private/ClosedOrders", {"userref": str(userref)}).get("closed", {})
                return {"vol": sum(float(q["vol_exec"]) for q in closed.values()),
                        "cost": sum(float(q["cost"]) for q in closed.values()),
                        "fee": sum(float(q["fee"]) for q in closed.values())}
            except Exception as e:
                last = e
        raise AmbiguousOrder("order with userref %s: fate unknown (%s)" % (userref, str(last)[:80]))

    def _add_order(self, data):
        assert_spot(data)
        live = live_enabled()
        if not live:
            data = dict(data, validate="true")
        try:
            res = self._priv("/0/private/AddOrder", data)
        except KrakenError:
            raise                             # Kraken refused it: nothing was placed
        except Exception:
            if not live:
                raise
            return {"recovered": self._recover(data["userref"])}
        return {"live": live, "descr": res.get("descr", {}).get("order"), "txid": (res.get("txid") or [None])[0]}

    def order(self, pair, side, volume):
        """Market order (used for exits). Returns {live, descr, txid, vol, cost, fee, status}."""
        info = self.pairs()[pair]
        data = {"pair": pair, "type": side, "ordertype": "market", "volume": _fmt(volume, info["lot_decimals"]),
                "userref": str(new_userref())}
        res = self._add_order(data)
        if "recovered" in res:
            if res["recovered"]["vol"] <= 0:
                raise KrakenError("market order was not placed (reply lost, nothing executed)")
            return dict(res["recovered"], live=True, descr="recovered", txid=None, status="closed")
        out = dict(res, vol=0.0, cost=0.0, fee=0.0, status="validated")
        if not (out["live"] and out["txid"]):
            return out
        last = None
        for _ in range(10):
            time.sleep(2)
            try:
                q = self.query_order(out["txid"])
            except Exception as e:
                last = e
                continue
            if q["status"] in FINAL:
                out.update(vol=q["vol_exec"], cost=q["cost"], fee=q["fee"], status=q["status"])
                return out
        raise AmbiguousOrder("market order %s is not final (%s)" % (out["txid"], str(last)[:80]))

    def order_maker(self, pair, side, volume, price, wait=90):
        """Post-only limit BUY (maker fee, never crosses the book). Waits up to `wait` seconds, then cancels what is
        left, ALWAYS, and returns what actually filled."""
        info = self.pairs()[pair]
        data = {"pair": pair, "type": side, "ordertype": "limit", "oflags": "post",
                "price": _fmt(price, info["pair_decimals"]), "volume": _fmt(volume, info["lot_decimals"]),
                "userref": str(new_userref())}
        res = self._add_order(data)
        if "recovered" in res:
            return dict(res["recovered"], live=True, descr="recovered", txid=None, status="recovered")
        out = dict(res, vol=0.0, cost=0.0, fee=0.0, status="validated")
        if not (out["live"] and out["txid"]):
            return out
        txid = out["txid"]
        try:
            end = time.time() + wait
            while time.time() < end and not ABORT.is_set():
                time.sleep(5)
                try:
                    if self.query_order(txid)["status"] in FINAL:
                        break
                except Exception:
                    continue                  # transient: the finally below still cancels
        finally:
            q = self.cancel_confirmed(txid)   # raises AmbiguousOrder if Kraken cannot confirm
        out.update(vol=q["vol_exec"], cost=q["cost"], fee=q["fee"], status=q["status"])
        return out

    def stop_order(self, pair, volume, stop_price):
        """Stop-loss SELL that lives on Kraken itself, so it fires even if this host is down."""
        info = self.pairs()[pair]
        data = {"pair": pair, "type": "sell", "ordertype": "stop-loss", "price": _fmt(stop_price, info["pair_decimals"]),
                "volume": _fmt(floor_to(volume, info["lot_decimals"]), info["lot_decimals"]), "userref": str(new_userref())}
        res = self._add_order(data)
        if "recovered" in res:                # reply lost: _recover cancelled it, so it is not armed
            raise KrakenError("stop order was not confirmed")
        return res
