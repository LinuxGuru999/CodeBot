#!/usr/bin/env bash
# safe_update.sh — safely stop entire botnet, update, restart.
# Usage:
#   ./safe_update.sh                  # drain, verify already-applied files, restart
#   ./safe_update.sh /path/to/new_bot_dir
#   ./safe_update.sh /path/to/file.md [/path/to/other.md ...]
#   ./safe_update.sh --rollback [backup_name]
# Must be run from ~/Work/bots or with BOTS_DIR set.
set -euo pipefail
BOTS_DIR="${BOTS_DIR:-$(cd "$(dirname "$0")" && pwd)}"
STATE="$BOTS_DIR/state"
PY="$BOTS_DIR/orchestrator.py"

run() { python3 "$PY" "$@" 2>&1; }

case "${1:-}" in
  --rollback)
    bdir="${2:-}"
    if [ -z "$bdir" ]; then echo "Usage: $0 --rollback <backup_dir_name_or_path>"; exit 2; fi
    echo "==> rollback to $bdir"
    run --rollback "$bdir"
    exit $?
    ;;
  --drain) run --safe-stop; exit $? ;;
  --clear-drain) run --clear-drain; exit $? ;;
  --status) run --status; run --drain-status 2>&1 | tail -n 20; exit $? ;;
esac

if [ $# -eq 0 ]; then
  echo "==> no sources: drain + verify orchestrator.py + restart"
  run --safe-stop
  python3 -c "import ast,pathlib; ast.parse(pathlib.Path('$PY').read_text()); print('orchestrator.py syntax OK')"
  run --clear-drain
  # orchestrator was stopped by --safe-stop; restart it detached
  if ! pgrep -f "python3.*orchestrator\.py" >/dev/null 2>&1; then
    nohup python3 "$PY" > "$BOTS_DIR/logs/orchestrator.log" 2>&1 &
    sleep 2; run --status | tail -n 20
  fi
  exit 0
fi

echo "==> safe update from: $*"
run --update "$@"
