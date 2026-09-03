# ProductAdvisors: a four-sleeve options-and-equity agent that cuts what it measures

**Team:** ProductAdvisors (Frank Villavicencio, Mihai Satmarean, Tashi, Vibhuti Singh)
**Paper account:** PA3TPECBLC29, $100,000 starting balance, options level 3
**Repo:** github.com/mihai-satmarean/alpaca-trading-agents

## What it is

One coordinator runs four independent strategy sleeves against a single Alpaca paper account. Each sleeve carries its own capital budget, enforced from the broker's positions rather than the strategy's own counters:

| sleeve | what it does | signal |
|---|---|---|
| SIXFOLD (75%) | Long quality names from 18 large caps plus the S&P 400, 20 names max | Tashi's six-lens fundamental score (Buffett moat, ROIC, Damodaran regression, mismatch, capital signals, historical returns); buy above 60, sell below 40 |
| CSP (15%) | Sells cash-secured puts, 7 to 45 DTE, delta under 0.30, open interest over 100 | Premium yield ranked, 30% per-name concentration cap |
| Pendulum (5%) | Mean reversion on long Treasuries (TLT) | z-score under −2.0 on a 20-day mean plus RSI(2) under 10; exits on reversion, RSI over 70, a 10-day time stop, or a 1.5×ATR hard stop; 200-day regime filter with half size and a 1.0×ATR stop below it |
| Vampire (5%) | Bi-directional 5-second VWAP micro-scalper: buys dips, shorts rips | Retired on day 5 on measured expectancy; back at 5% on day 7 behind an LLM regime gate |

## AI logic: the LLM only where ambiguity exists

Every entry, exit, size, and stop is deterministic math. An LLM never places an order. Where judgment genuinely helps, an **advisory council** votes to approve or reject each SIXFOLD buy against the quantitative score. The council is three self-hosted open models on the Dell4 cluster behind a LiteLLM proxy: a finance-tuned model, a general model, and Qwen3. Two of three must agree. The council gates buys only. Exits are never gated, so a model outage can stop the system opening a position but can never stop it leaving one. A narrator model writes the session reports; its absence changes nothing but the prose.

The Vampire, retired at 0% on day 5, got its own advisor on day 6. Every 15 minutes the general-purpose model dell4-chat reads the last 30 one-minute bars and labels the regime. New lots open only in "chop"; exits never wait for a verdict; a missing, stale, or unparseable verdict, or an unreachable model, keeps entries closed. The model labels and never decides. When dell4-finance answered "chop" and "trade: false" in one object, we took the label and ignored the flag. We scored the gate on 2,828 real fills from two sessions, bucketed into 52 fifteen-minute windows worth −$119 in total. It rules on 49 of them (the first window of each session precedes its first verdict and stays closed; those three held −$18). It would have switched off 22, holding −$93, and kept 27 worth −$8. Permutation p = 0.20: the right sign, not proof on two days. On that evidence the sleeve came back at 5%, funded from the reserve, with the gate deciding every entry and every verdict journalled to the dashboard.

Pendulum's signal is computed by the same `decide()` function in the backtest and the live agent, so the two cannot drift apart. Backtested 2016 to 2026 on adjusted daily bars with next-open fills and 2bp per side: +8.69% versus −9.51% buy-and-hold, max drawdown −2.56%, 58 trades, profit factor 1.90. Twelve parameter perturbations all stayed positive.

## Risk gates

- Per-trade cap 5% of equity, per-position cap 10%, minimum cash reserve $5,000, and a 2% daily-loss circuit breaker with a 30-minute cooldown. The breaker is baselined on the broker's previous close, so a process restart cannot re-arm it.
- Per-sleeve budgets computed from broker positions every cycle; each sleeve's ticker set is excluded from the others so no strategy is ever charged for another's holdings.
- Order fills confirmed by polling the order to a terminal state, never assumed from the submit response. An unreadable fill is assumed filled, because under-counting accumulates without bound while over-counting only trades less.
- Rejected submissions count against the rate limiter and back off exponentially after five in a row.
- An independent watchdog process reads the broker's book every 20 seconds. It contains a sleeve breach by stopping the agent and closing the sleeve, without consulting the agent's own opinion of its position.
- The intraday sleeve is flattened at 15:50 ET; multi-day sleeves and the options book are never touched by that path.
- Every notification and every advisor verdict is journalled with its outcome in a SHA-256 hash-chained log that the dashboard verifies on every load, so a failed alert is visible rather than indistinguishable from a quiet system, and an edited or deleted entry is visible too.

## Alpaca infrastructure

Trading API via alpaca-py for equities, options (cash-secured puts, level 3 approved), IOC and DAY limit orders, and position closes. Market Data API for real-time quote streaming (Vampire), SIP-adjusted daily bars (Pendulum), and option chains. The **Alpaca MCP server** (`alpaca-mcp-server` over stdio) supplies live option quotes to the CSP scanner, which refuses to price any contract it cannot quote. Everything runs unattended on an EC2 instance under systemd: the agent, the watchdog, a post-open verification timer, and a Streamlit dashboard with live signals, allocation, and the notification journal. 591 automated tests; the risk gates added or repaired this week are mutation-tested (the guard is deleted and the suite must fail).

## What we measured, and what we cut

P&L attribution comes from the broker's fill ledger, never from engine counters. That ledger retired two strategies mid-competition. We dropped HOOD and SPY from the Vampire on day 4 after they posted losses in every hour of a session. On day 5, across 10,470 real fills, the Vampire stood at −$663 realized while its internal counter claimed a large profit. A 5-second mean-reversion edge is smaller than the spread it pays, and no parameter fixes that. We retired the sleeve and moved its capital to SIXFOLD, the only sleeve with a positive mark. Two latent defects in the risk layer were found the same way, from the live log, and fixed with tests that fail if they return. We think an agent that can tell when it is wrong, and acts on it, is the part worth judging.

## What we don't claim

- Two sessions of regime-gate data is a sign, not proof. The p = 0.20 above would need roughly three times the 49 windows we have before it could settle either way.
- This is a paper account. Nothing here has met a real fill, a real borrow, or a real margin call, and seven days cannot tell skill from luck on any of the four sleeves.
- The Vampire signals off Alpaca's free IEX quote feed and fills against the consolidated book. This week's live fills landed 12 to 47 cents from that feed's mid on QQQ. A consolidated feed would change its numbers more than any gate.
- CSP sits at about twice its 15% target as of this morning. Our own post-open check hid that for two days behind a hardcoded threshold; it now reads the live config.
- The advisor call fails. Once this morning the model returned empty content and the engine logged "model unavailable". Every failure closes entries, none opens them, and every one is in the journal.
- The journals are tamper-evident from September 3, not from day one. Earlier entries carry no hash and are reported as such.

## Results

Final P&L and the equity curve are reported from the paper account at submission; the paper account is the source of truth.
