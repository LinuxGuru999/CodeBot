"""Test if flock works across threads when using codebot_flock wrapper vs direct fcntl."""
import os, sys, fcntl, threading, time
import tempfile

sys.path.insert(0, '/home/kozuka/Work/CodeBot')
from codebot.locks import flock as codebot_flock, LOCK_EX, LOCK_UN

tmpdir = tempfile.mkdtemp()
lockpath = os.path.join(tmpdir, 'test.lock')
open(lockpath, 'a+').close()

# Test 1: Direct fcntl.flock with os.open
print("=== Test 1: Direct fcntl.flock with os.open ===")
fd1 = os.open(lockpath, os.O_RDWR)
fcntl.flock(fd1, fcntl.LOCK_EX)
fd2 = os.open(lockpath, os.O_RDWR)
try:
    fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("UNBLOCKED - flock not working!")
except BlockingIOError:
    print("BLOCKED - flock working!")
fcntl.flock(fd1, fcntl.LOCK_UN)
os.close(fd1)
os.close(fd2)

# Test 2: codebot_flock with Python open fileno
print("\n=== Test 2: codebot_flock with open().fileno() ===")
f1 = open(lockpath, 'a+')
codebot_flock(f1.fileno(), LOCK_EX)
f2 = open(lockpath, 'a+')
try:
    fcntl.flock(f2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("UNBLOCKED - flock not working!")
except BlockingIOError:
    print("BLOCKED - flock working!")
codebot_flock(f1.fileno(), LOCK_UN)
f1.close()
f2.close()

# Test 3: Thread test - does codebot_flock actually serialize?
print("\n=== Test 3: Thread test with codebot_flock ===")
barrier = threading.Barrier(3)
log = []
log_lock = threading.Lock()

def thread_work(name, delay=0):
    barrier.wait()
    lock_fd = open(lockpath, 'a+')
    fd = lock_fd.fileno()
    codebot_flock(fd, LOCK_EX)
    with log_lock:
        log.append(f"{name}-IN {time.monotonic():.6f}")
    time.sleep(0.05)  # Hold lock for 50ms
    with log_lock:
        log.append(f"{name}-OUT {time.monotonic():.6f}")
    codebot_flock(fd, LOCK_UN)
    lock_fd.close()

threads = [threading.Thread(target=thread_work, args=(f"T{i}",)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=5)

print("Log:", log)
# Check serialization: no overlaps in IN-OUT pairs
in_times = {}
for entry in log:
    name = entry.split()[0]
    ts = float(entry.split()[1])
    if name.endswith('-IN'):
        in_times[name.replace('-IN', '')] = ts
    elif name.endswith('-OUT'):
        base = name.replace('-OUT', '')
        if base in in_times:
            print(f"  {base}: IN={in_times[base]:.6f}, OUT={ts:.6f}, duration={ts - in_times[base]:.6f}")
