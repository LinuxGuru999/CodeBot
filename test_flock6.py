"""Compare os.open+flock vs Python open+fileno+flock in threads."""
import os, sys, fcntl, threading, time, tempfile

tmpdir = tempfile.mkdtemp()

# Test A: os.open() + fcntl.flock()
lock_a = os.path.join(tmpdir, 'a.lock')
open(lock_a, 'a+').close()
barrier_a = threading.Barrier(4)
log_a = []

def worker_a(name):
    barrier_a.wait()
    fd = os.open(lock_a, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    log_a.append(f"{name}-IN  {time.monotonic():.9f}")
    time.sleep(0.05)
    log_a.append(f"{name}-OUT {time.monotonic():.9f}")
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

# Test B: Python open() + fcntl.flock() via fileno
lock_b = os.path.join(tmpdir, 'b.lock')
open(lock_b, 'a+').close()
barrier_b = threading.Barrier(4)
log_b = []

def worker_b(name):
    barrier_b.wait()
    f = open(lock_b, 'a+')
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    log_b.append(f"{name}-IN  {time.monotonic():.9f}")
    time.sleep(0.05)
    log_b.append(f"{name}-OUT {time.monotonic():.9f}")
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    f.close()

# Test C: Python open("a+") + codebot flock wrapper
import sys
sys.path.insert(0, '/home/kozuka/Work/CodeBot')
from codebot.locks import flock as codebot_flock, LOCK_EX, LOCK_UN

lock_c = os.path.join(tmpdir, 'c.lock')
open(lock_c, 'a+').close()
barrier_c = threading.Barrier(4)
log_c = []

def worker_c(name):
    barrier_c.wait()
    f = open(lock_c, 'a+')
    codebot_flock(f.fileno(), LOCK_EX)
    log_c.append(f"{name}-IN  {time.monotonic():.9f}")
    time.sleep(0.05)
    log_c.append(f"{name}-OUT {time.monotonic():.9f}")
    codebot_flock(f.fileno(), LOCK_UN)
    f.close()

def check_serial(label, log_entries):
    events = []
    for entry in log_entries:
        parts = entry.split()
        name = parts[0]
        ts = float(parts[1])
        is_in = '-IN' in name
        base = name.replace('-IN', '').replace('-OUT', '')
        events.append((ts, 'IN' if is_in else 'OUT', base))
    events.sort()
    open_locks = set()
    overlaps = 0
    for ts, action, name in events:
        if action == 'IN':
            if open_locks:
                overlaps += 1
            open_locks.add(name)
        else:
            open_locks.discard(name)
    status = "SERIAL" if overlaps == 0 else f"OVERLAPS({overlaps})"
    print(f"  {label}: {status} (entries: {len(log_entries)})")
    for entry in log_entries:
        print(f"    {entry}")

threads = []
for i in range(4):
    threads.append(threading.Thread(target=worker_a, args=(f"T{i}",)))
    threads.append(threading.Thread(target=worker_b, args=(f"T{i}",)))
    threads.append(threading.Thread(target=worker_c, args=(f"T{i}",)))
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=5)

print("Results:")
check_serial("os.open+flock (A)", log_a)
check_serial("open+fileno+flock (B)", log_b)
check_serial("open+codebot_flock (C)", log_c)
