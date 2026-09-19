import subprocess, sys
r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_anomaly_alerts.py", "-v", "--tb=short"], capture_output=True, text=True, cwd="/home/kozuka/Work/CodeBot", timeout=60)
print(r.stdout)
print(r.stderr)
print("RC:", r.returncode)
