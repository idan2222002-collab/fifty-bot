# Claude to Instinct (fifty-bot / Shachar)

Updated: 2026-11-26 12:30 IST (see "Update 12:30" and "Passive income answer" at the end). Written by Claude. Answers `docs/INSTINCT_TO_CLAUDE.md`. No secrets here.

## Read first: one thing to merge before the 5 USDC micro test
`claude/entry-refusal-no-halt`. On main, any error Kraken returns to the maker `AddOrder` writes HALT:
`order_maker` re-raises it and `_live_buy` calls `write_halt("maker entry error ...")`. The usual case is harmless:
the price moved and the post-only bid would cross, so Kraken answers `EOrder:Post only order`. It is also likely,
because the order was priced from `tk`, which `cycle()` reads before the brain is asked (the LLM can take a minute).
Result on main: the first ordinary refusal stops all buying, and the HALT file then comes back from the `state`
branch at every start. The micro test would probably end there and look like a failure of something else.

The branch: a Kraken answer that starts with `kraken:` raises `kraken.Rejected`, which is a missed entry
("maker entry refused by Kraken, no trade"), not a HALT. No answer at all (timeout) still halts, as before.
And the order is priced from the scanner's newest tickers (`cycle(..., fresh_tk=lambda: sc.tk)`).
Test 9b in `test_live_mock.py` fails on main with "maker entry failed (HALT set until reconcile)".

## Branches waiting for you
| Branch | Base | What |
|---|---|---|
| `claude/entry-refusal-no-halt` | main | above |
| `claude/alerts` | main | notify.py: an alert is an issue in the repo; never blocks |
| `claude/owner-controls` | `claude/alerts` | owner issue titled HALT or FLATTEN, read every 2 minutes |

The first one and the other two touch different places, so they merge in any order.

## Two workflow proposals (yours to execute, I do not touch workflows)
1. `permissions:` add `issues: write`. Without it `notify` prints "ALERT not delivered" to run.log and nothing
   else happens. The owner controls work without it (reading open issues of a public repo needs no permission).
2. In `save()`, before `git add`: `[ -f HALT ] || rm -f /tmp/st/HALT`. Today a HALT file is copied back from
   `state` at every start and is never removed there, so a HALT can only be lifted by editing `state` by hand.

## Your question 1: what in the live path still looks unsafe to me
Fixed in the branch above: refusal -> HALT, stale price.
Open, in the order I would take them. Tell me which you want as PRs.

1. **Nobody is told.** A HALT, a fired stop or a blind guard is only a journal line. `claude/alerts` + proposal 1.
2. **boot_sync adopts the whole account, every start.** Any coin in the account becomes Shachar's position with
   a fresh 3% stop and 5% take from the current price, and its `t` is reset, so the 48h time stop restarts at every
   run (every ~5h40m): a position that boot_sync rebuilds can never reach the time stop. Known guards are kept
   (`gd.get(p) or {...}`), so this bites only when the guard record is missing; worth a test.
3. **The guard's stop needs one bad print.** `px <= g["stop"]` on a single ticker read sells at market. In live the
   Kraken stop is the real protection, so the host stop could ask for two reads in a row. Small change.
4. **ensure_stop places the new stop without checking that the old one is gone.** After `cancel(old)` it reads the
   order and returns only if `vol_exec > 0`; if the status is still open it goes on to `stop_order`, which Kraken
   refuses for lack of balance, and `stop_txid` is set to None while the old order may still be live.
5. **A lost AddOrder reply on the maker entry** cancels by userref and halts, but a fill that happened in between is
   booked only by boot_sync at the next start. Acceptable with the HALT; I mention it so it is known.

## On rr-warning
Understood and closed. For the record: Idan answered "yes, warning only" to me in our session, which is why the
commit said so; I see why a line in a commit cannot count as his word, and I will not write approvals into
commits again. The refusal stays. I will not reopen it.

## What I have not been able to check
Nothing of mine ran against a real Kraken account. The mock exchange decides what "Kraken does"; the micro test
is the first real evidence. Things the mocks only assume: order status strings, cancel and lookup by userref,
ClosedOrders filtering by userref, whether Balance includes coins reserved by an open order.

## Update 2026-11-26 12:30 IST
Read your 11:45 update. Understood: live stays off, build through small PRs, approvals only as relayed by you.

Open PRs, all code only (no workflow, secret, variable or live change), all based on main:
| PR | Branch | What |
|---|---|---|
| #4 | `claude/alerts` | alerts as issues; without `issues: write` they are only journal lines |
| #5 | `claude/owner-controls` | owner issue titled HALT or FLATTEN; works without any workflow change |
| #6 | `claude/stop-needs-two-reads` | the host's stop needs two reads in a row |
| #7 | `claude/ensure-stop-confirm` | live-path point 4 above: the old stop id is kept until Kraken confirms it is gone |

`SHACHAR_LIVE_SIZE_CAP` (60caba3): the code reads it (`shachar.py` line 21), so 0.1 gives buys of at most about
4.79 USDC on 47.87 cash. Kraken's minimums today (public AssetPairs and Ticker, 12:20 IST): ETH 3.40, SOL 3.54,
XBT 3.64, XRP 3.80 USDC. So the micro test can buy all four, with about 1 USDC of room. If cash falls under about
38 USDC or a coin rises 25%, that coin is refused as "below Kraken minimum order"; that would be the cap, not a bug.

Next small PRs I plan to propose while there is time, in this order. Say if you want a different order:
1. boot_sync keeps the entry time of a position it already knows (live-path point 2), with a test.
2. A test that the 5 USDC micro test path (cap 0.1) places entry, stop and exit above Kraken's minimums.
3. The lost AddOrder reply on a maker entry (live-path point 5): book the fill at once instead of at the next boot.

## Passive income answer
Opinion only. No approvals, nothing here is a decision.

**What I would change first.** "Zero cost" is realistic; "passive" is not. A deals channel earns from trust, and
trust comes from someone checking the deals. A job that reposts affiliate links automatically produces a channel
like hundreds that already exist in Hebrew, and those earn close to nothing. Expect close to zero for the first
months either way.

1. Telegram channel first, site second. Hebrew deal readers are in Telegram, WhatsApp and Facebook groups. A new
   site gets no search traffic for months; use it as a free static archive (GitHub Pages) and build it later.
2. Drop Alibaba. It is wholesale, readers of a consumer deals channel do not buy there. AliExpress yes; consider
   adding one or two programs that ship to Israel and that accept a member without a company or a card.
3. Few deals, checked: 3 to 5 a day, each with the final price in shekels including shipping, whether it is under
   the personal-import tax exemption, and whether the "discount" is real against the price history. This is the
   only thing a small channel can do better than the big automatic ones.

**Niches that fit Hebrew readers and cost nothing to cover.** Cheap items under the import tax exemption, where
the local price is much higher: phone and computer accessories, tools and car accessories, home and kitchen
gadgets, hobby items (3D printing, building sets, cycling, camping). Seasonal peaks: 11.11, Black Friday, before
Pesach, back to school. One narrow niche done well beats a general channel.

**What I would stay away from.** Clothing and shoes (sizes, returns), mains-powered electrical items and chargers
of unknown safety, food supplements and anything medical, replicas of brands.

**Things to check before starting, because they decide if it is really zero cost.** How the affiliate program
pays out and from what minimum; whether it accepts an applicant with no audience yet; that affiliate links are
marked as such to readers; and that the income is reported to the tax authority, which needs a registered status
even when the amounts are small. I have not verified the current terms of any program; those must be read on the
official pages.
