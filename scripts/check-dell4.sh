#!/usr/bin/env bash
# Verify the whole Dell4 path end to end: VPN -> proxy -> model list -> chat completion.
# Reads the key from .env (gitignored) or the LITELLM_KEY env var. Never hardcode it here.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BASE="${OPENAI_BASE_URL:-}"
KEY="${LITELLM_KEY:-${OPENAI_API_KEY:-}}"
if [ -z "$KEY" ] || [ -z "$BASE" ]; then
  # shellcheck disable=SC1091
  [ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }
  BASE="${OPENAI_BASE_URL:-}"
  KEY="${LITELLM_KEY:-${OPENAI_API_KEY:-}}"
fi
BASE="${BASE%/v1}"

if [ -z "$KEY" ]; then
  echo "No API key found. Set OPENAI_API_KEY in .env (see .env.example) or export LITELLM_KEY."
  exit 1
fi

echo "1) Tailscale"
if command -v tailscale >/dev/null 2>&1; then TS=tailscale; else TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale; fi
if "$TS" status >/dev/null 2>&1; then
  # Capture once. Piping into `grep -q` under `set -o pipefail` reports failure
  # even on a match: grep exits at the first hit, the writer takes SIGPIPE, and
  # pipefail surfaces the writer's 141 as the pipeline status.
  ts_status="$("$TS" status 2>/dev/null || true)"
  peers="$(printf '%s\n' "$ts_status" | grep -c . || true)"
  echo "   connected ($peers node(s) visible)"
  case "$ts_status" in
    *100.69.81.102*) echo "   dell4 (100.69.81.102) is a visible peer" ;;
    *) echo "   WARNING: the LLM node 100.69.81.102 is not a peer -- wrong tailnet?" ;;
  esac
else
  echo "   NOT connected -- open Tailscale and sign in"; exit 1
fi

echo "2) Proxy reachable"
code=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "$BASE/v1/models" -H "Authorization: Bearer $KEY")
echo "   HTTP $code"
[ "$code" = "200" ] || { echo "   unreachable -- confirm you are on Mihai's tailnet"; exit 1; }

echo "3) Models available"
curl -s -m 10 "$BASE/v1/models" -H "Authorization: Bearer $KEY" \
  | python3 -c 'import sys,json;[print("   -",m["id"]) for m in json.load(sys.stdin).get("data",[])]' 2>/dev/null \
  || echo "   (could not parse model list)"

echo "4) Chat completion smoke test (dell4-coder)"
curl -s -m 45 "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"dell4-coder","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":10}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("   ->",d.get("choices",[{}])[0].get("message",{}).get("content","ERROR: "+json.dumps(d)[:200]))' 2>/dev/null \
  || echo "   chat call failed"
