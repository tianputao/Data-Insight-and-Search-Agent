#!/usr/bin/env bash
# ============================================================
# run.sh — MAF Data Insight Agent launcher
#
# Usage:
#   ./run.sh            Start the full stack (FastAPI + React)
#   ./run.sh backend    Start only the FastAPI backend
#   ./run.sh frontend   Start only the React frontend
#   ./run.sh streamlit  Start the standalone Streamlit UI (RAG only)
#   ./run.sh install    Install Python and Node.js dependencies
# ============================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_ROOT}/venv"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"
ENV_FILE="${PROJECT_ROOT}/.env"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
NC='\033[0m'  # No Color

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Helpers ───────────────────────────────────────────────────────────────────

activate_venv() {
  if [[ -d "${VENV_DIR}" ]]; then
    # shellcheck source=/dev/null
    source "${VENV_DIR}/bin/activate"
    success "Python venv activated: ${VENV_DIR}"
  else
    warn "No venv found at '${VENV_DIR}'. Using system Python."
    warn "Run './run.sh install' to set up the environment."
  fi
}

check_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found at ${ENV_FILE}."
    error "Copy .env.example to .env and fill in your credentials."
    exit 1
  fi
}

is_port_in_use() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -ltn "( sport = :${port} )" | tail -n +2 | grep -q .
    return $?
  fi
  if command -v lsof &>/dev/null; then
    lsof -iTCP:"${port}" -sTCP:LISTEN -t &>/dev/null
    return $?
  fi
  return 1
}

get_port_pid() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -ltnp "( sport = :${port} )" 2>/dev/null \
      | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' \
      | head -n1
    return 0
  fi
  if command -v lsof &>/dev/null; then
    lsof -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | head -n1
    return 0
  fi
  return 1
}

install_python_deps() {
  info "Setting up Python virtual environment…"
  if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
    success "Created venv at ${VENV_DIR}"
  fi
  source "${VENV_DIR}/bin/activate"
  info "Installing Python dependencies…"
  pip install --upgrade pip --quiet
  pip install -r "${PROJECT_ROOT}/requirements.txt" --quiet
  success "Python dependencies installed."
}

install_node_deps() {
  info "Installing Node.js frontend dependencies…"
  if ! command -v node &>/dev/null; then
    error "Node.js is not installed. Please install Node.js >= 18."
    exit 1
  fi
  if ! command -v npm &>/dev/null; then
    error "npm is not installed."
    exit 1
  fi
  cd "${FRONTEND_DIR}"
  npm install --silent
  cd "${PROJECT_ROOT}"
  success "Node.js dependencies installed."
}

start_backend() {
  info "Starting FastAPI backend on port ${BACKEND_PORT}…"
  cd "${PROJECT_ROOT}"
  activate_venv
  check_env

  if is_port_in_use "${BACKEND_PORT}"; then
    error "Port ${BACKEND_PORT} is already in use."
    error "Stop the existing process or run with another BACKEND_PORT."
    exit 1
  fi

  # Export .env into the shell so uvicorn picks up the variables
  set -a
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a

  exec uvicorn src.api.main:app \
    --host 0.0.0.0 \
    --port "${BACKEND_PORT}" \
    --reload \
    --log-level info
}

start_frontend() {
  info "Starting React frontend on port ${FRONTEND_PORT}…"
  cd "${FRONTEND_DIR}"
  export VITE_API_BASE_URL="/api"
  exec npm run dev -- --port "${FRONTEND_PORT}"
}

start_streamlit() {
  info "Starting standalone Streamlit UI (RAG/search only)…"
  cd "${PROJECT_ROOT}"
  activate_venv
  check_env

  set -a
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a

  exec streamlit run app.py --server.port 8501 --server.headless true
}

