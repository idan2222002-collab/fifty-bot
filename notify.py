"""Alerts for the owner. Free, standard library only, no phone number and no extra account.

An alert becomes an issue in this repository, so GitHub tells the owner by mail and in its phone app.
It needs GH_TOKEN and the repository name in the environment (the workflow already passes both) and the
`issues: write` permission in the workflow. Without them an alert is still written to the journal by the caller
and the failure is printed to the run log: nothing breaks.

alert() never blocks and never raises. It is safe to call while holding the state lock or in the middle of the
guard loop: the network call happens on a background thread."""
import os, json, time, queue, threading, urllib.request

GH_API = "https://api.github.com"
PREFIX = "[shachar] "
DEDUPE_S = 6 * 3600           # the same title is sent at most once in this time (per process)
_q = queue.Queue()
_sent = {}
_lock = threading.Lock()
_worker = [None]
LAST = {"result": None}       # result of the last delivery attempt, for the run log and for tests


def repo():
    return os.environ.get("GITHUB_REPOSITORY") or os.environ.get("REPO")


def _gh(method, url, payload=None, auth=True):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "shachar", "Content-Type": "application/json"}
    tok = os.environ.get("GH_TOKEN")
    if auth and tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(payload).encode() if payload is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))


def _send(title, body):
    r = repo()
    if not (r and os.environ.get("GH_TOKEN")):
        return "not configured (no GH_TOKEN or repository name)"
    _gh("POST", "%s/repos/%s/issues" % (GH_API, r), {"title": PREFIX + title[:120], "body": (body or title)[:3000]})
    return "sent"


def _run():
    while True:
        title, body = _q.get()
        try:
            LAST["result"] = _send(title, body)
        except Exception as e:
            LAST["result"] = "failed: %s" % str(e)[:120]
        finally:
            if LAST["result"] != "sent":
                print("ALERT not delivered (%s): %s" % (LAST["result"], title))
            _q.task_done()


def alert(title, body="", dedupe_s=None):
    """Queue an alert. Returns True if queued, False if the same title was sent recently."""
    try:
        now = time.time()
        with _lock:
            if now - _sent.get(title, 0) < (DEDUPE_S if dedupe_s is None else dedupe_s):
                return False
            _sent[title] = now
            if _worker[0] is None or not _worker[0].is_alive():
                _worker[0] = threading.Thread(target=_run, daemon=True)
                _worker[0].start()
        _q.put((str(title), str(body)))
        return True
    except Exception:
        return False


def drain(timeout=10):
    """Wait until queued alerts were attempted (used at shutdown and in tests). Returns True if the queue is empty."""
    end = time.time() + timeout
    while _q.unfinished_tasks and time.time() < end:
        time.sleep(0.05)
    return not _q.unfinished_tasks
