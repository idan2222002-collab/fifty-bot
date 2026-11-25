"""Hard limits. Every real-world action by a bot must pass through check().
Nothing here is advisory: a failed check means the action does not happen."""
import json, time, pathlib

LOG = pathlib.Path(__file__).with_name("actions.jsonl")
BANNED = {"gambling", "loans", "leverage", "scam", "spam", "impersonation", "unlicensed_finance",
          "scraping_against_tos", "fake_reviews", "adult", "weapons", "pump_and_dump"}
ALLOWED_PAYEES = set()      # owner-approved vendors/exchanges only; empty means nothing external yet
MAX_SINGLE_SPEND_FRAC = 0.5  # one action may use at most half of the bot's current balance


def check(bot, amount, category, payee, purpose, owner_approved=False):
    """Return (ok, reason). Logs every attempt, allowed or not."""
    reason = None
    if not bot["alive"]:
        reason = "bot is dead"
    elif category in BANNED:
        reason = "banned category"
    elif amount > bot["cash"]:
        reason = "exceeds the bot's own balance"
    elif amount > bot["cash"] * MAX_SINGLE_SPEND_FRAC and category != "trade":
        reason = "over single-action cap"
    elif payee not in ALLOWED_PAYEES and category != "trade" and not owner_approved:
        reason = "payee not on owner-approved list"
    ok = reason is None
    with LOG.open("a") as f:
        f.write(json.dumps({"t": time.time(), "bot": bot["name"], "amount": amount, "category": category,
                            "payee": payee, "purpose": purpose, "ok": ok, "reason": reason}) + "\n")
    return ok, reason
