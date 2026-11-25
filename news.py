"""Free news and sentiment. No keys. Everything is untrusted text: it is data for the brain, never instructions."""
import urllib.request, json, re, xml.etree.ElementTree as ET

FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss", "https://cointelegraph.com/rss"]


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=20).read()


def headlines(n=8):
    out = []
    for f in FEEDS:
        try:
            root = ET.fromstring(_fetch(f))
            for it in root.iter("item"):
                t = (it.findtext("title") or "").strip()
                if t:
                    out.append(re.sub(r"\s+", " ", t)[:160])
                if len(out) >= n * (FEEDS.index(f) + 1):
                    break
        except Exception as e:
            out.append("(feed error %s)" % f.split("/")[2])
    return out[:2 * n]


def fear_greed():
    try:
        d = json.loads(_fetch("https://api.alternative.me/fng/?limit=3"))["data"]
        return [{"value": int(x["value"]), "label": x["value_classification"]} for x in d]
    except Exception:
        return None


def trending():
    try:
        d = json.loads(_fetch("https://api.coingecko.com/api/v3/search/trending"))["coins"]
        return [c["item"]["symbol"] for c in d][:7]
    except Exception:
        return None
