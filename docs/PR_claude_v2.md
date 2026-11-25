# claude/v2-hardening: what this branch changes and how to review it

Written by Claude for the owner and for the agent that maintains `main`. Nothing here touches `main`, the `state`
branch, any secret, or any workflow file. The owner merges.

## How to check it
- `python -m unittest test_shachar` (68 tests, no network, no keys, standard library only)
- `python guard.py dry` runs on the existing `state_dry.json` unchanged: the open paper BTC position, its stop and
  take levels, cash, fees and trade count load as they are (checked against commit 4f4765b of `state`).
- `python baseline.py` prints the edge check on public Kraken data.

`test_safety.py` and `test_live_mock.py` are removed because they test function signatures that changed; every case
they covered has an equivalent in `test_shachar.py` (stop fired, sell with a stop active, failed sell, restart
mid-position, lost AddOrder reply, LLM input validation, size cap, no guard-prefix bypass).

## Why, relative to main at 1e140c9
`main` already has most of the first review's fixes. These are the gaps this branch closes:

1. **A market sell can execute and never be booked.** `Kraken.order()` polls `QueryOrders` without a try; one
   network error after `AddOrder` raises, `_live_sell` reports "sell failed", and the coins are gone while the ledger
   still holds them. Here the poll tolerates errors, a lost `AddOrder` reply is recovered by its userref, and an order
   whose fate cannot be established raises `AmbiguousOrder`, which writes HALT.
2. **DOGE.** Kraken's altname is `XDGUSDC`, so `UNIVERSE` ("DOGEUSDC") never matches in `assert_spot`, and
   `reconcile` looks for "DOGE" in the balances. Here the balance code comes from Kraken's own `base` field.
3. **Paper results are optimistic.** A paper maker entry fills at once and in full. Here it fills only if the market
   really trades through the price within 90 seconds, otherwise it expires, as it would on Kraken.
4. **No alerts and no way to resume.** Here: alerts as GitHub issues (and optional Telegram), owner controls by
   issue title (`HALT`, `FLATTEN`, `RESUME`), read once a minute. See the note on permissions below.
5. **An LLM error uses up the 40-minute brain window.** Here an error only waits 10 minutes.
6. **The scanner is blind for an hour after every restart** (every 5h40m). Here it is seeded from 1-minute candles.
7. **The replay uses closes only and 1.6% fees.** Here it uses each candle's high and low, the stop wins a tie, a
   gap exits at the open, and the fee is the real 1.2% (maker in, taker out).
8. **Size.** A buy is also cut so that a stop-out costs at most 2% of equity. One position per coin, at most two.
9. **Clean stop.** On SIGTERM a working maker order is cancelled before the process exits.
10. **Stop needs two ticks** under the level, so one bad print does not sell.

## Free mind
The owner's rule: Shachar's mind is free and his one goal is to survive. So code enforces survival rules only.
`main` refuses a buy when `take_pct / stop_pct < 1.5`. That is a judgement about which trade is good, so here it is
a warning the brain reads next cycle (`me.recent_results`), like the replay result. Owner switches turn either into
a refusal again: `RR_GATE=1`, `REPLAY_GATE=1`. The system prompt now opens with the goal (survive) and the freedom.

## What needs the owner's decision
- **Workflow.** This branch runs on the current `shachar-run.yml` as it is. To get alerts as issues, the RESUME
  answer, tests before start, a state push right after each trade and a clean stop, the owner can adopt
  `docs/proposed-shachar-run.yml` (it adds `issues: write`). Until then alerts are only written to the journal.
- **Idle strikes** are counted but not fatal in live (`IDLE_DEATH=1` restores death). `main` already treats idle
  time as informational, so this matches.
- **Hard equity stop** at 50% of start sells everything and halts. `main` has no sell-all. Remove it in `guard.py`
  (`HARD_EQUITY_STOP`) if the owner prefers stops only.
- **Boot.** `main` rebuilds the ledger from Kraken at every live start. This branch cancels unknown orders, mirrors
  the stops, reconciles, and HALTs with an alert on a mismatch; the owner then runs `python shachar.py adopt live`.
  Safer, but it needs a person. Say so if the automatic rebuild is preferred.

## Not verified
Nothing here ran against a real Kraken account or inside GitHub Actions. The private-API behaviour the tests
simulate (order status values, cancel and lookup by userref, whether Balance includes coins reserved by an order)
must be confirmed in a small live canary before real money. The last measured month shows no edge for the three
setups over random entries (`python baseline.py`); going live stays the owner's decision.
