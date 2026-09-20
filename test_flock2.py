"""Debug test matching the exact edit() pattern."""
import os, sys, threading, time, traceback
import tempfile
from pathlib import Path

sys.path.insert(0, '/home/kozuka/Work/CodeBot')
from codebot.locks import flock, LOCK_EX, LOCK_UN

tmpdir = tempfile.mkdtemp()
data_file = Path(tmpdir) / 'test.txt'
lock_file = Path(str(data_file) + '.lock')

markers = [f"MARKER_{i:04d}" for i in range(16)]
initial = "\n".join(markers) + "\n"
data_file.write_text(initial, encoding='utf-8')

print(f"Initial: {data_file.read_text()[:80]}...")

barrier = threading.Barrier(16)
errors = []

def do_edit(idx):
    try:
        barrier.wait(timeout=10)
        marker = f"MARKER_{idx:04d}"
        replacement = f"REPLACED_{idx:04d}"

        lock_fd = None
        try:
            lock_fd = open(lock_file, "a+")
            flock(lock_fd.fileno(), LOCK_EX)

            # Read
            text = data_file.read_text(encoding='utf-8')
            count = text.count(marker)
            if count != 1:
                errors.append((idx, f"count={count}"))
                return

            new_text = text.replace(marker, replacement, 1)

            # Atomic tmp+replace
            unique_id = f"{os.getpid()}.{time.monotonic_ns()}"
            tmp = Path(str(data_file) + f".tmp.{unique_id}")
            tmp.write_text(new_text, encoding='utf-8')
            tmp.replace(data_file)
        finally:
            if lock_fd is not None:
                flock(lock_fd.fileno(), LOCK_UN)
                lock_fd.close()
    except Exception as e:
        errors.append((idx, traceback.format_exc()))

threads = [threading.Thread(target=do_edit, args=(i,)) for i in range(16)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)

final = data_file.read_text()
repls = [m for m in markers if f"REPLACED_{m[-4:]}" in final]
missing = [m for m in markers if f"REPLACED_{m[-4:]}" not in final]
print(f"Replaced: {len(repls)}/16")
print(f"Missing: {missing}")
print(f"Errors: {errors}")
print(f"Final content:\n{final}")
