#!/usr/bin/env bash
set -euo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin:${PATH:-}"
export PATH

REPO_DIR="/root/uniti-model-service"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

cleanup() {
  status=$?
  if [[ $status -eq 0 ]];
  then
    log "Deployment completed successfully."
  else
    log "Deployment failed with exit code ${status}."
  fi
}
trap cleanup EXIT

if [[ ! -d "${REPO_DIR}" ]]; then
  log "ERROR: Repository directory ${REPO_DIR} not found."
  exit 1
fi

cd "${REPO_DIR}"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  log "ERROR: Neither 'docker compose' nor 'docker-compose' is available on PATH."
  log "Hint: if the helper runs inside Docker, bind-mount the host's docker binary and CLI plugins (e.g. /usr/bin/docker and /usr/lib/docker/cli-plugins)."
  exit 127
fi

if ! command -v git >/dev/null 2>&1; then
  log "ERROR: git is not available on PATH."
  exit 127
fi

log "Using compose command: ${COMPOSE_CMD[*]}"

log "Stopping running containers..."
"${COMPOSE_CMD[@]}" down

log "Pulling latest changes (git pull --ff-only)..."
git pull --ff-only

log "Starting containers in detached mode..."
"${COMPOSE_CMD[@]}" up -d

log "Deployment steps completed."
