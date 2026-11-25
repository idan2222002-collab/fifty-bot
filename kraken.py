"""Kraken connector. Stdlib only. Keys come from the environment (KRAKEN_KEY / KRAKEN_SECRET, GitHub Actions
secrets) or from a key directory (files `username` = API key, `password` = private key), never from the repo.
Live orders are OFF unless the environment variable SHACHAR_LIVE equals YES_IDAN_APPROVED; otherwise every order
is sent with validate=true, which Kraken checks but never places."""
import time, hmac, hashlib, base64, urllib.request, urllib.parse, json, glob, os, math, threading

API = "https://api.kraken.com"
LIVE_TOKEN = "YES_IDAN_APPROVED"
# Kraken altnames. Dogecoin is XDG on Kraken: "DOGEUSDC" resolves to the altname "XDGUSDC", which is the key that
# pairs(), the tickers and assert_spot() all use.
UNIVERSE = ["XBTUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC", "ADAUSDC", "XDGUSDC", "LINKUSDC", "LTCUSDC"]
DONE = ("closed", "canceled", "expired")
ALLOWED_KEYS = {"pair", "type", "ordertype", "volume", "price", "oflags", "userref", "validate", "nonce"}

_PRIV_LOCK = threading.Lock()      # one private call at a time, nonce strictly increasing
_LAST_NONCE = [0]


def assert_spot(data):
    """Hard rule: spot only. Allow-list: only these fields may ever be sent (no leverage, margin, close, ...).
    The pair must be in the universe, the side buy or sell, the type market / stop-loss (sell only) / limit
    (post-only maker with a price), and the volume positive."""
    extra = set(data) - ALLOWED_KEYS
    if extra:
        raise ValueError("refused: field not on the allow-list %s (spot only, no leverage/margin)" % sorted(extra))
    if data.get("pair") not in UNIVERSE:
        raise ValueError("refused: pair not in the universe")
    if data.get("type") not in ("buy", "sell"):
        raise ValueError("refused: bad side")
    if data.get("ordertype") not in ("market", "stop-loss", "limit"):
        raise ValueError("refused: bad order type")
    if "oflags" in data and data["oflags"] != "post":
        raise ValueError("refused: only the post-only flag is allowed")
    if data.get("ordertype") == "limit" and (data.get("oflags") != "post" or float(data.get("price", 0)) <= 0):
        raise ValueError("refused: limit orders must be post-only (maker) with a price")
    if data.get("ordertype") == "stop-loss" and (data.get("type") != "sell" or float(data.get("price", 0)) <= 0):
        raise ValueError("refused: stop-loss must be a sell with a price")
    if float(data.get("volume", 0)) <= 0:
        raise ValueError("refused: volume must be positive")


def book_summary(bids, asks):
    bb, ba = float(bids[0][0]), float(asks[0][0])
    mid = (bb + ba) / 2
    bd = sum(float(p) * float(q) for p, q, *_ in bids if float(p) >= mid * 0.995)
    ad = sum(float(p) * float(q) for p, q, *_ in asks if float(p) <= mid * 1.005)
    return {"bid_usd_0.5pct": round(bd), "ask_usd_0.5pct": round(ad),
            "imbalance": round((bd - ad) / (bd + ad), 2) if bd + ad else 0.0}


