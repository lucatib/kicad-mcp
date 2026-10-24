# import_altium — an aligned KiCad project from loose Altium files

**Date:** 2026-10-24
**Status:** Approved in conversation; builder check done (calls marked *[builder call]*)

## 1. Purpose

Importing an Altium design into KiCad today takes several manual steps:
create a project, import each schematic, import the layout. And it breaks
along the way. The user's last attempt ended with the second sheet's
designators renumbered, so they no longer matched the board, and every
"Update PCB from Schematic" wanted to swap parts.

`import_altium` takes the Altium files as they come, with or without a
`.PrjPcb`, and produces a KiCad project whose schematic and board are already
linked: R12 on the schematic is R12 on the board, on every sheet and in every
channel. Where the Altium files themselves disagree, it still produces the
project and lists every disagreement.

## 2. What the spike established (2026-10-24)

These facts are verified on KiCad 10.0.7 against KiCad's own Altium test files
(`eDP_adapter_dvt1`: flat, two sheets) and `sinara-hw/Sampler`
(hierarchical, 8 repeated `Input_channel` sheets, 573 footprints). The design
rests on them.

1. **Boards import headlessly.** `kicad-cli pcb import --format altium
   --report-format json` converts a `.PcbDoc` with KiCad closed. eDP takes
   0.7 s; Sampler (12 MB) takes 58 s. The output keeps Altium's PCB
   designators, including channel suffixes (`C10A` … `C10H`).
2. **Schematics do not.** No `kicad-cli` command loads a `.SchDoc`
   ("Failed to load schematic"). KiCad 10 has no schematic IPC API either.
   The schematic conversion must be written here.
3. **A `.SchDoc` is easy to read.** It is an OLE compound file. Its
   `FileHeader` stream is a sequence of length-prefixed `|KEY=VALUE|` records
   (types listed in KiCad's `altium_parser_sch.h`). The `Storage` stream holds
   embedded images. `olefile` reads both.
4. **The link key is Altium's UniqueId, not the designator.**
   - Every schematic component has an 8-letter `UNIQUEID`. Every sheet symbol
     has one too.
   - Every board component carries `SOURCEUNIQUEID`, the full path of those
     ids from the root: `\YIFCFPRE` for a top-level part, `\YTXDVDYF\WSKJVKTL`
     for part `WSKJVKTL` inside sheet symbol `YTXDVDYF`.
   - In a repeated channel the same part appears once per instance:
     `\GBVAIBAE\JSBUTAQD`, `\TJKXBNXR\JSBUTAQD`, and so on.
5. **KiCad's own mapping from id to UUID is reproducible.** KiCad's PCB
   importer computes `AltiumUniqueIdToKiid(id) = uuid5(6f9619ff-8b86-d011-b42d-00cf4fc964ff, id)`,
   where `id` is the whole `SOURCEUNIQUEID` without its leading backslash
   (`GBVAIBAE\JSBUTAQD` for a part in a sub-sheet, so every channel instance
   gets its own UUID). Python's `uuid.uuid5` reproduces it exactly (verified
   on eDP's 112 and Sampler's 567 footprints). The converted schematic gives
   each symbol `uuid5` of its own UniqueId and each sheet `uuid5` of its sheet
   symbol's, and the board's paths are rewritten to match (fact 7).
6. **KiCad's PCB importer breaks the sheet half of each footprint path.**
   It builds the sheet part as `KIID(SOURCEHIERARCHICALPATH)`. That is a name
   such as `power` or `Sampler\U_Input_channel`, not a UUID, so KiCad
   substitutes a **random UUID, different for every footprint**. With paths
   broken, KiCad falls back to matching by reference. That fallback is how
   renumbered sheets end up out of sync.
7. **Rewriting the footprint paths fixes the link.** In the spike, each path
   was rebuilt from `SOURCEUNIQUEID` (each segment mapped through 5). Symbols
   were given `uuid5(UNIQUEID)` and sheets `uuid5(sheet symbol UNIQUEID)`.
   `kicad-cli pcb drc --schematic-parity` then reported zero missing, extra or
   duplicate footprints on 112 footprints, with no `.PrjPcb` read.
8. **The source files can be wrong.** In eDP, `C22P` is on sheet `power` on
   the board (id `DEDIDVJI`) but on sheet `it6251_core` in the schematic
   (id `WFNKDDPR`). The schematic was changed in Altium and never pushed to
   the board. This is the kind of thing the report must surface.
9. **KiCad's schematic importer also renames.** For repeated sheets it
   appends `_<sheetname>` to every reference. This tool must not do that.

## 3. Goals

