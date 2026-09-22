import subprocess, sys, os, shutil, hashlib
from pathlib import Path

def test_probe_exit_codes(tmp_path):
    cmds = [
        (["false"], "false"),
        (["/usr/bin/false"], "/usr/bin/false"),
        (["/bin/false"], "/bin/false"),
        (["true"], "true"),
        (["python3","-c","import sys; sys.exit(1)"], "python exit 1"),
        (["python3","-c","import sys; sys.exit(2)"], "python exit 2"),
        (["sh","-c","exit 1"], "sh exit 1"),
        (["bash","-c","exit 1"], "bash exit 1"),
        (["ls","/nonexistent"], "ls nonexistent"),
    ]
    results=[]
    for argv, name in cmds:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
            results.append(f"{name}: argv={argv} code={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}")
        except Exception as e:
            results.append(f"{name} error {e}")
    # also try file command on /usr/bin/false
    try:
        proc = subprocess.run(["file","/usr/bin/false"], capture_output=True, text=True, timeout=5)
        results.append(f"file /usr/bin/false: {proc.stdout!r} code={proc.returncode}")
    except Exception as e:
        results.append(f"file error {e}")
    # sha256 of /usr/bin/false
    try:
        data = Path("/usr/bin/false").read_bytes()
        results.append(f"/usr/bin/false sha256={hashlib.sha256(data).hexdigest()} size={len(data)}")
        # try strings
        head = data[:500]
        results.append(f"head hex {head.hex()[:200]}")
    except Exception as e:
        results.append(f"read false error {e}")
    # check ld preload
    results.append(f"LD_PRELOAD={os.environ.get('LD_PRELOAD')}")
    results.append(f"PATH={os.environ.get('PATH')}")
    out = "\n".join(results)
    (tmp_path/"probe.txt").write_text(out)
    assert False, out

def test_false_via_quality_gate(tmp_path):
    from codebot.quality_gate import evaluate_gate
    ev = evaluate_gate({"name":"bad","command":"false"}, tmp_path)
    ev2 = evaluate_gate({"name":"bad2","command":"python3 -c \"import sys; sys.exit(1)\""}, tmp_path)
    ev3 = evaluate_gate({"name":"bad3","command":"/usr/bin/false"}, tmp_path)
    msg = f"false -> {ev.result} {ev.passed} code in err={ev.error_message}\npython exit1 -> {ev2.result} {ev2.passed}\n/usr/bin/false -> {ev3.result} {ev3.passed}"
    assert False, msg

def test_hash_logic(tmp_path):
    from codebot.quality_gate import _hash_files
    ws = tmp_path/"ws"
    ws.mkdir()
    (ws/"a.py").write_text("hello\n")
    outside = tmp_path/"outside.py"
    outside.write_text("SECRET\n")
    # before and after outside exists, traversal hash should be same (missing)
    h_no_outside = _hash_files(ws, ["../outside.py"])
    # now outside exists already, same call
    h_with_outside = _hash_files(ws, ["../outside.py"])
    # also hash for a missing inside file with same name but inside
    h_inside_missing = _hash_files(ws, ["missing.py"])
    # compute expected missing hash for "../outside.py" manually
    import hashlib
    # Use current implementation logic to predict
    # Also check what hash would be if it read SECRET
    ws_res = ws.resolve()
    def expected_missing(rel):
        d=hashlib.sha256()
        d.update(rel.encode()+b"\x00")
        d.update(b"<missing>\x00")
        d.update(b"\x00")
        return d.hexdigest()
    def expected_with_content(rel, content):
        d=hashlib.sha256()
        d.update(rel.encode()+b"\x00")
        d.update(content)
        d.update(b"\x00")
        return d.hexdigest()
    exp_missing = expected_missing("../outside.py")
    exp_read = expected_with_content("../outside.py", b"SECRET\n")
    msg = f"h_no_outside={h_no_outside}\nh_with_outside={h_with_outside}\nh_inside_missing={h_inside_missing}\nexp_missing={exp_missing}\nexp_read={exp_read}\nmatch_missing={h_with_outside==exp_missing}\nmatch_read={h_with_outside==exp_read}\n"
    assert False, msg
