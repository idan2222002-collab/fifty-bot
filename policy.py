"""Hard limits. Every real-world action by a bot must pass through check().
Nothing here is advisory: a failed check means the action does not happen.
Trade caps count REAL fills (bot["trade_ts"], kept in the per-mode state), not attempts, so paper trades and
unfilled orders never use up the live allowance."""
import json, time
import store

BANNED = {"gambling", "loans", "leverage", "margin", "futures", "scam", "spam", "impersonation",
          "unlicensed_finance", "scraping_against_tos", "fake_reviews", "adult", "weapons", "pump_and_dump"}
ALLOWED_PAYEES = {"kraken"}      # owner-approved. Anything else needs owner approval first.
MAX_SINGLE_SPEND_FRAC = 0.5      # non-trade action: at most half of balance
MAX_TRADE_FRAC = 0.5             # one buy may use at most 50% of the bot's cash
MAX_TRADES_PER_DAY = 8           # per bot, rolling 24h
MAX_TRADES_PER_WEEK = 25         # per bot, rolling 7 days
PAYEE_FOR_TRADE = "kraken"


def trades_since(bot, secs):
    cut = time.time() - secs
    return sum(1 for t in bot.get("trade_ts", []) if t > cut)


def check(bot, amount, category, payee, purpose, side="buy", guard=False, owner_approved=False, mode="dry"):
    """Return (ok, reason). Logs every attempt. `guard` is set by CODE for protective exits; it is never derived
    from text written by the brain."""
    reason = None
    if not bot["alive"] and not (guard and side == "sell"):
        reason = "bot is dead"
    elif category in BANNED:
        reason = "banned category"
    elif category == "trade":
        if payee != PAYEE_FOR_TRADE:
            reason = "trades only on the approved venue"
        elif side == "buy" and amount > bot["cash"] * MAX_TRADE_FRAC + 1e-9:
            reason = "buy exceeds %d%% of the bot's own cash" % (MAX_TRADE_FRAC * 100)
        elif guard and side == "sell":
            pass                      # a protective exit is never blocked by the trade caps
        elif trades_since(bot, 86400) >= MAX_TRADES_PER_DAY:
            reason = "daily trade cap reached"
        elif trades_since(bot, 7 * 86400) >= MAX_TRADES_PER_WEEK:
            reason = "weekly trade cap reached"
    else:
        if amount > bot["cash"]:
            reason = "exceeds the bot's own balance"
        elif amount > bot["cash"] * MAX_SINGLE_SPEND_FRAC:
            reason = "over single-action cap"
        elif payee not in ALLOWED_PAYEES and not owner_approved:
            reason = "payee not on owner-approved list"
    ok = reason is None
    try:
        with store.path("actions_%s.jsonl" % mode).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "bot": bot["name"], "amount": round(amount, 4), "category": category,
                                "side": side, "payee": payee, "purpose": str(purpose)[:200], "guard": bool(guard),
                                "ok": ok, "reason": reason}) + "\n")
    except Exception:
        pass
    return ok, reason
