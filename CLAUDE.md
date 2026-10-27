# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A documentation + Agent Skills repo for the Revopoint MetroX 3D scanner (and the `RevoMetro.exe` desktop app). Content is mostly Chinese. There is no build system, package manifest, linter, or test suite. Windows-only.

- `Revopoint-MetroX-使用手册与技巧大全.md` — full Chinese user manual distilled from 20 official Revopoint videos.
- `skills/revopoint-metrox/` — knowledge-only skill (SKILL.md + `references/metrox-complete-guide.md`): scan modes, marker strategy, hard materials, hardware/perf, post-processing.
- `skills/revo-metro-cad-copilot/` — skill with two Python scripts:
  - `scripts/revo_controller.py` — controls `RevoMetro.exe` (launch, window focus, keyboard shortcuts / ratio clicks, screenshots), edits `revoscan.cfg` and `.revo` project `scan_scene` params, and parses `rvScan5.log` for diagnostics (e.g. error codes `0x10002` marker shortage, `0x10006` feature loss).
  - `scripts/scan_to_cad.py` — scan-to-CAD pipeline.
  - `scripts/revo_pipeline.py` — end-to-end automation: opens a project in the running RevoMetro via the `\\.\pipe\revoscan5_localserver` named pipe, clicks One-click Edit "Apply" through `scripts/frida/qt_bridge.js` (Frida-injected Qt reflection, run via `rea instrument-with-frida`), waits on `rvScan5.log`, then runs `scan_to_cad.py` and `slice_print.py`.
  - `scripts/slice_print.py` — flattens Bambu preset `inherits` chains and slices with the OrcaSlicer CLI for Bambu Lab H2D / A2L.

## Skill mirror: keep `skills/` and `.agents/skills/` identical

`.agents/skills/` is a workspace-level auto-load copy of `skills/`. Any edit to a skill must be applied to both trees (or re-copied). Verify with `diff -r skills .agents/skills`.

## Running the scripts

```powershell
python skills/revo-metro-cad-copilot/scripts/revo_controller.py status
python skills/revo-metro-cad-copilot/scripts/revo_controller.py launch --project "C:\path\to\project.revo"
python skills/revo-metro-cad-copilot/scripts/revo_controller.py configure --fuse-pitch 0.30 --project-revo <file.revo>
python skills/revo-metro-cad-copilot/scripts/revo_controller.py action toggle_scan      # keys in SHORTCUT_ACTIONS, or click_ratio --rx --ry
python skills/revo-metro-cad-copilot/scripts/revo_controller.py screenshot
python skills/revo-metro-cad-copilot/scripts/revo_controller.py diagnose --watch 10

python skills/revo-metro-cad-copilot/scripts/scan_to_cad.py --project <ProjectName> --remove-base --out-dir ./output
python skills/revo-metro-cad-copilot/scripts/scan_to_cad.py --input part.ply --name part01

python skills/revo-metro-cad-copilot/scripts/revo_pipeline.py widgets            # live Qt widget list (needs RevoMetro running)
python skills/revo-metro-cad-copilot/scripts/revo_pipeline.py auto --project <Name> --printer h2d a2l [--force]
python skills/revo-metro-cad-copilot/scripts/slice_print.py model.stl --printer a2l --out-dir output/print
```

Treat the argparse definitions in each script's `main()` as the source of truth for CLI flags; `scan_to_cad.py` requires `--input` or `--project`.

## Environment assumptions (hard-coded paths)

- `RevoMetro.exe`: `C:\Program Files\Revo Metro\RevoMetro.exe`
- Projects: `%APPDATA%\RevoMetro\Projects\<name>\*.revo`; config `%LOCALAPPDATA%\RevoMetro\config\revoscan.cfg`; log `%LOCALAPPDATA%\RevoMetro\logs\rvScan5.log`
- `scan_to_cad.py` needs FreeCAD 1.x. If `import FreeCAD` fails it re-executes itself under `FREECAD_PYTHON` (`C:\Users\rogers\Applications\FreeCAD\bin\python.exe`), which provides FreeCAD/Part/Mesh/Points/ReverseEngineering, numpy, SciPy, Matplotlib. Use that interpreter directly for ad-hoc testing.
- Default output dirs are `d:\source\revopoint\output` (gitignored).
- `revo_pipeline.py` needs the `rea` CLI on PATH (Frida). OrcaSlicer at `C:\Program Files\OrcaSlicer`; A2L machine preset comes from the Bambu Studio cache `%APPDATA%\BambuStudio\system\BBL` (OrcaSlicer 2.4.x has none), with Orca's `@BBL A1` process/filament presets.
- RevoMetro started from a tool shell is killed with the shell's job object; launch it via `explorer.exe`. It opens forwarded projects only from the start page (click `Home` first).
- The system Python's pip is broken here (`SeparateBodyFileCache` import error); scripts avoid third-party packages outside FreeCAD's bundled Python.

## scan_to_cad.py pipeline (in `main()` order)

1. Resolve input: `--input` file or `--project` name (no default); for `.revo`, picks best scan node file (`fuse_mesh.ply` preferred over `raw_preview.ply`, `--node` overrides).
2. Load points (custom numpy PLY parser) + optional FreeCAD mesh.
3. Clean: statistical outlier removal, optional RANSAC base-plane removal (`--remove-base`), connected-component isolation to drop marker blocks (`--no-isolate` disables).
4. PCA align: base on XY, Z >= 0, longest axis on X.
5. Feature extraction: bounding dims, cross-sections, Kasa circle fit for holes/cylinders, RDP polygon simplification.
6. Outputs with prefix `<out-dir>/<name>`: `_cleaned.{stl,ply,obj}`, `_cad.{step,iges}` + generated `_parametric_cad.py` (cylinder or extruded profile minus through-holes), `_drawing.dxf`, `_blueprint.{png,pdf,svg}`, `_dimensions.json` (full report, also printed to stdout).

The CAD solid is a simplified prismatic reconstruction (extrude/cylinder + vertical holes), not a free-form surface fit.
