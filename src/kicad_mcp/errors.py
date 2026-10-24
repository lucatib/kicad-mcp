"""Exceptions that carry a remedy, not just a diagnosis.

Every error surfaced to an MCP client should tell the caller what to do next.
A model can act on "open the PCB editor"; it cannot act on a stack trace.
"""

from __future__ import annotations


class KicadMcpError(Exception):
    """Base error. `remedy` is shown to the caller alongside the message."""

    remedy: str = ""

    def __init__(self, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        if remedy:
            self.remedy = remedy

    def as_dict(self) -> dict:
        out = {"error": type(self).__name__, "message": self.message}
        if self.remedy:
            out["remedy"] = self.remedy
        return out


class KicadNotFoundError(KicadMcpError):
    remedy = (
        "Install KiCad 10, or set KICAD_MCP_KICAD_ROOT to the install directory "
        r"(the one containing bin\kicad-cli.exe)."
    )


class UnsupportedPlatformError(KicadMcpError):
    remedy = "This server currently supports Windows only."


class NotConnectedError(KicadMcpError):
    remedy = (
        "Start KiCad, then enable Preferences > Plugins > 'Enable IPC API server' "
        "and restart KiCad."
    )


class NoEditorOpenError(KicadMcpError):
    """KiCad is running but the relevant editor frame is not open.

    KiCad registers API handlers per editor window, so a request for a document
    type whose editor is closed fails rather than returning an empty result.
    """

    remedy = (
        "KiCad is running but no PCB editor is open. Open your board in the PCB "
        "editor - the API only serves editors that are currently open."
    )


class SchematicApiUnavailableError(KicadMcpError):
    """KiCad 10 exposes no schematic IPC API at all.

    The 10.0 release branch defines zero schematic commands; the full schematic
    type model and its commands exist only on KiCad's master branch. Schematic
    work therefore goes through kicad-cli and direct .kicad_sch parsing.
    """

    remedy = (
        "KiCad 10 has no schematic IPC API. Use the file-based schematic tools "
        "(open_schematic, schematic_netlist, run_erc) instead."
    )


class ToolInputError(KicadMcpError):
    """Caller passed something invalid. Remedy is supplied per-instance."""


class CliError(KicadMcpError):
    remedy = "Check the KiCad file path and that the file is not open elsewhere."
