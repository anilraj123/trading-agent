#!/bin/bash
# auto-deploy-main.sh — droplet host cron, weekdays pre-market (13:10 UTC).
#
# Ships whatever landed on origin/main since the last deploy, gated on the
# unit tests passing INSIDE the freshly built image. This closes the loop for
# the twice-daily analyst routines (cloud agents that open/merge PRs but have
# no droplet access): merged main auto-deploys next pre-market, while the
# market is closed and a container restart is safe (all v2 state is on disk).
#
# Never deploys on: no new commits, non-ff divergence, build failure, or red
# tests — and says so loudly on ntfy in every failure case.
set -u
cd /root/trading-agent || exit 1
LOG=/root/trading-agent/deploy.log
NTFY_TOPIC=$(grep -oP '^V2_NTFY_TOPIC=\K.*' .env 2>/dev/null || echo trading-agent)

notify() {  # notify "message" [high]
    curl -s ${2:+-H "Priority: $2"} -d "$1" "https://ntfy.sh/$NTFY_TOPIC" > /dev/null
}

echo "[$(date -u +%FT%TZ)] auto-deploy check" >> "$LOG"
git fetch -q origin main 2>> "$LOG" || { notify "[DEPLOY] git fetch failed — check droplet" high; exit 1; }
LOCAL=$(git rev-parse HEAD)
[ "$LOCAL" = "$(git rev-parse origin/main)" ] && exit 0

if ! git merge -q --ff-only origin/main >> "$LOG" 2>&1; then
    notify "[DEPLOY] droplet diverged from main (no ff) — NOT deploying, fix manually" high
    exit 1
fi
SHORT=$(git rev-parse --short HEAD)

if ! docker compose build trader-v2 >> "$LOG" 2>&1; then
    notify "[DEPLOY] build FAILED at $SHORT — live bot untouched on $(git rev-parse --short "$LOCAL")" high
    exit 1
fi
if docker compose run --rm --no-deps trader-v2 python -m pytest tests/ -q >> "$LOG" 2>&1; then
    docker compose up -d trader-v2 >> "$LOG" 2>&1
    notify "[DEPLOY] main $SHORT deployed pre-market (tests green)"
else
    notify "[DEPLOY] BLOCKED: tests RED at $SHORT — live bot still on $(git rev-parse --short "$LOCAL")" high
fi
