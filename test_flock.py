"""Minimal test: does flock serialize in this process?"""
import threading
import os
import sys
import re
import tempfile
from pathlib import Path

sys.path.insert(0, '.')
from codebot.locks import flock, LOCK_EX, LOCK_UN

tmpdir = tempfile.mkdtemp()
lockfile = os.path.join(tmpdir, 'test.lock')
datafile = os.path.join(tmpdir, 'data.txt')

with open(datafile, 'w') as f:
    f.write('ORIGINAL')

results = []
errors = []
barrier = threading.Barrier(8)


def do_edit(idx):
    try:
        barrier.wait(timeout=5)
        lock_fd = open(lockfile, 'a+')
        flock(lock_fd.fileno(), LOCK_EX)
        try:
            with open(datafile, 'r') as f:
                content = f.read()
            new_content = content.replace('ORIGINAL', f'REPL_{idx}')
            tmpfile = datafile + f'.tmp.{idx}'
            with open(tmpfile, 'w') as f:
                f.write(new_content)
            os.rename(tmpfile, datafile)
            results.append(idx)
        finally:
            flock(lock_fd.fileno(), LOCK_UN)
            lock_fd.close()
    except Exception as e:
        errors.append((idx, str(e)))


threads = [threading.Thread(target=do_edit, args=(i,)) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=15)

final = open(datafile).read()
print('Results:', sorted(results))
print('Errors:', errors)
print('Final content:', repr(final))
repls = re.findall(r'REPL_\d+', final)
print('Replacements found:', repls)
print('COUNT:', len(repls))
