#!/usr/bin/env bash
# Laptop runner for Mihai's isolated Alpaca STAGING paper account.
#
# This script refuses to talk to the contest book. Contest keys may still sit
# in .env as ALPACA_API_KEY; they are never passed to the client from here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

STAGING_NTFY_DEFAULT="alpaca-hackathon-staging-mihai"

usage() {
  cat <<'EOF'
Laptop staging runner — isolated paper account, never the contest book.

Usage:
  ./scripts/run_staging.sh smoke       Account + clock only. No orders.
  ./scripts/run_staging.sh llm         Council models via k3s LiteLLM (no orders).
  ./scripts/run_staging.sh hunt        Vampire pre-market scan (read-only).
  ./scripts/run_staging.sh dashboard   Streamlit UI against staging.
  ./scripts/run_staging.sh dry-run     Full coordinator; orders logged, not sent.
  ./scripts/run_staging.sh observe     Tail the decision journal (thoughts/votes).
  ./scripts/run_staging.sh live        Real orders on the STAGING paper account.
                                       Requires typing "yes" (or CONFIRM_STAGING_LIVE=yes).

Requires ALPACA_STAGING_API_KEY / ALPACA_STAGING_SECRET_KEY in .env.
Contest ALPACA_API_KEY is ignored.
LLM is k3s LiteLLM (pillar5 :30400), not Dell4 :4000. Override with
OPENAI_STAGING_BASE_URL if you need the LAN NodePort instead.
EOF
}

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.example and set ALPACA_STAGING_* keys."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${ALPACA_STAGING_API_KEY:-}" || -z "${ALPACA_STAGING_SECRET_KEY:-}" ]]; then
  echo "ERROR: ALPACA_STAGING_API_KEY / ALPACA_STAGING_SECRET_KEY missing in .env"
  echo "This runner will not fall back to the contest ALPACA_API_KEY."
  exit 1
fi

if [[ -n "${ALPACA_API_KEY:-}" && "${ALPACA_API_KEY}" == "${ALPACA_STAGING_API_KEY}" ]]; then
  echo "ERROR: staging key equals contest ALPACA_API_KEY. Refusing to run."
  exit 1
fi

export ALPACA_ENV=staging
unset SNS_TOPIC_ARN
export NTFY_TOPIC="${NTFY_TOPIC:-$STAGING_NTFY_DEFAULT}"

# Laptop staging talks to office k3s LiteLLM, not the hackathon Dell4:4000 proxy.
# Dell4 aliases on k3s still run on Dell4 GPUs; k3s is only the gateway.
K3S_LITELLM_DEFAULT="http://100.101.239.56:30400/v1"
export OPENAI_BASE_URL="${OPENAI_STAGING_BASE_URL:-$K3S_LITELLM_DEFAULT}"
case "${OPENAI_API_KEY:-}" in
  ""|your*|*"here")
    export OPENAI_API_KEY="sk-k3s-staging"
    ;;
esac

if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  PY="$PROJECT_DIR/.venv/bin/python"
else
  PY="python3"
fi

if [[ -x "$PROJECT_DIR/.venv/bin/streamlit" ]]; then
  STREAMLIT="$PROJECT_DIR/.venv/bin/streamlit"
else
  STREAMLIT="streamlit"
fi

mkdir -p "$PROJECT_DIR/logs"
export DECISION_LOG="${DECISION_LOG:-$PROJECT_DIR/logs/staging-decisions.jsonl}"
export AGENT_STATUS_PATH="${AGENT_STATUS_PATH:-$PROJECT_DIR/logs/agent-status.json}"

