"""Entry point: `python -m kicad_mcp` runs the server; `doctor` diagnoses."""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from .doctor import main as doctor_main

        raise SystemExit(doctor_main())

    from .server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
