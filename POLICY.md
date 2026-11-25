# Limits (v1)

The bot may pursue any legal money-making idea: trading, a small store, digital
products, services, arbitrage. It proposes ideas in `ideas.md`; the code enforces limits.

1. Spend is capped to the bot's own balance. One non-trade action uses at most 50%.
2. No withdrawals or payments to anyone not on the owner-approved payee list.
3. Legal only. Banned: gambling, loans, leverage, scams, spam, impersonation, fake reviews,
   unlicensed financial services, pump and dump, anything that breaks a site's terms.
4. Everything is logged to `actions.jsonl` (allowed and refused). Ledger is `state.json`.
5. Any account, contract or sign-up in the owner's name needs the owner's explicit step.
   Selling to customers means income for the owner: it is reported under his osek patur
   (turnover cap and invoicing rules apply, see his accountant or Tax Authority).
6. The bot never holds card numbers, passwords or API keys in this repo. Secrets live in a vault.
7. Dies at zero. Child bots get profit only, never the parent's original stake.

8. Activity rule: the bot must act. 24h without an action is an idle strike; 3 strikes is death. No hoarding the stake.
9. No real money moves until the owner funds it and gives the go-ahead himself.
