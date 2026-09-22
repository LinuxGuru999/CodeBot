import os, tempfile, sys
from pathlib import Path
sys.path.insert(0, '/home/kozuka/Work/CodeBot')
from codebot.tool_policy import resolve_workspace_path
import inspect
print("src contains realpath:", "realpath" in inspect.getsource(resolve_workspace_path))
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)/"workspace"
    ws.mkdir()
    (ws/"src").mkdir()
    results=[]
    def c(n, cond): print(f"{'PASS' if cond else 'FAIL'}: {n}")
    c("legit relative", resolve_workspace_path("src/file.py", ws) is not None)
    c("traversal", resolve_workspace_path("../escape.txt", ws) is None)
    c("outside absolute", resolve_workspace_path("/etc/passwd", ws) is None)
    evil=Path(td)/"workspace-evil"
    evil.mkdir()
    (evil/"secret.txt").write_text("evil")
    c("prefix blocked", resolve_workspace_path(str(evil/"secret.txt"), ws) is None)
    ext=Path(td)/"external.txt"
    ext.write_text("SENSITIVE")
    link=ws/"link.txt"
    link.symlink_to(ext)
    c("symlink file abs", resolve_workspace_path(str(link), ws) is None)
    c("symlink file rel", resolve_workspace_path("link.txt", ws) is None)
    ext_dir=Path(td)/"ext_dir"
    ext_dir.mkdir()
    (ext_dir/"passwd").write_text("evil")
    ld=ws/"escape_link"
    ld.symlink_to(ext_dir)
    c("symlink dir", resolve_workspace_path("escape_link/passwd", ws) is None)
    c("ws root", resolve_workspace_path(str(ws), ws) is not None)
    c("nonexistent inside", resolve_workspace_path("new_file.txt", ws) is not None)
    dang=ws/"dangling"
    dang.symlink_to(Path(td)/"nonexistent/file")
    c("dangling outside", resolve_workspace_path(str(dang), ws) is None)
    dang2=ws/"dangling_inside"
    if dang2.exists() or dang2.is_symlink(): dang2.unlink()
    dang2.symlink_to(ws/"nowhere_inside.txt")
    c("dangling inside allowed", resolve_workspace_path(str(dang2), ws) is not None)
    # double symlink
    link2=ws/"link2"
    link2.symlink_to(link)
    c("chain", resolve_workspace_path("link2", ws) is None)
    # dot
    c("dot allowed", resolve_workspace_path(".", ws) is not None)
    c("empty allowed as root", resolve_workspace_path("", ws) is not None)
    print("done")
