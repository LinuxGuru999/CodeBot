import pathlib, os, subprocess, shlex, hashlib
from pathlib import Path

def test_false_path_inspect(tmp_path):
    import shutil, os
    # inspect PATH entries for false
    paths = os.environ.get("PATH","").split(":")
    found=[]
    for p in paths:
        cand = Path(p)/"false"
        if cand.exists():
            try:
                found.append(f"{cand} exists size={cand.stat().st_size} is_symlink={cand.is_symlink()} target={cand.resolve() if cand.is_symlink() else 'n/a'}")
                # try read first bytes
                with open(cand,"rb") as f:
                    head = f.read(200)
                    found.append(f" head hex={head[:50].hex()} text={head[:200]!r}")
            except Exception as e:
                found.append(f"{cand} error {e}")
        else:
            found.append(f"{cand} not exists")
    # also check /bin/false and /usr/bin/false directly
    for extra in ["/bin/false","/usr/bin/false","/usr/local/bin/false"]:
        cand=Path(extra)
        if cand.exists():
            try:
                found.append(f"{cand} exists size={cand.stat().st_size} symlink={cand.is_symlink()}")
                with open(cand,"rb") as f:
                    head=f.read(200)
                    found.append(f" {cand} head={head[:100]!r}")
            except Exception as e:
                found.append(f"{cand} error {e}")
    # also try running false via different methods
    try:
        proc = subprocess.run(["false"], capture_output=True)
        found.append(f"subprocess ['false'] code={proc.returncode}")
    except Exception as e:
        found.append(f"subprocess false error {e}")
    try:
        proc = subprocess.run(["/usr/bin/false"], capture_output=True)
        found.append(f"/usr/bin/false code={proc.returncode}")
    except Exception as e:
        found.append(f"/usr/bin/false error {e}")
    try:
        proc = subprocess.run(["/bin/false"], capture_output=True)
        found.append(f"/bin/false code={proc.returncode}")
    except Exception as e:
        found.append(f"/bin/false error {e}")
    # check true for comparison
    try:
        proc = subprocess.run(["true"], capture_output=True)
        found.append(f"true code={proc.returncode}")
    except Exception as e:
        found.append(f"true error {e}")
    # check shlex
    found.append(f"shlex false {shlex.split('false')}")
    # dump to file for assert
    out = "\n".join(found)
    (tmp_path/"out.txt").write_text(out)
    # also test shell via subprocess with shell True
    try:
        proc = subprocess.run("false", shell=True, capture_output=True)
        found.append(f"shell True false code={proc.returncode}")
    except Exception as e:
        found.append(f"shell error {e}")
    (tmp_path/"out2.txt").write_text("\n".join(found))
    assert False, "\n".join(found)

def test_hash_inspect(tmp_path):
    from codebot.quality_gate import _hash_files
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws/"a.py").write_text("hello\n")
    # compute hashes manually to understand
    import hashlib
    ws_res = ws.resolve()
    def hash_manual(files):
        d=hashlib.sha256()
        for rel in sorted(files):
            p=ws/rel
            d.update(rel.encode()+b"\x00")
            try:
                cand=p.resolve()
                is_inside=cand.is_relative_to(ws_res)
                if not is_inside:
                    raise ValueError(f"escape {rel} -> {cand} not in {ws_res}")
                with open(cand,"rb") as f:
                    for chunk in iter(lambda: f.read(65536),b""):
                        d.update(chunk)
            except (OSError, ValueError) as e:
                d.update(b"<missing>\x00")
            d.update(b"\x00")
        return d.hexdigest()
    h1 = _hash_files(ws, ["../outside.py"])
    h2 = _hash_files(ws, ["nonexistent.py"])
    h1m = hash_manual(["../outside.py"])
    h2m = hash_manual(["nonexistent.py"])
    # also test same name missing vs traversal but same string length? no
    # test that traversal with same rel name as missing equals
    # e.g., both use "foo.py" where one is missing due to traversal and other missing due to not exists?
    # but traversal rel is different from missing rel, so can't be equal
    # Instead test that traversal file outside is NOT read
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET\n")
    h_trav = _hash_files(ws, ["../outside.py"])
    # if it incorrectly read outside, hash would include SECRET
    d = hashlib.sha256()
    d.update(b"../outside.py\x00")
    d.update(b"SECRET\n")
    d.update(b"\x00")
    hash_if_read = d.hexdigest()
    d2 = hashlib.sha256()
    d2.update(b"../outside.py\x00")
    d2.update(b"<missing>\x00\x00")
    hash_if_missing = d2.hexdigest()
    # actually our code does rel + missing + 0x00 after, let's compute precisely:
    d3 = hashlib.sha256()
    d3.update(b"../outside.py\x00")
    d3.update(b"<missing>\x00")
    d3.update(b"\x00")
    hash_missing_correct = d3.hexdigest()
    out = f"h_traversal={h1}\nh_missing={h2}\nh1m={h1m}\nh2m={h2m}\nh_trav_after_outside={h_trav}\nhash_if_read={hash_if_read}\nhash_if_missing={hash_missing_correct}\nis_read={h_trav==hash_if_read}\nis_missing={h_trav==hash_missing_correct}\n"
    (tmp_path/"hash.txt").write_text(out)
    assert False, out