- **G1 Aligned by construction.** Every footprint links to its symbol by path,
  never by reference fallback. `drc --schematic-parity` on the result reports
  no missing, extra or duplicate footprints, except for parts that really
  exist on only one side in the Altium files. Those are listed in the report.
- **G2 Designators are never changed.** The board keeps Altium's PCB
  designators. Each schematic symbol instance shows the same designator as its
  footprint, including per-channel ones (`C10A` in instance A, `C10B` in
  instance B). Duplicates and conflicts are reported, never resolved by
  renaming.
- **G3 Electrically identical.** The schematic's connectivity, as KiCad
  netlists it, partitions the pins exactly as the board's pad nets do. Net
  names should match too. Topology comes first: a name difference is a
  warning, a topology difference is an error (see 6.3).
- **G4 Notes faithful (required).** Free text, text frames, notes, graphic
  lines, shapes and images keep their position, size, wrapping and content.
  Sheet size and title block fields carry over.
- **G5 Symbols faithful where possible.** Every symbol is rebuilt from the
  graphics placed in the `.SchDoc`: pins (name, number, type, length,
  orientation, hidden), lines, polylines, polygons, rectangles, rounded
  rectangles, arcs, ellipses, beziers and text. No Altium library is needed.
  Multi-part components become multi-unit symbols.
- **G6 Loose files are enough.** Input can be a folder or any mix of
  `.SchDoc`/`.PcbDoc` files: schematics only, board only, or both. The
  `.PrjPcb` is optional. The hierarchy, the root sheet and the channels are
  derived from the files themselves.
- **G7 KiCad can stay closed.** Everything runs from files and `kicad-cli`.

## 4. Non-goals

- Separate `.kicad_sym` / `.pretty` libraries. Symbols and footprints live
  inside the project's schematic and board (user's decision).
- Altium libraries (`.SchLib`, `.PcbLib`, `.IntLib`) as inputs.
- Altium variants, Draftsman, OutJobs, and rules beyond what `kicad-cli pcb
  import` already maps.
