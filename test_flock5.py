"""Test: does fcntl.flock work for thread serialization on this Linux system?"""
import os, sys, fcntl, threading, time, tempfile

tmpdir = tempfile.mkdtemp()
lockpath = os.path.join(tmpdir, 'test.lock')
open(lockpath, 'a+').close()

# Test: Use os.open() + fcntl.flock() directly in threads
barrier = threading.Barrier(4)
log = []
log_lock = threading.Lock()

def worker(name):
    barrier.wait()
    fd = os.open(lockpath, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    with log_lock:
        log.append(f"{name}-IN  {time.monotonic():.9f}")
    time.sleep(0.05)
    with log_lock:
        log.append(f"{name}-OUT {time.monotonic():.9f}")
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

threads = [threading.Thread(target=worker, args=(f"T{i}",)) for i in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=5)

print("Log:")
for entry in log:
    print(f"  {entry}")

# Parse to check overlaps
events = []
for entry in log:
    parts = entry.split()
    name = parts[0]
    ts = float(parts[1])
    is_in = '-IN' in name
    base = name.replace('-IN', '').replace('-OUT', '')
    events.append((ts, 'IN' if is_in else 'OUT', base))

events.sort()
print("\nSorted:")
open_locks = set()
overlaps = False
for ts, action, name in events:
    if action == 'IN':
        if open_locks:
            print(f"  OVERLAP at {ts}: {name} entering while {open_locks} hold lock!")
            overlaps = True
        open_locks.add(name)
        print(f"  {ts:.9f} {name} ENTERS (held by: {open_locks})")
    else:
        open_locks.discard(name)
        print(f"  {ts:.9f} {name} EXITS  (held by: {open_locks})")

if not overlaps:
    print("\nflock IS serializing threads correctly!")
else:
    print("\nflock is NOT serializing threads!")
