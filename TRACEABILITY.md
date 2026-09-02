# alpaca-trading-agents -- AI session traceability

Append-only ledger of AI agent sessions that modified this repository.
Each row links a Cursor/AI session to the tickets it worked on and what changed.

## Log

| Date (ISO) | Session ID | Ticket(s) | Scope | agent-hours | Status | Notes |
|------------|------------|-----------|-------|-------------|--------|-------|
| 2026-09-01 | 945a8d64 | -- | Laptop staging runner, isolated paper | 1–2h | completed | scripts/run_staging.sh; smoke OK on PKPIWG $100k paper; contest PK2UEW unused |
| 2026-09-01 | 945a8d64 | -- | Staging LLM via k3s LiteLLM | 0.5–1h | completed | run_staging.sh pins OPENAI_BASE_URL to pillar5:30400; contest stays on Dell4:4000 |
| 2026-09-01 | 945a8d64 | -- | Dry-run decision journal for agents | 1–1.5h | completed | `[DECISION]` + logs/staging-decisions.jsonl; Vampire/council/SIXFOLD/options; dry-run submit_order; observe |
| 2026-09-01 | 945a8d64 | -- | Live agent Streamlit cockpit | 2–3h | completed | Branch feat/live-agent-cockpit; Hermes board alpaca-hackathon; 88 pytest passed; dashboard staging+dry_run; v2 buttons disabled |
| 2026-09-01 | 945a8d64 | -- | Per-agent books, hunt LLM, CSP council | 2–3h | completed | Sleeve P&L/AUM in cockpit; Vampire picker Fino1+Fin-R1 veto; evaluate_csp on execute; live restart still pending |
| 2026-09-01 | 945a8d64 | -- | Fino1 thinking, Frank merge, full-toolset | 2–3h | completed | local/full-toolset (not pushed); Fino1 thinks; dashboard STAGING vs CONTEST banner; 530 pytest; staging agents restarted |
| 2026-09-02 | 945a8d64 | -- | Fetch Frank main, staging day review | 0.5–1h | completed | origin/main 19e9d4c (+8); not merged into vampire-agent-async-fix; journal 9146 rows; staging ~-$778 |
| 2026-09-02 | 945a8d64 | #64 #65 | Merge Frank + staging post-mortem tweaks | 2–3h | completed | Local commit of cockpit + #65 gates on vampire-agent-async-fix; not pushed |

## Session notes

- 945a8d64: `origin/main` still `7a59add` (open-check + deploy verify). PRs #51 and #50 have no review comments. Laptop runner is `scripts/run_staging.sh` (`ALPACA_ENV=staging`, SNS unset, ntfy `alpaca-hackathon-staging-mihai`). Coordinator now accepts `staging=` / `dry_run=`. Contest host remains Frank's EC2.
- 945a8d64: Laptop staging LLM is k3s NodePort `http://100.101.239.56:30400/v1` (LAN fallback `10.108.111.119:30400`). `10.108.111.115:30400` is dead. Dell4 `:4000` stays the contest path. Council/narrator send `chat_template_kwargs.enable_thinking=false` so Qwen3.6/3.8 return `content` instead of empty+reasoning.
- 945a8d64: Dry-run visibility is `DECISION_LOG` JSONL plus `[DECISION]` on stdout. `./scripts/run_staging.sh observe` tails formatted thoughts/votes. SIXFOLD/CSP/CC/spreads now go through `AlpacaClient.submit_order` so dry-run cannot place real orders. Staging CSP quotes use `ALPACA_STAGING_*` when `ALPACA_ENV=staging`.
- 945a8d64: Live cockpit is Streamlit fragments reading `logs/agent-status.json` plus the decision journal. Dashboard Alpaca client is staging + dry_run. Operator buttons exist and are disabled (v2). Hermes board `alpaca-hackathon` (do not switch off dell3-109). Profile `python-coder`. Git branch `feat/live-agent-cockpit`.
- 945a8d64: Per-agent books classify broker positions with the live Vampire universe (config ∪ snapshot victims), not the static YAML list. CSP invested is strike×100 collateral. Vampire hunt uses Fino1-14B + Fin-R1 as a veto on pick/replace only; ticks stay deterministic. CSP `execute_best` now calls `evaluate_csp`. Live `run_live.py` must be restarted to pick up hunt/CSP gates; do not restart without Mihai.
- 945a8d64: Fino1 (`dell4-fino1-14b`) is the only council model allowed to think (`enable_thinking=true`, 2000 tokens, 45s). Qwen stays muted so it still returns a short APPROVE/REJECT. Votes parse the answer tail, not the `## Thinking` scratchpad; the journal keeps the full trace. Narrator follows Frank: `NARRATOR_MAX_TOKENS=4000`, thinking not muted.
- 945a8d64: Local test branch `local/full-toolset` merges `origin/main` (Frank: narrator 4000, opening-window poll 4s 09:30–09:40 ET, vampire 20%/reserve 10%, flatten cancel-before-close) plus `feat/dell4-deployment`. Not pushed. Do not merge PR #51 to GitHub main. Dashboard banner shows STAGING vs CONTEST by key prefix (`PKPIWG` vs `PK2UEW`) and Alpaca `account_number` (staging paper `PA310V54AWBY`). Restart flattened Vampire and cancelled resting scalper orders; CSPs stayed. Dashboard process left running for Mihai to restart.
- 945a8d64: 2026-09-02 local merge `origin/main` (`19e9d4c`) into `vampire-agent-async-fix` as `cf545b8`. Also `git branch -f local/full-toolset HEAD`. Not pushed; GitHub `main` unchanged. #65 uncommitted on top of that merge: Fino1 trailing vote line, hunt `keep_symbols`, HOOD/SPY denylist, vampire mixed-side P&L, SIXFOLD `_in_flight`, pendulum sleeve in `agent_book`. pytest 612 passed. Staging journal: broker ~$99.3k close (−$778). Fino1 had abstained 63/63 before the parse fix. HOOD +$10k was lot-accounting garbage.
