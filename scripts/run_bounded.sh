#!/bin/bash
# Corre el paper trader + auto-publish del dashboard por una duracion acotada
# (pensado para GitHub Actions, limite duro de 6h por job). Commitea/pushea
# seguido para no perder el estado si el job se corta.
set -u
cd "$(dirname "$0")/.."

DURATION="${1:-20400}"  # 5h40m, deja margen bajo el limite de 6h
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
  git add docs/index.html state/paper_state.json state/paper_trades.jsonl
  git diff --cached --quiet || git commit -q -m "Paper trader: $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
  git_retry git push -q
}

echo "starting papertrader.py in background for ${DURATION}s"
python3 -u scripts/papertrader.py > /tmp/papertrader.log 2>&1 &
BOT_PID=$!

while [ "$(date +%s)" -lt "$END" ]; do
  sleep 60
  publish_once
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "papertrader murio, reiniciando"
    tail -30 /tmp/papertrader.log
    python3 -u scripts/papertrader.py > /tmp/papertrader.log 2>&1 &
    BOT_PID=$!
  fi
done

echo "window elapsed, stopping papertrader"
kill "$BOT_PID" 2>/dev/null
sleep 2
publish_once
echo "done"
