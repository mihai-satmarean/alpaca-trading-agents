#!/usr/bin/env bash
# Toggle Claude Code between the Dell4 cluster and the normal Anthropic API.
#   ./scripts/use-dell4.sh on    -> route Claude Code at the Dell4 LiteLLM proxy
#   ./scripts/use-dell4.sh off   -> back to the normal Anthropic API
#   ./scripts/use-dell4.sh       -> report current state
# Pointing at Dell4 while the VPN is down makes Claude Code fail in this repo,
# so "on" refuses unless the proxy actually answers.
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.claude" && pwd)"
LIVE="$D/settings.local.json"; PARKED="$D/settings.dell4.json"

case "${1:-status}" in
  on)
    [ -f "$LIVE" ] && { echo "Already on Dell4."; exit 0; }
    [ -f "$PARKED" ] || { echo "Missing $PARKED"; exit 1; }
    key=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["env"]["ANTHROPIC_AUTH_TOKEN"])' "$PARKED")
    code=$(curl -s -m 6 -o /dev/null -w "%{http_code}" http://100.69.81.102:4000/v1/models -H "Authorization: Bearer $key")
    [ "$code" = "200" ] || { echo "Proxy not reachable (HTTP $code). Connect to Mihai's tailnet first; leaving Claude Code on the Anthropic API."; exit 1; }
    mv "$PARKED" "$LIVE"; echo "Claude Code -> Dell4 (dell4-coder). Restart any session in this repo."
    ;;
  off)
    [ -f "$LIVE" ] || { echo "Already on the Anthropic API."; exit 0; }
    mv "$LIVE" "$PARKED"; echo "Claude Code -> Anthropic API. Restart any session in this repo."
    ;;
  *)
    if [ -f "$LIVE" ]; then echo "Dell4 ACTIVE (dell4-coder)"; else echo "Anthropic API active; Dell4 config parked at .claude/settings.dell4.json"; fi
    ;;
esac
