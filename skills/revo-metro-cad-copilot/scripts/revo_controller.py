#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Revo Metro (RevoMetro.exe) AI Copilot & GUI/Config/Log Controller
=================================================================
Controls `C:\\Program Files\\Revo Metro\\RevoMetro.exe`, inspects & modifies
`revoscan.cfg` and `.revo` project files, monitors `rvScan5.log` in real time
for AI-assisted scanning diagnostics, captures live screenshots, and sends
Win32 keyboard shortcuts & mouse actions.
"""

import argparse
import ctypes
from ctypes import wintypes
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

# Default paths on Windows
REVO_EXE = r"C:\Program Files\Revo Metro\RevoMetro.exe"
APPDATA_ROAMING = os.path.expandvars(r"%APPDATA%\RevoMetro")
APPDATA_LOCAL = os.path.expandvars(r"%LOCALAPPDATA%\RevoMetro")
PROJECTS_DIR = os.path.join(APPDATA_ROAMING, "Projects")
CFG_PATH = os.path.join(APPDATA_LOCAL, "config", "revoscan.cfg")
LOG_PATH = os.path.join(APPDATA_LOCAL, "logs", "rvScan5.log")
PERF_PATH = os.path.join(APPDATA_LOCAL, "logs", "perftoolrecord.txt")
TEMP_MODEL_PNG = os.path.expandvars(r"%TEMP%\rv_model.png")

# Win32 Constants & Key Codes
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SW_RESTORE = 9
SW_SHOW = 5
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

VK_MAP = {
    "SPACE": 0x20,
    "ESC": 0x1B,
    "DELETE": 0x2E,
    "BACKSPACE": 0x08,
    "ENTER": 0x0D,
    "CTRL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,
    "A": 0x41,
    "D": 0x44,
    "I": 0x49,
    "L": 0x4C,
    "M": 0x4D,
    "N": 0x4E,
    "O": 0x4F,
    "S": 0x53,
    "T": 0x54,
    "Y": 0x59,
    "Z": 0x5A,
}

# Built-in Revo Metro shortcut combos
SHORTCUT_ACTIONS = {
    "start_pause": ["SPACE"],               # 开始/暂停扫描
    "cancel": ["ESC"],                      # 取消扫描/取消选择
    "new_project": ["CTRL", "N"],           # 新建工程
    "open_project": ["CTRL", "O"],          # 打开工程
    "import_model": ["CTRL", "I"],          # 导入模型 (.revo/.ply/.obj/.stl)
    "save": ["CTRL", "S"],                  # 保存工程
    "select_all": ["CTRL", "A"],            # 全选
    "deselect": ["CTRL", "D"],              # 取消选择
    "invert_selection": ["CTRL", "SHIFT", "I"],  # 反选
    "delete_invert": ["CTRL", "SHIFT", "DELETE"],  # 反选删除 (一键删外围标志块/杂点)
    "delete": ["DELETE"],                   # 删除选中点云/网格
    "undo": ["CTRL", "Z"],                  # 撤销
    "redo": ["CTRL", "Y"],                  # 重做
    "toggle_light": ["CTRL", "L"],          # 切换光照方向
    "fullscreen_scan": ["SHIFT", "M"],      # 扫描预览全屏切换
    "model_screenshot": ["CTRL", "SHIFT", "M"],  # 模型截屏
    "full_screenshot": ["CTRL", "ALT", "A"],     # 全屏截图
}


def find_revo_windows():
    """Find top-level visible windows belonging to RevoMetro.exe."""
    windows = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def enum_cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if any(k in title for k in ["Revo Metro", "Revo Scan", "Revopoint"]):
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    windows.append({
                        "hwnd": hwnd,
                        "pid": pid.value,
                        "title": title,
                        "rect": {
                            "left": rect.left,
                            "top": rect.top,
                            "right": rect.right,
                            "bottom": rect.bottom,
                            "width": rect.right - rect.left,
                            "height": rect.bottom - rect.top,
                        }
                    })
        return True

    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    return windows


def focus_revo_window():
    """Bring the main Revo Metro window to foreground and return its info."""
    wins = find_revo_windows()
    if not wins:
        return None
    # Pick the largest window
    win = max(wins, key=lambda w: w["rect"]["width"] * w["rect"]["height"])
    hwnd = win["hwnd"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    # Refresh rect
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    win["rect"] = {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }
    return win


def send_key_combo(keys):
    """Send a keyboard shortcut combo via Win32 keybd_event."""
    vks = [VK_MAP[k.upper()] for k in keys]
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)


def click_window_ratio(rx, ry, double=False):
    """Click at normalized coordinates (rx, ry) in [0.0, 1.0] inside Revo Metro window."""
    win = focus_revo_window()
    if not win:
        raise RuntimeError("RevoMetro.exe 窗口未找到，请先运行 launch 命令启动程序。")
    r = win["rect"]
    x = int(r["left"] + r["width"] * float(rx))
    y = int(r["top"] + r["height"] * float(ry))
    user32.SetCursorPos(x, y)
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double:
        time.sleep(0.08)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return {"clicked_screen_xy": [x, y], "window_rect": r}


def parse_projects():
    """Inspect all .revo projects in %APPDATA%\\RevoMetro\\Projects."""
    projects = []
    if not os.path.isdir(PROJECTS_DIR):
        return projects
    for revo_file in sorted(glob.glob(os.path.join(PROJECTS_DIR, "*", "*.revo"))):
        proj_dir = os.path.dirname(revo_file)
        proj_name = os.path.basename(proj_dir)
        if proj_name == "GlobalMarkerLibrary":
            continue
        try:
            with open(revo_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            meta = {"error": str(e)}

        nodes_info = []
        for node in meta.get("nodes", []):
            guid = node.get("guid", "")
            node_dir = os.path.join(proj_dir, "data", guid)
            prop_path = os.path.join(node_dir, "property.rvproj")
            prop = {}
            if os.path.isfile(prop_path):
                try:
                    with open(prop_path, "r", encoding="utf-8") as pf:
                        prop = json.load(pf)
                except Exception:
                    pass
            ply_files = glob.glob(os.path.join(node_dir, "*.ply")) + glob.glob(os.path.join(node_dir, "*.stl")) + glob.glob(os.path.join(node_dir, "*.obj"))
            nodes_info.append({
                "name": node.get("name"),
                "guid": guid,
                "node_dir": node_dir,
                "vf_count": prop.get("vf_count", 0),
                "point_count": prop.get("point_count", 0),
                "face_count": prop.get("face_count", 0),
                "recommend_point_pitch_mm": round(prop.get("recommend_point_pitch", 0.0), 4),
                "scan_device": prop.get("scan_device", ""),
                "scan_mode": prop.get("scan_param", {}).get("scan_mode"),
                "scan_pattern": prop.get("scan_param", {}).get("scan_pattern"),
                "scan_object": prop.get("scan_param", {}).get("scan_object"),
                "accuracy_type": prop.get("scan_param", {}).get("accuracy_type"),
                "files": [os.path.basename(p) for p in ply_files],
                "full_paths": ply_files,
            })

        projects.append({
            "project_name": meta.get("name", proj_name),
            "revo_file": revo_file,
            "project_dir": proj_dir,
            "size_mb": round(int(meta.get("size", 0)) / (1024 * 1024), 2),
            "scan_scene": meta.get("scan_scene", {}),
            "nodes": nodes_info,
        })
    return projects


def analyze_log(tail_lines=500):
    """Parse the tail of rvScan5.log to extract real-time state, scanner info, and diagnostics."""
    result = {
        "log_exists": os.path.isfile(LOG_PATH),
        "scanner_online": False,
        "scanner_name": None,
        "scanner_serial": None,
        "firmware_version": None,
        "work_mode": "Unknown (0=Home, 1=Scan, 2=Edit)",
        "scan_state": "Unknown (0=Idle, 1=Preview, 2=Scanning, 3=Paused)",
        "last_project": None,
        "recommend_density_mm": None,
        "bounding_box": None,
        "recent_tracking_ok_count": 0,
        "recent_insufficient_markers_count": 0,
        "recent_feature_loss_count": 0,
        "camera_disconnect_errors": 0,
        "diagnostics_zh": [],
    }
    if not result["log_exists"]:
        return result

    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
        # Read last ~256 KB
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 262144), os.SEEK_SET)
        lines = f.readlines()[-tail_lines:]

    for line in lines:
        if "Scanner online," in line:
            result["scanner_online"] = True
            for part in line.split(","):
                if "name=" in part:
                    result["scanner_name"] = part.split("name=")[-1].strip()
                elif "serial=" in part:
                    result["scanner_serial"] = part.split("serial=")[-1].strip()
                elif "firmwareVersion=" in part:
                    result["firmware_version"] = part.split("firmwareVersion=")[-1].strip()
        elif "slotScannerDisconnect" in line:
            result["scanner_online"] = False
        elif "setWorkMode:" in line:
            mode_num = line.split("setWorkMode:")[-1].strip()
            mode_map = {"0": "0 (Home 主页)", "1": "1 (ScanPanel 扫描界面)", "2": "2 (Edit 模型后处理界面)"}
            result["work_mode"] = mode_map.get(mode_num, mode_num)
        elif "ScanManager::switchState, from" in line and "end" in line:
            # e.g. from 1 to 2 end
            parts = line.split("to")
            if len(parts) > 1:
                st = parts[-1].replace("end", "").strip()
                st_map = {
                    "0": "0 (Idle 未扫描)",
                    "1": "1 (Preview 预览就绪)",
                    "2": "2 (Scanning 正在扫描采集)",
                    "3": "3 (Paused 已暂停)",
                }
                result["scan_state"] = st_map.get(st, st)
        elif "openProject:" in line or "newProject, path:" in line:
            result["last_project"] = line.split(":", 3)[-1].strip()
        elif "get Recommend Density is" in line:
            try:
                result["recommend_density_mm"] = float(line.split("get Recommend Density is")[-1].strip())
            except ValueError:
                pass
        elif "adjust mesh plane, boundingBox center:" in line:
            result["bounding_box"] = line.split("adjust mesh plane,")[-1].strip()
        elif "value 0x10001" in line:
            result["recent_tracking_ok_count"] += 1
        elif "value 0x10002" in line or "getLastRegState is 6" in line:
            result["recent_insufficient_markers_count"] += 1
        elif "value 0x10006" in line or "getLastRegState is 10" in line:
            result["recent_feature_loss_count"] += 1
        elif "Get frame data from Scanner failed" in line:
            result["camera_disconnect_errors"] += 1

    # Generate actionable AI Copilot advice in Chinese
    if result["recent_insufficient_markers_count"] > 5:
        result["diagnostics_zh"].append(
            f"⚠️ 检测到近期出现 {result['recent_insufficient_markers_count']} 次『标志点不足 (getLastRegState=6 / 0x10002)』！"
            "建议：(1) 确保单帧视野内至少有 5 个 6mm 反光标志点（间距 6~7cm）；"
            "(2) 在工件外围或孔洞内补充磁力球/半球标志块；"
            "(3) 将扫描仪水平持握扫边缘，或在软件中将精度模式切换为『High Speed（高速度模式，支持 28°~30° 极限识点角）』；"
            "(4) 调高深度曝光（Depth Exposure）使预览画面中的标志点清晰可见。"
        )
    if result["recent_feature_loss_count"] > 3:
        result["diagnostics_zh"].append(
            f"⚠️ 检测到近期出现 {result['recent_feature_loss_count']} 次『特征追踪丢失 (getLastRegState=10 / 0x10006)』！"
            "建议：若在全场结构光（Full-field）模式下扫描对称件或薄边件，请在工件旁放置 2~3 个不规则参考小物件辅助特征锚定，或改用激光标志点模式。"
        )
    if result["recommend_density_mm"]:
        result["diagnostics_zh"].append(
            f"💡 最近一次扫描推荐融合点距 (Recommend Point Pitch) 为 {result['recommend_density_mm']:.2f} mm。"
            "若需正反面拼接 (Merge)，请确保正反两个扫描节点使用完全一致的融合点距（如统一设为 0.30 mm 或 0.45 mm）。"
        )
    if not result["diagnostics_zh"]:
        result["diagnostics_zh"].append("✅ 日志状态正常，未发现频繁丢帧或拼帧错误。")

    return result


def cmd_status(_args):
    """Output full system, process, config, project, and log status as JSON."""
    wins = find_revo_windows()
    cfg = {}
    if os.path.isfile(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            cfg = {"error": str(e)}

    status = {
        "revo_exe_exists": os.path.isfile(REVO_EXE),
        "revo_exe_path": REVO_EXE,
        "running_windows": wins,
        "gpu_configured": cfg.get("common", {}).get("curSelectionGPUName"),
        "device_type": cfg.get("common", {}).get("environmentCheckDeviceType"),
        "software_version": cfg.get("common", {}).get("revoscanVersion"),
        "turntable_settings": cfg.get("parts", {}).get("twoAxisTurntable", {}),
        "edit_settings": cfg.get("edit", {}),
        "log_analysis": analyze_log(),
        "projects": parse_projects(),
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))


def cmd_launch(args):
    """Launch RevoMetro.exe (optionally with a project or model path) and wait for window."""
    if not os.path.isfile(REVO_EXE):
        raise FileNotFoundError(f"未找到 RevoMetro.exe: {REVO_EXE}")

    wins = find_revo_windows()
    if wins and not args.force_new:
        win = focus_revo_window()
        print(json.dumps({"status": "already_running", "window": win}, ensure_ascii=False, indent=2))
        return

    cmd = [REVO_EXE]
    if args.project:
        cmd.append(os.path.abspath(args.project))

    proc = subprocess.Popen(cmd, cwd=os.path.dirname(REVO_EXE))
    for _ in range(30):
        time.sleep(0.5)
        win = focus_revo_window()
        if win:
            print(json.dumps({"status": "launched", "pid": proc.pid, "window": win}, ensure_ascii=False, indent=2))
            return

    print(json.dumps({"status": "launched_waiting_ui", "pid": proc.pid}, ensure_ascii=False, indent=2))


def cmd_configure(args):
    """Modify revoscan.cfg and/or a target .revo project's scan_scene parameters."""
    changes = {}
    if os.path.isfile(CFG_PATH):
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {"common": {}, "edit": {}, "parts": {}}

    # Update point cloud fusion pitch if requested
    if args.fuse_pitch is not None:
        cfg.setdefault("edit", {}).setdefault("pointCloud", {}).setdefault("fuse", {}).setdefault("spacing", {})["current"] = float(args.fuse_pitch)
        changes["fuse_pitch_mm"] = float(args.fuse_pitch)

    # Update dual-axis turntable settings (Golden Rule: Rotation 1 angle = 0)
    if any(v is not None for v in [args.turntable_speed, args.turntable_step, args.turntable_angles]):
        tt = cfg.setdefault("parts", {}).setdefault("twoAxisTurntable", {})
        if args.turntable_speed is not None:
            tt["turnSpeed"] = int(args.turntable_speed)
            changes["turntable_speed_sec"] = int(args.turntable_speed)
        if args.turntable_step is not None:
            tt["stepAngle"] = int(args.turntable_step)
            changes["turntable_step_deg"] = int(args.turntable_step)
        if args.turntable_angles:
            angles = [int(x.strip()) for x in args.turntable_angles.split(",") if x.strip()]
            # Enforce Golden Rule: Rotation 1 should be 0 deg unless overridden
            angle_dict = {"1": 0, "2": None, "3": None, "4": None, "5": None}
            for i, a in enumerate(angles[:5], start=1):
                angle_dict[str(i)] = a
            tt["angle"] = angle_dict
            changes["turntable_angles"] = angle_dict

    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

    # Optionally update a .revo project's scan_scene
    if args.project_revo and os.path.isfile(args.project_revo):
        with open(args.project_revo, "r", encoding="utf-8") as f:
            revo_data = json.load(f)
        scene = revo_data.setdefault("scan_scene", {})
        if args.scan_mode is not None:
            # 0 = Full-field / Feature, 1 = Laser Marker
            scene["scanMode"] = str(args.scan_mode)
            changes["project_scanMode"] = str(args.scan_mode)
        if args.scan_object is not None:
            # 0 = General, 1 = Black, 2 = Metallic Shiny
            scene["scanObject"] = str(args.scan_object)
            changes["project_scanObject"] = str(args.scan_object)
        if args.depth_exposure is not None:
            if args.depth_exposure == 0:
                scene["depthCamAutoEx"] = "1"
            else:
                scene["depthCamAutoEx"] = "0"
                scene["depthCamExValue000"] = str(args.depth_exposure)
            changes["project_depthExposure"] = args.depth_exposure
        with open(args.project_revo, "w", encoding="utf-8") as f:
            json.dump(revo_data, f, indent=4, ensure_ascii=False)

    print(json.dumps({"status": "configured", "changes": changes}, ensure_ascii=False, indent=2))


