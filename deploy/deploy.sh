#!/usr/bin/env bash
# Update an already-provisioned box to the current origin/main and restart.
#
#   ./deploy/deploy.sh <host>
#
# Pulls from git rather than copying the working tree, so what runs is what was
# reviewed and merged. Verifies the service came back rather than assuming it.
set -euo pipefail

HOST="${1:?usage: deploy.sh <host>}"
DIR=/opt/alpaca-agent

ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/alpaca-agent
git fetch -q origin
git reset -q --hard origin/main
./.venv/bin/pip install -q -e ".[dev]"
./.venv/bin/python -m pytest tests/ -q 2>&1 | tail -2
sudo systemctl restart alpaca-agent alpaca-watchdog
sleep 6
for unit in alpaca-agent alpaca-watchdog; do
  systemctl is-active --quiet "$unit" \
    && echo "$unit: active at $(cd /opt/alpaca-agent && git rev-parse --short HEAD)" \
    || { echo "$unit FAILED to start"; sudo journalctl -u "$unit" -n 30 --no-pager; exit 1; }
done
REMOTE