- **ASCII `.SchDoc` is in scope after all.** It carries the same records, one
  per line (`|HEADER=Protel for Windows - Schematic Capture Ascii File…`), so
  it is read too (KiCad's `ticket1303` sample). Binary records inside an OLE
  `.SchDoc` (flag byte ≠ 0) are counted in `not_converted`. *[builder call]*
- Round-tripping back to Altium.
- Editing or fixing the Altium files.

## 5. Anti-goals: a failure even if it technically works

- **Silent renumbering.** Any reference that differs from Altium's, on either
  side.
- **Silent loss.** An Altium object that is not converted and does not appear
  in the report.
- **Touching the inputs.** Altium files are only read.
- **Overwriting without being asked.** Writing over an existing output folder
  without `overwrite=True`.
- **A false "aligned".** Reporting success while parity, or the net
  comparison, disagrees.

## 6. Behaviour

### 6.1 The tool

```
import_altium(sources, output_dir=None, project_name=None, overwrite=False)
```

- **`sources`:** one folder, or a list of files and folders. Folders are
  scanned non-recursively for `.SchDoc`, `.PcbDoc` and `.PrjPcb`, matched
  case-insensitively, since real projects mix `Sampler.schdoc` and
  `Sampler.PCBDOC`.
- **`project_name`:** defaults to the `.PrjPcb` stem, else the `.PcbDoc`
  stem, else the root sheet's stem.
- **`output_dir`:** defaults to `<folder of the first source>/<project_name>_kicad`.
  If it exists and is not empty, the tool refuses with a remedy, unless
  `overwrite=True`.
- **More than one board.** If more than one `.PcbDoc` is given, the tool
  refuses and asks which board to use. Panels and variants are common
  neighbours (Sampler ships `Sampler_4HP.PCBDOC` and `Panel_*.PcbDoc`).

### 6.2 Output

```
<output_dir>/
  <name>.kicad_pro
  <name>.kicad_sch           root sheet (converted Altium root)
  <sheet>.kicad_sch          one per distinct .SchDoc; repeated channels share one file
  <name>.kicad_pcb           when a board was given
  import_report.json         full report (6.4)
```

- **Names.** The root sheet is renamed to `<name>.kicad_sch` so KiCad opens
  it as the project root. Other sheets keep their `.SchDoc` stem.
- **Images.** Embedded images go inside the `.kicad_sch` (KiCad 10 embeds
  them), not as loose files.

### 6.3 Pipeline

1. **Read** each `.SchDoc` into a neutral model (records, owner tree, fonts,
   sheet settings, images), and the `.PcbDoc`'s `Components6` stream.
2. **Hierarchy.** Sheet symbols name their child file (`FILE_NAME` record).
   - **Hierarchical design.** At least one sheet has a sheet symbol. The root
     is the sheet no sheet symbol refers to. More than one candidate means
     some sheets are not part of the design. If a board is given, its
     `SOURCEHIERARCHICALPATH` first segments name the real root. Otherwise a
     `.PrjPcb`'s document order decides, and failing that it is an error that
     names the candidates. Sheets outside the hierarchy are left out and
     listed as `unreferenced_sheet`.
   - **Flat design.** No sheet has a sheet symbol, as in eDP, whose parts carry
     single-segment ids (`\DEDIDVJI`) and bare sheet names. KiCad needs one
     root, so the tool makes one. The synthesized root holds one sheet symbol
     per `.SchDoc`.
     - Sheet order: `.PrjPcb` order, else the order the board's
       `SOURCEHIERARCHICALPATH` values first appear, else file name.
     - Sheet symbol UUIDs: `uuid5(KICAD_ALTIUM_NS, "flat-sheet:" + stem)`,
       because no Altium id exists to hash.
     - The report records it under `inferred`.
     - With a board, a `.SchDoc` whose stem no footprint names is still
       included but listed as `unreferenced_sheet`. *[builder call]*
   - **Missing child.** A sheet symbol whose file is missing is reported by
     name. The sheet symbol stays in the parent, pointing at a placeholder
     file name, and no empty sheet is invented.
   - **Recursion.** A sheet that includes itself, directly or indirectly, is
     an error.
3. **UUIDs.** Every component, sheet symbol and sheet gets
   `uuid5(KICAD_ALTIUM_NS, UNIQUEID)` (fact 5).
   - **Missing or duplicate ids.** A `.SchDoc` component with no UniqueId, or
     one shared with another component on the same sheet, gets a fresh UUID
     and is reported. It will then link by reference only.
   - **Root sheet.** The root sheet's own UUID is derived from the project
     name, so re-running the import gives the same UUIDs.
4. **Board.** Run `kicad-cli pcb import --format altium --report-format json`
   into the output folder.
   - **Join.** Join each KiCad footprint to its `Components6` row. The key is
     the symbol UUID (from the last id segment) plus position. A channel shares
     the UUID, so position decides. KiCad's coordinate conversion is
     deterministic, so the tolerance can be tight.
   - **Rewrite.** Rewrite `(path …)`, `(sheetname …)` and `(sheetfile …)`
     from `SOURCEUNIQUEID` (fact 7).
   - **No row.** Footprints without a row (logos, mounting holes, parts placed
     only on the board) keep their reference and get no path. They appear in
     the report as board-only.
5. **References.** Each symbol instance (sheet path × symbol) takes the
   reference of the footprint whose path it is.
   - **Instances without a footprint.** Schematic-only parts, and every part
     when no board is given, take the `.SchDoc` designator.
   - **Repeated channels without a board.** The designator would repeat
     across instances. Apply Altium's default channel naming
     (`$Component$ChannelAlpha`: suffix A, B, … in sheet symbol order) and
     report that the naming was inferred. If a `.PrjPcb` is present, read its
     channel-naming setting instead.
   - **Never renumber.** A designator that is still duplicated is reported,
     not renumbered.
   **Multi-part components.** Placed parts with the same designator and
   library reference on the same sheet are units of one symbol. Each keeps
   its own UUID from its own UniqueId. The footprint path keeps the id the
   board names; KiCad matches a footprint against any unit of the symbol
   (the spike's two-part `U10C` linked cleanly). *[builder call]*
6. **Fields.** The goal is that, where the sources agree, the first "Update
   PCB from Schematic" changes nothing. *[builder call]*
   - **Footprint.** The symbol's `Footprint` field is the lib id of the
     footprint `kicad-cli` created. That is the only footprint that exists in
     KiCad, since no libraries are made. Altium's footprint name is kept as a
     hidden `Altium_Footprint` field. Without a board, `Footprint` stays empty
     and `Altium_Footprint` holds the name.
   - **Value.** The symbol's value is the schematic's, because the schematic is
     what Altium's ECO would push. A different board value is reported as
     `value_differs`.
7. **Schematics.** Convert each sheet (6.5) and write it with the format
   version the installed KiCad writes (`cli.schematic_format_version`).
8. **Net names.** KiCad names a sheet-local net by its sheet path
   (`/power/SDA`). Altium's board names are bare (`SDA`). Left alone, the
   first update would rename most nets on the board, which is the same churn
   this tool exists to remove. *[builder call]*
   - **Renaming.** Once the net check (9) has matched board nets to schematic
     nets by their pad sets, each matched board net is renamed to the
     schematic's name in the `.kicad_pcb`.
   - **Net classes.** Net-class membership in the board moves with the
     rename.
   - **Unmatched nets.** Unmatched nets keep their Altium name.
   - **Report.** Every rename is listed in `net_renames` (old → new).
9. **Verify.**
   - **Nets.** `kicad-cli sch export netlist --format kicadxml` on the root,
     compared with the board's pad nets as two partitions of (ref, pad) sets.
     - **Exclusions.** Pads of parts already listed as `board_only`,
       `schematic_only` or `moved_sheet` are left out of both partitions
       before comparing.
     - **What remains.** A remaining topology difference cannot be told apart
       from a connectivity change made in Altium and never pushed to the
       board. It is reported as `topology_differences`, with that caveat, and
       makes `aligned` false. *[builder call]*
   - **Parity.** After the rename, run `kicad-cli pcb drc --schematic-parity
     --format json` on the result.
   - **ERC.** `kicad-cli sch erc`, with the count recorded in the report and
     not used to fail.
10. **Report** (6.4). The tool always writes the project, even when the
    sources disagree (user's decision). The result says `aligned: true` only
    when both of these hold:
    - **Parity.** Every parity entry, of any type including net conflicts and
      footprint or value mismatches, involves a part already listed in
      `source_disagreements`.
    - **Topology.** The net check has no topology differences.

### 6.4 Report

`import_altium` returns a summary. `import_report.json` holds everything:

```json
{
  "output_dir": "...", "project": "...kicad_pro", "aligned": false,
  "sources": {"schematics": ["..."], "board": "...", "project": null},
  "counts": {"sheets": 6, "sheet_instances": 13, "symbols": 412, "footprints": 573},
  "source_disagreements": [
    {"kind": "moved_sheet", "ref": "C22P",
     "board": {"sheet": "power", "id": "DEDIDVJI"},
     "schematic": {"sheet": "it6251_core", "id": "WFNKDDPR"}},
    {"kind": "board_only", "ref": "LOGO1"},
    {"kind": "schematic_only", "ref": "TP3", "sheet": "power"},
    {"kind": "value_differs", "ref": "R10P", "board": "10k", "schematic": "12k"},
    {"kind": "footprint_differs", "ref": "U3", "board": "...", "schematic": "..."}
  ],
  "net_check": {"topology_differences": [], "excluded_parts": ["C22P"]},
  "net_renames": [{"board": "SDA", "schematic": "/power/SDA"}],
  "parity": {"missing": 0, "extra": 0, "duplicates": 0, "other": {"...": 0}},
  "erc_violations": 12,
  "approximations": [{"what": "font 'Arial Narrow' -> KiCad default", "count": 37}],
  "not_converted": [{"record": "HARNESS_CONNECTOR", "sheet": "...", "count": 2}],
  "inferred": [{"what": "channel suffixes A-H for Input_channel", "reason": "no board, no .PrjPcb"}]
}
```

- **Summary.** It carries `aligned`, the counts, each list's length and the
  first 50 entries of each list, capped with the existing `_cap` convention.
- **Moved part.** `moved_sheet` is how a part that changed sheet shows up:
  both sides have the designator, but under different ids and sheets.

### 6.5 Schematic conversion

These rules are the user's priorities, in order:

1. **Connectivity first.** It must be exactly right.
2. **Notes and annotations faithful.**
3. **Symbols faithful where possible.**

| Altium | KiCad |
|---|---|
| Wire, junction, bus, bus entry | wire, junction, bus, bus entry |
| Net label | local label (bus labels keep `D[0..7]` style, converted to KiCad `D[0..7]`) |
| Power port | power symbol; its value is the net name; style (bar, ground, arrow…) drawn to match |
| Port | hierarchical label on a child sheet; on the root, a global label |
| Sheet symbol + sheet entries | sheet + sheet pins (pin side and type from the entry) |
| No ERC | no-connect flag |
| Component | symbol instance; its own lib symbol, built from its graphics; identical graphics share one lib symbol |
| Parameters | fields: Comment → Value, footprint from the implementation list or the board, others as hidden fields unless visible in Altium |
| Text, text frame, note | text / text box, same position, size, wrapping, justification, rotation |
| Line, polyline, polygon, rectangle, rounded rect, arc, ellipse, bezier (sheet graphics) | the matching KiCad graphic |
| Image | embedded image, same position and size |
| Sheet (size, title block parameters) | paper size (custom when needed) and title block |
| Harness, compile mask, blanket, hyperlink, IEEE symbol, pie chart | not converted, or approximated (a blanket's text survives as text); always listed in the report |

- **Net scope.** Altium's net-identifier scope comes from the `.PrjPcb`
  (`HierarchyMode`) when present. Without one, Altium's "Automatic" rule
  applies:

  | Mode | When | Ports | Net labels |
  |---|---|---|---|
  | Hierarchical | any sheet entry exists | connect only through sheet entries (hierarchical labels) | local |
  | Flat | ports, no sheet entries | global (KiCad global labels) | local |
  | Global | neither | global | global (KiCad global labels) |

  Power ports are always global. The mode used is recorded in the report.
- **`Repeat()` channels.** A sheet symbol named `Repeat(…)` is not expanded
  in this version. It is converted as a single instance, listed under
  `not_converted`, and makes `aligned` false. Separate sheet symbols for the
  same file, as in Sampler, are fully supported. *[builder call: scope]*
- **Coordinates.** Altium's are in 10-mil units plus a fractional part, with
  Y up. KiCad's are millimetres with Y down. Pins must land on KiCad's
  1.27 mm connection grid wherever Altium's do. The conversion is exact
  arithmetic, not rounding to a grid.
- **Fonts.** The font table (sheet record) gives each text its size, bold and
  italic. Font faces map to KiCad's default font. Each face that differs is
  counted once in `approximations`.
- **Multi-part components.**
  - **Units.** A component's parts (`OWNERPARTID`) become units of one
    symbol.
  - **Display modes.** Alternate display modes become a De Morgan
    alternate when there are exactly two; otherwise only mode 0 is converted
    and the rest are reported.
  - **Parts placed separately.** Each placed part (its own record and
    UniqueId, as `U10C` in eDP) is a separate instance with its own UUID and
    unit number. The footprint links to the first unit's UUID.

### 6.6 Code shape

The builder's call, but it should fit the repo:

- **Parsing** in its own module, with no KiCad dependency, testable with
  plain pytest.
- **Writing** through `sexpr` like the rest of the repo.
- **The tool** through `server.py` with `@tool_result`.
- **`olefile`** becomes a dependency in `pyproject.toml`. It is pure Python
  and BSD-licensed.
- **Licensing.** KiCad's GPL-licensed importer is a reference for the format
  only. No code is copied into this MIT project.

## 7. Testing

- **Fixtures.** Tests need real Altium files: KiCad's
  `qa/data/*/plugins/altium` files and `sinara-hw/Sampler` (CERN-OHL).
  - **Not vendored.** Their licences (GPL repo, CERN-OHL-S) do not sit well
    in an MIT repo.
  - **Downloaded once** by a fixture into `tests/.altium_cache/` (gitignored),
    pinned by URL and SHA-256.
  - **Offline.** The tests skip when the files cannot be fetched.
  - **Unit tests.** Parser tests that need only record framing build tiny
    synthetic OLE files in the test itself. *[builder call]*
- **Parser unit tests** (no KiCad): record stream framing; owner tree;
  designator and parameter attachment; UniqueId paths; coordinate and
  fraction conversion; sheet-symbol → file graph and root detection.
- **UUID tests:** the fact-5 mapping reproduces footprint UUIDs from a
  `kicad-cli`-imported eDP board.
- **End-to-end (`requires_kicad`):**
  - **eDP** (flat): parity shows no missing/extra/duplicates; `C22P` is
    reported as `moved_sheet`; the net check has no topology differences;
    references are identical on both sides.
  - **Sampler** (opt-in): 8 instances of `Input_channel` share one file, with
    per-instance references equal to the board (`C10A` … `C10H`); parity is
    clean; topology is equal.
  - **Schematics only** (`issue16903`): the hierarchy is rebuilt and nothing
    is lost silently.
  - **Board only:** the project opens, every footprint is reported as
    board-only, and nothing else fails.
  - **Inputs and output:** an existing output folder is refused without
    `overwrite`; inputs are byte-identical afterwards.
- **Visual:** export each converted sheet to PDF with `kicad-cli` and compare
  it by eye with Altium's own PDF where one exists (eDP has none; the
  KiCad-rendered SVG/PDF of the converted sheets is reviewed).

## 8. Left to the builder

- Module split, internal model, and how lib symbols are deduplicated.
- How each Altium power-port style is drawn as a KiCad power symbol, provided
  the net name is kept and the shape is recognisable.
- Exact text-size mapping from Altium font points to KiCad mm, provided text
  fits where it did in Altium.
- Timeouts for big boards. Sampler's import takes about a minute, so the
  existing 180 s default may need a parameter.
- Whether the join in 6.3.4 uses the board file or `pcbnew`.
