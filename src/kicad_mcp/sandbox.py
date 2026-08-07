"""The escape hatch: run Python against the KiCad API in-process.

This exists so the server has complete API coverage from day one. Curated tools
cover the common paths; anything else is a script. It also means new curated
tools can be justified by observed use rather than guessed at.

Security posture, stated plainly: this is arbitrary local code execution with the
user's privileges. That is the deliberate trade for full API access, not an
oversight. Run the server only against KiCad instances and files you trust.
"""

from __future__ import annotations

import contextlib
import io
import threading
import traceback
from typing import Any

from .session import SESSION


def _build_globals() -> dict[str, Any]:
    """Pre-import what scripts almost always need.

    `board` is fetched leniently: a script that only wants pcbnew or file parsing
    should not fail merely because the PCB editor happens to be closed.
    """
    from . import cli, generator, schematic, sexpr, symbols, units

    env: dict[str, Any] = {
        "__name__": "__kicad_mcp_script__",
        "sexpr": sexpr,
        "units": units,
        "schematic": schematic,
        "symbols": symbols,
        "generator": generator,
        "cli": cli,
    }

    try:
        import kipy

        env["kipy"] = kipy
    except Exception:  # noqa: BLE001 - optional
        env["kipy"] = None

    try:
        import pcbnew

        env["pcbnew"] = pcbnew
    except Exception:  # noqa: BLE001 - optional
        env["pcbnew"] = None

    try:
        env["kicad"] = SESSION.connect()
    except Exception:  # noqa: BLE001 - script may not need a live connection
        env["kicad"] = None

    try:
        env["board"] = SESSION.board()
    except Exception:  # noqa: BLE001
        env["board"] = None

    return env


def run_script(code: str, timeout: int = 30) -> dict:
    """Execute `code`, returning captured output and any `result` value.

    Runs on a worker thread so a runaway script cannot wedge the server. Python
    cannot forcibly kill a thread, so on timeout we report and let it finish in
    the background rather than pretending it was stopped.
    """
    env = _build_globals()
    stdout = io.StringIO()
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
                exec(code, env)  # noqa: S102 - arbitrary execution is the point
            outcome["ok"] = True
        except Exception:  # noqa: BLE001 - tracebacks are the useful output here
            outcome["ok"] = False
            outcome["traceback"] = traceback.format_exc()

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        return {
            "ok": False,
            "timed_out": True,
            "timeout_seconds": timeout,
            "stdout": stdout.getvalue()[-8000:],
            "message": (
                f"Script exceeded {timeout}s and is still running in the background; "
                "Python cannot forcibly terminate it."
            ),
            "remedy": "Raise the timeout, or make the script incremental.",
        }

    result: dict[str, Any] = {
        "ok": outcome.get("ok", False),
        "stdout": stdout.getvalue()[-16000:],
    }
    if "traceback" in outcome:
        result["traceback"] = outcome["traceback"][-4000:]
    if "result" in env:
        try:
            result["result"] = repr(env["result"])[:8000]
        except Exception as exc:  # noqa: BLE001 - repr can fail on odd objects
            result["result"] = f"<unreprable: {exc}>"
    return result
