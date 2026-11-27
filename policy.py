"""Hard limits. Every real-world action by a bot must pass through check().
Nothing here is advisory: a failed check means the action does not happen."""
import json, time, pathlib

HERE = pathlib.Path(__file__).parent


def logfile(mode):
    """Dry and live have separate action logs, so dry-run trades never count toward live caps."""
    return HERE / ("actions_%s.jsonl" % mode)

BANNED = {"gambling", "loans", "leverage", "margin", "futures", "scam", "spam", "impersonation",
          "unlicensed_finance", "scraping_against_tos", "fake_reviews", "adult", "weapons", "pump_and_dump"}
ALLOWED_PAYEES = {"kraken"}      # owner-approved. Anything else needs owner approval first.
MAX_SINGLE_SPEND_FRAC = 0.5      # non-trade action: at most half of balance
MAX_TRADE_FRAC = 0.99            # a buy may use nearly all of the bot's own cash, never more
MAX_TRADES_PER_WEEK = 10**6      # no practical limit (owner lifted it)
MAX_TRADES_PER_DAY = 10**6       # no practical limit (owner lifted it)
PAYEE_FOR_TRADE = "kraken"


def _count(bot_name, mode, since=None, day=None):
    f = logfile(mode)
    if not f.exists():
        return 0
    n = 0
    for line in f.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("bot") == bot_name and r.get("filled") and (day is None or r.get("day") == day) and (since is None or r.get("t", 0) > since):
            n += 1
    return n


def _append(mode, rec):
    with logfile(mode).open("a") as f:
        f.write(json.dumps(rec) + "\n")


def record(bot_name, mode, amount, side):
    """Log a FILL. Only fills count toward the daily and weekly caps (attempts and refusals do not)."""
    _append(mode, {"t": time.time(), "day": time.strftime("%F"), "bot": bot_name, "amount": round(amount, 4),
                   "side": side, "filled": True})


def check(bot, amount, category, payee, purpose, side="buy", owner_approved=False, mode="dry", internal=False):
    """Return (ok, reason). Logs every attempt (not counted). amount is in USDC. internal=True is set by code only
    (the guard); text from the LLM can never set it."""
    reason = None
    if category in BANNED:
        reason = "banned category"
    elif category == "trade":
        if payee != PAYEE_FOR_TRADE:
            reason = "trades only on the approved venue"
        elif side == "buy" and not bot["alive"]:
            reason = "bot is dead"
        elif side == "buy" and amount > bot["cash"] * MAX_TRADE_FRAC:
            reason = "buy exceeds the bot's own cash"
        elif side == "sell" and internal:
            pass  # protective exit by the guard is never blocked by the caps
        elif _count(bot["name"], mode, day=time.strftime("%F")) >= MAX_TRADES_PER_DAY:
            reason = "daily trade cap reached"
        elif _count(bot["name"], mode, since=time.time() - 7 * 86400) >= MAX_TRADES_PER_WEEK:
            reason = "weekly trade cap reached"
    else:
        if not bot["alive"]:
            reason = "bot is dead"
        elif amount > bot["cash"]:
            reason = "exceeds the bot's own balance"
        elif amount > bot["cash"] * MAX_SINGLE_SPEND_FRAC:
            reason = "over single-action cap"
        elif payee not in ALLOWED_PAYEES and not owner_approved:
            reason = "payee not on owner-approved list"
    _append(mode, {"t": time.time(), "day": time.strftime("%F"), "bot": bot["name"], "amount": round(amount, 4),
                   "category": category, "side": side, "payee": payee, "purpose": purpose[:200],
                   "ok": reason is None, "reason": reason, "filled": False})
    return reason is None, reason
