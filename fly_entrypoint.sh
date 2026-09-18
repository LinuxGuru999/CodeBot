#!/bin/bash
set -euo pipefail

# CodeBot Fly.io entrypoint
# Resolves project root, sets up credentials, starts orchestrator

PROJECT_ROOT="${CODEBOT_PROJECT_ROOT:-/project}"
STATE_DIR="${CODEBOT_STATE_DIR:-/data/state}"
LOGS_DIR="${CODEBOT_LOGS_DIR:-/data/logs}"

mkdir -p "$STATE_DIR" "$LOGS_DIR"

export CODEBOT_PROJECT_ROOT="$PROJECT_ROOT"

if [ -n "${SSH_PRIVATE_KEY:-}" ]; then
    mkdir -p /home/codebot/.ssh
    echo "$SSH_PRIVATE_KEY" > /home/codebot/.ssh/id_ecdsa
    chmod 600 /home/codebot/.ssh/id_ecdsa
    export SSH_PRIVATE_KEY_PATH="/home/codebot/.ssh/id_ecdsa"
fi

if [ -f "$PROJECT_ROOT/.codebot/project.yaml" ]; then
    echo "[codebot] Project contract found at $PROJECT_ROOT/.codebot/project.yaml"
else
    echo "[codebot] WARNING: No .codebot/project.yaml at $PROJECT_ROOT"
fi

echo "[codebot] Starting CodeBot orchestrator..."
echo "[codebot] Project root: $PROJECT_ROOT"
echo "[codebot] State dir: $STATE_DIR"
echo "[codebot] Logs dir: $LOGS_DIR"
echo "[codebot] Dry run: ${GITHUB_DRY_RUN:-1}"

python3 -m codebot.control_server &
CONTROL_PID=$!
echo "[codebot] Control server started (PID $CONTROL_PID)"

exec python3 -m codebot serve --project "$PROJECT_ROOT"