def floor_to(x, dec):
    """Round DOWN to `dec` decimals (never sell or buy more than intended)."""
    f = 10 ** dec
    return math.floor(x * f + 1e-9) / f


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
    MAKER_WAIT = 90   # seconds a maker entry may rest before it is cancelled
    POLL = 5          # seconds between order status polls (tests set 0)

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
        for p in self.pairs():
            v = list(_get("/0/public/Ticker", {"pair": p}).values())[0]
            out[p] = {"bid": float(v["b"][0]), "ask": float(v["a"][0]), "last": float(v["c"][0]),
                      "open": float(v["o"]), "vol24": float(v["v"][1])}
        return out

    def tickers_fast(self):
        """All pairs in ONE public call, for the scanner. `open` is today's UTC opening price, NOT 24h ago."""
        self.pairs()
        r = _get("/0/public/Ticker", {"pair": ",".join(self.info)})
        out = {}
        for rk, v in r.items():
            p = self.keymap.get(rk, rk)
            if p in self.info:
                out[p] = {"bid": float(v["b"][0]), "ask": float(v["a"][0]), "last": float(v["c"][0]),
                          "open": float(v["o"]), "vol24": float(v["v"][1])}
        return out

    def depth(self, pair, count=25):
        r = list(_get("/0/public/Depth", {"pair": pair, "count": count}).values())[0]
        return book_summary(r["bids"], r["asks"])

    def usdc_peg(self):
        v = list(_get("/0/public/Ticker", {"pair": "USDCUSD"}).values())[0]
        return float(v["c"][0])

    def ohlc_full(self, pair, interval=60):
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        return [float(x[4]) for x in [v for kk, v in r.items() if kk != "last"][0]]

    def ohlc_rows(self, pair, interval=1):
        """[(candle start time, close)] for the finished candles (the last one, still forming, is dropped)."""
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        rows = [v for kk, v in r.items() if kk != "last"][0]
        return [(float(x[0]), float(x[4])) for x in rows[:-1]]

    def ohlc_candles(self, pair, interval=60):
        """Up to 720 candles as {o, h, l, c} (about 30 days of hourly candles), for the replay."""
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        rows = [v for kk, v in r.items() if kk != "last"][0]
        return [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]

    def ohlc(self, pair, interval=60, n=48):
        r = _get("/0/public/OHLC", {"pair": pair, "interval": interval})
        rows = [v for k, v in r.items() if k != "last"][0][-n:]
        return [[float(x[4]) for x in rows]]  # list with one series of closes

    # private
    def _priv(self, path, data):
        if not self.key:
            raise RuntimeError("no Kraken key available")
        with _PRIV_LOCK:
            data = dict(data)
            n = max(int(time.time() * 1000), _LAST_NONCE[0] + 1)
            _LAST_NONCE[0] = n
            data["nonce"] = str(n)
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

    def open_orders(self):
        return self._priv("/0/private/OpenOrders", {}).get("open", {})

    def query_order(self, txid):
        q = self._priv("/0/private/QueryOrders", {"txid": txid})[txid]
        return {"status": q["status"], "vol_exec": float(q["vol_exec"]), "cost": float(q["cost"]), "fee": float(q["fee"])}

    def cancel(self, txid):
        """Cancel one order. Raises on failure. An order that is already gone counts as cancelled."""
        if not live_enabled() or not txid:
            return {"live": False}
        try:
            return self._priv("/0/private/CancelOrder", {"txid": txid})
        except RuntimeError as e:
            if "Unknown order" in str(e):
                return {"already_gone": True}
            raise

    def _dec(self, pair):
        return self.pairs()[pair]

    def order(self, pair, side, volume, userref=None):
        """Market order. Returns dict. Without the live token it only validates."""
        live = live_enabled()
        dec = self._dec(pair)["lot_decimals"]
        data = {"pair": pair, "type": side, "ordertype": "market", "volume": ("%." + str(dec) + "f") % floor_to(volume, dec)}
        if userref:
            data["userref"] = str(userref)
        assert_spot(data)
        if not live:
            data["validate"] = "true"
        try:
            res = self._priv("/0/private/AddOrder", data)
        except RuntimeError:
            raise                                   # Kraken answered with an error: nothing was placed
        except Exception as e:
            if not live:
                raise
            return self._lost_reply(data.get("userref"), e)     # no answer: the order may have executed
        out = {"live": live, "descr": res.get("descr", {}).get("order"), "txid": (res.get("txid") or [None])[0]}
        if live and out["txid"]:
            q = None
            for _ in range(6):
                time.sleep(min(2, self.POLL))
                try:
                    q = self.query_order(out["txid"])
                except Exception:
                    q = None                        # the order IS placed: a failed status read must not lose the fill
                    continue
                if q["status"] in DONE:
                    break
            if q is None:
                found = self._lost_reply(data.get("userref"), "status read failed", placed=True)
                out.update(vol=found["vol"], cost=found["cost"], fee=found["fee"], status=found["status"])
            else:
                out.update(vol=q["vol_exec"], cost=q["cost"], fee=q["fee"], status=q["status"])
        return out

    def _lost_reply(self, ref, err, placed=False):
        """The answer to a market order was lost. Find the order by its userref and report what really executed.
        Returns status "closed" with the fill, or status "unknown" (the caller halts). Raises only when Kraken
        confirms that no such order exists, which means nothing was placed."""
        out = {"live": True, "descr": "reply lost: %s" % str(err)[:60], "txid": None, "vol": 0.0, "cost": 0.0,
               "fee": 0.0, "status": "unknown"}
        if not ref:
            return out
        for _ in range(3):
            time.sleep(min(2, self.POLL))
            try:
                closed = self._priv("/0/private/ClosedOrders", {"userref": str(ref)}).get("closed", {})
                still_open = [t for t, o in self.open_orders().items() if str(o.get("userref")) == str(ref)]
            except Exception:
                continue
            if closed:
                out.update(vol=sum(float(o["vol_exec"]) for o in closed.values()),
                           cost=sum(float(o["cost"]) for o in closed.values()),
                           fee=sum(float(o["fee"]) for o in closed.values()), status="closed")
                return out
            if not still_open and not placed:
                raise RuntimeError("order was not placed (no answer and Kraken has no order with this userref)")
        return out

    def order_maker(self, pair, side, volume, price, wait=None, userref=None):
        wait = self.MAKER_WAIT if wait is None else wait
        """Post-only limit order (maker fee, never crosses the book). Waits up to `wait` seconds, then cancels what is
        left (3 tries) and reads the final fill. If anything fails after the order may exist, it hunts the order down
        by its unique userref and cancels it. Raises only when the final state could not be established."""
        live = live_enabled()
        info = self._dec(pair)
        ref = str(userref or (int(time.time()) % 2000000000))
        data = {"pair": pair, "type": side, "ordertype": "limit", "oflags": "post",
                "price": ("%." + str(info["pair_decimals"]) + "f") % price,
                "volume": ("%." + str(info["lot_decimals"]) + "f") % floor_to(volume, info["lot_decimals"]),
                "userref": ref}
        assert_spot(data)
        if not live:
            data["validate"] = "true"
        try:
            res = self._priv("/0/private/AddOrder", data)
        except Exception as e:
            if live:                                    # the order may exist even though we got no answer
                self._cancel_by_userref(ref)
            raise
        out = {"live": live, "descr": res.get("descr", {}).get("order"), "txid": (res.get("txid") or [None])[0]}
        if not (live and out["txid"]):
            return out
        txid, q, polled_err = out["txid"], {}, None
        try:
            end = time.time() + wait
            while True:
                time.sleep(self.POLL)
                q = self.query_order(txid)
                if q["status"] in DONE or time.time() >= end:
                    break
        except Exception as e:
            polled_err = e
        if q.get("status") not in DONE:
            last = None
            for _ in range(3):
                try:
                    self.cancel(txid); last = None; break
                except Exception as e:
                    last = e; time.sleep(min(1, self.POLL))
            if last is not None:
                self._cancel_by_userref(ref, strict=False)
        try:
            q = self.query_order(txid)
        except Exception as e:
            raise RuntimeError("order %s final state unknown: %s" % (txid, str(e)[:80]))
        if q["status"] not in DONE:
            raise RuntimeError("order %s still open after cancel attempts" % txid)
        out.update(vol=q["vol_exec"], cost=q["cost"], fee=q["fee"], status=q["status"])
        if polled_err:
            out["note"] = "poll error: %s" % str(polled_err)[:60]
        return out

    def _cancel_by_userref(self, ref, strict=True):
        try:
            for txid, o in self.open_orders().items():
                if str(o.get("userref")) == ref:
                    self.cancel(txid)
        except Exception:
            if strict:
                pass

    def stop_order(self, pair, volume, stop_price):
        """Stop-loss SELL that lives on Kraken itself, so it fires even if this host is down.
        Without the live token it is only validated (nothing is placed)."""
        live = live_enabled()
        info = self._dec(pair)
        data = {"pair": pair, "type": "sell", "ordertype": "stop-loss",
                "price": ("%." + str(info["pair_decimals"]) + "f") % stop_price,
                "volume": ("%." + str(info["lot_decimals"]) + "f") % floor_to(volume, info["lot_decimals"])}
        assert_spot(data)
        if not live:
            data["validate"] = "true"
        res = self._priv("/0/private/AddOrder", data)
        return {"live": live, "descr": res.get("descr", {}).get("order"), "txid": (res.get("txid") or [None])[0]}


_INST = []


def get():
    """One shared Kraken object (single nonce counter, one place for private calls)."""
    if not _INST:
        _INST.append(Kraken())
    return _INST[0]
