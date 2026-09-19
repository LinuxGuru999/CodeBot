#!/usr/bin/env python3
import json
from pathlib import Path

with open(Path('/home/kozuka/Work/CodeBot/.codebot/state/tickets.json')) as f:
    data = json.load(f)

claims_dir = Path('/home/kozuka/Work/CodeBot/.codebot/state/claims')
my_claims = set()
all_claims = {}
for cf in claims_dir.glob('*.json'):
    parts = cf.stem.split('.', 1)
    if len(parts) == 2:
        ticket_id, bot = parts
        all_claims.setdefault(ticket_id, []).append(bot)
        if 'general_implementer-2' in bot:
            my_claims.add(ticket_id)

for t in data['tickets']:
    if t['state'] in ('READY', 'IMPLEMENTING'):
        has_my_claim = t['id'] in my_claims
        claimants = ', '.join(all_claims.get(t['id'], [])) or 'none'
        marker = ' [MY CLAIM]' if has_my_claim else ''
        print(f"{t['id']} | {t['state']:12s} | {t['ticket_class']:12s} | claims: {claimants}{marker}")
