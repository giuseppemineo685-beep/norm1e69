#!/bin/bash
# Corre live_trader.py (plata real o dry-run, segun LIVE) por una duracion
# acotada, mismo patron que run_bounded.sh. Commitea state/live_state.json
# y state/live_trades.jsonl seguido para no perder historial si el job corta.
set -u
cd "$(dirname "$0")/.."

DURATION="${1:-20400}"  # 5h40m
END=$(( $(date +%s) + DURATION ))

git config user.name "polycool-strategy-bot" 2>/dev/null
git config user.email "polycool-strategy-bot@users.noreply.github.com" 2>/dev/null

git_retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    if [ "$n" -ge 3 ]; then
      echo "git $* failed after 3 attempts" >&2
      return 1
    fi
    sleep $((n * 5))
  done
}

publish_once() {
  git checkout -- docs/index.html 2>/dev/null
  git_retry git pull --no-rebase -q -X ours
  python3 -B scripts/generate_dashboard.py > /tmp/dashboard_regen.log 2>&1
  git add docs/index.html state/live_state.json state/live_trades.jsonl 2>/dev/null
  git diff --cached --quiet || git commit -q -m "Live trader: $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
  git_retry git push -q
}

echo "starting live_trader.py in background for ${DURATION}s (LIVE=${LIVE:-0})"
python3 -u scripts/live_trader.py > /tmp/live_trader.log 2>&1 &
BOT_PID=$!

PUBLISH_INTERVAL="${PUBLISH_INTERVAL:-5}"  # segundos entre publicaciones (commit+push a git)
while [ "$(date +%s)" -lt "$END" ]; do
  sleep "$PUBLISH_INTERVAL"
  publish_once
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "live_trader murio, mostrando log y reiniciando"
    tail -50 /tmp/live_trader.log
    python3 -u scripts/live_trader.py > /tmp/live_trader.log 2>&1 &
    BOT_PID=$!
  fi
done

echo "window elapsed, stopping live_trader"
kill "$BOT_PID" 2>/dev/null
sleep 2
publish_once
echo "done"
