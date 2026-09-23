from pathlib import Path
p = Path("codebot/telemetry.py")
lines = p.read_text().splitlines()
for i, l in enumerate(lines, 1):
    print(f"{i:3}: {l}")
