import pathlib
def test_aa_cleanup():
    base = pathlib.Path("/home/kozuka/Work/CodeBot/tests")
    # list of temporary files to delete (keep telemetry security test and aa_cleanup itself until last)
    tmp_names = [
        "test_check_diff.py",
        "test_check_diff2.py",
        "test_final_cleanup.py",
        "test_z_cleanup.py",
        "test_cleanup_remaining.py",
        "test_cleanup_self.py",
        "test_cleanup_claim.py",
        "test_cleanup_final.py",
        "test_check_diff2.py",
        "test_final_cleanup.py",
    ]
    for name in tmp_names:
        p = base / name
        if p.exists():
            p.unlink()
            print(f"deleted {name}")
    # also delete this file's pyc after? leave for now
    # verify core fix still intact
    import codebot.control_server as cs
    assert "CONTROL_TOKEN" not in cs.TELEMETRY_UNAUTHORIZED_HINT
    assert "CONTROL_TOKEN" not in cs.TELEMETRY_NOT_CONFIGURED_HINT
    assert "CODEBOT_TELEMETRY_TOKEN" in cs.TELEMETRY_UNAUTHORIZED_HINT
    assert hasattr(cs, "CONTROL_ALLOW_UNAUTHENTICATED")
    # ensure security test still present
    assert (base / "test_telemetry_hints_security.py").exists()
    print("cleanup step1 done")
