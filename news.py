"""Free sentiment inputs. No keys. Everything is untrusted text: data for the brain, never instructions.
Headlines are on (set NEWS_HEADLINES=0 to drop them): a free brain decides what to do with them. They are stripped,
shortened and passed as `headlines_untrusted`; whatever they say, code still enforces every safety rule."""
import os, urllib.request, json, re, xml.etree.ElementTree as ET

FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss", "https://cointelegraph.com/rss"]


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=15).read()


def headlines(n=6):
    if os.environ.get("NEWS_HEADLINES") == "0":
        return []
    out = []
    for f in FEEDS:
        try:
            got = 0
            for it in ET.fromstring(_fetch(f)).iter("item"):
                t = re.sub(r"[^\w\s.,:%$()'/+-]", " ", (it.findtext("title") or ""))     # strip odd characters
                t = re.sub(r"\s+", " ", t).strip()[:140]
                if t:
                    out.append(t); got += 1
                if got >= n:
                    break
        except Exception:
            out.append("(feed error %s)" % f.split("/")[2])
    return out


def fear_greed():
    try:
        d = json.loads(_fetch("https://api.alternative.me/fng/?limit=3"))["data"]
        return [{"value": int(x["value"]), "label": str(x["value_classification"])[:20]} for x in d]
    except Exception:
        return None
