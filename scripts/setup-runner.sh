#!/usr/bin/env bash
set -euo pipefail
#
# One-time setup of a GitHub Actions self-hosted runner on Dell4.
#
# Prerequisites:
#   - Docker and Docker Compose installed
#   - Git installed
#   - The repo cloned at the DEPLOY_DIR below
#   - A GitHub PAT with repo + workflow scopes, or use the browser flow
#
# Usage:
#   1. Go to https://github.com/mihai-satmarean/alpaca-trading-agents/settings/actions/runners/new
#   2. Select Linux x64
#   3. Copy the token from the configure step
#   4. Run: ./setup-runner.sh <TOKEN>
#

TOKEN="${1:-}"
RUNNER_VERSION="2.322.0"
RUNNER_DIR="$HOME/actions-runner-alpaca"
REPO="mihai-satmarean/alpaca-trading-agents"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 <GITHUB_RUNNER_TOKEN>"
    echo ""
    echo "Get the token from:"
    echo "  https://github.com/${REPO}/settings/actions/runners/new"
    exit 1
fi

echo "--- Installing GitHub Actions runner v${RUNNER_VERSION} ---"
mkdir -p "$RUNNER_DIR" && cd "$RUNNER_DIR"

if [ ! -f run.sh ]; then
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  ARCH_LABEL="x64" ;;
        aarch64) ARCH_LABEL="arm64" ;;
        *)       echo "Unsupported arch: $ARCH"; exit 1 ;;
    esac

    curl -sL -o actions-runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${ARCH_LABEL}-${RUNNER_VERSION}.tar.gz"
    tar xzf actions-runner.tar.gz
    rm actions-runner.tar.gz
fi

echo "--- Configuring runner ---"
./config.sh \
    --url "https://github.com/${REPO}" \
    --token "$TOKEN" \
    --name "dell4-trading" \
    --labels "dell4,self-hosted,linux,x64" \
    --work "_work" \
    --replace

echo "--- Installing as systemd service ---"
sudo ./svc.sh install
sudo ./svc.sh start

echo ""
echo "Runner installed and running."
echo "  Name:   dell4-trading"
echo "  Labels: dell4,self-hosted,linux,x64"
echo "  Dir:    $RUNNER_DIR"
echo ""
echo "Verify at: https://github.com/${REPO}/settings/actions/runners"
