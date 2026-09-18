# credentials.py

Resolves SSH keys, GitHub tokens, and API credentials from environment variables, secret files, or adapter configuration. Never hardcodes secrets.

## Key Exports
- `get_github_token()`: Function
- `get_control_token()`: Function
- `get_ssh_key_path()`: Function
- `get_ssh_auth_sock()`: Function
- `get_git_ssh_command()`: Function

## Invariants
- stdlib-only
- Secrets never logged or written to state files
- Environment variables take precedence over file-based secrets
- Missing credentials degrade gracefully (operations fail, don't crash)
