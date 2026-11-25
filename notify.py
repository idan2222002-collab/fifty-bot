"""Alerts and the owner's remote controls. Free, stdlib only.

Alerts: every alert is written to the journal. If GH_TOKEN and REPO are set (they are inside the GitHub workflow),
an issue is opened in the repo, so GitHub notifies the owner by mail and in the phone app with no extra setup.
Optional: TG_TOKEN + TG_CHAT also send the alert to Telegram.

Remote controls: the OWNER of the repo opens an issue whose title starts with
  HALT     -> no new buys (stops and exits keep working)
  FLATTEN  -> sell everything to USDC and no new buys
and closes the issue to lift it. After the bot halted ITSELF (reconcile mismatch, drawdown breaker, unknown order
state) the owner lifts the halt by opening an issue whose title starts with
  RESUME   -> the bot removes its HALT file, restarts the drawdown peak, answers and closes the issue
Issues from anyone else are ignored (the repo is public)."""
import os, json, time, urllib.request, urllib.parse
import store

GH_API = "https://api.github.com"
PREFIX = "[shachar] "
STALE_S = 900          # live only: if the controls cannot be read for this long, block new buys (fail closed)
_sent = {}
_flags = {"halt": False, "flatten": False, "ok_t": 0.0, "configured": False}


def _gh(method, url, payload=None):
    tok = os.environ.get("GH_TOKEN")
    if not tok:
        return None
    req = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                                          "User-Agent": "shachar", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def _issue(title, body):
    repo = os.environ.get("REPO")
    if not repo or not os.environ.get("GH_TOKEN") or os.environ.get("ALERT_ISSUES", "1") != "1":
        return
    try:
        _gh("POST", "%s/repos/%s/issues" % (GH_API, repo), {"title": PREFIX + title[:120], "body": body[:3000] or title})
    except Exception as e:
        store.jlog({"event": "alert_issue_failed", "err": str(e)[:120]})


def _telegram(text):
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not (tok and chat):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[:900]}).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data=data, timeout=10)
    except Exception:
        pass


def alert(title, body="", dedupe_s=6 * 3600):
    """Never raises. The same title is sent at most once per `dedupe_s` (per process)."""
    try:
        store.jlog({"event": "alert", "title": title, "body": body[:500]})
        now = time.time()
        if now - _sent.get(title, 0) < dedupe_s:
            return
        _sent[title] = now
        _issue(title, body)
        _telegram(PREFIX + title + ("\n" + body if body else ""))
    except Exception:
        pass


def halt(why):
    store.write_halt(why)
    alert("HALT: " + why[:90], why + "\n\nNo new buys until the HALT file is removed from the `state` branch. "
          "Open positions keep their stops.")


def parse_flags(issues):
    """Pure function (tested). Only issues written by the repo owner count; pull requests and bot alerts are ignored."""
    halt_, flat, resume = False, False, []
    for it in issues or []:
        if "pull_request" in it or it.get("author_association") != "OWNER":
            continue
        t = (it.get("title") or "").strip().upper()
        if t.startswith("FLATTEN"):
            flat = True
        elif t.startswith("HALT"):
            halt_ = True
        elif t.startswith("RESUME"):
            resume.append(it.get("number"))
    return {"halt": halt_ or flat, "flatten": flat, "resume": [] if (halt_ or flat) else resume}


def _resume(repo, numbers):
    """The owner asked to continue: remove the local HALT, restart the drawdown peak, close the request."""
    had = store.path("HALT").exists()
    if had:
        store.path("HALT").unlink()
    with store.LOCK:
        for mode in ("dry", "live"):
            if store.exists(mode):
                s = store.load(mode)
                for b in s["bots"].values():
                    b["peak"] = None              # the next cycle starts the peak again from the current equity
                store.save(mode, s)
    store.jlog({"event": "resume", "by_issue": numbers, "halt_removed": had})
    store.request_save()
    for n in numbers:
        try:
            _gh("POST", "%s/repos/%s/issues/%s/comments" % (GH_API, repo, n),
                {"body": "Resumed: HALT %s, drawdown peak restarted." % ("removed" if had else "was not set")})
            _gh("PATCH", "%s/repos/%s/issues/%s" % (GH_API, repo, n), {"state": "closed"})
        except Exception as e:
            store.jlog({"event": "resume_close_failed", "err": str(e)[:120]})


def refresh_flags():
    """Poll the owner's control issues (call about once a minute, never while holding a lock)."""
    repo = os.environ.get("REPO")
    if not repo or not os.environ.get("GH_TOKEN"):
        _flags["configured"] = False
        return dict(_flags)
    _flags["configured"] = True
    try:
        issues = _gh("GET", "%s/repos/%s/issues?state=open&per_page=50" % (GH_API, repo))
        f = parse_flags(issues)
        _flags.update(halt=f["halt"], flatten=f["flatten"], ok_t=time.time())
        if f["resume"]:
            _resume(repo, f["resume"])
    except Exception as e:
        store.jlog({"event": "controls_unreachable", "err": str(e)[:120]})
    return dict(_flags)


def remote_halt(mode="dry"):
    if not _flags["configured"]:
        return False
    if _flags["halt"] or _flags["flatten"]:
        return True
    return mode == "live" and _flags["ok_t"] > 0 and time.time() - _flags["ok_t"] > STALE_S


def remote_flatten():
    return bool(_flags["configured"] and _flags["flatten"])
