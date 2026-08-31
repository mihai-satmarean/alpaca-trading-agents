# ---- Claude Code model routing (Alpaca hackathon, Aug 2026) ------------------
# Default `claude` stays on Opus everywhere, including inside the hackathon repo.
# `dell4` launches a session against Mihai's GPU cluster instead, for mechanical
# work: tests, boilerplate, adapters, dashboard. Opt-in on purpose, so a
# forgotten flag costs tokens rather than silently putting weak reasoning on a
# diagnosis task.
dell4() {
  local key="${LITELLM_KEY:?set LITELLM_KEY or paste the shared key here}"
  local base="http://100.69.81.102:4000"
  local model="${DELL4_MODEL:-dell4-coder}"

  if ! curl -s -m 6 -o /dev/null "$base/v1/models" -H "Authorization: Bearer $key"; then
    echo "dell4: cluster unreachable. Connect Tailscale, then retry." >&2
    echo "  /Applications/Tailscale.app/Contents/MacOS/Tailscale status" >&2
    return 1
  fi

  echo "\033[33m▲ Claude Code on Dell4 · $model · not billed to your subscription\033[0m"
  ANTHROPIC_BASE_URL="$base" \
  ANTHROPIC_AUTH_TOKEN="$key" \
  ANTHROPIC_API_KEY="$key" \
  ANTHROPIC_MODEL="$model" \
  ANTHROPIC_SMALL_FAST_MODEL="$model" \
  claude "$@"
}

# Devstral: ~68% SWE-bench vs ~50% for the coder model. Slower, better at
# multi-file agentic edits. The -cc alias is required under Claude Code.
dell4-devstral() { DELL4_MODEL=dell4-devstral-cc dell4 "$@"; }
