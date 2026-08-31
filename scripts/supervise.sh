#!/usr/bin/env bash
# Keep the agent running on a host that has no systemd.
#
# The agent died at 09:30:52 on a DNS failure and stayed dead through the open,
# because nothing was watching it. Restart=always is what the EC2 unit gets;
# this is the laptop equivalent until the migration.
cd "$(dirname "$0")/.."
LOG="logs/session-$(date +%Y%m%d).log"
mkdir -p logs

notify_death() {
  .venv/bin/python - "$1" <<'PY' 2>/dev/null || true
import sys
from dotenv import load_dotenv; load_dotenv()
from src.core.notify import notify
notify("Alpaca agent · RESTARTED", f"The agent exited ({sys.argv[1]}) and was restarted by the supervisor.",
       severity="high", tags=["warning"])
PY
}

n=0
while true; do
  .venv/bin/python scripts/run_live.py >> "$LOG" 2>&1
  code=$?
  n=$((n+1))
  echo "$(date '+%H:%M:%S') supervisor: agent exited ($code), restart #$n" >> "$LOG"
  [ "$code" -eq 0 ] && break            # clean shutdown: stay down
  notify_death "exit $code"
  sleep 10
done