banner() {
  echo "=============================================="
  echo "STAGING laptop runner"
  echo "environment:     staging"
  echo "staging key:     ${ALPACA_STAGING_API_KEY:0:6}..."
  if [[ -n "${ALPACA_API_KEY:-}" ]]; then
    echo "contest key:     ${ALPACA_API_KEY:0:6}... (NOT USED)"
  fi
  echo "ntfy topic:      ${NTFY_TOPIC}"
  echo "SNS:             unset (will not notify Frank's topic)"
  echo "LLM:             ${OPENAI_BASE_URL}  (k3s, not Dell4:4000)"
  echo "decision log:    ${DECISION_LOG:-unset}"
  echo "agent snapshot:  ${AGENT_STATUS_PATH:-unset}"
  echo "=============================================="
}

smoke() {
  banner
  "$PY" - <<'PY'
from src.core.alpaca_client import AlpacaClient, load_config

cfg = load_config(staging=True)
assert cfg.environment == "staging", cfg.environment
client = AlpacaClient(config=cfg, dry_run=True)
account = client.get_account()
clock = client.get_clock()
positions = client.get_positions()
print(f"environment:  {client.environment}")
print(f"key_prefix:   {cfg.api_key[:6]}")
print(f"status:       {account.status}")
print(f"equity:       {account.equity}")
print(f"cash:         {account.cash}")
print(f"buying_power: {account.buying_power}")
print(f"positions:    {len(positions)}")
print(f"market_open:  {clock.is_open}")
print("CONTEST ACCOUNT NOT USED")
PY
}

llm() {
  banner
  echo "Council smoke via k3s. No Alpaca orders."
  "$PY" - <<'PY'
import json, os, urllib.request, time

base = os.environ["OPENAI_BASE_URL"].rstrip("/")
key = os.environ.get("OPENAI_API_KEY") or "sk-k3s-staging"
models = ["dell4-finance", "dell4-fino1-14b", "dell4-chat", "dell4-qwen38"]
print("OPENAI_BASE_URL", base)
ok = 0
for model in models:
    thinks = "fino1" in model
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 256 if thinks else 16,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": thinks},
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read())
        dt = time.time() - t0
        msg = ((payload.get("choices") or [{}])[0].get("message") or {})
        text = (msg.get("content") or msg.get("reasoning_content") or "")
        text = text.replace("\n", " ")[:80]
        print(f"  {model:18} {dt:4.1f}s  thinking={thinks}  {text!r}")
        if text.strip():
            ok += 1
    except Exception as e:
        print(f"  {model:18} FAIL {type(e).__name__}: {str(e)[:180]}")
print(f"{ok}/{len(models)} council models returned content")
if ok < 2:
    raise SystemExit("k3s council smoke failed")
PY
}

cmd="${1:-}"
case "$cmd" in
  ""|-h|--help|help)
    usage
    exit 0
    ;;
  smoke)
    smoke
    ;;
  llm)
    llm
    ;;
  hunt)
    banner
    "$PY" scripts/vampire_scan.py --staging
    ;;
  dashboard)
    banner
    exec "$STREAMLIT" run dashboard/app.py
    ;;
  dry-run)
    banner
    echo "Orders will be logged, not submitted. Contest account is not used."
    echo "Decisions: grep [DECISION] in this terminal, or in another:"
    echo "  ./scripts/run_staging.sh observe"
    echo "Journal: $DECISION_LOG"
    exec "$PY" scripts/run_live.py --staging --dry-run
    ;;
  observe)
    banner
    echo "Following $DECISION_LOG"
    exec "$PY" -c "from src.core.decision_log import follow; follow()"
    ;;
  live)
    banner
    echo "This places REAL orders on the STAGING paper account."
    echo 'The contest $100k book is not used.'
    if [[ "${CONFIRM_STAGING_LIVE:-}" != "yes" ]]; then
      if [[ ! -t 0 ]]; then
        echo "ERROR: non-interactive live requires CONFIRM_STAGING_LIVE=yes"
        exit 1
      fi
      printf "Type yes to continue: "
      read -r ans
      if [[ "$ans" != "yes" ]]; then
        echo "Aborted."
        exit 1
      fi
    fi
    exec "$PY" scripts/run_live.py --staging
    ;;
  *)
    echo "Unknown command: $cmd"
    usage
    exit 1
    ;;
esac
