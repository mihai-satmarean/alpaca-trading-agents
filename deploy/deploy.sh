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
sudo systemctl restart alpaca-agent
sleep 6
systemctl is-active --quiet alpaca-agent \
  && echo "alpaca-agent: active at $(cd /opt/alpaca-agent && git rev-parse --short HEAD)" \
  || { echo "alpaca-agent FAILED to start"; sudo journalctl -u alpaca-agent -n 30 --no-pager; exit 1; }
REMOTE