def cmd_action(args):
    """Execute a GUI shortcut action or normalized window click on RevoMetro.exe."""
    win = focus_revo_window()
    if not win:
        raise RuntimeError("未检测到正在运行的 RevoMetro.exe 窗口，请先运行 launch 命令。")

    if args.command in SHORTCUT_ACTIONS:
        keys = SHORTCUT_ACTIONS[args.command]
        send_key_combo(keys)
        print(json.dumps({
            "status": "shortcut_sent",
            "action": args.command,
            "keys": keys,
            "window": win["title"]
        }, ensure_ascii=False, indent=2))
    elif args.command == "click_ratio":
        if args.rx is None or args.ry is None:
            raise ValueError("click_ratio 需要提供 --rx 与 --ry 参数 (0.0 ~ 1.0)")
        res = click_window_ratio(args.rx, args.ry, double=args.double)
        print(json.dumps({"status": "clicked", **res}, ensure_ascii=False, indent=2))
    else:
        raise ValueError(f"未知操作: {args.command}。支持的操作: {list(SHORTCUT_ACTIONS.keys()) + ['click_ratio']}")


def cmd_screenshot(args):
    """Capture the RevoMetro.exe window using PowerShell/System.Drawing or copy rv_model.png."""
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    win = focus_revo_window()
    if win:
        r = win["rect"]
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            f"$bmp = New-Object System.Drawing.Bitmap({r['width']}, {r['height']}); "
            "$g = [System.Drawing.Graphics]::FromImage($bmp); "
            f"$g.CopyFromScreen({r['left']}, {r['top']}, 0, 0, $bmp.Size); "
            f"$bmp.Save('{out_path}'); $g.Dispose(); $bmp.Dispose();"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True)
        print(json.dumps({
            "status": "window_captured",
            "path": out_path,
            "window_rect": r,
            "temp_model_png": TEMP_MODEL_PNG if os.path.isfile(TEMP_MODEL_PNG) else None
        }, ensure_ascii=False, indent=2))
    elif os.path.isfile(TEMP_MODEL_PNG):
        shutil.copy2(TEMP_MODEL_PNG, out_path)
        print(json.dumps({
            "status": "copied_cached_rv_model_png",
            "path": out_path
        }, ensure_ascii=False, indent=2))
    else:
        raise RuntimeError("RevoMetro.exe 未运行且未找到缓存截图。")


