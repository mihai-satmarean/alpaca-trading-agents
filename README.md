# ProductAdvisors -- AI Trading Agents

Autonomous AI trading agents for the **Alpaca AI Trading Agents Hackathon** (Aug 28 -- Sep 4, 2026).

## Architecture

Multi-agent system managing a $100K paper trading account with two strategies:

- **Options Income Agent** (80% allocation) -- sells cash-secured puts and covered calls on blue-chip stocks
- **Vampire Scalper Agent** (15% allocation) -- bi-directional micro-scalper bleeding profits from price oscillations on liquid ETFs
- **Risk Manager Agent** -- portfolio-wide circuit breakers, position limits, end-of-day flattening
- **Coordinator** -- capital allocation, strategy delegation, daily rebalancing

## Stack

| Component | Technology |
|-----------|------------|
| Trading API | alpaca-py SDK + Alpaca MCP Server |
| LLM Backend | Dell4 cluster (Devstral, Qwen3-Coder) via LiteLLM |
| Agent Orchestration | Ruflo (Claude Code multi-agent swarm) |
| Dashboard | Streamlit |
| Language | Python 3.12 |

## Quick Start

```bash
# Clone and setup
uv venv --python 3.12
uv pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your Alpaca API keys

# Run tests
uv run pytest

# Start trading system (contest paper account — Frank's EC2, not the laptop)
uv run python -m src.agents.coordinator

# Laptop: isolated staging paper account (never the contest book).
# LLM calls go through k3s LiteLLM (pillar5 :30400), not Dell4 :4000.
./scripts/run_staging.sh smoke
./scripts/run_staging.sh llm
./scripts/run_staging.sh hunt
./scripts/run_staging.sh dashboard
./scripts/run_staging.sh dry-run

# Launch dashboard (uses ALPACA_ENV; prefer run_staging.sh dashboard on a laptop)
uv run streamlit run dashboard/app.py

# Backtest vampire algorithm
uv run python scripts/backtest.py --symbol SPY --days 10
```

## Team

**ProductAdvisors** -- Mihai, Frank, Tashi, Vibhuti
