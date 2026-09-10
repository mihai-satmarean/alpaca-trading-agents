# Port the Tradier SixFold exit rules (take-profit, time-stop, re-entry lockout) into the Alpaca build

**Status:** COMPLETE 2026-09-10. Implemented after approval and review; a 4th bug (score-dispose re-derivation breaking an unreadable-score case) was caught by the test suite during implementation, not by the review, and fixed the same way -- the analyst's flagged set is now passed through as a bool rather than re-derived. 20 mutations caught, including literal reverts of all three BLOCKING findings.
**Date:** 2026-09-10
**Repo:** alpaca-trading-agents (not on the CLAUDE.md critical-repo list, but this
adds automated selling logic for the first time to a live-armed paper account,
so it gets the same plan-first + adversarial-review discipline as those repos)

## Why

Frank asked to understand the Tradier-deployed SixFold exit logic and then
ensure it is live in the Alpaca build, both merged to `main` and running on
AWS. Read directly from the live source, not from memory or a PR description.

## What the Tradier build actually does

Source: `Options-Trader/sixfold_trader/src/exits.mjs` +
`SixfoldExecutor.runExits` in `sixfold_trader/src/executor.mjs`, live on
Lambda `sixfold-trader` since 2026-09-01.

Four rules, precedence matters, ALL scoped to the Lambda's own DynamoDB
registry of positions it opened, never to the account's shares in general:

1. **Time stop, 183 days.** Checked FIRST, independent of price, so it still
   fires when quotes are unavailable.
2. **Take-profit, +35% unrealized gain.** Computed from mark vs. cost basis.
3. **NO stop-loss**, deliberately. A selftest asserts no price level alone
   can trigger an exit.
4. **Score-dispose, composite < 50.**

