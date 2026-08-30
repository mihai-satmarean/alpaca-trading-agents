#!/usr/bin/env bash
# Source this to point your shell at the Dell4 LiteLLM proxy.
#   source scripts/dell4-env.sh
# Requires Tailscale to be connected (100.69.81.102 is a tailnet address).

set -a
# shellcheck disable=SC1091
[ -f "$(dirname "${BASH_SOURCE[0]}")/../.env" ] && . "$(dirname "${BASH_SOURCE[0]}")/../.env"
set +a

echo "Dell4 env loaded:"
echo "  OPENAI_BASE_URL    = ${OPENAI_BASE_URL}"
echo "  ANTHROPIC_BASE_URL = ${ANTHROPIC_BASE_URL}"
echo "  ANTHROPIC_MODEL    = ${ANTHROPIC_MODEL}"
if curl -s -m 5 -o /dev/null "${OPENAI_BASE_URL}/models" -H "Authorization: Bearer ${OPENAI_API_KEY}"; then
  echo "  proxy reachable    = YES"
else
  echo "  proxy reachable    = NO (is Tailscale connected? run: tailscale status)"
fi
