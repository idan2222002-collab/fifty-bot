# CLAUDE.md: rules for Claude working on fifty-bot (Shachar)

Approved by Idan, 2026-11-26. These rules are binding.

## Workflow
- Work only on branches named `claude/...` and open Pull Requests. Never push to `main`.
- Never touch the `state` branch.
- Instinct reviews and merges, only after Idan approves.
- Do not change workflows (`.github/workflows/`), secrets or variables.
- Do not turn on live mode. `SHACHAR_LIVE`, the Kraken secrets and `SHACHAR_MODE=live` are set only after Idan's explicit GO.

## Hard safety rules (never weaken)
- No keys, tokens, secrets or personal data anywhere in the repo or its history.
- Spot only. Never a negative balance. No leverage, margin, shorting or futures.
- Free only: no card, no phone, no paid service.
- Kraken only; every order goes through `kraken.assert_spot`.

## Tests (must pass before a PR)
- `python3 test_safety.py`
- `python3 test_live_mock.py`

## Notes
- A PR should say what it changes, why, and which tests cover it.
- Keep changes small. Do not remove a safety check without Idan's approval.
