#!/bin/bash
cd /home/kozuka/Work/CodeBot
rm -f logs/orchestrator.log .codebot/logs/*.log .codebot/state/.drain .codebot/state/*.heartbeat .codebot/state/*.state.json .codebot/state/*.checkpoint.json state/*.heartbeat state/*.state.json state/*.checkpoint.json 2>/dev/null
rm -rf .codebot/state/alignment_events/* .codebot/state/alignment_triggers/* 2>/dev/null
mkdir -p logs .codebot/logs .codebot/state .codebot/state/alignment_events .codebot/state/alignment_triggers state
export GITHUB_DRY_RUN=0
export CODEBOT_PROJECT_ROOT=/home/kozuka/Work/CodeBot
exec python3 -m codebot serve --project /home/kozuka/Work/CodeBot
