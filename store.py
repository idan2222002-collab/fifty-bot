"""Persistent state, journal and the HALT flag. Stdlib only.
All files live in DATA (this folder by default, or the folder named by SHACHAR_DATA).
Rule for every caller: change the ledger only inside `with LOCK: s = load(mode); ...; save(mode, s)`.
Never keep a loaded state object across a network call."""
import os, json, time, pathlib, threading

DATA = pathlib.Path(os.environ.get("SHACHAR_DATA") or pathlib.Path(__file__).parent)
LOCK = threading.RLock()          # guards the state file; held only for load-change-save, never for network I/O
_JLOCK = threading.Lock()


def path(name):
    return pathlib.Path(DATA) / name


def atomic_write(name, text):
    """Write through a temp file and rename, so a crash can never leave a half-written file."""
    f = path(name)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(6):                        # Windows can refuse the rename for a moment while another program reads the file
        try:
            os.replace(tmp, f)
            return
        except PermissionError:
            if i == 5:
                raise
            time.sleep(0.05 * (i + 1))


def sfile(mode):
    return path("state_%s.json" % mode)


def newbot(name, cash, parent=None):
    now = time.time()
    return {"name": name, "alive": True, "cash": cash, "start": cash, "peak": cash, "pos": {}, "dust": {}, "guards": {},
            "cool": {}, "parent": parent, "children": [], "born": now, "fees": 0.0, "realized": 0.0, "wins": 0,
            "losses": 0, "last_action": now, "idle_strikes": 0, "trades": 0, "trade_ts": [], "last_brain": 0}


def exists(mode):
    return sfile(mode).exists()


def load(mode, start=0.0):
    f = sfile(mode)
    if f.exists():
        s = json.loads(f.read_text(encoding="utf-8"))
        for b in s["bots"].values():          # state files written by older versions: add the fields they lack
            for key, val in newbot(b["name"], b.get("start", 0.0)).items():
                if key not in b:
                    b[key] = val if key not in ("peak", "cash", "start") else b.get("start", 0.0)
        s.setdefault("log", [])
        return s
    return {"mode": mode, "bots": {"shachar": newbot("shachar", start)}, "log": []}


def save(mode, s):
    atomic_write("state_%s.json" % mode, json.dumps(s, indent=1))


def jlog(rec):
    rec = dict(rec, t=time.strftime("%Y-%m-%d %H:%M:%S"))
    with _JLOCK:
        with path("journal.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def halted():
    """Local halt: no new buys. Exits and stops keep working."""
    return os.environ.get("SHACHAR_HALT") == "1" or path("HALT").exists()


def write_halt(why):
    """Fast and local (safe to call while holding LOCK). Use notify.halt() when you also want an alert."""
    with path("HALT").open("a", encoding="utf-8") as f:
        f.write("%s at %s\n" % (why, time.strftime("%Y-%m-%d %H:%M:%S")))
    jlog({"event": "halt", "why": why})


def request_save():
    """Ask the host (the workflow loop) to push state now instead of waiting for the periodic save."""
    try:
        path(".save_now").write_text("1", encoding="utf-8")
    except Exception:
        pass
