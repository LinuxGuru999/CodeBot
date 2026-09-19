# CodeBot Deployment Guide

Last updated: 2026-09-18

## Prerequisites

- Python 3.11+ (3.14 recommended)
- Git with SSH key configured
- GitHub CLI (`gh`) authenticated
- LLM API access (dialagram or compatible OpenAI-compatible endpoint)
- Target project with `.codebot/project.yaml` configured

## Local Development

```bash
# Clone
git clone git@github.com:LinuxGuru999/CodeBot.git
cd CodeBot

# Verify
python3 -m pytest tests/ -q
python3 -m codebot validate --project /path/to/target/project

# Run (dry run — no git push)
GITHUB_DRY_RUN=1 python3 -m codebot serve --project /path/to/target/project
```

## Docker Deployment

```bash
# Build
docker build -t codebot:latest .

# Run
docker run -d \
  --name codebot \
  -v /path/to/project:/project \
  -v codebot_data:/data \
  -e CODEBOT_PROJECT_ROOT=/project \
  -e GH_TOKEN=ghp_xxx \
  -e SSH_PRIVATE_KEY="$(cat ~/.ssh/id_ecdsa)" \
  -e GITHUB_DRY_RUN=1 \
  -p 8081:8081 \
  codebot:latest

# Check health
curl http://localhost:8081/health
```

## Fly.io Deployment

### Initial Setup

```bash
# Create app
fly apps create codebot

# Create persistent volume (3GB minimum)
fly volumes create codebot_data --size 3 --region iad

# Set secrets
fly secrets set \
  CONTROL_TOKEN=$(openssl rand -hex 32) \
  GH_TOKEN=$GH_TOKEN \
  SSH_PRIVATE_KEY="$(cat ~/.ssh/id_ecdsa)" \
  GITHUB_DRY_RUN=1
```

### Deploy

```bash
# Build check
fly deploy --build-only

# Deploy
fly deploy

# Verify
fly machine list
fly logs --app codebot | tail -20

# Test remote control
curl -H "Authorization: Bearer $CONTROL_TOKEN" https://codebot.fly.dev/health
```

### Enable Live Operations

After confirming stable dry-run operation (recommended: 24h soak):

```bash
fly secrets set GITHUB_DRY_RUN=0
```

### Monitoring

```bash
# Real-time logs
fly logs --app codebot

# Agent status
curl -H "Authorization: Bearer $CONTROL_TOKEN" https://codebot.fly.dev/bots

# Specific agent logs
curl -H "Authorization: Bearer $CONTROL_TOKEN" "https://codebot.fly.dev/bots/bug_hunter/logs?lines=50"
```

### Rollback

```bash
# Revert to previous image
fly deploy --image registry.fly.io/codebot:deployment-<previous>

# Or drain and stop
fly ssh console -C "python3 -m codebot drain --project /project"
fly machines stop <machine-id>
```

## Project Configuration

Every managed project needs a `.codebot/` directory:

```
project-root/
├── .codebot/
│   ├── project.yaml        # Required: project contract
│   ├── constitution.md     # Required: protected invariants
│   ├── quality_gates.yaml  # Optional: custom gate policy
│   ├── state/              # Created by CodeBot at runtime
│   └── logs/               # Created by CodeBot at runtime
├── src/                    # Source code
├── tests/                  # Tests
└── ...
```

### Minimal project.yaml

```yaml
schema_version: "1.0"
project:
  name: "my-project"
  repository_root: "."
  primary_language: "python"
architecture:
  style: "monorepo"
  components:
    - name: "core"
      path: "src/"
      type: "backend"
      language: "python"
      description: "Core logic"
testing:
  framework: "pytest"
  test_command: "python3 -m pytest -q"
  test_directories: ["tests/"]
dependencies:
  policy: "stdlib-only"
  allowed_third_party: []
paths:
  state_dir: ".codebot/state/"
  logs_dir: ".codebot/logs/"
  docs_dir: "docs/"
  constitution_file: ".codebot/constitution.md"
  project_config: ".codebot/project.yaml"
autonomy:
  level: 3
  escalation_model: "qa_recommendations"
  qa_recommendation_required_for: ["secrets", "destructive_migrations"]
  autonomous_allowed_for: ["test_additions", "safe_refactors"]
```

## Credential Management

CodeBot resolves credentials in priority order:

1. **Environment variables** (highest priority)
2. **Secret files** (`~/.config/codebot/{name}.txt`)
3. **OpenCode config** (`~/.config/opencode/opencode.jsonc` fallback for API keys)
4. **SSH default paths** (`~/.ssh/id_ecdsa`, `id_ed25519`, `id_rsa`)

Never hardcode credentials in configuration files. Use environment variables or secret management systems.

## Resource Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| Memory | 512MB | 1GB |
| CPU | 1 shared core | 1 dedicated core |
| Disk | 1GB | 3GB+ |
| Network | Outbound HTTPS + SSH | Stable low-latency to LLM API |

Memory usage scales with concurrent agents: ~26MB per agent process plus orchestrator overhead (~50MB).
