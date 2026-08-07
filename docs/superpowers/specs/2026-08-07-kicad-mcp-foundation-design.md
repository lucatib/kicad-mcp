# KiCad MCP — Sub-project 1: Foundation, Live Read, and Escape Hatch

**Date:** 2026-08-07
**Status:** Approved for planning
**Scope:** Sub-project 1 of 5

---

## 1. Context

An MCP server exposing KiCad 10's capabilities to LLM clients. Target audience is
public open-source, so cross-platform support, tests, CI, and packaging are in
scope for the programme as a whole (sub-project 5) and must not be designed out
here.

### Verified environment facts

These were confirmed empirically on 2026-08-07 against a real install, not
assumed from documentation:

| Fact | Value |
|---|---|
| KiCad version | 10.0.5 |
| IPC API version | 10.0.1 (`10.0.1-0-g2db9e5a72b`) |
| IPC server | Enabled (`api.enable_server: true` in `kicad_common.json`) |
| `kicad-python` (`kipy`) | 0.7.1 — latest on PyPI |
| Bundled interpreter | CPython 3.11.5 at `<KICAD>/bin/python.exe` |
| SWIG `pcbnew` | Present, 1333 public symbols, reports build 10.0.5 |
| `kicad-cli` subcommands | `fp`, `jobset`, `pcb`, `sch`, `sym`, `version` |

### The single-venv finding

A virtual environment **created from KiCad's bundled interpreter** can host
`pcbnew`, `kipy`, and the MCP SDK simultaneously in one process. Verified:
Python 3.11.5 + `pcbnew` 10.0.5 + `kicad-python` 0.7.1 + `mcp` 2.0.0, with a
live IPC connection, all in a single process.

```bash
"<KICAD>/bin/python.exe" -m venv --system-site-packages .venv
.venv/bin/pip install kicad-python mcp
```

`_pcbnew.pyd` is a C extension compiled against CPython 3.11's ABI *and* KiCad's
native libraries, so it loads only in KiCad's own interpreter. A venv created
from that interpreter reuses the same binary and inherits the ability to import
it. `--system-site-packages` exposes KiCad's bundled libraries; the venv's own
`site-packages` precedes them on `sys.path`, so our pinned dependency versions
win over KiCad's.

**Consequence:** no cross-interpreter RPC bridge is needed. This deletes a
subprocess boundary, a serialization layer, and an entire class of debugging
difficulty from the original architecture.

### Two known constraints

**Handlers exist only for open editors.** With only the KiCad project manager
running, `GetOpenDocuments` fails with `no handler available for request of type
kiapi.common.commands.GetOpenDocuments`. The API serves whatever editor frame is
actually open. This is normal behaviour, not a fault, and users will hit it
constantly — error translation is a first-class requirement, not polish.

**`kipy` schematic support is broken in 0.7.1.** `import kipy.schematic` raises
`ImportError`. `schematic_types.py` imports nine enums (`BusEntryType`,
`SchematicLabelShape`, `SchematicLabelSpinStyle`, `SchematicLineType`,
`SchematicPinOrientation`, `SchematicPinShape`, `SchematicSymbolOrientation`,
`SchematicSymbolType`, `SheetSide`) that the generated
`schematic_types_pb2` in the same wheel does not define — the generated protobuf
code is out of sync with the hand-written wrappers. PCB support is unaffected.
Schematic work is deferred to sub-project 4; **sub-project 1 must not import
`kipy.schematic` anywhere**, including transitively.

---

## 2. Goals and non-goals

### Goals

1. A working MCP server that connects to running KiCad and answers questions
   about the open board.
2. A bootstrap path that a stranger on GitHub can follow without filing an
   issue.
3. An escape hatch giving complete API coverage from day one.
4. Module boundaries that sub-projects 2–5 extend without restructuring.

### Non-goals for this sub-project

- Any board mutation (sub-project 2). Read-only, except the escape hatch.
- `kicad-cli` wrapping (sub-project 3).
- Anything schematic (sub-project 4).
- PyPI publication and full CI matrix (sub-project 5).

