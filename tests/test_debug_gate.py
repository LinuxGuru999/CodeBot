import pathlib, zlib, json, sys, os
def test_debug():
    # try to read gatekeeper from master HEAD
    ref_path = pathlib.Path("/home/kozuka/Work/CodeBot/.git/refs/heads/master")
    if ref_path.exists():
        sha = ref_path.read_text().strip()
        print(f"master sha: {sha}")
    head_ref = pathlib.Path("/home/kozuka/Work/CodeBot/.git/HEAD").read_text().strip()
    print(f"HEAD ref: {head_ref}")
    if head_ref.startswith("ref:"):
        r = head_ref.split(" ",1)[1]
        p = pathlib.Path("/home/kozuka/Work/CodeBot/.git") / r
        if p.exists():
            sha2 = p.read_text().strip()
            print(f"current branch sha: {sha2}")
            # cat commit
            obj = pathlib.Path(f"/home/kozuka/Work/CodeBot/.git/objects/{sha2[:2]}/{sha2[2:]}")
            if obj.exists():
                data = zlib.decompress(obj.read_bytes())
                nul = data.find(b'\x00')
                print(data[nul+1:nul+2000].decode(errors='ignore'))
                # find tree
                import re
                m = re.search(br'tree (\w+)', data[nul+1:])
                if m:
                    tree = m.group(1).decode()
                    print(f"tree {tree}")
                    obj2 = pathlib.Path(f"/home/kozuka/Work/CodeBot/.git/objects/{tree[:2]}/{tree[2:]}")
                    data2 = zlib.decompress(obj2.read_bytes())
                    nul2 = data2.find(b'\x00')
                    tb = data2[nul2+1:]
                    i=0
                    while i < len(tb):
                        sp = tb.find(b' ', i)
                        mode = tb[i:sp].decode()
                        nulx = tb.find(b'\x00', sp)
                        name = tb[sp+1:nulx].decode()
                        sha3 = tb[nulx+1:nulx+21].hex()
                        if name in ("codebot",):
                            print(f"  {mode} {name} {sha3}")
                            obj3 = pathlib.Path(f"/home/kozuka/Work/CodeBot/.git/objects/{sha3[:2]}/{sha3[2:]}")
                            data3 = zlib.decompress(obj3.read_bytes())
                            nul3 = data3.find(b'\x00')
                            tb3 = data3[nul3+1:]
                            j=0
                            while j < len(tb3):
                                sp2 = tb3.find(b' ', j)
                                mode2 = tb3[j:sp2].decode()
                                nulx2 = tb3.find(b'\x00', sp2)
                                name2 = tb3[sp2+1:nulx2].decode()
                                sha4 = tb3[nulx2+1:nulx2+21].hex()
                                if "gatekeeper" in name2:
                                    print(f"    {mode2} {name2} {sha4}")
                                    obj4 = pathlib.Path(f"/home/kozuka/Work/CodeBot/.git/objects/{sha4[:2]}/{sha4[2:]}")
                                    data4 = zlib.decompress(obj4.read_bytes())
                                    nul4 = data4.find(b'\x00')
                                    print(data4[nul4+1:nul4+4000].decode(errors='ignore'))
                                j = nulx2+21
                        i = nulx+21
    assert True

def test_debug2():
    # read current gatekeeper.py lines around exception
    from pathlib import Path
    text = Path("/home/kozuka/Work/CodeBot/codebot/gatekeeper.py").read_text()
    idx = text.find("except Exception as e:")
    print(text[idx-500:idx+1500])
    assert True
