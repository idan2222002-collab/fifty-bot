"""Hard limits. Every real-world action by a bot must pass through check().
Nothing here is advisory: a failed check means the action does not happen."""
import json, time, pathlib

LOG = pathlib.Path(__file__).with_name("actions.jsonl")
BANNED = {"gambling", "loans", "leverage", "margin", "futures", "scam", "spam", "impersonation",
          "unlicensed_finance", "scraping_against_tos", "fake_reviews", "adult", "weapons", "pump_and_dump"}
ALLOWED_PAYEES = {"kraken"}      # owner-approved. Anything else needs owner approval first.
MAX_SINGLE_SPEND_FRAC = 0.5      # non-trade action: at most half of balance
MAX_TRADE_FRAC = 0.9             # one buy may use at most 90% of the bot's cash
MAX_TRADES_PER_DAY = 6           # per bot, stops fee-burning busywork
PAYEE_FOR_TRADE = "kraken"


def _today_trades(bot_name):
    if not LOG.exists():
        return 0
    day = time.strftime("%F")
    n = 0
    for line in LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("bot") == bot_name and r.get("ok") and r.get("category") == "trade" and r.get("day") == day:
            n += 1
    return n


def check(bot, amount, category, payee, purpose, side="buy", owner_approved=False):
    """Return (ok, reason). Logs every attempt, allowed or not. amount is in USDC."""
    reason = None
    if not bot["alive"]:
        reason = "bot is dead"
    elif category in BANNED:
        reason = "banned category"
    elif category == "trade":
        if payee != PAYEE_FOR_TRADE:
            reason = "trades only on the approved venue"
        elif side == "buy" and amount > bot["cash"] * MAX_TRADE_FRAC:
            reason = "buy exceeds 90% of the bot's own cash"
        elif side == "sell" and purpose.startswith("guard:"):
            pass  # protective exit by the 1-second guard is never blocked by the daily cap
        elif _today_trades(bot["name"]) >= MAX_TRADES_PER_DAY:
            reason = "daily trade cap reached"
    else:
        if amount > bot["cash"]:
            reason = "exceeds the bot's own balance"
        elif amount > bot["cash"] * MAX_SINGLE_SPEND_FRAC:
            reason = "over single-action cap"
        elif payee not in ALLOWED_PAYEES and not owner_approved:
            reason = "payee not on owner-approved list"
    ok = reason is None
    with LOG.open("a") as f:
        f.write(json.dumps({"t": time.time(), "day": time.strftime("%F"), "bot": bot["name"], "amount": round(amount, 4),
                            "category": category, "side": side, "payee": payee, "purpose": purpose[:200],
                            "ok": ok, "reason": reason}) + "\n")
    return ok, reason
