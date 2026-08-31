#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu box. Run once; deploy.sh handles updates.
#
#   scp deploy/provision.sh ubuntu@<host>:~ && ssh ubuntu@<host> 'bash provision.sh'
set -euo pipefail

REPO="${REPO:-https://github.com/mihai-satmarean/alpaca-trading-agents.git}"
DIR=/opt/alpaca-agent

sudo apt-get update -qq
sudo apt-get install -y -qq git build-essential

# 3.12 ships with Ubuntu 24.04 and does not with 22.04. deadsnakes has no
# release for noble, so adding it unconditionally aborts provisioning on the
# newer image under `set -e` - and the box this deploys to is 24.04. Ask
# before reaching for the PPA.
if command -v python3.12 >/dev/null 2>&1; then
    echo "python3.12 present: $(python3.12 --version)"
    sudo apt-get install -y -qq python3.12-venv python3.12-dev
else
    echo "python3.12 absent; adding deadsnakes"
    sudo apt-get install -y -qq software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
fi

# The CSP strategy gets its option quotes from the Alpaca MCP server, which is
# launched as `uvx alpaca-mcp-server`. Without uv on PATH the server never
# starts and CSP silently stops scanning. systemd units get a minimal PATH, so
# it has to land somewhere that PATH already looks.
if ! command -v uvx >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UVDIR=$(dirname "$(find "$HOME" /root -name uv -type f -perm -u+x 2>/dev/null | head -1)")
    sudo install -m 0755 "$UVDIR/uv"  /usr/local/bin/uv
    sudo install -m 0755 "$UVDIR/uvx" /usr/local/bin/uvx
fi
uvx --version

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
echo "  2. sudo cp deploy/alpaca-agent.service deploy/alpaca-watchdog.service /etc/systemd/system/"
echo "  3. sudo systemctl daemon-reload"
echo "  4. sudo systemctl enable --now alpaca-agent alpaca-watchdog"
