# Inventor to SolidCraft

Convert **Autodesk Inventor** parts and assemblies (`.ipt` / `.iam`) into **[SolidCraft](https://github.com/storytold)** designs — without Inventor installed.

For every part it recovers the parameters (with their real expressions), the 2D sketches (geometry, constraints, dimensions) and the feature tree, and rebuilds them as native, editable SolidCraft features where it can. Every native rebuild is checked against the exact solid stored in the Inventor file; when it does not match, the part falls back to that exact solid *plus* all of its parameters and sketches, so nothing is ever silently wrong. Assemblies are rebuilt with every occurrence at Inventor's exact placement.

```
 ___ _   ___   __    ___  ___    ___ ___  _  ___   __
|_ _| \ | \ \ / /__ / __|/ __|  / __/ _ \| \| \ \ / /
 | ||  \| |\ V /___|\__ \ (__  | (_| (_) | .` |\ V /
|___|_|\__| \_/     |___/\___|  \___\___/|_|\_| \_/
```

## How it works

| Stage | Script | What it does |
|---|---|---|
| 1. Decode parts | `batch_decode.py` → `il_decode.py` | Runs the [InventorLoader](https://github.com/jmplonka/InventorLoader) readers headless inside FreeCAD's console and dumps every decoded node (parameters + formulas, sketches, features, profiles, edge sets) to JSON, plus the exact solid as STEP (ACIS → STEP). Parts missing from the live folders are recovered from the newest `OldVersions` backup. |
| 2. Decode assemblies | `batch_decode.py --assemblies` | Dumps each `.iam`'s occurrence segment: component definitions, occurrence ids and 4×4 placements. |
| 3. Translate parts | `batch_translate.py` → `sc_translate.py` | Writes a SolidCraft command script per part and runs it with `solidcraft-cli`, then verifies volume + bounding box against the exact solid. |
| 4. Build assemblies | `sc_assembly.py` | Inserts each component's `.sc` as a linked component at the decoded matrix; sub-assemblies first. |
| Progress | `progress_gui.py` | Retro green-phosphor progress window (watch a log, or run the whole pipeline). |
| Quick look | `inv_extract.py` | Pure-Python parameter / feature-name / assembly-tree dump (no FreeCAD needed). |

### What becomes native

* **Parameters** — every user and model parameter, in dependency order. An expression is kept only when it re-evaluates to the decoded value; otherwise the exact value is used (and the report says why).
* **Sketches** — points, lines, circles, arcs, ellipses on their exact planes; coincident, horizontal/vertical, parallel, perpendicular, collinear, tangent, equal, symmetric and fixed constraints; distance, horizontal/vertical, radius, diameter, angle dimensions driven by the parameters. A constraint or dimension is only sent when the stored geometry already satisfies it, so sketches keep Inventor's exact shape.
* **Features** — Extrude (new/join/cut/intersect, symmetric, two-sided, taper, through-all), Revolve, constant-radius Fillet and Chamfer. Fillet/chamfer edges are found by building the part up to that feature and classifying each edge midpoint against the exact final solid.

Anything else (Hole, Mirror, patterns, Shell, Sweep, Loft, derived/reference bodies, …) currently keeps the exact solid + parameters + sketches.

### Result kinds (`translate_report.csv`)

| Result | Meaning |
|---|---|
| `native` | Full editable feature tree, verified against the exact solid |
| `step+sketches` | Exact solid + all parameters and sketches |
| `sewn-solid+sketches` | Surfaces sewn into a solid with FreeCAD's kernel |
| `mesh+sketches` / `project-STL+sketches` | No closed solid in the file (surface model, or geometry not stored) |
| `parameters+sketches only` | Parameter sheets and other parts with no geometry |

## Requirements

* Python 3.10+ with `olefile` and `openpyxl` (`pip install olefile openpyxl`)
* [FreeCAD](https://www.freecad.org/) 1.x with the **InventorLoader** add-on installed in its `Mod` folder, and `xlrd xlwt xlutils olefile` installed into FreeCAD's own Python
* [SolidCraft](https://github.com/storytold) with `solidcraft-cli`

Programs are found automatically; override with `FREECADCMD` and `SOLIDCRAFT_CLI`.

## Usage

```powershell
# everything, with the retro progress window
pythonw progress_gui.py --run --project "C:\path\to\Inventor project" --work C:\temp\inv-work --export "C:\path\to\SolidCraft Export"

# or stage by stage
python batch_decode.py   "C:\path\to\Inventor project" C:\temp\inv-work
python batch_decode.py   "C:\path\to\Inventor project" C:\temp\inv-work\asm --assemblies
python batch_translate.py C:\temp\inv-work "C:\path\to\SolidCraft Export" --project "C:\path\to\Inventor project"
python sc_assembly.py     C:\temp\inv-work\asm "C:\path\to\SolidCraft Export"

# watch a running batch
pythonw progress_gui.py --watch translate.log
```

Source files are never modified: the decoder works on copies (InventorLoader writes dump folders next to its input).

## Limitations

* Inventor 2024 files decode, but InventorLoader predates them; a few records (e.g. some sub-assembly occurrence records) are inferred rather than read.
* Splines, elliptical arcs, Holes, Mirrors, patterns and the other features listed above are not yet rebuilt natively.
* Assembly mates are not recreated — occurrences are placed at their solved positions.

## License

GPL-2.0-or-later. This project drives and builds on [InventorLoader](https://github.com/jmplonka/InventorLoader) (GPL-2.0) by Jens M. Plonka. Autodesk and Inventor are trademarks of Autodesk, Inc.; this project is not affiliated with Autodesk or SolidCraft.
