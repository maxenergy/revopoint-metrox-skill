#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bambu Lab H2D / A2L slicing via the OrcaSlicer CLI
==================================================
Resolves Bambu system presets (machine / process / filament) by flattening their
`inherits` chains into self-contained JSON files, then runs `orca-slicer.exe` headless
to produce a ready-to-print .3mf (with sliced G-code plates) for Bambu Lab printers.

Preset sources (searched in order, first match by preset name wins):
  1. <OrcaSlicer>\\resources\\profiles\\BBL   (bundled with OrcaSlicer; has H2D, no A2L in 2.4.x)
  2. %APPDATA%\\BambuStudio\\system\\BBL      (Bambu Studio cache; the only local source of A2L presets)
Bambu Studio process/filament values can fail Orca's range validation (e.g. tree_support_wall_count=-1),
so A2L uses the Bambu Studio machine preset with Orca's A1 process/filament presets, whose
compatible_printers lists are extended with the A2L machine name.
"""

import argparse
import glob
import json
import os
import subprocess
import sys

ORCA_EXE = r"C:\Program Files\OrcaSlicer\orca-slicer.exe"
PRESET_ROOTS = [
    os.path.join(os.path.dirname(ORCA_EXE), "resources", "profiles", "BBL"),
    os.path.expandvars(r"%APPDATA%\BambuStudio\system\BBL"),
]

# Default presets per printer (0.4 mm nozzle, Bambu PLA Basic, 0.20 mm Standard)
PRINTERS = {
    "h2d": {
        "machine": "Bambu Lab H2D 0.4 nozzle",
        "process": "0.20mm Standard @BBL H2D",
        "filament": "Bambu PLA Basic @BBL H2D",
    },
    "a2l": {
        "machine": "Bambu Lab A2L 0.4 nozzle",
        "process": "0.20mm Standard @BBL A1",
        "filament": "Bambu PLA Basic @BBL A1",
    },
}

# Keys that only describe preset bookkeeping; dropped after flattening
META_KEYS = {"inherits", "instantiation", "setting_id", "filament_id", "base_id"}


def build_index(kind):
    """Map preset name -> JSON path for one preset kind across all preset roots."""
    index = {}
    for root in PRESET_ROOTS:
        for path in glob.glob(os.path.join(root, kind, "*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    name = json.load(f).get("name")
            except (OSError, ValueError):
                continue
            if name and name not in index:
                index[name] = path
    return index


def flatten_preset(kind, name, index=None):
    """Merge a preset with every ancestor in its `inherits` chain (child keys override parents)."""
    index = index or build_index(kind)
    chain = []
    current = name
    while current:
        if current not in index:
            raise FileNotFoundError(f"找不到 {kind} 预设 '{current}' (搜索目录: {PRESET_ROOTS})")
        with open(index[current], "r", encoding="utf-8") as f:
            data = json.load(f)
        chain.append(data)
        current = data.get("inherits", "")
    merged = {}
    for data in reversed(chain):
        merged.update(data)
    for key in META_KEYS:
        merged.pop(key, None)
    merged["name"] = name
    return merged


def write_resolved_presets(printer, out_dir, machine=None, process=None, filament=None):
    """Write flattened machine/process/filament JSON files and return their paths."""
    defaults = PRINTERS[printer]
    names = {
        "machine": machine or defaults["machine"],
        "process": process or defaults["process"],
        "filament": filament or defaults["filament"],
    }
    preset_dir = os.path.join(out_dir, f"presets_{printer}")
    os.makedirs(preset_dir, exist_ok=True)
    paths = {}
    for kind, name in names.items():
        data = flatten_preset(kind, name)
        compat = data.get("compatible_printers")
        if kind != "machine" and isinstance(compat, list) and compat and names["machine"] not in compat:
            data["compatible_printers"] = compat + [names["machine"]]
        path = os.path.join(preset_dir, f"{kind}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        paths[kind] = path
    return paths, names


def slice_model(model_path, printer, out_dir, machine=None, process=None, filament=None,
                orient=True, timeout=900):
    """Slice one model (.stl/.obj/.3mf/.step) with OrcaSlicer CLI; returns a result dict."""
    if not os.path.isfile(ORCA_EXE):
        raise FileNotFoundError(f"未找到 OrcaSlicer: {ORCA_EXE}")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    presets, names = write_resolved_presets(printer, out_dir, machine, process, filament)
    base = os.path.splitext(os.path.basename(model_path))[0]
    out_3mf = os.path.join(out_dir, f"{base}_{printer}.3mf")
    datadir = os.path.join(out_dir, "orca_datadir")
    os.makedirs(datadir, exist_ok=True)
    cmd = [
        ORCA_EXE,
        "--datadir", datadir,
        "--debug", "3",
        "--slice", "0",
        "--arrange", "1",
        "--orient", "1" if orient else "0",
        "--load-settings", f"{presets['machine']};{presets['process']}",
        "--load-filaments", presets["filament"],
        "--outputdir", out_dir,
        "--export-3mf", os.path.basename(out_3mf),  # resolved relative to --outputdir
        os.path.abspath(model_path),
    ]
    for stale in glob.glob(os.path.join(out_dir, "plate_*.gcode")):
        os.remove(stale)
    # cwd=out_dir: with --debug the CLI writes its 00000.log into the working directory
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, cwd=out_dir)
    gcodes = []
    for plate in sorted(glob.glob(os.path.join(out_dir, "plate_*.gcode"))):
        named = os.path.join(out_dir, f"{base}_{printer}_{os.path.basename(plate)}")
        os.replace(plate, named)
        gcodes.append(named)
    result_json = os.path.join(out_dir, "result.json")
    orca_result = None
    if os.path.isfile(result_json):
        with open(result_json, "r", encoding="utf-8", errors="replace") as f:
            try:
                orca_result = json.load(f)
            except ValueError:
                orca_result = None
    return {
        "printer": printer,
        "presets": names,
        "returncode": proc.returncode,
        "output_3mf": out_3mf if os.path.isfile(out_3mf) else None,
        "gcode_files": gcodes,
        "orca_result": orca_result,
        "command": cmd,
        "stderr_tail": proc.stderr[-2000:],
    }


def main():
    parser = argparse.ArgumentParser(description="拓竹 H2D / A2L 自动切片 (OrcaSlicer CLI)")
    parser.add_argument("model", help="要切片的模型 (.stl / .obj / .3mf / .step)")
    parser.add_argument("--printer", choices=sorted(PRINTERS), default="h2d", help="目标打印机")
    parser.add_argument("--out-dir", default=r"d:\source\revopoint\output\print", help="输出目录")
    parser.add_argument("--machine", help="覆盖机器预设名称")
    parser.add_argument("--process", help="覆盖工艺预设名称，如 '0.16mm Standard @BBL H2D'")
    parser.add_argument("--filament", help="覆盖耗材预设名称，如 'Bambu PETG HF @BBL H2D'")
    parser.add_argument("--no-orient", action="store_true", help="禁用自动摆放朝向")
    parser.add_argument("--list-presets", action="store_true", help="列出该打印机可用的工艺/耗材预设后退出")
    args = parser.parse_args()

    if args.list_presets:
        tag = "H2D" if args.printer == "h2d" else "A2L"
        for kind in ("process", "filament"):
            print(f"== {kind}")
            for name in sorted(n for n in build_index(kind) if f"@BBL {tag}" in n and "template" not in n):
                print("  " + name)
        return

    res = slice_model(args.model, args.printer, args.out_dir, args.machine, args.process,
                      args.filament, orient=not args.no_orient)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res["returncode"] == 0 and res["output_3mf"] else 1)


if __name__ == "__main__":
    main()