start_full_stack() {
  info "Starting full stack: FastAPI backend + React frontend"
  check_env

  BACKEND_PID=""
  FRONTEND_PID=""
  REUSED_BACKEND_PID=""
  USE_EXISTING_BACKEND=false

  if is_port_in_use "${BACKEND_PORT}"; then
    warn "Port ${BACKEND_PORT} is already in use."
    existing_pid="$(get_port_pid "${BACKEND_PORT}" || true)"
    existing_cmd=""
    if [[ -n "${existing_pid}" ]]; then
      existing_cmd="$(ps -p "${existing_pid}" -o args= 2>/dev/null || true)"
      warn "Existing PID on ${BACKEND_PORT}: ${existing_pid}"
    fi

    if curl -fsS "http://localhost:${BACKEND_PORT}/health" >/dev/null 2>&1; then
      # If this is our previous uvicorn instance, stop it and launch a fresh owned one.
      if [[ -n "${existing_cmd}" && "${existing_cmd}" == *"uvicorn src.api.main:app"* ]]; then
        warn "Stopping stale backend PID ${existing_pid} for a clean restart."
        kill "${existing_pid}" 2>/dev/null || true
        sleep 1
        if is_port_in_use "${BACKEND_PORT}"; then
          error "Failed to stop existing backend on ${BACKEND_PORT}."
          exit 1
        fi
      else
        success "Detected healthy backend on ${BACKEND_PORT}; reusing it."
        USE_EXISTING_BACKEND=true
        REUSED_BACKEND_PID="${existing_pid}"
      fi
    else
      error "Port ${BACKEND_PORT} is occupied but /health is unreachable."
      error "Please stop the conflicting process and retry."
      exit 1
    fi
  fi

  if [[ "${USE_EXISTING_BACKEND}" == false ]]; then
    # Start backend in background
    info "Launching backend (port ${BACKEND_PORT})…"
    (
      cd "${PROJECT_ROOT}"
      activate_venv
      set -a
      # shellcheck source=/dev/null
      source "${ENV_FILE}"
      set +a
      uvicorn src.api.main:app \
        --host 0.0.0.0 \
        --port "${BACKEND_PORT}" \
        --reload \
        --log-level info
    ) &
    BACKEND_PID=$!

    # Ensure backend process did not exit immediately
    sleep 1
    if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
      error "Backend failed to start."
      wait "${BACKEND_PID}" 2>/dev/null || true
      exit 1
    fi

    # Wait for health endpoint
    local backend_ready=false
    for _ in {1..20}; do
      if curl -fsS "http://localhost:${BACKEND_PORT}/health" >/dev/null 2>&1; then
        backend_ready=true
        break
      fi
      sleep 0.5
    done
    if [[ "${backend_ready}" != true ]]; then
      error "Backend did not become healthy on port ${BACKEND_PORT}."
      kill "${BACKEND_PID}" 2>/dev/null || true
      wait "${BACKEND_PID}" 2>/dev/null || true
      exit 1
    fi
  fi

  # Start frontend (foreground -- Ctrl+C kills both via trap)
  info "Launching frontend (port ${FRONTEND_PORT})…"
  (
    cd "${FRONTEND_DIR}"
    VITE_API_BASE_URL="/api" \
    npm run dev -- --port "${FRONTEND_PORT}"
  ) &
  FRONTEND_PID=$!

  # Trap SIGINT / SIGTERM and kill both children
  cleanup() {
    info "Shutting down…"
    if [[ -n "${FRONTEND_PID}" ]]; then
      kill "${FRONTEND_PID}" 2>/dev/null || true
      wait "${FRONTEND_PID}" 2>/dev/null || true
    fi
    if [[ -n "${BACKEND_PID}" ]]; then
      kill "${BACKEND_PID}" 2>/dev/null || true
      wait "${BACKEND_PID}" 2>/dev/null || true
    fi
    if [[ -n "${REUSED_BACKEND_PID}" ]]; then
      kill "${REUSED_BACKEND_PID}" 2>/dev/null || true
      wait "${REUSED_BACKEND_PID}" 2>/dev/null || true
    fi
    success "All processes stopped."
  }
  trap cleanup INT TERM

  success "Full stack running."
  echo ""
  echo -e "  ${GREEN}Backend :${NC} http://localhost:${BACKEND_PORT}"
  echo -e "  ${GREEN}Frontend:${NC} http://localhost:${FRONTEND_PORT}"
  echo ""
  echo "Press Ctrl+C to stop."

  # Wait for frontend; if backend was spawned by this script, also wait for it.
  if [[ -n "${BACKEND_PID}" ]]; then
    wait "${BACKEND_PID}" "${FRONTEND_PID}"
  else
    wait "${FRONTEND_PID}"
  fi
}

# ── Entry point ───────────────────────────────────────────────────────────────

MODE="${1:-}"

case "${MODE}" in
  backend)
    start_backend
    ;;
  frontend)
    start_frontend
    ;;
  streamlit)
    start_streamlit
    ;;
  install)
    install_python_deps
    install_node_deps
    success "All dependencies installed. Run './run.sh' to start."
    ;;
  ""|full)
    start_full_stack
    ;;
  *)
    echo "Usage: $0 [backend|frontend|streamlit|install|full]"
    exit 1
    ;;
esac