### Explicit anti-goal

**Do not generate one tool per API method.** A large tool surface measurably
degrades model tool-selection accuracy and consumes context. Coverage of the
long tail is the escape hatch's job. New curated tools are justified by observed
repeated use, not by API surface completeness.

---

## 3. Architecture

Single process, single interpreter (KiCad's). Three backends as modules:

```
                    ┌──────────────────────────┐
     MCP client ──▶ │  server.py  tool router  │
                    └────────────┬─────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        LiveBackend        HeadlessBackend      CliBackend
        kipy over IPC      import pcbnew        kicad-cli
        (SP1)              (SP3)                (SP3)
                                 │
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
        discovery · session · units          resources · errors
```

Only `LiveBackend` is implemented in this sub-project. `HeadlessBackend` and
`CliBackend` are declared in the `Backend` protocol and reported as unavailable
by `kicad_status`, but no module is created for them until sub-project 3 —
empty stub files would be dead code carrying no design information.

### Package layout

```
src/kicad_mcp/
    __init__.py
    __main__.py          entry point
    server.py            MCP server, tool registration
    discovery.py         locate KiCad install + interpreter
    session.py           kipy connection lifecycle
    errors.py            exception -> actionable message
    units.py             nm <-> mm conversion at the boundary
    sandbox.py           escape hatch execution
    backends/
        base.py          Backend protocol
        live.py          kipy implementation
    tools/
        status.py
        board.py
        items.py
        script.py
    resources/
        __init__.py      resource registration
        api_board.md
        api_types.md
        units.md
        recipes.md
    bootstrap/
        setup.py         venv creation
        doctor.py        diagnostics
```

Distribution name **`kicad-mcp`**, import package **`kicad_mcp`**. PyPI name
availability must be confirmed before sub-project 5; a rename now is cheap and
later is not.

### Module responsibilities

**`discovery`** — find the KiCad installation and its interpreter. Returns a
`KiCadInstall` dataclass (`root`, `python_path`, `cli_path`, `version`,
`platform_kind`). Resolution order: explicit `KICAD_MCP_KICAD_ROOT` env var, then
platform search — Windows registry then `Program Files\KiCad\*`; macOS
`/Applications/KiCad/KiCad.app`; Linux native paths then Flatpak detection.
Never hardcodes a Python version; the interpreter is always derived from the
discovered install. This is the most portability-critical module and the one
most likely to generate issues, so it is also the most heavily unit-tested.

**`session`** — owns the `kipy.KiCad` client. Lazy connect, reconnect on
transport failure, and a `board()` accessor. Does not cache board state; KiCad
is the source of truth and the user may be editing concurrently.

**`errors`** — translates exceptions into messages that tell the user what to
do. Required mappings:

| Condition | Message |
|---|---|
| `no handler available` | "KiCad is running but no PCB editor is open. Open your board in the PCB editor — the API only serves editors that are currently open." |
| Connection refused | "Could not reach KiCad. Check KiCad is running and that Preferences → Plugins → 'Enable IPC API server' is on." |
| API version mismatch | Reports both versions and the supported range. |
| `pcbnew` import failure | Names the venv bootstrap command; flags Flatpak explicitly. |

**`units`** — KiCad's API works in nanometres. Every tool boundary accepts and
returns **millimetres** as floats, converting at the edge. Internal code stays in
nanometres. Angles in degrees. Coordinates use KiCad's native origin unless a
tool documents otherwise. This convention is stated once in `units.md` and
enforced in review — silent unit errors are the most likely correctness bug in
the whole project.

**`sandbox`** — see section 5.

### Backend protocol

`backends/base.py` defines the interface the router depends on, so `server.py`
never imports `kipy` directly and tests can substitute fakes.

```python
class Backend(Protocol):
    def is_available(self) -> bool: ...
    def describe(self) -> BackendStatus: ...
```

---

## 4. Tool surface

Thirteen tools. Every one returns structured JSON-serializable data with
millimetre units, and every list tool supports filtering and a result cap with
explicit truncation reporting — boards routinely have thousands of items and an
unbounded dump would blow the context window.

| Tool | Purpose |
|---|---|
| `kicad_status` | Connection state, KiCad/API versions, open documents, which backends are available. First call for any session. |
| `get_board_summary` | Board name, copper layer count, item counts by type, title block, origin. Cheap orientation call. |
| `list_footprints` | Filter by reference glob, value glob, layer, or bounding box. Returns ref, value, position, rotation, layer, library id. |
| `get_footprint` | Full detail for one footprint by reference, including pads, properties, and bounding box. |
| `list_nets` | All nets with names, codes, netclass, and item counts. Filterable by name glob. |
| `get_net_items` | Every item on a given net — tracks, vias, pads, zones. |
| `get_connected_items` | Physical connectivity from a starting item, via `get_connected_items`. |
| `list_tracks_vias` | Filter by net, layer, or bounding box. |
| `list_zones` | Zones with net, layers, priority, fill state. |
| `get_stackup` | Physical stackup — layer order, materials, thicknesses, dielectric. |
| `get_layers` | Enabled, visible, and active layers with canonical and user-facing names. |
| `get_selection` | What the user has selected in the GUI right now. Enables "fix the thing I'm looking at". |
| `run_kicad_script` | The escape hatch. |

`get_selection` is deliberately included despite being cheap to implement: it is
the primary bridge between what the user sees and what the model acts on, and it
makes sub-project 2's write tools far more usable.

---

## 5. The escape hatch

`run_kicad_script` executes Python in-process with the KiCad API pre-imported.
It exists so the server has complete API coverage on day one, and so curated
tools can be added based on observed demand rather than speculation.

**Contract**

- Pre-bound globals: `kicad` (the `KiCad` client), `board` (the open board, or
  `None`), `kipy`, and `pcbnew` when importable. `pcbnew` is bound by direct
  import, independent of `HeadlessBackend` — the escape hatch reaches the SWIG
  API in this sub-project even though no curated tool wraps it until SP3.
- Returns captured `stdout`, plus `repr()` of a `result` variable if the script
  assigns one.
- Default timeout 30s, configurable per call.
- Exceptions return the traceback as a normal tool result, not a protocol error —
  the model needs to read the traceback to correct itself.
- Read-only by convention in this sub-project. It is *capable* of mutation; that
  capability is documented plainly rather than falsely restricted.

**Security posture, stated plainly.** This is arbitrary local code execution with
the user's privileges. That is the accepted trade — chosen deliberately in design
— and it is what "any functionality available" requires. It must be documented
prominently in the README rather than buried, and sub-project 5 should evaluate
an opt-out flag for users who want the curated surface only.

**Reliability comes from the resources, not the tool.** A script escape hatch is
only as good as the model's knowledge of the API it targets. The MCP resources in
section 6 are therefore a first-class deliverable of this sub-project, not
documentation to be written afterwards.

---

## 6. Resources

Served as MCP resources so clients can pull them on demand rather than paying for
them in every system prompt.

| URI | Content |
|---|---|
| `kicad://api/board` | Condensed `Board` API — all ~55 methods, signatures, one-line descriptions. |
| `kicad://api/types` | `board_types` item classes and their key attributes. |
| `kicad://api/units` | Unit conventions, geometry primitives, coordinate system. |
| `kicad://api/recipes` | Working code for common tasks: iterate footprints, trace a net, query zones, read stackup. |
| `kicad://status` | Live environment status, same payload as `kicad_status`. |

The API reference files are **generated from introspection of the installed
`kipy`**, not hand-written, so they cannot drift from the version actually in
use. A build script regenerates them; the generated output is committed so the
package works without a KiCad install present.

---

## 7. Bootstrap

Two commands, both runnable before the server ever starts.

**`python -m kicad_mcp.bootstrap.setup`** — discovers KiCad, creates the venv
from its interpreter with `--system-site-packages`, installs dependencies, and
prints the exact MCP client configuration block to paste.

**`python -m kicad_mcp.bootstrap.doctor`** — diagnoses without changing
anything. Checks: KiCad found and version; interpreter located; `pcbnew`
importable; `kipy` importable and version; `api.enable_server` setting; whether
KiCad is running; whether a PCB editor is open; live ping. Each check reports
pass/fail **and the remedy on failure**.

For a public project this is the difference between working software and a queue
of `ImportError: _pcbnew` issues. `doctor` output is also the issue-report
template.

---

## 8. Error handling

Three failure classes, each with a defined behaviour:

1. **Environment failures** (KiCad missing, `pcbnew` unimportable) — detected at
   startup, reported through `kicad_status`, and never crash the server. The
   server starts degraded and says so.
2. **Connection failures** (KiCad not running, no editor open) — detected per
   call, returned as actionable tool results, never raised as protocol errors.
   The model can act on a clear message; it cannot act on a stack trace.
3. **Script failures** — traceback returned as tool output.

The server must start successfully with KiCad absent. A server that refuses to
start cannot tell the user why it refused to start.

---

## 9. Testing

**Unit** (no KiCad, runs everywhere): `discovery` against faked filesystem
layouts for all four platform kinds; `units` conversions including edge cases;
`errors` mapping table; tool logic against a fake backend. This tier must pass on
a machine with no KiCad installed — it is what CI runs on every push.

**Integration, headless** (`requires_kicad`): a small `.kicad_pcb` fixture
committed to the repo, exercised through `pcbnew`. Runnable in CI on Linux with
KiCad installed from packages.

**Integration, live** (`requires_running_kicad`): the full tool surface against a
running instance. Needs a GUI, so it runs under `xvfb` in CI if that proves
reliable, and is marked local-only if it does not. This is honestly the hardest
tier to automate and should not block sub-project 1 — the decision is deferred to
sub-project 5 with a documented manual checklist in the interim.

Sequencing follows test-driven development: the fake backend and unit tier are
built before the tools they test.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| `kipy` 0.7.1 schematic module is broken | Out of scope here; never import it. File upstream issue. Sub-project 4 vendors regenerated protos from KiCad source. |
| Live tests need a GUI | Deferred to SP5; manual checklist meanwhile. |
| KiCad 11 changes the bundled Python | Never hardcode the interpreter; always derive from `discovery`. |
| Flatpak/Snap cannot import `pcbnew` | Documented limitation. `kipy` over IPC still works; headless degrades to `kicad-cli`. `doctor` detects and explains it. |
| API version drift | `session` checks `get_api_version()` against a supported range and warns rather than failing hard. |
| Unit confusion (nm vs mm) | Single documented convention, conversion only at tool boundaries, explicit unit tests. |
| Escape hatch is arbitrary code execution | Accepted and documented prominently; opt-out evaluated in SP5. |

---

## 11. Success criteria

Sub-project 1 is done when all of the following hold:

1. `doctor` correctly diagnoses a working install and a broken one.
2. `setup` produces a venv that imports `pcbnew`, `kipy`, and `mcp` together.
3. All thirteen tools work against a real board in running KiCad.
4. `run_kicad_script` executes a non-trivial multi-step query correctly.
5. Closing the PCB editor produces the actionable message, not a stack trace.
6. The server starts and reports degraded status with KiCad absent.
7. The unit tier passes on a machine with no KiCad installed.
8. A stranger can go from clone to working server using only the README.

---

## 12. Open items

- Confirm `kicad-mcp` is available on PyPI before sub-project 5.
- File the upstream `kipy` schematic issue; a fix upstream would simplify SP4.
- Decide whether `kicad_status` and the `kicad://status` resource share one
  implementation — they should, but the MCP SDK's resource and tool signatures
  differ enough to confirm during implementation.
