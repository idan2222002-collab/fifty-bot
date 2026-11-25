# fifty-bot

Separate experiment. Not connected to any other project, repo, key or account.

A bot gets a small stake (about 50 USD, about 180 NIS). If it hits zero it dies.
When its equity reaches 2x, it funds a child bot with half of the profit above
its start (never the original stake). Trading is the first strategy, but the bot may pursue any legal idea within POLICY.md.
All actions go through `policy.check()`.

Legal note: the account belongs to the human owner (KYC). "Dying" and "child
bots" are ledger rules in code. Gains are taxable income for the owner.

## Phases
- Phase 0 (now): paper mode, no money, no keys. `python bot.py tick`
- Phase 1: owner opens Bit2C, deposits the stake himself, creates a trade-only
  API key (no withdrawal). Key goes in a vault, never in this repo.
- Hard rules: max loss = deposit, no withdrawals by the bot, spot only.

## Run
`python bot.py tick` then `python bot.py status`. State is `state.json`.
The workflow in `.github/workflows/tick.yml` runs a tick every 30 minutes.
