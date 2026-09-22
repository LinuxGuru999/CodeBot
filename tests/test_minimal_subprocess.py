import subprocess, os

def test_raw_subprocess(tmp_path):
    # Do not import codebot
    import sys
    results=[]
    results.append(f"subprocess.run module={subprocess.run.__module__} name={getattr(subprocess.run,'__name__', 'unknown')} qual={getattr(subprocess.run,'__qualname__','')}")
    try:
        results.append(f"run source file={subprocess.__file__}")
    except Exception as e:
        results.append(f"source err {e}")
    # Check if it's wrapped
    import inspect
    try:
        results.append(f"run is function? inspect.isfunction={inspect.isfunction(subprocess.run)}")
        results.append(f"run code? {inspect.getsourcefile(subprocess.run)}")
    except Exception as e:
        results.append(f"inspect error {e}")
    # Try Popen directly
    for argv in [["false"], ["/usr/bin/false"], ["true"], ["ls","/nonexistent"]]:
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate(timeout=2)
            results.append(f"Popen {argv} returncode={p.returncode} stdout={out!r} stderr={err!r}")
        except Exception as e:
            results.append(f"Popen {argv} error {e}")
    for argv in [["false"], ["/usr/bin/false"]]:
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=5)
            results.append(f"run {argv} code={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r} args={proc.args}")
        except Exception as e:
            results.append(f"run {argv} error {e}")
    # os.system
    try:
        rc = os.system("false; echo EXIT:$?")
        results.append(f"os.system false => {rc} (raw wait status {rc} exit {(os.WEXITSTATUS(rc) if rc!=-1 else 'n/a')})")
    except Exception as e:
        results.append(f"os.system error {e}")
    try:
        rc = os.system("/usr/bin/false; echo hi")
        results.append(f"os.system /usr/bin/false => {rc}")
    except Exception as e:
        results.append(f"os.system2 error {e}")
    assert False, "\n".join(results)
