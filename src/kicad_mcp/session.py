"""Live connection to a running KiCad instance over the IPC API.

Board state is never cached. KiCad is the source of truth and the user is very
likely editing while the model reads, so every call re-fetches.

The important behaviour here is error translation. KiCad registers API handlers
per editor window, so a request whose editor is closed fails with "no handler
available" rather than returning an empty result. Passed through raw that reads
like a server bug; translated it tells the user to open the PCB editor.
"""

from __future__ import annotations

import threading

from .errors import NoEditorOpenError, NotConnectedError

_NO_HANDLER_MARKERS = ("no handler available", "no handler for")
_UNREACHABLE_MARKERS = (
    "timed out",
    "timeout",
    "connection refused",
    "could not connect",
    "no such file",
    "econnrefused",
)


def translate(exc: Exception) -> Exception:
    """Map a kipy exception onto an error that names the remedy."""
    text = str(exc).lower()
    if any(m in text for m in _NO_HANDLER_MARKERS):
        return NoEditorOpenError(
            "KiCad is running but the editor for this document is not open."
        )
    if any(m in text for m in _UNREACHABLE_MARKERS):
        return NotConnectedError(f"Could not reach KiCad: {exc}")
    return exc


class Session:
    """Lazily-connected kipy client with reconnect on transport failure."""

    def __init__(self) -> None:
        self._kicad = None
        self._lock = threading.Lock()

    def connect(self, force: bool = False):
        with self._lock:
            if self._kicad is not None and not force:
                return self._kicad
            try:
                from kipy import KiCad  # imported lazily so the server starts without KiCad
            except ImportError as exc:  # pragma: no cover - env-specific
                raise NotConnectedError(
                    f"kicad-python (kipy) is not importable: {exc}"
                ) from exc
            try:
                self._kicad = KiCad()
                self._kicad.get_version()
            except Exception as exc:
                self._kicad = None
                raise translate(exc) from exc
            return self._kicad

    @property
    def kicad(self):
        return self.connect()

    def is_connected(self) -> bool:
        try:
            self.connect()
            return True
        except Exception:
            return False

    def board(self):
        """The open board.

        Retries once on failure: a stale socket after KiCad restarts is common
        in a long-lived server, and a single transparent reconnect turns an
        avoidable error into a working call.
        """
        kicad = self.connect()
        try:
            return kicad.get_board()
        except Exception as exc:
            translated = translate(exc)
            if isinstance(translated, NoEditorOpenError):
                raise translated from exc
            try:
                kicad = self.connect(force=True)
                return kicad.get_board()
            except Exception as exc2:
                raise translate(exc2) from exc2

    def status(self) -> dict:
        """Environment report that never raises; used by kicad_status/doctor."""
        out: dict = {"connected": False, "board_open": False}
        try:
            kicad = self.connect()
        except Exception as exc:
            out["error"] = str(exc)
            remedy = getattr(exc, "remedy", "")
            if remedy:
                out["remedy"] = remedy
            return out

        out["connected"] = True
        try:
            out["kicad_version"] = str(kicad.get_version())
            out["api_version"] = str(kicad.get_api_version())
        except Exception as exc:  # pragma: no cover - defensive
            out["version_error"] = str(exc)

        try:
            board = kicad.get_board()
            out["board_open"] = True
            out["board_name"] = getattr(board, "name", None)
        except Exception as exc:
            translated = translate(exc)
            out["board_open"] = False
            out["board_status"] = str(translated)
            remedy = getattr(translated, "remedy", "")
            if remedy:
                out["board_remedy"] = remedy
        return out


SESSION = Session()
