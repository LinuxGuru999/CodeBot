"""CodeBot CLI entry point.

Usage:
    python -m codebot serve --project /path/to/project
    python -m codebot status --project /path/to/project
    python -m codebot drain --project /path/to/project
    python -m codebot clear-drain --project /path/to/project
    python -m codebot validate --project /path/to/project
"""
import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="CodeBot — Autonomous Engineering Platform")
    sub = parser.add_subparsers(dest="cmd")

    for name, help_text in [
        ("serve", "Start the orchestrator"),
        ("status", "Print agent status and exit"),
        ("drain", "Set drain flag (stop spawning)"),
        ("clear-drain", "Clear drain flag (resume spawning)"),
        ("validate", "Validate project contract and exit"),
        ("stop-all", "Stop all running agents"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--project", type=str, default=".", help="Path to project root containing .codebot/")

    start = sub.add_parser("start", help="Start specific agents or all")
    start.add_argument("agents", nargs="*", help="Agent names to start")
    start.add_argument("--project", type=str, default=".", help="Path to project root")

    args = parser.parse_args()
    project_root = Path(getattr(args, "project", ".") or ".").resolve()

    if args.cmd == "validate":
        _cmd_validate(project_root)
    elif args.cmd in ("serve", "status", "drain", "clear-drain", "stop-all", "start"):
        _cmd_orchestrator(args.cmd, project_root, args)
    else:
        parser.print_help()
        sys.exit(2)


def _cmd_validate(project_root: Path) -> None:
    from codebot.codebot_bootstrap import bootstrap
    adapter = bootstrap(project_root)
    if adapter is None:
        print(f"No .codebot/project.yaml found at {project_root}", file=sys.stderr)
        sys.exit(1)
    errors = adapter.validate_project()
    if errors:
        print(f"Validation failed ({len(errors)} errors):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print(f"Project '{adapter.project_name()}' validated successfully.")
    print(f"  Components: {len(adapter.components())}")
    print(f"  Agents: {len(adapter.bot_registry())}")
    print(f"  Models: {len(adapter.model_profiles())}")
    print(f"  Autonomy level: {adapter.autonomy_config().level}")


def _cmd_orchestrator(cmd: str, project_root: Path, args: argparse.Namespace) -> None:
    import os
    os.environ["CODEBOT_PROJECT_ROOT"] = str(project_root)

    from codebot.codebot_bootstrap import bootstrap
    bootstrap(project_root)

    from codebot.orchestrator import main as orch_main
    sys.argv = [sys.argv[0]]
    if cmd == "status":
        sys.argv.append("--status")
    elif cmd == "drain":
        sys.argv.append("--drain")
    elif cmd == "clear-drain":
        sys.argv.append("--clear-drain")
    elif cmd == "stop-all":
        sys.argv.append("--stop-all")
    elif cmd == "start":
        sys.argv.append("--start")
        if hasattr(args, "agents") and args.agents:
            sys.argv.extend(args.agents)
    orch_main()


if __name__ == "__main__":
    main()