Plus a **56-day re-entry lockout** after any exit, and execution-side safety:
exits before entries in the same cycle; sell size bounded to
`min(registered, live)`; a resting sell blocks a duplicate; an **unreadable
lockout table fails CLOSED** (`executor.mjs:173-181`, "without it the sleeve
would immediately rebuy what it just sold"); sells are marketable limits
crossing down 20bp; tag `SIXFOLDX-<ACCT>-<SYM>`.

## What the Alpaca build already has, verified by reading `main` directly

`sixfold_executor.py:run_disposals()` **already implements rule 4** (score
< 50). It already has exits-before-entries, excluded-sleeve protection, a
covered-call guard, and per-symbol exception isolation.

**Missing: rules 1-3 and the durable state both require.** The existing
code's own docstring documents a deliberate decision not to build these
(Tradier's SPEC 3.8 is `[UNKNOWN]`, every rule `[P]`). Frank has now
explicitly decided to port them anyway — a considered reversal, noted here
rather than silently overridden.

## Adversarial review: three BLOCKING findings, all independently re-verified against source before accepting

**1. The disposal loop's iteration set would make the new rules dead code
for their primary case.** `run_disposals()` computes `flagged =
get_disposal_candidates()` (score < 50) and **returns `[]` immediately if
nothing is currently score-flagged**
(`sixfold_executor.py:117-123`); the loop itself is `for sym in
sorted(flagged & set(held))` (line 129). A position up 35%+ or 183+ days
old almost always still scores fine — a winner scoring well is the common
case a take-profit rule exists for — so it would never enter `flagged` and
the function would return before ever looking at it. **Take-profit and
time-stop would never fire in their intended scenario as the first draft
was checklisted.**

Fix, and it also closes a second finding below (blast radius): drive the
loop off `held & (flagged | registry_tracked)` instead of `flagged & held`
alone, and **gate take-profit and time-stop to registry-tracked symbols
only** — the registry becomes the record of what SixFold actually opened,
not a report of what currently scores badly. Score-dispose keeps its
existing, already-accepted broader scope (any held+flagged symbol,
registry-tracked or not) so nothing about its current behavior changes.

**2. A lockout check following this repo's own established journal-read
pattern would fail OPEN, on exactly the check whose entire purpose is
"don't immediately rebuy what was just sold."** `notify.py:read_journal()`
is the established convention here: `except Exception: return []` — reads
that swallow errors and hand back a safe-looking empty result
(`notify.py:58-64`). The natural, pattern-matching implementation of
`is_locked_out(symbol) -> bool` would do the same and return `False` (not
locked out) on any read failure — the opposite of Tradier's own explicit
choice to fail closed here (`executor.mjs:173-181`), and a direct
contradiction of this project's own stated doctrine: *"A critical read must
fail SAFE, never fail open... turns 'I couldn't verify' into 'assume the
safe/empty/zero case' — which is usually the dangerous one"*
(`~/.claude/rules/conversation-learnings.md`).

Fix: lockouts are read ONCE per cycle, not per-candidate (this also closes
a real performance finding below — the live universe is the S&P 400,
~400 symbols, and a per-candidate file reparse would run hundreds of times
per 10-minute cycle). `load_lockouts()` **raises** on an unreadable file
rather than returning a safe default; the buy loop wraps that single call
and, on failure, rejects every SIXFOLD candidate for that cycle rather than
guessing. Writes (`record_open`, `record_disposal`) keep the
`notify.py`-style never-raise posture — that guarantee exists so a failed
*write* doesn't block the trading path, which is the opposite requirement
from a *read* whose result gates a buy. Conflating the two postures was the
original plan's mistake.

**3. `record_open()` at the original call site fires on order acceptance,
not fill, with no rollback if a DAY limit order never fills or is later
canceled.** `sixfold_executor.py:260-283`: `submit_order()` (a `DAY` limit)
is followed immediately by `record_trade(...)` with **no fill confirmation
anywhere in `run_cycle()`** — contrast Tradier's `confirmOrder()` poll and
its explicit `releasePosition()` on a definitively dead order
(`executor.mjs`, "Definitively dead: release the slot").

Fix, deliberately NOT a synchronous fill-poll (this is a 10-minute batch
cycle, not the Vampire's tick loop, and adding a blocking poll to the buy
loop would slow every OTHER candidate in the same pass): write the
open-date optimistically at submission as before, but add a
**reconciliation step at the top of `run_disposals()`**, which already runs
first in every cycle and already computes `held` — for every symbol the
registry currently tracks as open, if it is not currently held, clear the
registry row. This bounds a phantom entry (an order that never filled) to
exactly one 10-minute cycle: the very next cycle's reconciliation clears it
before that cycle's buy loop could resubmit and re-register it, so no
perpetual reset and no stale-date-freeze. This mirrors Tradier's own
registry-reconciliation pattern (its "position not held at broker" release
path) rather than its confirm-then-register pattern, which is the right
adaptation for a batch system rather than an async Lambda — stated
explicitly here because it is a deliberate deviation from the source, not
an oversight.

## Two SHOULD-FIX findings, addressed by scope decision rather than by building more

**Notify:** the existing `run_disposals()` sends **zero alerts** today
(confirmed: no `notify(` call anywhere in `sixfold_executor.py`,
`sixfold_analyst.py`, or their construction in `coordinator.py`). Tradier's
source sends `urgent` on submit failure/rejection. Decision: add `notify()`
calls for the two NEW triggers (take-profit fired, time-stop fired) at
`default` severity, matching this repo's existing severity conventions —
but leave the pre-existing score-dispose path's silence exactly as it is.
Retrofitting alerting onto existing, unrelated code is scope creep the user
did not ask for; the plan adds visibility only to the new behavior it
introduces.

**Registry has no qty/reconciliation-against-partial-sale concept**, unlike
Tradier's `registerPosition`/`releasePosition` dance. `close_position(sym)`
already fully liquidates the whole ticker with no quantity parameter
(`alpaca_client.py:132-133`) — that blast radius is pre-existing and
unchanged by this plan. Building partial-quantity tracking would mean
redesigning `run_disposals()`'s selling mechanism itself, which is out of
scope for "port the exit rules" onto the existing sell path. Stated here as
an accepted, explicit scope boundary rather than silently dropped.

## Plan

### 1. A small, dependency-free exit-rule module (mirrors `exits.mjs`)
- [x] `src/strategies/sixfold_exits.py`: pure function
  `exit_decision(position, score, age_days, cfg) -> ExitDecision`, reading
  Alpaca's own `unrealized_plpc` directly (already computed on the
  snapshot) rather than re-deriving pnl% from cost basis
- [x] Precedence: time-stop first (fires whenever `age_days` is known,
  independent of price), then take-profit, then score-dispose
- [x] No stop-loss branch, ever — a test sweeps `unrealized_plpc` from
  -10% through -99% and asserts none of it alone triggers an exit

### 2. Durable registry: open-dates and lockouts, with the read/write posture split explicitly
- [x] `src/core/sixfold_registry.py`:
  - `record_open(symbol)`, `record_disposal(symbol)`, `clear(symbol)` —
    writes, never raise (matches `notify.py`'s write posture)
  - `age_days(symbol) -> float | None` — read, safe to swallow (returning
    `None` for "unknown age" correctly makes time-stop not fire, which is
    the safe direction for THIS specific read)
  - `load_lockouts() -> dict[str, str]` — read, **raises** on an unreadable
    file; called ONCE per cycle, not per-symbol
  - `reconcile(held: set[str])` — clears any registry-tracked symbol not
    currently in `held`; called at the top of `run_disposals()`
- [x] Both journal paths env-overridable
  (`SIXFOLD_POSITIONS_JOURNAL`, `SIXFOLD_LOCKOUT_JOURNAL`), matching the
  `NOTIFY_JOURNAL`/`REGIME_JOURNAL` convention
- [x] `record_open()` called from the existing buy-fill call site
  (`sixfold_executor.py:282-283`), written optimistically; correctness
  comes from `reconcile()` running every cycle, not from confirming the
  fill synchronously

### 3. Wire into `run_disposals()` and the buy path
- [x] `run_disposals()`: call `reconcile(held)` first; iterate
  `held & (flagged | registry_open_symbols)`; for a registry-tracked
  symbol, also evaluate take-profit/time-stop (not just score); same
  excluded-sleeve and covered-call guards apply regardless of which rule
  triggered
- [x] On a confirmed disposal: `record_disposal(symbol)` (writes the
  lockout), `clear(symbol)` (open-date), and a `notify()` call at
  `default` severity naming which of the two NEW rules fired
- [x] `run_cycle()`'s buy loop: `load_lockouts()` ONCE before the
  candidate loop; on a read failure, reject every candidate this cycle
  with reason "lockout table unreadable" rather than proceeding; otherwise
  reject a locked-out candidate before spending a quote/council call on it

### 4. Config
- [x] `config/strategies.yml` `sixfold:` block gains `take_profit_pct: 35`,
  `time_stop_days: 183`, `reentry_lockout_days: 56`
- [x] Typed accessors on the config dataclass, same pattern as
  `sixfold_dispose_threshold`

### 5. Tests
- [x] Port the selftest cases from `sixfold_trader/selftest.mjs`
  (take-profit boundary, time-stop boundary, the full no-stop-loss sweep,
  score-dispose unchanged, a null/unscored symbol never disposed alone)
- [x] Registry: open-date recorded once, survives a fresh module import
  (simulates a restart); `reconcile()` clears an entry that isn't held;
  `load_lockouts()` raises on an unreadable file, does not return `{}`
- [x] Call-site test: an unreadable lockout table rejects every buy
  candidate that cycle, proven by asserting zero orders placed when
  `load_lockouts` is stubbed to raise — not just that the function has a
  raise statement in it
- [x] Call-site test: `run_disposals()` actually evaluates take-profit and
  time-stop for a registry-tracked, NOT-score-flagged symbol (this is the
  literal regression test for BLOCKING finding #1)
- [x] A symbol with no registry entry (simulating a CSP-assignment or
  externally-acquired position) is never disposed by take-profit or
  time-stop, only ever by the existing score path
- [x] Mutation-test all new logic

### 6. Deploy
- [x] Merge to `main` via PR (blocked by Mihai's ruleset like everything
  else right now — flag this explicitly)
- [x] Deploy to the box regardless of merge status, same tarball/S3-presign
  pattern used all session, so "running on AWS" does not wait on Mihai
- [x] Verify live: registry files exist and are being written/read;
  a dry-run or constructed check confirms the new rules are evaluated

## Risks

1. **The 10-minute reconciliation window** means a phantom open-date can
   exist for up to one cycle before self-healing. Immaterial against a
   183-day time-stop.
2. **Score-dispose's blast radius is unchanged**, including its pre-existing
   exposure to a mis-attributed held position (e.g., CSP-assignment overlap
   with the SixFold universe) — not introduced by this plan, but also not
   fixed by it. Take-profit and time-stop are the two rules newly gated to
   registry-tracked symbols specifically to avoid ADDING to that exposure.
3. **Merge still blocked.** Deploy proceeds independent of the PR.

## Rollback

Revert the box to the pre-change tarball (kept for one session); revert the
PR if merged. No systemd unit changes, no new external infrastructure.

## Test

Full suite green; every new rule and the fail-closed lockout mutation-tested;
a live check on the box after deploy showing the registry files exist and
are being consulted.
