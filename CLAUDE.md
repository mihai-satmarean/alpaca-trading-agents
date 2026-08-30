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
