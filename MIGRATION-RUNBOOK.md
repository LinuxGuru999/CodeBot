# CodeBot Migration Runbook

Procedure for replacing Monitor-BotNet with standalone CodeBot.

Last updated: 2026-09-18

---

## Pre-Migration Checklist

- [ ] CodeBot repo cloned at `~/Work/CodeBot`
- [ ] All 143 tests passing: `cd ~/Work/CodeBot && python3 -m pytest tests/ -q`
- [ ] Monitor project profile exists at `~/Work/.codebot/project.yaml`
- [ ] Monitor constitution exists at `~/Work/.codebot/constitution.md`
- [ ] Migration script tested dry-run: `python3 -m codebot.migrate_queue --project ~/Work --queue bots/QUEUE.md --dry-run`
- [ ] Fly.io secrets configured: `CONTROL_TOKEN`, `SSH_PRIVATE_KEY`, `GH_TOKEN`
- [ ] Backup of current BotNet state taken

---

## Phase 1: Stop Old BotNet

```bash
# Drain first — prevents new bot spawns
python3 ~/Work/bots/orchestrator.py --drain

# Wait for running bots to finish current iteration
tail -f ~/Work/bots/logs/orchestrator.log
# Look for: "Drain active" + all bots showing "stopped"

# Stop all bots
python3 ~/Work/bots/orchestrator.py --stop-all

# Verify stopped
ps aux | grep orchestrator | grep -v grep
# Should return nothing
```

---

## Phase 2: Backup State

```bash
# Backup existing BotNet state
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/Work/bots/state/backup/$TIMESTAMP
cp -r ~/Work/bots/state/* ~/Work/bots/state/backup/$TIMESTAMP/
cp -r ~/Work/bots/logs/* ~/Work/bots/state/backup/$TIMESTAMP/

# Verify backup
ls -la ~/Work/bots/state/backup/$TIMESTAMP/
echo "Backup created at $TIMESTAMP"
```

---

## Phase 3: Migrate Queue

```bash
cd ~/Work/CodeBot

# Dry run first
python3 -m codebot.migrate_queue --project ~/Work --queue bots/QUEUE.md --dry-run

# If output looks correct, run actual migration
python3 -m codebot.migrate_queue --project ~/Work --queue bots/QUEUE.md

# Verify tickets created
python3 -c "
import sys; sys.path.insert(0, '.')
from codebot.ticket_engine import TicketStore
from pathlib import Path
store = TicketStore(Path('~/Work/.codebot/state/codebot_tickets.json').expanduser())
print(f'Total tickets: {store.count()}')
print(f'Summary: {store.summary()}')
for t in store.list_ready()[:5]:
    print(f'  READY: {t.id} [{t.severity.value}] {t.title[:60]}')
"
```

---

## Phase 4: Validate Project Contract

```bash
cd ~/Work/CodeBot
python3 -m codebot validate --project ~/Work
```

Expected output:
```
Project 'monitor' validated successfully.
  Components: 4
  Agents: N
  Models: 12
  Autonomy level: 2
```

If validation fails, fix `.codebot/project.yaml` paths before proceeding.

---

## Phase 5: Start CodeBot (Local)

```bash
cd ~/Work/CodeBot

# Start with status check first
python3 -m codebot status --project ~/Work

# If status works, start serving
GITHUB_DRY_RUN=1 python3 -m codebot serve --project ~/Work
```

Monitor the output for:
- "CodeBot core bootstrapped: project=monitor"
- Agent spawn messages
- No crash/traceback output

---

## Phase 6: Health Verification

```bash
# Check agents are running
python3 -m codebot status --project ~/Work

# Check logs
tail -f ~/Work/.codebot/logs/orchestrator.log

# Verify heartbeat files being written
ls -la ~/Work/.codebot/state/*.heartbeat

# Check control server (if started)
curl -s http://127.0.0.1:8081/health
```

### Success Criteria
- [ ] Orchestrator shows "Health check every 30s"
- [ ] At least 3 agents in RUNNING state
- [ ] Heartbeat files updating every interval
- [ ] No repeated crash/restart cycles
- [ ] Tickets being picked up from TicketStore

---

## Phase 7: Deploy to Fly.io (Production)

```bash
cd ~/Work/CodeBot

# Set secrets
fly secrets set --app codebot \
  CONTROL_TOKEN=$(openssl rand -hex 32) \
  GH_TOKEN=$GH_TOKEN \
  SSH_PRIVATE_KEY="$SSH_PRIVATE_KEY" \
  GITHUB_DRY_RUN=1

# Build check
fly deploy --app codebot --build-only

# Deploy
fly deploy --app codebot

# Verify
fly machine list --app codebot
fly volume list --app codebot
fly logs --app codebot | tail -20

# Test remote
CONTROL_URL=https://codebot.fly.dev CONTROL_TOKEN=$CONTROL_TOKEN \
  python3 -m codebot status --project ~/Work
```

---

## Phase 8: Enable Live GitHub Writes

After 24h soak test with `GITHUB_DRY_RUN=1`:

```bash
fly secrets set --app codebot GITHUB_DRY_RUN=0
```

Monitor `fly logs` for successful git pushes and GitHub Issue creation.

---

## Rollback Procedure

If CodeBot fails after cutover:

```bash
# 1. Stop CodeBot
python3 -m codebot stop-all --project ~/Work
# Or on Fly: fly machines stop <machine-id>

# 2. Restore old BotNet state
cp -r ~/Work/bots/state/backup/<TIMESTAMP>/* ~/Work/bots/state/

# 3. Clear drain and restart old BotNet
python3 ~/Work/bots/orchestrator.py --clear-drain
~/Work/bots/start_botnet.sh --force

# 4. Verify old BotNet recovered
python3 ~/Work/bots/orchestrator.py --status
tail -f ~/Work/bots/logs/orchestrator.log
```

---

## Post-Migration Cleanup (After 1 Week Stable)

Once CodeBot has been running stably for 7 days:

```bash
# Archive old BotNet files (don't delete yet)
mkdir -p ~/Work/bots-archived
mv ~/Work/bots/*_BOT.md ~/Work/bots-archived/
mv ~/Work/bots/QUEUE.md ~/Work/bots-archived/
mv ~/Work/bots/orchestrator.py ~/Work/bots-archived/
mv ~/Work/bots/api_runner.py ~/Work/bots-archived/
# ... etc for all legacy files

# Update Monitor-BotNet-Client to point to CodeBot control server
# Update fly.toml references
# Remove old systemd service
systemctl --user stop botnet.service
systemctl --user disable botnet.service
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "No .codebot/project.yaml found" | Project profile missing | Create `.codebot/project.yaml` |
| Agents spawn but immediately die | Model API unreachable | Check dialagram config, network |
| Tickets not being picked up | Migration didn't run | Re-run `migrate_queue.py` |
| Gate failures blocking everything | Tests failing in target repo | Fix tests first, or adjust gates |
| Memory OOM on Fly | Too many concurrent agents | Reduce `CODEBOT_MAX_CONCURRENT` |
| "invalid transition" error | Ticket in wrong state | Check ticket store, manual transition |
