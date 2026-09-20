"""Debug flock with timing to find the real issue."""
import os, sys, threading, time, traceback
import fcntl
import tempfile
from pathlib import Path

sys.path.insert(0, '/home/kozuka/Work/CodeBot')
from codebot.locks import flock as codebot_flock, LOCK_EX, LOCK_UN

tmpdir = tempfile.mkdtemp()
data_file = Path(tmpdir) / 'test.txt'
lock_file = Path(str(data_file) + '.lock')

markers = [f"MARKER_{i:04d}" for i in range(8)]
initial = "\n".join(markers) + "\n"
data_file.write_text(initial, encoding='utf-8')

barrier = threading.Barrier(8)
log = []
log_lock = threading.Lock()

def log_msg(msg):
    with log_lock:
        log.append(f"{time.monotonic():.6f} {threading.current_thread().name}: {msg}")

def do_edit(idx):
    try:
        barrier.wait(timeout=10)
        marker = f"MARKER_{idx:04d}"
        replacement = f"REPLACED_{idx:04d}"

        lock_fd = None
        try:
            lock_fd = open(lock_file, "a+")
            fd_num = lock_fd.fileno()
            log_msg(f"open lock → fd={fd_num}, calling flock(LOCK_EX)")
            codebot_flock(fd_num, LOCK_EX)
            log_msg(f"GOT lock, reading file")

            text = data_file.read_text(encoding='utf-8')
            count = text.count(marker)
            log_msg(f"marker count={count}, replacing {marker}→{replacement}")
            
            if count != 1:
                log_msg(f"SKIP: count={count}")
                return

            new_text = text.replace(marker, replacement, 1)

            unique_id = f"{os.getpid()}.{time.monotonic_ns()}"
            tmp = Path(str(data_file) + f".tmp.{unique_id}")
            tmp.write_text(new_text, encoding='utf-8')
            tmp.replace(data_file)
            log_msg(f"wrote and renamed tmp→data_file")
        finally:
            if lock_fd is not None:
                codebot_flock(fd_num, LOCK_UN)
                log_msg(f"released lock")
                lock_fd.close()
    except Exception as e:
        log_msg(f"ERROR: {traceback.format_exc()}")

threads = [threading.Thread(target=do_edit, args=(i,), name=f"T{i}") for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)

final = data_file.read_text()
print("=== LOG ===")
for entry in log:
    print(entry)
print("\n=== FINAL CONTENT ===")
print(final)
repls = [m for m in markers if f"REPLACED_{m[-4:]}" in final]
print(f"Replaced: {len(repls)}/8")
