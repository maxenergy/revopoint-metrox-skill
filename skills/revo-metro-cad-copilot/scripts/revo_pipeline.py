#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Revo Metro 扫描 → 网格 → 3D 图纸 → 拓竹切片 全自动流水线
=========================================================
Drives the running RevoMetro.exe (Qt 5.12, MSVC x64) through in-process Qt reflection
injected with Frida (via the REA CLI), then hands the meshed scan to scan_to_cad.py
and slice_print.py.

Reverse-engineered control surface (RevoMetro 5.8.8.460):
  * Single instance: a second `RevoMetro.exe <project.revo>` writes the UTF-8 path to
    QLocalServer "revoscan5_localserver" (Windows pipe \\.\pipe\revoscan5_localserver);
    StartPageWidget::openDocument() acts only while the start page shows and no modal
    window is open (click the "Home" tool button first when in a project).
  * Widgets are addressed by QObject::objectName / button text. UIA exposes no AutomationId.
  * One-click Edit (QuickEditPage, objectName "editPage") "Apply" button runs fusion +
    meshing + denoise with recommended parameters on the active scan node.
  * rvScan5.log markers: "[QuickEdit] one-click edit end", then
    "mesh change, save to : <node>/fuse_mesh.ply"; property.rvproj gets "mesh_file".
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import slice_print  # noqa: E402

REVO_EXE = r"C:\Program Files\Revo Metro\RevoMetro.exe"
PROJECTS_DIR = os.path.expandvars(r"%APPDATA%\RevoMetro\Projects")
LOG_PATH = os.path.expandvars(r"%LOCALAPPDATA%\RevoMetro\logs\rvScan5.log")
IPC_PIPE = r"\\.\pipe\revoscan5_localserver"
QT_BRIDGE_JS = os.path.join(SCRIPT_DIR, "frida", "qt_bridge.js")
SCAN_TO_CAD = os.path.join(SCRIPT_DIR, "scan_to_cad.py")
DEFAULT_OUT = r"d:\source\revopoint\output"

FAIL_PATTERNS = re.compile(r"RVE_SCAN_FUSE_FAILED|RVE_SCAN_MESH_FAILED|RVE_MESHING_EXCEPTION|"
                           r"fuseProcess error|abort fuse point cloud|The document is null")


# ---------------------------------------------------------------- process & log helpers
def revo_pid():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RevoMetro.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    m = re.search(r'"RevoMetro\.exe","(\d+)"', out)
    return int(m.group(1)) if m else None


