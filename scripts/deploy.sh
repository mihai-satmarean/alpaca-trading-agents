#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: pulling latest code"
git fetch origin main
git reset --hard origin/main

echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: rebuilding containers"
docker compose -f docker-compose.prod.yml build --pull

echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: rolling restart"
docker compose -f docker-compose.prod.yml up -d --remove-orphans

echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: waiting for health check"
sleep 15

if docker inspect --format='{{.State.Health.Status}}' alpaca-agent 2>/dev/null | grep -q healthy; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: agent is healthy"
else
    STATUS=$(docker inspect --format='{{.State.Status}}' alpaca-agent 2>/dev/null || echo "not found")
    echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: agent status=$STATUS (health check may still be pending)"
fi

docker compose -f docker-compose.prod.yml ps
echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: done"
