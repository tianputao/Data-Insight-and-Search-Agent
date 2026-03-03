#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

echo "🛑 Stopping background services in $ROOT_DIR"

kill_pattern() {
  local pattern="$1"
  local label="$2"
  local pids

  pids="$(pgrep -f "$pattern" || true)"
  if [[ -n "$pids" ]]; then
    echo "- Killing $label: $pids"
    pkill -f "$pattern" || true
  else
    echo "- No $label process found"
  fi
}

# Project-specific runners
kill_pattern "bash ./run.sh" "run.sh"
kill_pattern "uvicorn src.api.main:app" "backend uvicorn"
kill_pattern "npm run dev --port 3000" "frontend npm dev"
kill_pattern "node .*vite --port 3000" "frontend vite"

# Generic fallbacks (if launched with different command forms)
kill_pattern "uvicorn.*src.api.main:app" "backend uvicorn (fallback)"
kill_pattern "node .*vite" "frontend vite (fallback)"

sleep 1

echo
echo "🔎 Remaining related processes:"
pgrep -af "uvicorn|run.sh|vite|src.api.main|npm.*dev|node.*vite" || echo "(none)"

echo
echo "🔌 Port check (8000/3000/5173):"
ss -ltnp | grep -E ':8000|:3000|:5173' || echo "(all clear)"

echo
echo "✅ stop.sh finished"
