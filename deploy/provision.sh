#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu box. Run once; deploy.sh handles updates.
#
#   scp deploy/provision.sh ubuntu@<host>:~ && ssh ubuntu@<host> 'bash provision.sh'
set -euo pipefail

REPO="${REPO:-https://github.com/mihai-satmarean/alpaca-trading-agents.git}"
DIR=/opt/alpaca-agent

sudo apt-get update -qq
# 3.12 is not in Ubuntu 22.04's default archive; the project requires it.
sudo apt-get install -y -qq software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev git build-essential

sudo mkdir -p "$DIR" && sudo chown "$USER:$USER" "$DIR"
[ -d "$DIR/.git" ] || git clone -q "$REPO" "$DIR"
cd "$DIR"
mkdir -p logs

python3.12 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e ".[dev]"

echo
echo "Provisioned. Remaining steps:"
echo "  1. copy .env to $DIR/.env   (never committed; holds the Alpaca keys)"
echo "  2. sudo cp deploy/alpaca-agent.service /etc/systemd/system/"
echo "  3. sudo systemctl daemon-reload && sudo systemctl enable --now alpaca-agent"
