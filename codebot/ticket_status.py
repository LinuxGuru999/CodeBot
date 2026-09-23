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
        # Add project root to path to ensure imports work
        project_root = Path(__file__).resolve().parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
            
        from codebot.ticket_engine import TicketStore
        ts = TicketStore(tickets_file)
        print(f"Tickets: {ts.count()} total")
        for s, c in sorted(ts.summary().items()):
            if c > 0:
                print(f"  {s}: {c}")
    except Exception as e:
        print(f"Tickets: error reading store ({e})")


if __name__ == "__main__":
    main()
