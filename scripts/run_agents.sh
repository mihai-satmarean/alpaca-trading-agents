#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example and fill in your keys."
    exit 1
fi

source .env

echo "Starting ProductAdvisors Trading System..."
echo "Alpaca endpoint: ${ALPACA_API_ENDPOINT:-https://paper-api.alpaca.markets}"
echo ""

python -m src.agents.coordinator
