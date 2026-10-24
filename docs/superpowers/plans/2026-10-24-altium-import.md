# import_altium Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `import_altium` MCP tool that turns loose Altium `.SchDoc`/`.PcbDoc` files into a KiCad project whose schematic and board are linked by path, with Altium's designators untouched and every disagreement reported.

**Architecture:**
- **Package.** A new `kicad_mcp/altium/` package with these modules, one per step:
  - `records` reads the OLE or ASCII record streams.
  - `model` turns records into typed sheet objects.
  - `pcbdoc` reads board components.
  - `design` builds the instance tree, UUIDs and references.
  - `symbols` and `sheets` write the `.kicad_sch` files through `sexpr`.
  - `board` rewrites the `kicad-cli`-imported board.
  - `verify` compares nets and parity.
  - `importer` orchestrates.
- **Tool.** `server.import_altium` wraps `importer.import_altium` with `@tool_result`.

**Tech Stack:**
- Python ≥3.10 (KiCad's bundled 3.11)
- `olefile` (new dependency)
- the repo's `sexpr`, `cli` and `errors`
- `kicad-cli` 10 for board import, netlist, ERC and DRC parity
- pytest

**Spec:** `docs/superpowers/specs/2026-10-24-altium-import-design.md`

## Global Constraints

- **UUID mapping.** `KICAD_ALTIUM_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")`, and `altium_kiid(id) = str(uuid.uuid5(KICAD_ALTIUM_NS, id.lstrip("\\")))`. Never change these.
- **Flat sheets.** A synthesized flat-design sheet uses `uuid5(KICAD_ALTIUM_NS, "flat-sheet:" + stem)`.
- **Root sheet.** Its own UUID is `uuid5(KICAD_ALTIUM_NS, "root:" + project_name)`.
- **Units.**
  - `mm = (v + frac / 100000) * 0.254` for `KEY`/`KEY_FRAC`.
  - `mm = (v * 10 + frac / 100000) * 0.254` for `KEY`/`KEY_FRAC1`.
  - Altium Y is up, KiCad Y is down: `y_kicad = flip_mm - y_altium_mm`.
  - `flip_mm` is the sheet height rounded **up** to a multiple of 2.54 mm, so 100-mil-grid pins stay on KiCad's grid.
- **Record keys.** Matched case-insensitively (old files write `LOCATION.X`, new ones `Location.X`). A `%UTF8%KEY` value wins over `KEY`.
- **Designators.** Never renamed. Duplicates are reported only.
- **Inputs.** Altium inputs are opened read-only. Output goes only to `output_dir`, which must be empty or absent unless `overwrite=True`.
- **Errors.** Returned as data via `KicadMcpError` subclasses (`ToolInputError` with a `remedy`), never raised through the tool.
- **CI.** Unit-tier tests must pass with no KiCad and no network. KiCad tests carry `@pytest.mark.requires_kicad`. Downloaded fixtures skip when offline.
- **Licensing.** No code copied from KiCad's GPL importer.
- **Version.** Bump to `0.2.0` in `pyproject.toml` and in `server.py`'s `MCPServer(version=…)`.

## Review Focus

1. **Accented text.** Accented or non-Latin text in notes and parameters (`%UTF8%` keys, latin-1 fallback) must come out exactly. Pinned by `test_utf8_key_wins` in Task 1.
2. **Odd file names.** A `.SchDoc` whose file name differs in case from the sheet symbol's `FILE_NAME` (`Sampler.schdoc` vs `SAMPLER.SCHDOC`) must still resolve. Pinned by `test_child_file_matched_case_insensitively` in Task 4.
3. **Rotated or mirrored parts.** Pins must land where Altium drew them and stay connected to their wires. Pinned by `test_rotated_component_pins_hit_wire_ends` in Task 5.
4. **Re-running into the same folder.** Without `overwrite`, it is refused and nothing is written. Pinned by `test_existing_output_refused` in Task 9.
5. **A board-only or schematics-only import.** It must not crash on empty sides. Pinned by `test_board_only` and `test_schematics_only` in Task 9.

---

### Task 1: Record reader

**Files:**
- Create: `src/kicad_mcp/altium/__init__.py` (empty docstring module)
- Create: `src/kicad_mcp/altium/records.py`
- Modify: `pyproject.toml` (add `"olefile>=0.47"` to dependencies)
- Test: `tests/test_altium_records.py`

**Interfaces:**
- Produces:
  - `class Record(dict)`: upper-cased keys plus `.index: int` (position after the header) and `.type: int` (`RECORD`, or -1). Methods:
    - `text(key, default="")` (honours `%UTF8%`)
    - `int(key, default=0)`
    - `bool(key)` (`T`/`TRUE`)
    - `mm(key)` (`KEY` + `KEY_FRAC`)
    - `mm1(key)` (`KEY` + `KEY_FRAC1`)
    - `owner -> int | None` (`OWNERINDEX`)
  - `parse_record_stream(data: bytes) -> tuple[list[Record], int]` (framed OLE stream) and `parse_ascii_records(text: str) -> list[Record]`. Both return the header as element 0.
  - `read_schdoc_records(path) -> tuple[Record, list[Record], int]`: the header record, the records, and the count of binary records skipped. OLE files are read from stream `FileHeader`; ASCII files have one `|…` record per line.
  - `read_storage_images(path) -> list[bytes]`: embedded images from stream `Storage`, an empty list for ASCII files.
  - `read_pcbdoc_components(path) -> list[Record]`: stream `Components6/Data`, framed by a 4-byte length.
  - `class AltiumFormatError(ToolInputError)`

Framing in `FileHeader`: a 3-byte little-endian length plus 1 flag byte, then the payload. Flag 0 means `|K=V|…` latin-1 text, NUL-terminated. Any other flag is a binary record.

- [ ] **Step 1: Write failing tests** against `parse_record_stream`/`parse_ascii_records` with bytes built in the test (no OLE writer needed). The OLE path is covered by the fixture-based tests in Task 2. Cover these tests:
  - `test_frames_text_records`: two records, indices 0 and 1.
  - `test_binary_record_counted`: flag 1 is skipped and counted.
  - `test_keys_case_insensitive`: `Location.X=5` and `LOCATION.X` read the same.
  - `test_frac_units`: `X=364, X_FRAC=1576` gives `364.01576*0.254`; `DistanceFromTop=3, _Frac1=937008` gives `10.0` mm ±1e-6.
  - `test_utf8_key_wins`: `TEXT=caf?` and `%UTF8%TEXT=café` give `café`.
  - `test_ascii_schdoc`: a 3-line ASCII file parses like OLE.
  - `test_not_altium_file`: a `.SchDoc` holding PNG bytes raises `AltiumFormatError`.
- [ ] **Step 2:** `pytest tests/test_altium_records.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `records.py`.
- [ ] **Step 4:** `pytest tests/test_altium_records.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `Add Altium record reader for SchDoc and PcbDoc`.

### Task 2: Sheet model

**Files:**
- Create: `src/kicad_mcp/altium/model.py`
- Test: `tests/test_altium_model.py`

**Interfaces:**
- Consumes: Task 1 `Record`, `read_schdoc_records`.
- Produces dataclasses. All coordinates are in Altium mm with Y up. The `design`/`sheets` modules flip them.
  - `Font(size_pt: float, bold: bool, italic: bool, rotation: int, name: str)`
  - `Pin(name, number, electrical: int, orientation: int, x, y, length, hidden: bool, show_name: bool, show_number: bool, part: int, mode: int)`
  - `Graphic(kind: str, rec: Record, part: int, mode: int)`. `kind` is one of `line`, `polyline`, `polygon`, `rect`, `roundrect`, `arc`, `ellipse`, `ellipticalarc`, `bezier`, `label`, `textframe`, `image`.
  - `Param(name, text, x, y, hidden: bool, font_id: int, orientation: int, justification: int)`
  - `Part(uid: str, designator: str, libref: str, part_id: int, part_count: int, mode_count: int, x, y, orientation: int, mirrored: bool, pins: list[Pin], graphics: list[Graphic], params: dict[str, Param], designator_param: Param | None, footprints: list[str])`. `footprints` holds the PCBLIB `MODELNAME`s; the current one comes first.
  - `SheetEntry(uid, name, side: int, distance_mm, io_type: int)`
  - `SheetSymbol(uid, x, y, width, height, name: str, file_name: str, entries: list[SheetEntry], name_param: Record, file_param: Record)`
  - `Sheet(path: Path, stem: str, fonts: dict[int, Font], width_mm, height_mm, title: dict[str, str], parts: list[Part], wires: list[list[tuple]], buses: list[list[tuple]], bus_entries: list[tuple], junctions: list[tuple], net_labels: list[Record], power_ports: list[Record], ports: list[Record], no_ercs: list[tuple], sheet_symbols: list[SheetSymbol], graphics: list[Graphic], images: list[bytes], unsupported: Counter[str], binary_records: int)`
  - `load_sheet(path: Path) -> Sheet`

Rules:
- **Ownership.** `OWNERINDEX` points at a component (record 1) or a sheet symbol (15). Children with `OWNERPARTID` not in (-1, the part's `CURRENTPARTID`) belong to other parts of a multi-part library symbol. Altium stores only the placed part's graphics, so keep `part = OWNERPARTID`.
- **Display mode.** Children with `OWNERPARTDISPLAYMODE` ≠ the part's `DISPLAYMODE` are skipped.
- **Template graphics.** Graphics owned by record 39 (template) go to `Sheet.graphics`.
- **Sheet size.** Read from record 31.
  - **Custom size:** `USECUSTOMSHEET` → `CUSTOMX`/`CUSTOMY` via `mm()`.
  - **Standard size:** otherwise `SHEETSTYLE`, through the table `{0:(1150,760),1:(1550,1110),2:(2230,1570),3:(3150,2230),4:(4460,3150),5:(950,750),6:(1500,950),7:(2000,1500),8:(3200,2000),9:(4200,3200),10:(1100,850),11:(1400,850),12:(1700,1100),13:(990,790),14:(1540,990),15:(2060,1540),16:(3260,2060),17:(4280,3260)}` in 10-mil units.
- **Title.** Sheet-level parameters (record 41 with no owner): `Title`, `Revision`, `Date`, `DocumentNumber`, `CompanyName`, `SheetNumber`, `SheetTotal`. A value of `*` means unset.
- **Unsupported.** Kinds counted by name: `IEEE_SYMBOL`(3), `PIECHART`(9), `COMPILE_MASK`(211), `HARNESS_*`(215-218), `BLANKET`(225), `HYPERLINK`(226), `PARAMETER_SET`(43, directives).

- [ ] **Step 1: Write failing tests**, against the downloaded fixture `altium_fixture("eDP/power.SchDoc")` (Task 3 provides `tests/altium_fixtures.py`; do Task 3's fixture step first if executing out of order):
  - `test_power_sheet_parts`: 30 parts; `C13P` has uid `RKDRJMCU`, libref `Cap Semi`, footprint `CAPC2012N`-like non-empty.
  - `test_pins_attach_to_owner`: `C13P` has 2 pins, numbers `{"1","2"}`.
  - `test_multipart_separate_records`: in `it6251_core`, `U10C` appears as two `Part`s with part_id 1 and 2 and uids `PLEULKAG`, `GRKVCHXY`.
  - `test_sheet_symbols_and_entries`: Sampler root has 12 sheet symbols, 8 with file `Input_channel.SchDoc`; `UMAXLLEH` has 31 entries.
  - `test_unsupported_counted`: Sampler root counts `PARAMETER_SET`; nothing is lost silently, since the counts per kind add up.
  - `test_ascii_ticket1303_loads`: `overview.SchDoc` loads as ASCII with 1 sheet symbol.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement `model.py`.
- [ ] **Step 4:** Run; expected PASS.
- [ ] **Step 5:** Commit: `Model Altium schematic sheets: parts, pins, wiring, sheets`.

### Task 3: Test fixtures and PcbDoc components

**Files:**
- Create: `tests/altium_fixtures.py`
- Create: `src/kicad_mcp/altium/pcbdoc.py`
- Modify: `.gitignore` (add `tests/.altium_cache/`)
- Test: `tests/test_altium_pcbdoc.py`

**Interfaces:**
- Produces:
  - `altium_fixture(name: str) -> Path` (pytest helper): downloads `FIXTURES[name] = (url, sha256)` into `tests/.altium_cache/`, verifies the hash, and calls `pytest.skip("offline")` when it cannot fetch. Names:
    - `eDP/eDP_adapter_dvt1.PcbDoc`, `eDP/it6251_core.SchDoc`, `eDP/power.SchDoc`, `eDP/eDP_adapter_dvt1.PrjPcb` (from `gitlab.com/kicad/code/kicad/-/raw/10.0/qa/data/pcbnew/plugins/altium/eDP_adapter_dvt1_source/…`)
    - `qa/issue16903/*`, `qa/issue24861/*`, `qa/ticket1303/*` (eeschema qa)
    - `sampler/*` (`raw.githubusercontent.com/sinara-hw/Sampler/<pinned commit sha>/PCB/…`; resolve the commit sha once with `gh api repos/sinara-hw/Sampler/commits/HEAD -q .sha` and pin it).

    Compute every sha256 once while writing the task and hardcode it.
  - `pcbdoc.py`:
    - `BoardPart(source_designator, id_path: tuple[str, ...], sheet_path: str, x_mm, y_mm, rotation, pattern, footprint_library)`. `id_path` holds `SOURCEUNIQUEID` split on `\`, empties dropped.
    - `read_board_parts(path) -> list[BoardPart]`. Position: `X`/`Y` are in 1/10000 mil, so `mm = v * 0.0254 / 10000`; Y stays Altium-up here.

- [ ] **Step 1:** Write the failing tests:
  - `test_edp_board_parts`: 112 rows have a non-empty id path; C10P's `id_path == ("GHHNYTHT",)`, `sheet_path == "power"`.
  - `test_sampler_channel_paths` (download, marked `slow`): 8 rows with `source_designator == "C10"`, all with `id_path[-1] == "JSBUTAQD"`, and 8 distinct `id_path[0]`.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement. Register the `slow` marker in `pyproject.toml`.
- [ ] **Step 4:** Run; expected PASS.
- [ ] **Step 5:** Commit: `Read Altium board components; fetch Altium test fixtures once`.

### Task 4: Design: hierarchy, UUIDs, references

**Files:**
- Create: `src/kicad_mcp/altium/design.py`
- Test: `tests/test_altium_design.py`

**Interfaces:**
- Consumes: `model.Sheet`, `pcbdoc.BoardPart`.
- Produces:
  - `KICAD_ALTIUM_NS`, `altium_kiid(id: str) -> str`
  - `SheetNode(sheet: Sheet | None, file_name: str, sheet_symbol: SheetSymbol | None, kicad_uuid: str, children: list[SheetNode], id_path: tuple[str, ...], kicad_path: str)`
    - `kicad_path` is the instance path used in `(instances (path …))`: `"/" + root_uuid + "/" + "/".join(child sheet uuids)`. The root's is `"/" + root_uuid`.
    - `id_path` holds the Altium sheet symbol ids from the root.
    - `sheet` is None for a missing file.
  - `Design(project_name, root: SheetNode, sheets: dict[str, Sheet], mode: str, flat: bool, root_uuid: str, issues: list[dict], inferred: list[dict], unreferenced: list[str])`. `mode` is `hierarchical`, `flat` or `global`.
  - `build_design(sheets: list[Sheet], project_name: str, board_parts: list[BoardPart] | None, prjpcb: Path | None) -> Design`
  - `instance_references(design, board_parts, board_refs: dict[tuple[str, ...], str]) -> dict[tuple[str, str], str]`. It maps `(node.kicad_path, part.uid)` to the reference.
    - `board_refs` maps a full Altium id path (sheet ids + part id) to the KiCad board reference, from Task 7's join.
    - Fallback: the part's designator. In a repeated sheet with no board ref, add Altium's channel suffix `A, B, …` in sheet-symbol order (`Location` y descending, then x), and record it in `design.inferred`.
  - `footprint_kicad_path(design, id_path: tuple[str, ...]) -> str | None`. Gives `"/" + "/".join(altium_kiid(s) for s in sheet_ids) + "/" + altium_kiid(part_id)` for hierarchical designs. For a flat design (single-segment ids) it uses the flat sheet uuid of the part's sheet, looked up by `uid` across sheets. It is None if no sheet holds that part id.

Rules:
- **Hierarchy.** Files are matched case-insensitively by name.
- **Root.** It is per spec 6.3.2: the board's first path segment names it; a `.PrjPcb`'s `DocumentPath=` order is the fallback; otherwise an error names the candidates.
- **Cycles.** A cycle raises `ToolInputError`.
- **Mode.** It is chosen as in spec 6.5. A `.PrjPcb` `HierarchyMode=` maps `0` flat, `1` global, `2` hierarchical, and `3` (automatic) or absent applies the automatic rule.

- [ ] **Step 1: Write failing tests:**
  - `test_uuid_matches_kicad`: `altium_kiid("GHHNYTHT") == "0a04476f-831d-59cf-a134-9523a5894990"`, and `altium_kiid("\\IMJRJHOQ") == "cb8c9dff-72b7-5ec6-8c1f-0963372f7ffe"`.
  - `test_flat_design_synthesizes_root`: eDP's two sheets give `flat=True`, 2 children ordered `it6251_core`, `power` (from board order), and an `inferred` entry.
  - `test_hierarchy_sampler`: the root is `Sampler`; there are 12 children; 8 share `Input_channel`; child uuid `== altium_kiid("GBVAIBAE")` for that sheet symbol.
  - `test_root_from_board_when_extra_sheet`: adding an unrelated sheet to Sampler's list keeps the root and lists it in `unreferenced`.
  - `test_missing_child_reported`: dropping `LVDS_IFCB.SchDoc` gives a node with `sheet is None` and an issue `{"kind":"missing_sheet","file":"LVDS_IFCB.SchDoc"}`.
  - `test_child_file_matched_case_insensitively`
  - `test_channel_suffix_inferred_without_board`: Sampler with no board gives the `Input_channel` C10 instances `C10A`…`C10H`, and an inferred entry.
  - `test_mode_automatic`: Sampler → `hierarchical`; ticket1303 → per its records.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit: `Build the Altium sheet hierarchy and KiCad paths from UniqueIds`.

### Task 5: Symbols

**Files:**
- Create: `src/kicad_mcp/altium/symbols.py`
- Test: `tests/test_altium_symbols.py`

**Interfaces:**
- Consumes: `model.Part`, `model.Font`, a `to_kicad(x, y) -> (x, y)` callable (flip) passed in.
- Produces:
  - `lib_symbol(name: str, units: list[Part], fonts) -> sexpr.SExpr`. One `(symbol "altium:<name>" …)`, with one `"<name>_<unit>_1"` sub-symbol per placed part, holding that part's pins and graphics.
  - `symbol_key(units) -> str`. A stable hash of the graphics and pins relative to the anchor; identical parts share one lib symbol.
  - `placed_symbol(lib_id: str, part: Part, unit: int, uuid: str, instances: list[tuple[str, str, str]], fields: dict[str, tuple[str, float, float, bool]], fonts, to_kicad) -> sexpr.SExpr`. `instances` holds `(project_name, kicad_path, reference)`.

Rules:
- **Baked placement.** Placement transforms are baked: lib graphics are the part's sheet graphics, translated so the anchor is (0,0), converted to KiCad Y-down. The instance sits at the flipped anchor with rotation 0 and no mirror. This keeps every pin exactly where Altium drew it. *[builder call: avoids a rotation/mirror convention mismatch]*
- **Pin position.** A KiCad pin `(at)` is Altium's pin location plus `length` along its orientation: 0 right, 1 up, 2 left, 3 down, in Altium Y-up. The KiCad angle points back toward the body: Altium 0 → 180, 1 → 90 (KiCad Y-down: up = 90), 2 → 0, 3 → 270.
- **Pin type.** Altium electrical → KiCad: `0 input, 1 bidirectional, 2 output, 3 open_collector, 4 passive, 5 tri_state, 6 open_emitter, 7 power_in`.
- **Pin visibility.** A hidden pin gets `(hide yes)`. `show_name`/`show_number` false becomes `(pin_names (hide yes))`/`(pin_numbers (hide yes))` at symbol level only when false for every pin. Otherwise the per-pin text is emptied.
- **Graphics.**
  - Lines and polylines become `polyline`; polygons become filled `polyline`; rectangles become `rectangle`.
  - Rounded rectangles become `rectangle`, with the radius in `approximations`.
  - Arcs and elliptical arcs become `arc` via start/mid/end; full circles become `circle`; ellipses with rx≠ry become a 32-segment `polyline`.
  - Beziers become `bezier`.
  - Line width is `LINEWIDTH * 0.254` mm.
  - `ISSOLID`/`TRANSPARENT` set the fill: `background` when solid, `none` when transparent.
- **Fonts.** A text size in mm is `size_pt * 0.3528 * 0.72`. That is cap-height ≈ 72% of the em; tune by comparing the eDP PDF in Task 8's visual check. Record the factor as a module constant `PT_TO_MM`.
- **Fields.** The designator becomes `Reference`, Comment becomes `Value`, `Footprint` comes from Task 7, the Altium footprint goes to `Altium_Footprint` (hidden), and other parameters become fields, hidden unless visible in Altium.

- [ ] **Step 1: Write failing tests** (pure, no KiCad):
  - `test_pin_endpoint_and_angle`: for each orientation, the endpoint and angle match the rule above.
  - `test_identical_parts_share_symbol`: two `Cap Semi` parts at different positions give an equal `symbol_key`.
  - `test_rotated_component_pins_hit_wire_ends`: in eDP `power`, every pin endpoint of every part (after flip) coincides within 1e-6 mm with a wire vertex, a junction, a power port, a label, or another pin, or else carries no net on the board. Pin it on the count of pins touching wire vertices: ≥ 95 % of the pins with a board net.
  - `test_multi_unit_symbol`: `U10C` gives one lib symbol with sub-symbols `_1_1` and `_2_1`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit: `Rebuild Altium parts as KiCad symbols from their placed graphics`.

### Task 6: Sheets

**Files:**
- Create: `src/kicad_mcp/altium/sheets.py`
- Test: `tests/test_altium_sheets.py`

**Interfaces:**
- Consumes: `design.Design`/`SheetNode`, `symbols.*`, `cli.schematic_format_version`.
- Produces:
  - `write_sheets(design, out_dir: Path, refs, footprint_fields, version: int) -> dict`. It writes `<project>.kicad_sch` (root) plus one file per distinct sheet stem, and returns `{"files": [...], "approximations": [...], "not_converted": [...]}`.
  - `power_symbol(net: str, style: int) -> sexpr.SExpr`. A `power:<net>` lib symbol drawn per style: 0 circle, 1 arrow, 2 bar, 3 wave, 4 power ground, 5 signal ground, 6 earth, 7-10 GOST. Unknown styles draw as a bar. The `Value` is the net name.

Mapping is spec 6.5. Specifics:
- **Ports.** In `hierarchical` mode, ports on child sheets become `hierarchical_label` and ports on the root become `global_label`. In `flat`/`global` mode, ports become `global_label`. In `global` mode, net labels become `global_label`; otherwise they become `label`.
  - Label shape from `IOTYPE`: `0 passive, 1 output, 2 input, 3 bidirectional`.
  - The connection point is on the port end that wires touch: the port's location, or location + width along its alignment. Choose the end that coincides with a wire vertex; else the location.
- **Sheet pins.** Side from `SIDE` (0 left, 1 right, 2 top, 3 bottom); position `distance_mm` from the top/left edge via `mm1("DISTANCEFROMTOP")`.
- **Text.** Text frames and notes become `text_box` with the same rectangle, wrapping and justification. Labels (record 4) become `text`. `~1` in Altium text is a newline.
- **Page.** `(paper "User" w h)` with Altium's sheet size. Title block from `Sheet.title`.
- **Images.** A linked image whose file is missing goes to `not_converted` as `{"record":"IMAGE","file":…}`. An embedded one from `Storage` becomes `(image (at) (scale) (data …))` base64.
- **Root of a flat design.** It holds only sheet symbols laid out in a row.

- [ ] **Step 1: Write failing tests** (`requires_kicad`, since they load the output with `kicad-cli`):
  - `test_edp_sheets_load`: `kicad-cli sch export netlist` on the written root exits 0.
  - `test_notes_survive`: every Altium text/textframe/note string of Sampler root (`~1` → `\n`) appears verbatim in the written file.
  - `test_ports_become_hierarchical_labels`: in Sampler `Input_channel.kicad_sch`, the hierarchical label names equal the root sheet's pin names for `TJKXBNXR`.
  - `test_power_ports_are_power_symbols`: eDP `power.kicad_sch` has `power:GND` symbols, and the netlist has a `GND` net.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit: `Write Altium sheets as KiCad schematics: wiring, ports, notes, pages`.

### Task 7: Board rewrite

**Files:**
- Create: `src/kicad_mcp/altium/board.py`
- Test: `tests/test_altium_board.py`

**Interfaces:**
- Consumes: `pcbdoc.BoardPart`, `design.footprint_kicad_path`, `altium_kiid`.
- Produces:
  - `import_board(install, pcbdoc: Path, out_pcb: Path, timeout: int = 600) -> dict`. It runs `kicad-cli pcb import --format altium --report-format json --report-file <tmp>` and returns that report.
  - `join_footprints(board_text: str, parts: list[BoardPart]) -> list[tuple[int, BoardPart | None, str]]`. It returns `(footprint index, part, reference)`. The match is the last path segment `uuid5` equal to `altium_kiid(part.id_path[-1])` **and** a position within 0.01 mm of `(x_mm, -y_mm)`, with rotation ignored.
  - `rewrite_board(board_text, joins, design) -> tuple[str, dict[tuple[str, ...], str], list[dict]]`. It returns the new text, `board_refs` (full id path → reference), and the issues. Each matched footprint gets `(path …)`, `(sheetname …)` and `(sheetfile …)` from the design; an unmatched one is left as is and reported `board_only`.
  - `footprint_fields(board_text) -> dict[str, str]`: reference → footprint lib id.
  - `rename_nets(board_text, renames: dict[str, str]) -> str`. It renames `(net N "old")` declarations, every pad/track/zone `(net N "old")` or `(net_name …)`, and net-class assignments, as whole tokens only.
  - `board_pad_nets(board_text) -> dict[str, set[tuple[str, str]]]`: net name → {(ref, pad)}, skipping unnamed and `""` nets.

  Use `sexpr.parse` for reading. For writing, regenerate with `sexpr.dumps`, but only if a round-trip of an untouched imported board is byte-stable under `kicad-cli pcb upgrade`. Otherwise do targeted text substitution as the spike did. Check that first (Step 1). *[builder call]*

- [ ] **Step 1:** Test `test_roundtrip_choice` (`requires_kicad`): run `import_board` on eDP, then `sexpr.dumps(sexpr.parse(text))`. `kicad-cli pcb drc` on the result loads; record which writing strategy is used.
- [ ] **Step 2: Failing tests:**
  - `test_join_edp`: 112 joined, every C*/R*/U* matched.
  - `test_paths_rewritten_flat`: C10P path `== "/" + flat_uuid("power") + "/" + altium_kiid("GHHNYTHT")`.
  - `test_join_sampler_channels` (slow): 280 channel footprints are joined to 8 distinct paths per part.
  - `test_rename_nets_whole_token`: renaming `SDA` leaves `SDA2` alone.
- [ ] **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit: `Rewrite imported Altium boards to link footprints by Altium id`.

### Task 8: Verify

**Files:**
- Create: `src/kicad_mcp/altium/verify.py`
- Test: `tests/test_altium_verify.py`

**Interfaces:**
- Produces:
  - `compare_nets(board: dict[str, set], schematic: dict[str, set], exclude_refs: set[str]) -> dict`. It returns `{"matched": {board_net: sch_net}, "topology_differences": [...], "excluded_parts": sorted(exclude_refs)}`. Two nets match when their pad sets (minus excluded refs) are equal. A topology difference lists each board net whose set has no equal schematic net, with the closest schematic net (largest overlap) and the pads only on each side, capped at 20 pads per entry.
  - `schematic_pin_nets(install, root_sch: Path) -> dict[str, set]`, from `cli.export_netlist` + `cli.parse_kicadxml_netlist`.
  - `parity(install, pcb: Path, known_refs: set[str]) -> dict`. It runs `kicad-cli pcb drc --schematic-parity --format json --severity-all` and returns `{"missing":n,"extra":n,"duplicates":n,"other":{type:n},"unexplained":[…]}`.
    - Counting: `missing` counts `missing_footprint`, `extra` counts `extra_footprint`, `duplicates` counts `duplicate_footprints`.
    - `unexplained` holds entries whose items name no ref in `known_refs`. The ref is parsed from item descriptions such as `Footprint C10C` and `Pad 1 [...] of C10C`.

- [ ] **Step 1: Failing tests** (pure):
  - `test_equal_partitions_match_despite_names`
  - `test_split_net_is_topology_difference`
  - `test_excluded_refs_removed_first`
  - `test_parity_parses_refs` (feed a captured snippet of the spike's drc.json: 3 entries).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit: `Compare Altium board and converted schematic nets; parse parity`.

### Task 9: Importer, tool, docs, release

**Files:**
- Create: `src/kicad_mcp/altium/importer.py`
- Modify: `src/kicad_mcp/server.py` (tool + version), `pyproject.toml` (version `0.2.0`), `README.md` (tools list + an "Importing from Altium" section)
- Test: `tests/test_altium_import.py`

**Interfaces:**
- Produces:
  - `import_altium(install, sources: list[str] | str, output_dir: str | None = None, project_name: str | None = None, overwrite: bool = False) -> dict`. It returns the spec 6.4 summary and writes `import_report.json`.
  - `server.import_altium(sources, output_dir=None, project_name=None, overwrite=False)`. Its docstring says it works with KiCad closed, that loose files are fine, that designators are never changed, and where the report goes.

Order:
1. Gather the sources: refuse more than one `.PcbDoc` (remedy: name the board), refuse no files at all, refuse a non-empty `output_dir` unless `overwrite`.
2. Load the sheets and the board parts.
3. `build_design`.
4. Import and join the board.
5. Compute the references.
6. Detect the source disagreements: `board_only`, `schematic_only`, `moved_sheet` (same reference, different sheet and different id), `value_differs`, `footprint_differs`, and `duplicate_reference`.
7. Write the sheets.
8. Write `.kicad_pro`: `{"meta":{"filename":…,"version":3}}` plus `"sheets": [[uuid, name], …]` for KiCad's sheet list.
9. Compare nets, rename board nets, write the board.
10. Run parity and ERC (`cli.erc_violation_count`).
11. Compute `aligned`. Write the report. Return the capped summary.

- [ ] **Step 1: Failing tests:**
  - `test_existing_output_refused` (unit; a fake board is not needed): a non-empty dir gives an `error` with a `remedy`, and the dir is unchanged.
  - `test_two_boards_refused` (unit)
  - `test_edp_end_to_end` (`requires_kicad`):
    - `parity.missing == extra == duplicates == 0`;
    - `source_disagreements` contains `moved_sheet` for `C22P`;
    - every board reference equals its schematic instance reference;
    - the inputs' sha256 are unchanged;
    - `aligned` equals `(net_check.topology_differences == [] and parity.unexplained == [])`.
  - `test_sampler_end_to_end` (`requires_kicad`, slow):
    - `Input_channel.kicad_sch` exists once;
    - the netlist has refs `C10A`…`C10H`;
    - parity has 0 missing/extra/duplicates.
  - `test_schematics_only` (`requires_kicad`): issue16903 imports; `counts.footprints == 0`; `not_converted` lists the harness records.
  - `test_board_only` (`requires_kicad`): the eDP board alone imports; every footprint is `board_only`; no exception.
  - `test_server_tool_registered`: `"import_altium"` is among the server's tools.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. Also run the full suite: `pytest -q -m "not slow"` and `pytest -q -m slow`.
- [ ] **Step 5:** Visual check: `kicad-cli sch export pdf` on eDP and Sampler output. Inspect pages 1-2 against expectations (symbols readable, notes in place); adjust `PT_TO_MM` if text visibly over- or undersized.
- [ ] **Step 6:** Commit `Add import_altium tool: aligned KiCad project from loose Altium files`, then commit `Release 0.2.0`; tag `v0.2.0`; push `main` and the tag.
