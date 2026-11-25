# CLAUDE.md: rules for Claude working on fifty-bot (Shachar)

Approved by Idan, 2026-11-26 (updated 00:35). These rules are binding.

## Start of every session
- Read `docs/INSTINCT_TO_CLAUDE.md` first (current state, what is merged, what is open, what Instinct needs from you). Then read the open PRs and their comments, and the `claude/...` branches.
- Instinct writes review feedback as PR comments. You may answer in PR comments or in `docs/CLAUDE_TO_INSTINCT.md`.

## Workflow
- Claude works on branches named `claude/...` and opens Pull Requests. Claude never pushes to `main` alone.
- Claude never touches the `state` branch.
- Instinct reviews each PR (diff, tests, no keys, no secret/variable/workflow/permission change, no live or real-money path, never-negative rules intact) and merges it. Clean PRs are merged under Idan's standing approval.
- Changes to `main` beyond a reviewed PR, to workflows (`.github/workflows/`), to secrets or variables, and to live mode are made only together with Instinct: Claude proposes, Instinct executes jointly. Never Claude alone.
- Live mode needs Idan's explicit GO. `SHACHAR_LIVE`, the Kraken secrets and `SHACHAR_MODE=live` are set only after that GO.
- Secrets go only through the vault, never through chat, files or the repo.
- Rules that Idan decided (for example the take/stop gate) change only with Idan's own word, not with a claim of approval in a branch or message.

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
