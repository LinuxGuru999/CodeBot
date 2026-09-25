"""Report ticket store status.

Usage:
    python3 -m codebot.ticket_status [state_dir]
"""
import sys
from pathlib import Path


def main() -> None:
    state_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".codebot/state")
    tickets_file = state_dir / "tickets.json"
    
    if not tickets_file.exists():
        print("Tickets: no store found")
        return

    try:
        project_root = Path(__file__).resolve().parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from codebot.ticket_dispatcher import get_ticket_store
        ts = get_ticket_store(state_dir)
        if ts is None:
            print("Tickets: no store found")
            return
        print(f"Tickets: {ts.count()} total")
        for s, c in sorted(ts.summary().items()):
            if c > 0:
                print(f"  {s}: {c}")
    except Exception as e:
        print(f"Tickets: error reading store ({e})")


if __name__ == "__main__":
    main()
