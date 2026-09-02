# ProductAdvisors -- Alpaca Trading Agents

## Project

Autonomous AI trading agents for the Alpaca AI Trading Agents Hackathon (Aug 28 -- Sep 4, 2026).
Paper account with $100K. Must include options trading. Judged on P&L + creativity.

## Architecture

- **Coordinator Agent**: allocates capital (80% options, 15% vampire, 5% reserve), delegates, rebalances
- **Options Income Agent**: CSP + covered calls on blue-chip stocks
- **Vampire Agent**: bi-directional micro-scalper on liquid tickers (SPY, QQQ, AAPL)
- **Risk Manager Agent**: portfolio-wide circuit breakers, position limits, daily loss cap

## Stack

- **alpaca-py** SDK for deterministic trading logic (math, not LLM)
- **Alpaca MCP Server** for agent-level tool access (`uvx alpaca-mcp-server`)
- **Dell4 LLM cluster** via LiteLLM proxy at `http://100.69.81.102:4000`
- **Streamlit** dashboard for live monitoring and demo

## LLM Routing (Dell4)

| Model | Use |
|-------|-----|
| dell4-devstral | Primary coding / complex reasoning |
| dell4-coder | Code review / fast iterations |
| dell4-chat | General reasoning / analysis |

## Key Principle

> LLM only where ambiguity exists; math everywhere else; risk logic never inside LLM.

## Commands

```bash
pip install -e ".[dev]"            # Install with dev deps
python -m src.agents.coordinator   # Run the trading system
streamlit run dashboard/app.py     # Launch dashboard
pytest                             # Run tests
```

## MCP Config (per agent)

```json
{
  "mcpServers": {
    "alpaca": {
      "command": "uvx",
      "args": ["alpaca-mcp-server"],
      "env": {
        "ALPACA_API_KEY": "${ALPACA_API_KEY}",
        "ALPACA_SECRET_KEY": "${ALPACA_SECRET_KEY}",
        "ALPACA_PAPER_TRADE": "true"
      }
    }
  }
}
```

## Ruflo + Hermes Bridge (Multi-Agent Orchestration)

This project uses **Ruflo** for multi-agent swarm coordination and **Hermes Agent** for native Devstral coding tasks via a bridge script.

### Configuration already in place

- `.ruflo.json` -- models, routing, swarm topology, and Hermes bridge config
- `~/.hermes/bin/ruflo-hermes-bridge` -- delegates coding tasks to Hermes Agent
- `~/.hermes/config.yaml` -- Hermes Agent model config (dell4-devstral via LiteLLM)

### How to use Ruflo from this project

```bash
# Initialize swarm (already done -- skip if .swarm/state.json exists)
npx ruflo@latest swarm init --topology hierarchical --max-agents 5 --strategy specialized

# Run a development swarm on a specific task
npx ruflo@latest swarm run --objective "Fix vampire_engine.py async bugs" --strategy development

# Delegate a coding task directly to Hermes (bypasses Ruflo, uses Devstral natively)
~/.hermes/bin/ruflo-hermes-bridge "implement shadow mode for vampire engine" .

# Memory operations
npx ruflo@latest memory store --content "vampire: order-in-flight guard verified" --tags "vampire,fix"
npx ruflo@latest memory search --query "vampire bugs"
```

### Agent routing (from .ruflo.json)

| Role | Model | Purpose |
|------|-------|---------|
| researcher | dell4-fast (Qwen3.8-27B) | Triage, quick research |
| architect | dell4-chat (Qwen3.6-35B-A3B) | Planning, design |
| coder | dell4-coder (Qwen3-Coder-30B-A3B) | Implementation |
| reviewer | dell4-devstral (Devstral-Small-2-24B) | Code review (model diversity) |
| tester | dell4-coder | QA |

### Hermes bridge

Coding tasks (`implement`, `code`, `fix-bug`, `refactor`) are delegated to Hermes Agent which runs Devstral natively -- no LiteLLM message-ordering issues. The bridge is at `~/.hermes/bin/ruflo-hermes-bridge`.

### Tracking

Bugs, progress, and design decisions are tracked in GitHub Issues and GitLab wikis -- not here.
Consult the issue tracker and wiki before starting work to get current state.
