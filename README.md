# fifty-bot (brain: Shachar)

Separate experiment. Not connected to any other project, repo, key or account.
See SHACHAR.md for the brief, survival rule and reproduction logic. POLICY.md has the hard limits.

## Files
- shachar.py: the cycle. Reads Kraken prices, news, lessons; asks the brain; acts through policy.check().
- brain.py: LLM brain (OpenAI-compatible endpoint, key from a file outside the repo). With no key it holds.
- kraken.py: Kraken connector (spot). Live orders are OFF unless SHACHAR_LIVE=YES_IDAN_APPROVED; otherwise orders are only validated.
- news.py: free headlines, fear and greed index, trending coins. Treated as untrusted text.
- policy.py: hard limits (trade only on Kraken, max 90% of cash per buy, 6 trades per day, banned categories, owner-approved payees).
- bot.py, state: legacy Phase 0 paper engine (ILS), kept for reference. Not used.

## Run
- `python shachar.py cycle dry` simulated fills at live Kraken prices (no key needed)
- `python shachar.py cycle dry --validate` also sends each order to Kraken with validate=true (nothing is placed)
- `python shachar.py cycle live` real orders, only with the live token set by the owner's go-ahead
- `python shachar.py status dry|live`

## Survival and reproduction
Dead when equity < 3 USDC (no Kraken order possible); positions stay protected. Idle time is informational only.
At 2x start with enough free cash, a child is funded with half the profit above start (max 3 children each).
A child is a ledger allocation inside the same Kraken account.

Legal note: the account belongs to the human owner (KYC). Gains are taxable income for him.
Keys live only in the vault, never in this repo. Real money moves only after the owner says go.
