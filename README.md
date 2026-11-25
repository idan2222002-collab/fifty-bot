# fifty-bot (brain: Shachar)

Separate experiment. Not connected to any other project, repo, key or account.
See SHACHAR.md for the brief, POLICY.md for the hard limits, knowledge.md for what the brain reads every cycle.
Everything here is free: Python standard library only, GitHub Actions as the host, a free LLM tier, GitHub issues
for alerts and for the owner's remote controls.

## Files
- guard.py: the always-on loop (price guard, brain thread, housekeeping thread).
- shachar.py: decision cycle, hard gates, order execution in three steps (plan, place, apply), the ledger,
  stop mirroring, per-asset reconcile, the replay.
- kraken.py: Kraken connector (spot only, allow-list). Live orders are OFF unless SHACHAR_LIVE=YES_IDAN_APPROVED;
  otherwise orders are only validated.
- brain.py: LLM brain (OpenAI-compatible endpoint). No key or no usable answer means HOLD.
- policy.py: hard limits (venue, cash fraction per buy, trade caps on real fills, banned categories, payees).
- scanner.py: price scanner with a warm start. news.py: fear and greed index and headlines (untrusted text).
- store.py: state files (atomic writes), journal, HALT flag. notify.py: alerts and the owner's remote controls.
- baseline.py: do the setups beat random entries and buy-and-hold? Run it before believing any strategy.
- test_shachar.py: 68 safety and behaviour tests, no network. The workflow refuses to start the bot if one fails.

## Run
- `python -m unittest test_shachar`       all tests
- `python guard.py dry`                   paper ledger on real prices (no key needed)
- `python guard.py dry --validate`        also sends each order to Kraken with validate=true (nothing is placed)
- `python guard.py live`                  real orders, only with the live token set by the owner
- `python shachar.py status dry|live`     one line per bot
- `python shachar.py reconcile live`      compare the ledger with the real account
- `python shachar.py adopt live`          owner tool: rebuild the ledger from the real account after a HALT
- `python baseline.py`                    edge check on public data

Environment: SHACHAR_DATA (folder for state, default: this folder), TICK (default 2s), BRAIN_EVERY (default 300s),
LLM_KEY, LLM_MODELS, KRAKEN_KEY, KRAKEN_SECRET, SHACHAR_LIVE, SHACHAR_HALT=1, IDLE_DEATH=0|1, REPLAY_GATE=0|1, RR_GATE=0|1,
NEWS_HEADLINES=0, TG_TOKEN + TG_CHAT (optional Telegram alerts).

## Owner controls (from the phone, in the GitHub app)
Open an issue in this repo. Only issues written by the repo owner count.
- title starts with `HALT`: no new buys. Stops and exits keep working. Close the issue to lift it.
- title starts with `FLATTEN`: sell everything to USDC, no new buys. Close the issue to lift it.
- title starts with `RESUME`: after the bot halted itself (reconcile mismatch, drawdown breaker, unknown order
  state): removes the HALT file, restarts the drawdown peak, answers and closes the issue.
The controls are read once a minute. The last resort is always: delete the API key on Kraken.
Alerts (halt, guard exits, failed orders, missing stop, blind guard, death) arrive as issues titled `[shachar] ...`.

## What protects the money
1. The brain only proposes. parse_decision() validates its JSON before any order exists.
2. Gates in code: spot only; one position per coin, at most 2; size cut so a stop-out costs at most 2% of equity and at most 50% of cash per buy; liquidity gate;
   cooldown; minimum hold; trade caps; HALT; 15% drawdown breaker; 50% hard equity stop.
3. Every position has a stop-loss order on Kraken itself. The host guard adds take-profit, trailing stop
   (the Kraken order follows it), time stop.
4. Kraken is the source of truth: fired stops are mirrored into the ledger every 15s, the ledger is compared with
   the account per asset every 10 minutes and at start, unknown open orders are cancelled at start.
5. Anything the code cannot explain (unknown order state, mismatch) writes HALT and alerts the owner.

The brain stays free: code never judges WHICH trade is good. A poor reward/risk ratio or a setup whose replay lost
money only produces a warning that the brain reads next cycle. The owner can turn either into a hard gate with
RR_GATE=1 or REPLAY_GATE=1.

## Before real money (the owner's checklist)
1. Paper run for 4-8 weeks. The paper ledger fills a maker entry only when the market really trades through it.
2. `python baseline.py`: the setups must beat random entries by a clear margin, net of fees.
3. Kraken API key with ONLY: query funds, query orders, create/modify orders, cancel orders. No withdrawal.
   A separate Kraken account with nothing but the stake.
4. Canary with 10-15 USDC: watch one full life cycle (entry, stop armed, guard exit, stop fired on purpose, restart
   in the middle of a position). Check that Balance asset codes match the ledger (`python shachar.py reconcile live`).
5. Pin the two actions in the workflow to commit SHAs.

## Survival and reproduction
Dead when equity < 3 USDC. Idle strikes (24h without a trade) are counted; 3 strikes kill in the paper game, but with
real money they are not fatal unless IDLE_DEATH=1, because forcing trades to stay alive loses money.
At 2x start with enough free cash, a child is funded with half the profit above start (paper mode only for now:
several bots in one Kraken account would fight over the same coins and stops).

Legal note: the account belongs to the human owner (KYC). Gains are taxable income for him.
Keys live only in GitHub secrets or a vault, never in this repo. Real money moves only after the owner says go.
Using GitHub Actions as an always-on host may not fit GitHub's terms for Actions; read them, and move to any other
free host (the Dockerfile works anywhere) if in doubt.
