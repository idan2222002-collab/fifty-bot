# Limits (v2)

The bot may pursue any legal money-making idea: trading, a small store, digital
products, services, arbitrage. It proposes ideas in `ideas_proposed.jsonl`; the code enforces limits.

1. Spend is capped to the bot's own balance. One non-trade action uses at most 50%.
2. No withdrawals or payments to anyone not on the owner-approved payee list.
3. Legal only. Banned: gambling, loans, leverage, scams, spam, impersonation, fake reviews,
   unlicensed financial services, pump and dump, anything that breaks a site's terms.
4. Everything is logged to `actions_<mode>.jsonl` (allowed and refused). Ledger is `state_<mode>.json`.
5. Any account, contract or sign-up in the owner's name needs the owner's explicit step.
   Selling to customers means income for the owner: it is reported under his osek patur
   (turnover cap and invoicing rules apply, see his accountant or Tax Authority).
6. The bot never holds card numbers, passwords or API keys in this repo. Secrets live in a vault.
7. Trades: only on Kraken, spot only. One buy uses at most 50% of the bot's cash and is cut so that a stop-out
   costs at most 2% of equity. One position per coin, at most 2 open positions.
   At most 8 trades per rolling 24h and 25 per rolling 7 days, counted on real fills. Protective exits are never capped.
7b. Free mind: code does not judge which trade is good. A buy whose net reward is smaller than its net risk, or whose
   setup lost money in the 30-day replay, goes through with a warning the brain reads. Owner switches: RR_GATE=1,
   REPLAY_GATE=1 turn the warnings into refusals.
7c. Dies under 3 USDC. Before dying it sells what it holds. Child bots get profit only, never the parent's stake.
8. Activity: 24h without a trade is an idle strike. Three strikes kill in the paper game. With real money strikes
   are only counted (owner switch: IDLE_DEATH=1), because a forced trade is a losing trade.
9. No real money moves until the owner funds it and gives the go-ahead himself.
10. The owner can stop the bot at any time: an issue titled HALT or FLATTEN, or deleting the Kraken API key.
    The bot stops itself (HALT) on a 15% drawdown from its peak, on a ledger mismatch, and on any order whose
    state it cannot establish. Only the owner lifts a HALT (an issue titled RESUME).