def cmd_diagnose(args):
    """Monitor rvScan5.log for N seconds (or snapshot immediately) and output AI Copilot advice."""
    if args.watch > 0:
        time.sleep(args.watch)
    analysis = analyze_log(tail_lines=800)
    print(json.dumps(analysis, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Revo Metro (RevoMetro.exe) AI Controller")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("status", help="查看 RevoMetro 运行状态、硬件连接、工程列表与日志诊断")

    p_launch = sub.add_parser("launch", help="启动或唤醒 RevoMetro.exe")
    p_launch.add_argument("--project", help="启动时直接打开的 .revo 工程或 .ply/.stl/.obj 模型路径")
    p_launch.add_argument("--force-new", action="store_true", help="强制启动新进程")

    p_cfg = sub.add_parser("configure", help="预配置 revoscan.cfg 及 .revo 工程扫描参数")
    p_cfg.add_argument("--fuse-pitch", type=float, help="设置点云融合点距 (mm)，如 0.30")
    p_cfg.add_argument("--turntable-speed", type=int, help="双轴转盘单圈耗时 (35~90s)，推荐 70")
    p_cfg.add_argument("--turntable-step", type=int, help="自动转盘单帧间隔角度 (5~30°)，推荐 15")
    p_cfg.add_argument("--turntable-angles", help="多圈俯仰角列表 (第1圈固定建议0°)，如 '0,15,-15,25,-25'")
    p_cfg.add_argument("--project-revo", help="要修改的 .revo 工程文件路径")
    p_cfg.add_argument("--scan-mode", type=int, choices=[0, 1], help="0=全场结构光, 1=蓝色激光模式")
    p_cfg.add_argument("--scan-object", type=int, choices=[0, 1, 2], help="0=普通材质, 1=黑色材质, 2=金属反光材质")
    p_cfg.add_argument("--depth-exposure", type=int, help="深度相机曝光 (0=自动曝光, 1~10=手动曝光档位)")

    p_act = sub.add_parser("action", help="向 RevoMetro 窗口发送快捷键或点击指令")
    p_act.add_argument("command", help=f"支持指令: {', '.join(list(SHORTCUT_ACTIONS.keys()) + ['click_ratio'])}")
    p_act.add_argument("--rx", type=float, help="窗口内横向相对坐标 (0.0~1.0)")
    p_act.add_argument("--ry", type=float, help="窗口内纵向相对坐标 (0.0~1.0)")
    p_act.add_argument("--double", action="store_true", help="是否双击")

    p_shot = sub.add_parser("screenshot", help="截取 RevoMetro 实时画面供 AI 视觉诊断")
    p_shot.add_argument("--out", default=r"d:\source\revopoint\output\revo_live.png", help="截图保存路径")

    p_diag = sub.add_parser("diagnose", help="实时监控 rvScan5.log 并生成中文 AI 领航诊断建议")
    p_diag.add_argument("--watch", type=float, default=0.0, help="持续采样等待秒数")

    args = parser.parse_args()
    if args.subcommand == "status":
        cmd_status(args)
    elif args.subcommand == "launch":
        cmd_launch(args)
    elif args.subcommand == "configure":
        cmd_configure(args)
    elif args.subcommand == "action":
        cmd_action(args)
    elif args.subcommand == "screenshot":
        cmd_screenshot(args)
    elif args.subcommand == "diagnose":
        cmd_diagnose(args)


if __name__ == "__main__":
    main()