class LogCursor:
    """Reads rvScan5.log lines appended after construction."""

    def __init__(self):
        self.pos = os.path.getsize(LOG_PATH) if os.path.isfile(LOG_PATH) else 0
        self.pending = []  # lines read but not yet consumed by wait_for

    def new_lines(self):
        if self.pending:
            lines, self.pending = self.pending, []
            return lines
        if not os.path.isfile(LOG_PATH):
            return []
        size = os.path.getsize(LOG_PATH)
        if size < self.pos:  # log rotated
            self.pos = 0
        with open(LOG_PATH, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        self.pos += len(data)
        return data.decode("utf-8", errors="replace").splitlines()

    def wait_for(self, pattern, timeout, fail=None):
        """Block until a new log line matches `pattern`; returns (match, lines_seen)."""
        rx = re.compile(pattern)
        seen = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            lines = self.new_lines()
            for i, line in enumerate(lines):
                seen.append(line)
                if fail is not None and fail.search(line):
                    raise RuntimeError(f"RevoMetro 报告失败: {line.strip()}")
                m = rx.search(line)
                if m:
                    self.pending = lines[i + 1:]
                    return m, seen
            time.sleep(1.0)
        raise TimeoutError(f"等待日志 /{pattern}/ 超时 ({timeout}s)")


# ---------------------------------------------------------------- Frida Qt bridge
def rea_command():
    exe = shutil.which("rea")
    return [exe] if exe else ["npx", "-y", "rea-agents@6.4.0"]


def qt(cmd, duration_ms=3000):
    """Run one qt_bridge.js command (list/click) inside RevoMetro; returns the script payload."""
    pid = revo_pid()
    if not pid:
        raise RuntimeError("RevoMetro.exe 未运行。")
    with open(QT_BRIDGE_JS, "r", encoding="utf-8") as f:
        bridge = f.read()
    fd, script = tempfile.mkstemp(suffix=".js", prefix="revo_qt_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("const CMD = " + json.dumps(cmd) + ";\n" + bridge)
    try:
        proc = subprocess.run(rea_command() + ["instrument-with-frida", "attach", str(pid), "--script-file", script,
                                               "--duration-ms", str(duration_ms), "--format", "json"],
                              capture_output=True, text=True, errors="replace", timeout=120)
    finally:
        os.remove(script)
    try:
        result = json.loads(proc.stdout)
        messages = result["evidence"]["normalized_result"]["messages"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"Frida 注入失败: {proc.stdout[-800:]} {proc.stderr[-800:]}")
    for m in messages:
        if m.get("type") == "error":
            raise RuntimeError(f"qt_bridge.js 出错: {m.get('description') or m}")
    payloads = [m["payload"] for m in messages if "payload" in m]
    if not payloads:
        raise RuntimeError("qt_bridge.js 未返回结果。")
    return payloads[0]


def widgets(**flt):
    return qt(dict(op="list", **flt))["widgets"]


def click(**flt):
    res = qt(dict(op="click", **flt))
    if not res.get("ok"):
        raise RuntimeError(f"点击失败 {flt}: {res.get('error')} {res.get('candidates', '')}")
    return res


def invoke(method, arg_type=None, value=None, **flt):
    """Queue a Qt slot call (e.g. setText/setChecked/setCurrentIndex/setValue) on one matching widget."""
    cmd = dict(op="invoke", method=method, **flt)
    if arg_type:
        cmd["arg"] = {"type": arg_type, "value": value}
    res = qt(cmd)
    if not res.get("ok"):
        raise RuntimeError(f"调用 {method} 失败 {flt}: {res.get('error')} {res.get('candidates', '')}")
    return res


def mouse_click(rect, double=False):
    """Real mouse click at the centre of a widget's logical global rect [x, y, w, h].

    Brings RevoMetro to the foreground first; Qt logical pixels are scaled by the window DPI."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        user32.SetProcessDPIAware()
    pid = revo_pid()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(enum, 0)
    if not found:
        raise RuntimeError("找不到 RevoMetro 窗口。")
    hwnd = found[0]  # topmost visible window of the process (dialogs come first in z-order)
    scale = user32.GetDpiForWindow(hwnd) / 96.0
    user32.keybd_event(0x12, 0, 0, 0)  # ALT tap lets SetForegroundWindow succeed from a background process
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    x = int((rect[0] + rect[2] / 2) * scale)
    y = int((rect[1] + rect[3] / 2) * scale)
    user32.SetCursorPos(x, y)
    for _ in range(2 if double else 1):
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        time.sleep(0.08)
    return {"x": x, "y": y, "scale": scale}


def visible(name=None, text=None, cls=None):
    return [w for w in widgets() if (name is None or w["name"] == name)
            and (text is None or w["text"].strip() == text) and (cls is None or w["cls"] == cls)]


# ---------------------------------------------------------------- RevoMetro operations
def ensure_running(timeout=90):
    if revo_pid():
        return
    # explorer.exe launches it outside this process tree, so it survives job-object cleanup
    subprocess.run(["explorer.exe", REVO_EXE])
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if revo_pid():
            try:
                if visible(text="New Project") or visible(text="Home", cls="QToolButton"):
                    return
            except RuntimeError:
                pass
    raise TimeoutError("RevoMetro 启动超时。")


def resolve_revo(project):
    if project and os.path.isfile(project) and project.lower().endswith(".revo"):
        return os.path.abspath(project)
    if project:
        hits = glob.glob(os.path.join(PROJECTS_DIR, project, "*.revo"))
        if not hits:
            raise FileNotFoundError(f"未找到工程 {project} (目录 {PROJECTS_DIR})")
        return hits[0]
    # latest user project by .revo mtime
    cands = [p for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.revo"))
             if os.path.basename(os.path.dirname(p)) not in ("GlobalMarkerLibrary", "Project_Example")]
    if not cands:
        raise FileNotFoundError("没有可用的扫描工程。")
    return max(cands, key=os.path.getmtime)


def read_nodes(revo_path):
    with open(revo_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    proj_dir = os.path.dirname(revo_path)
    nodes = []
    for n in meta.get("nodes", []):
        node_dir = os.path.join(proj_dir, "data", n["guid"])
        prop = {}
        try:
            with open(os.path.join(node_dir, "property.rvproj"), "r", encoding="utf-8") as f:
                prop = json.load(f)
        except (OSError, ValueError):
            pass
        mesh = os.path.join(node_dir, prop["mesh_file"]) if prop.get("mesh_file") else None
        nodes.append({"name": n["name"], "guid": n["guid"], "dir": node_dir,
                      "mesh": mesh if mesh and os.path.isfile(mesh) else None,
                      "point_pitch": prop.get("point_pitch"), "device": prop.get("scan_device")})
    return meta.get("name"), nodes


def open_project(revo_path, timeout=120):
    """Open a project in the running instance via the single-instance forwarding channel."""
    ensure_running()
    if visible(text="Home", cls="QToolButton"):
        click(text="Home", cls="QToolButton")
        time.sleep(3)
    cur = LogCursor()
    try:
        # Same bytes a second RevoMetro instance sends: the UTF-8 path, written to the QLocalServer pipe
        with open(IPC_PIPE, "wb") as pipe:
            pipe.write(revo_path.encode("utf-8"))
    except OSError:
        subprocess.run([REVO_EXE, revo_path], timeout=60)
    cur.wait_for(r"openDocument: " + re.escape(revo_path), timeout)
    cur.wait_for(r"\[scan document\] model loading time", timeout)
    time.sleep(2)


def one_click_edit(timeout=1800):
    """Press One-click Edit > Apply on the active node; returns the saved fuse_mesh.ply path."""
    if not visible(name="editPage", cls="QuickEditPage"):
        click(name="fastProcessFuncBtn", cls="SwitchButton")
        time.sleep(2)
    apply_btn = visible(text="Apply", cls="RvDefaultButton")
    if not apply_btn or not apply_btn[0]["enabled"]:
        raise RuntimeError("One-click Edit 的 Apply 按钮不可用 (该节点可能没有原始帧数据)。")
    cur = LogCursor()
    click(text="Apply", cls="RvDefaultButton")
    cur.wait_for(r"\[QuickEdit\] one-click edit begin", 60, FAIL_PATTERNS)
    cur.wait_for(r"\[QuickEdit\] one-click edit end", timeout, FAIL_PATTERNS)
    m, _ = cur.wait_for(r"mesh change, save to : (.+fuse_mesh\.ply)", 300, FAIL_PATTERNS)
    path = os.path.normpath(m.group(1).strip())
    deadline = time.time() + 120
    while time.time() < deadline:  # wait until the PLY write settles
        if os.path.isfile(path):
            size = os.path.getsize(path)
            time.sleep(2)
            if size > 0 and size == os.path.getsize(path):
                return path
        time.sleep(1)
    raise TimeoutError(f"网格文件未写出: {path}")


# ---------------------------------------------------------------- downstream stages
def run_scan_to_cad(mesh_path, name, out_dir, remove_base):
    cmd = [sys.executable, SCAN_TO_CAD, "--input", mesh_path, "--name", name, "--out-dir", out_dir]
    if remove_base:
        cmd.append("--remove-base")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    report = os.path.join(out_dir, f"{name}_dimensions.json")
    if proc.returncode != 0 or not os.path.isfile(report):
        raise RuntimeError(f"scan_to_cad 失败:\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")
    with open(report, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_widgets(args):
    ws = widgets(name=args.name, text=args.text, cls=args.cls, win=args.win, visibleOnly=not args.all)
    for w in ws:
        print(f"{w['win']:22} {w['name'] or '-':32} {w['cls']:28} en={int(w['enabled'])} {w['text'][:60]}")
    print(f"# {len(ws)} widgets")


def cmd_click(args):
    print(json.dumps(click(name=args.name, text=args.text, cls=args.cls, win=args.win), ensure_ascii=False, indent=2))


def cmd_invoke(args):
    value = args.value
    if args.arg_type == "int":
        value = int(value)
    elif args.arg_type == "double":
        value = float(value)
    elif args.arg_type == "bool":
        value = value.lower() in ("1", "true", "yes", "on")
    print(json.dumps(invoke(args.method, args.arg_type, value, name=args.name, text=args.text, cls=args.cls,
                            win=args.win), ensure_ascii=False, indent=2))


def cmd_actions(args):
    for a in qt(dict(op="actions", text=args.text))["actions"]:
        print(f"{a['owner']:28} en={int(a['enabled'])} chk={int(a['checked'])} {a['text']}"
              + (f"  -> {a['submenu']}" if a['submenu'] else ""))


def trigger(text, owner=None):
    res = qt(dict(op="trigger", text=text, owner=owner))
    if not res.get("ok"):
        raise RuntimeError(f"触发菜单项失败 {text}: {res.get('error')} {res.get('candidates', '')}")
    return res


def cmd_trigger(args):
    print(json.dumps(trigger(args.text, args.owner), ensure_ascii=False, indent=2))


def cmd_mclick(args):
    ws = [w for w in widgets(name=args.name, text=args.text, cls=args.cls, win=args.win)]
    if len(ws) != 1:
        raise RuntimeError(f"需要唯一匹配的控件，实际 {len(ws)} 个")
    print(json.dumps(mouse_click(ws[0]["rect"], args.double), ensure_ascii=False))


def cmd_open(args):
    revo = resolve_revo(args.project)
    open_project(revo)
    print(f"已打开: {revo}")


def cmd_process(args):
    print(json.dumps(process_project(args), ensure_ascii=False, indent=2))


def process_project(args):
    revo = resolve_revo(args.project)
    proj_name, nodes = read_nodes(revo)
    active = nodes[-1]  # RevoMetro loads the most recently added scan node on open
    if active["mesh"] and not args.force:
        return {"project": revo, "node": active["name"], "mesh": active["mesh"], "skipped": "已有网格 (用 --force 重新处理)"}
    open_project(revo)
    t0 = time.time()
    mesh = one_click_edit()
    node = next((n for n in nodes if os.path.normcase(n["dir"]) in os.path.normcase(mesh)), active)
    return {"project": revo, "project_name": proj_name, "node": node["name"], "mesh": mesh,
            "process_seconds": round(time.time() - t0, 1)}


def cmd_auto(args):
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
    proc = process_project(args)
    report["revo"] = proc
    name = f"{proc.get('project_name') or os.path.basename(os.path.dirname(proc['project']))}_{proc['node']}"
    out_dir = os.path.join(args.out_dir, name)
    os.makedirs(out_dir, exist_ok=True)
    cad = run_scan_to_cad(proc["mesh"], name, out_dir, args.remove_base)
    report["cad"] = cad["outputs"]
    bbox = cad["features"]["bounding_box_mm"]
    report["dimensions_mm"] = {k: bbox[k] for k in ("length_x", "width_y", "height_z")}
    report["detected_holes"] = len(cad["features"].get("detected_holes") or [])
    report["cleaning"] = cad["cleaning_stats"]
    if args.printer:
        src = cad["outputs"]["cad_solids"]["step"] if args.print_source == "cad" else cad["outputs"]["cleaned_meshes"]["stl"]
        report["print"] = []
        for printer in args.printer:
            res = slice_print.slice_model(src, printer, os.path.join(out_dir, "print"), process=args.process,
                                          filament=args.filament)
            res.pop("command", None)
            report["print"].append(res)
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = os.path.join(out_dir, f"{name}_pipeline_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告: {path}")


def cmd_watch(args):
    """Wait for a scan session to finish saving, then run the full pipeline on that project."""
    cur = LogCursor()
    print("等待 RevoMetro 扫描结束 (日志 'Processor saveTo out')… Ctrl+C 退出")
    while True:
        _, lines = cur.wait_for(r"Processor saveTo out", 10 ** 9)
        project = None
        for line in reversed(lines):
            m = re.search(r"openProject: (.+\.revo)", line)
            if m and "GlobalMarkerLibrary" not in m.group(1):
                project = os.path.normpath(m.group(1).strip())
                break
        args.project = project or args.project
        args.force = True
        print(f"检测到扫描保存，开始处理: {args.project or '最新工程'}")
        try:
            cmd_auto(args)
        except Exception as exc:  # keep watching after a failed run
            print(f"流水线失败: {exc}")
        if args.once:
            return


def main():
    parser = argparse.ArgumentParser(description="RevoMetro 扫描 → 图纸 → 拓竹切片 自动化")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("widgets", help="列出 RevoMetro 当前 Qt 控件 (所属窗口类/objectName/类/文字/可用状态)")
    p.add_argument("--name"); p.add_argument("--text"); p.add_argument("--cls"); p.add_argument("--win", help="所属顶层窗口类名")
    p.add_argument("--all", action="store_true", help="包含不可见控件")
    p.set_defaults(func=cmd_widgets)

    p = sub.add_parser("click", help="在 GUI 线程上点击一个按钮 (按 objectName 或文字定位，须唯一)")
    p.add_argument("--name"); p.add_argument("--text"); p.add_argument("--cls"); p.add_argument("--win", help="所属顶层窗口类名")
    p.set_defaults(func=cmd_click)

    p = sub.add_parser("invoke", help="在 GUI 线程上调用控件槽函数，如 setText / setChecked / setCurrentIndex / setValue")
    p.add_argument("--name"); p.add_argument("--text"); p.add_argument("--cls"); p.add_argument("--win")
    p.add_argument("--method", required=True)
    p.add_argument("--arg-type", choices=["QString", "int", "bool", "double"])
    p.add_argument("--value")
    p.set_defaults(func=cmd_invoke)

    p = sub.add_parser("mclick", help="真实鼠标点击控件中心 (用于非按钮的自定义控件，如列表条目)")
    p.add_argument("--name"); p.add_argument("--text"); p.add_argument("--cls"); p.add_argument("--win")
    p.add_argument("--double", action="store_true")
    p.set_defaults(func=cmd_mclick)

    p = sub.add_parser("actions", help="列出菜单/工具栏中的 QAction (菜单项)")
    p.add_argument("--text")
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("trigger", help="在 GUI 线程上触发一个菜单项 (QAction::trigger)")
    p.add_argument("--text", required=True); p.add_argument("--owner", help="所属菜单/工具栏 objectName 或类名")
    p.set_defaults(func=cmd_trigger)

    p = sub.add_parser("open", help="在运行中的 RevoMetro 打开工程")
    p.add_argument("--project", help="工程名或 .revo 路径 (默认最新工程)")
    p.set_defaults(func=cmd_open)

    def add_pipeline_args(p):
        p.add_argument("--project", help="工程名或 .revo 路径 (默认最新工程)")
        p.add_argument("--force", action="store_true", help="节点已有网格时仍重新一键处理")

    p = sub.add_parser("process", help="一键融合+网格 (One-click Edit)，输出 fuse_mesh.ply")
    add_pipeline_args(p)
    p.set_defaults(func=cmd_process)

    for name, func, hlp in (("auto", cmd_auto, "一键处理 → 3D 图纸 → 拓竹切片"),
                            ("watch", cmd_watch, "监听扫描结束后自动执行 auto")):
        p = sub.add_parser(name, help=hlp)
        add_pipeline_args(p)
        p.add_argument("--out-dir", default=DEFAULT_OUT)
        p.add_argument("--remove-base", action="store_true", help="RANSAC 去除桌面/转盘平面")
        p.add_argument("--printer", nargs="*", choices=sorted(slice_print.PRINTERS), default=["h2d"],
                       help="切片目标打印机，可多选；留空则跳过切片")
        p.add_argument("--print-source", choices=["mesh", "cad"], default="mesh",
                       help="mesh=清洗后的扫描网格 STL (默认)；cad=逆向 STEP 实体")
        p.add_argument("--process", help="覆盖工艺预设名")
        p.add_argument("--filament", help="覆盖耗材预设名")
        if name == "watch":
            p.add_argument("--once", action="store_true", help="处理一次后退出")
        p.set_defaults(func=func)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
