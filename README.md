# kicad-mcp

An MCP server for **KiCad 10** on Windows. Gives an LLM live access to the board
open in KiCad, file-based access to schematics, symbol library search, schematic
generation from a firmware pinout, and a scripting escape hatch for everything
else.

Verified against KiCad **10.0.6**, API **10.0.1**, `kicad-python` **0.7.1**,
`mcp` **2.0.0**.

## Why a venv built from KiCad's own Python

`pcbnew` is a C extension compiled against CPython 3.11's ABI *and* KiCad's
native libraries, so it only loads in KiCad's bundled interpreter. A venv
**created from** that interpreter reuses the same binary, so it can import
`pcbnew`, while `pip` adds `kipy` and the MCP SDK. One process, all three APIs,
no cross-interpreter bridge.

## Install

```bash
"C:/Program Files/KiCad/10.0/bin/python.exe" -m venv --system-site-packages .venv
```

```bash
.venv/Scripts/python.exe -m pip install -e .
```

Then check the environment:

```bash
.venv/Scripts/python.exe -m kicad_mcp doctor
```

`doctor` reports every dependency, whether the IPC server is enabled, whether
KiCad is running, and a remedy for anything that fails.

## MCP client configuration

```json
{
  "mcpServers": {
    "kicad": {
      "command": "C:\\Workspace\\mcp_kicad\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kicad_mcp"]
    }
  }
}
```

## Tools

**Environment** — `kicad_status`

**Live PCB** (needs KiCad running with the PCB editor open) — `get_board_summary`,
`list_footprints`, `get_footprint`, `list_nets`, `get_layers`, `get_stackup`,
`get_selection`, `list_tracks_vias`, `list_zones`

**Schematic** (files; works with KiCad closed) — `open_schematic`,
`list_schematic_symbols`, `find_project_files`, `schematic_netlist`, `run_erc`,
`export_bom`, `export_schematic`, `run_drc`

**Symbol libraries** — `list_symbol_libraries`, `search_symbols`, `get_symbol_pins`

**Footprint libraries** — `search_footprints`, `get_library_footprint`

**Generation** — `create_pinout_schematic`

**Editing existing schematics** — `add_symbol_to_schematic`, `label_pins`,
`set_symbol_fields`, `mark_pins_unused`, `rewire_power_symbol`,
`add_decoupling_capacitors`

**Escape hatch** — `run_kicad_script`

### Generating a schematic from a pinout

```json
{
  "output": "C:/proj/esp32.kicad_sch",
  "mcu_lib_id": "RF_Module:ESP32-S3-MINI-1",
  "assignments": {"IO4": "LED_STATUS", "GPIO_8": "SPI_MOSI", "io9": "SPI_MISO"}
}
```

Pin matching ignores case, underscores and hyphens, so `IO4`, `GPIO_4` and `io4`
all resolve to the same pin. Assigned pins get a wire and a net label; power pins
get power symbols plus `PWR_FLAG` so ERC does not report undriven rails.
Unmatched assignments are reported back rather than silently dropped. Placed
symbols take their library's default footprint unless you pass one.

To wire a symbol that is already on the sheet, `label_pins` takes the same
`assignments` map for a `reference`. It skips any pin that already has something
connected, so an existing net is never shorted to a new one.

New symbols are placed in the first free spot on the page: right of existing
parts, wrapping to a new row, never over the title block. A full sheet is
refused rather than drawn off-page; pass `x`/`y` to place explicitly. Footprints
written by `add_symbol_to_schematic` or `set_symbol_fields` are checked against
the project's `fp-lib-table`, and one that will not resolve is reported with the
same footprint under a library that does.

The output is a normal `.kicad_sch` you can open and keep editing. It is written
directly, so KiCad does not need to be running.

## Two things to know about KiCad 10

**The API only serves editors that are open.** KiCad registers API handlers per
editor window. If only the project manager is running, live PCB calls fail with
*"no handler available"*. The server translates that into a message telling you
to open the PCB editor. Open it **from the KiCad project manager**, not as a
standalone `pcbnew.exe` — the API socket is owned by one process, and a
standalone editor is a different one.

**There is no schematic IPC API in KiCad 10.** This is not a binding bug. The
`10.0` branch of `schematic_commands.proto` defines zero commands, and the full
schematic type model exists only on `master` (KiCad 11 development).
`kipy` 0.7.1 ships `master`-era Python wrappers against `10.0`-era generated
protobuf, which is why `import kipy.schematic` raises `ImportError`. Regenerating
the protos would clear the import and still return *"no handler available"* on
every call.

So schematic support here is file-based: `kicad-cli` plus direct `.kicad_sch`
parsing. That has an upside — it works with KiCad closed, which is what makes
schematic *generation* possible at all.

### Seeing schematic changes if the file is already open

Because there's no live schematic API, a change any tool here makes to a
`.kicad_sch` never reaches KiCad's in-memory copy on its own — KiCad only reads
the file from disk when told to. If you have the schematic open while a tool
edits it:

- **Use File → Revert**, not just switching windows or waiting. Revert is what
  makes KiCad re-read the file and show the change.
- **Don't hit Save first.** Saving from the GUI writes KiCad's in-memory state
  back over the file, discarding whatever the tool just wrote — the one order
  that loses work.
- If Revert isn't available or doesn't pick it up, close the schematic tab and
  reopen it from the project manager; that always re-reads from disk.

This applies in both directions: if you're editing by hand in KiCad, save from
KiCad *before* asking for another file-based change, or the tool will act on a
stale copy and your hand edits will be the ones lost.

## The escape hatch

`run_kicad_script` executes Python with `kicad`, `board`, `kipy`, `pcbnew`, and
`sexpr` pre-bound. It exists so the server covers the whole API surface without
hundreds of thin tool wrappers, which measurably degrade tool selection.

**This is arbitrary local code execution with your privileges.** That is the
deliberate trade for full API access. Run this server only against KiCad
instances and files you trust.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

Tests marked `requires_kicad` need an installation for its symbol libraries.
Live-IPC behaviour is verified manually — it needs a GUI session with a board
open, which is not worth automating on Windows.

## Scope

Windows only, by choice. `discovery.InstallResolver` is the single
platform-aware seam, so adding macOS or Linux later means adding a resolver
rather than threading `sys.platform` checks through the package.

## License

MIT
