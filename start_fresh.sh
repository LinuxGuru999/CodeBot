#!/usr/bin/env bash
set -e
rm -f /home/kozuka/Work/CodeBot/.codebot/state/.restart
rm -f /home/kozuka/Work/CodeBot/.codebot/state/.drain
rm -f /home/kozuka/Work/CodeBot/logs/orchestrator.log
touch /home/kozuka/Work/CodeBot/logs/orchestrator.log
trap '' SIGTERM SIGINT SIGHUP
exec python3 -m codebot serve --project /home/kozuka/Work/CodeBot >> /home/kozuka/Work/CodeBot/logs/orchestrator.log 2>&1
