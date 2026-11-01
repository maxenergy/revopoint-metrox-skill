#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bambu Lab LAN control: status, camera, upload, print start, job control, watchdog
=================================================================================
Talks to the printer directly with the Python standard library, no cloud account or Bambu Studio:
  MQTT over TLS, port 8883 (user bblp, password = LAN access code)  status reports and commands
  implicit FTPS, port 990                                           file upload to the SD card
  JPEG stream over TLS, port 6000 (A / P series)                    camera frames

Starting a print needs Developer Mode on the printer; without it project_file is silently ignored and
HMS 0500-0400-0001-0007 appears. Status, camera and upload work either way.

The access code is a credential: pass it with --code or BAMBU_ACCESS_CODE and do not store it in this repository.
Printers present self-signed certificates ("BBL Device CA"), so TLS is used without verification.
"""

import argparse
import ftplib
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
import zipfile

MQTT_PORT = 8883
FTPS_PORT = 990
CAMERA_PORT = 6000
USER = "bblp"

REPORT_TIMEOUT = 10  # seconds to wait for a full status report
MAX_OFFLINE_POLLS = 10  # consecutive failed polls before watch gives up

ACTIVE_STATES = {"PREPARE", "RUNNING", "PAUSE"}
# "Pull out the external filament" prompts raised by an end-of-print unload (see slice_print.py --external-spool)
UNLOAD_PROMPT_ERRORS = {0x07FFC003, 0x07FF8003}


def tls_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _enc_str(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _enc_len(n):
    out = b""
    while True:
        n, digit = n // 128, n % 128
        out += bytes([digit | (0x80 if n else 0)])
        if not n:
            return out


def _packet(ptype, body):
    return bytes([ptype]) + _enc_len(len(body)) + body


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class PrinterMQTT:
    """Minimal MQTT 3.1.1 client: CONNECT, SUBSCRIBE to the report topic, PUBLISH to the request topic."""

    def __init__(self, ip, serial, code):
        self.request_topic = f"device/{serial}/request"
        self.sock = tls_context().wrap_socket(socket.create_connection((ip, MQTT_PORT), timeout=10))
        client_id = f"revo-skill-{int(time.time())}"
        self.sock.sendall(_packet(0x10, _enc_str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
                                  + _enc_str(client_id) + _enc_str(USER) + _enc_str(code)))
        first = self._recv_exact(1)
        ptype, data = first[0], self._read_body()
        if ptype >> 4 != 2 or data[1] != 0:
            self.close()
            sys.exit(f"MQTT 连接被拒绝，返回码 {data[1]} (5 = 访问码错误)")
        self.sock.sendall(_packet(0x82, struct.pack("!H", 1) + _enc_str(f"device/{serial}/report") + b"\x00"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.sock.close()

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("打印机关闭了连接")
            buf += chunk
        return buf

    def _read_body(self):
        mult, size = 1, 0
        while True:
            b = self._recv_exact(1)[0]
            size += (b & 0x7F) * mult
            mult *= 128
            if not b & 0x80:
                return self._recv_exact(size)

    def publish(self, payload):
        self.sock.sendall(_packet(0x30, _enc_str(self.request_topic) + json.dumps(payload).encode()))

    def messages(self, seconds):
        """Yield the `print` section of each report for up to `seconds`."""
        end = time.time() + seconds
        while time.time() < end:
            self.sock.settimeout(1)
            try:
                first = self.sock.recv(1)
            except socket.timeout:
                continue
            if not first:
                raise ConnectionError("打印机关闭了连接")
            self.sock.settimeout(10)  # the header byte is in, so a timeout now would lose the stream position
            body = self._read_body()
            if first[0] >> 4 != 3:
                continue
            topic_len = struct.unpack("!H", body[:2])[0]
            try:
                section = json.loads(body[2 + topic_len:]).get("print")
            except ValueError:
                continue
            if section:
                yield section

    def status(self):
        """Request and return a full status report."""
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
        state = {}
        for section in self.messages(REPORT_TIMEOUT):
            _merge(state, section)
            if "gcode_state" in state and "hms" in state:
                return state
        raise ConnectionError(f"{REPORT_TIMEOUT} 秒内没有收到完整状态报告")


def read_status(args):
    with PrinterMQTT(args.ip, args.serial, args.code) as mqtt:
        return mqtt.status()


def hms_codes(p):
    """HMS entries in the form shown on the printer screen, e.g. 0300-1100-0002-0001."""
    codes = []
    for entry in p.get("hms") or []:
        s = "%08X%08X" % (entry["attr"], entry["code"])
        codes.append("-".join(s[i:i + 4] for i in range(0, 16, 4)))
    return codes


def error_hex(p):
    return "%08X" % (p.get("print_error") or 0)


def last_layer_done(p):
    """True while a job is still open but has nothing left to print."""
    total = p.get("total_layer_num")
    return (p.get("gcode_state") in ACTIVE_STATES and bool(total) and p.get("layer_num") == total
            and not p.get("mc_remaining_time"))


def job_outcome(p, baseline_hms):
    """Why a watched job needs attention, or None while it runs normally."""
    state = p.get("gcode_state")
    error = p.get("print_error") or 0
    if state == "FINISH":
        return "finished"
    if error in UNLOAD_PROMPT_ERRORS and last_layer_done(p):
        return "finished_unload_prompt"
    if state not in ACTIVE_STATES:
        return "stopped"
    if state == "PAUSE":
        return "paused"
    if error:
        return "error"
    if set(hms_codes(p)) - baseline_hms:
        return "new_hms"
    return None


class ImplicitFTPS(ftplib.FTP_TLS):
    """Bambu printers speak implicit FTPS and require the data channel to resume the control TLS session."""

    def connect(self, host, port=FTPS_PORT, timeout=30):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        # the stock storbinary waits for a TLS close_notify the printer never sends; close the data socket directly
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
        finally:
            conn.close()
        return self.voidresp()


def remote_name(path):
    """Printers only list sliced projects named *.gcode.3mf."""
    base = os.path.basename(path)
    if base.endswith(".gcode.3mf"):
        return base
    if base.endswith(".3mf"):
        return base[:-len(".3mf")] + ".gcode.3mf"
    sys.exit(f"需要切片后的 .3mf 文件: {path}")


def upload_file(ip, code, path):
    remote = remote_name(path)
    ftp = ImplicitFTPS(context=tls_context())
    ftp.connect(ip)
    ftp.login(USER, code)
    ftp.prot_p()
    started = time.time()
    with open(path, "rb") as f:
        ftp.storbinary(f"STOR {remote}", f)
    size = ftp.size(remote)
    ftp.quit()
    if size != os.path.getsize(path):
        sys.exit(f"上传后大小不一致: 打印机 {size} 字节，本地 {os.path.getsize(path)} 字节")
    print(f"已上传 {remote}: {size} 字节，用时 {time.time() - started:.1f} 秒", flush=True)
    return remote


def plate_gcode_md5(path, plate):
    with zipfile.ZipFile(path) as z:
        try:
            return hashlib.md5(z.read(f"Metadata/plate_{plate}.gcode")).hexdigest()
        except KeyError:
            sys.exit(f"{path} 里没有第 {plate} 块板的 G-code，文件可能没有切片")


def project_file_command(remote, gcode_md5, plate):
    """The command Bambu Studio sends for a LAN print; assumes an external spool (no AMS)."""
    return {"print": {"sequence_id": "20000", "command": "project_file", "param": f"Metadata/plate_{plate}.gcode",
                      "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
                      "subtask_name": remote[:-len(".gcode.3mf")], "file": remote,
                      "url": f"file:///sdcard/{remote}",  # ftp:// URLs are answered with result: fail
                      "md5": gcode_md5, "timelapse": False, "bed_type": "auto", "bed_levelling": True,
                      "flow_cali": True, "vibration_cali": False, "layer_inspect": False,
                      "use_ams": False, "ams_mapping": [], "skip_objects": None}}


def grab_frame(ip, code, out):
    """Save one JPEG frame; the first frames after connecting can be stale, so keep the third."""
    sock = tls_context().wrap_socket(socket.create_connection((ip, CAMERA_PORT), timeout=10))
    sock.settimeout(15)
    sock.sendall(struct.pack("<IIII", 0x40, 0x3000, 0, 0) + USER.encode().ljust(32, b"\0")
                 + code.encode().ljust(32, b"\0"))

    def read(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("摄像头关闭了连接 (访问码错误或摄像头被占用)")
            buf += chunk
        return buf

    try:
        for _ in range(3):
            size = struct.unpack("<I", read(16)[:4])[0]
            frame = read(size)
    finally:
        sock.close()
    if frame[:2] != b"\xff\xd8":
        raise ConnectionError("摄像头返回的不是 JPEG")
    with open(out, "wb") as f:
        f.write(frame)
    return len(frame)


def cmd_status(args):
    p = read_status(args)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=1)
    keys = ["gcode_state", "subtask_name", "layer_num", "total_layer_num", "mc_percent", "mc_remaining_time",
            "nozzle_temper", "nozzle_target_temper", "bed_temper", "bed_target_temper", "nozzle_diameter",
            "nozzle_type", "sdcard", "wifi_signal"]
    summary = {k: p[k] for k in keys if k in p}
    summary["print_error"] = error_hex(p)
    summary["hms"] = hms_codes(p)
    tray = p.get("vt_tray") or {}
    summary["external_spool"] = {k: tray[k] for k in ("tray_type", "tray_info_idx", "tray_color") if k in tray}
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def cmd_cam(args):
    size = grab_frame(args.ip, args.code, args.out)
    print(f"已保存 {args.out}: {size} 字节")


def cmd_upload(args):
    upload_file(args.ip, args.code, args.file)


def cmd_print(args):
    if not args.plate_clear:
        sys.exit("打印会让打印机立刻动作。先用 cam 确认打印板是空的、没有人在操作打印机，再加 --plate-clear")
    gcode_md5 = plate_gcode_md5(args.file, args.plate)
    p = read_status(args)
    if p.get("gcode_state") in ACTIVE_STATES:
        sys.exit(f"打印机正忙 ({p['gcode_state']})，没有发送打印命令")
    remote = remote_name(args.file) if args.skip_upload else upload_file(args.ip, args.code, args.file)
    with PrinterMQTT(args.ip, args.serial, args.code) as mqtt:
        mqtt.publish(project_file_command(remote, gcode_md5, args.plate))
        print("已发送 project_file", flush=True)
        accepted = False
        for section in mqtt.messages(args.timeout):
            if section.get("command") == "project_file" and "result" in section:
                print("回复:", section["result"], section.get("reason", ""), flush=True)
                if str(section["result"]).lower() != "success":
                    sys.exit(1)
                accepted = True
            if accepted and section.get("gcode_state") in ACTIVE_STATES:
                print("任务已开始:", section["gcode_state"])
                return
    if not accepted:
        sys.exit("没有收到回复。确认打印机已开启 Developer Mode (否则命令会被忽略，并出现 HMS 0500-0400-0001-0007)")
    print(f"打印机已接受命令，但 {args.timeout:g} 秒内还没进入打印状态；用 status 确认")


def cmd_control(args):
    with PrinterMQTT(args.ip, args.serial, args.code) as mqtt:
        p = mqtt.status()
        print("发送前:", json.dumps({"gcode_state": p.get("gcode_state"), "layer_num": p.get("layer_num"),
                                     "total_layer_num": p.get("total_layer_num"), "print_error": error_hex(p)}),
              flush=True)
        if args.only_if_finished and not last_layer_done(p):
            sys.exit("最后一层还没打完 (或没有进行中的任务)，没有发送命令")
        mqtt.publish({"print": {"sequence_id": "20001", "command": args.action, "param": ""}})
        for section in mqtt.messages(args.timeout):
            if section.get("command") == args.action and "result" in section:
                print("回复:", section["result"], section.get("reason", ""))
                sys.exit(0 if str(section["result"]).lower() == "success" else 1)
    sys.exit(f"{args.timeout:g} 秒内没有收到回复")


def cmd_watch(args):
    """Poll until the job ends or needs attention. Exit code 0 only when the print finished."""
    deadline = time.time() + args.hours * 3600
    baseline, offline, outcome, p = None, 0, "timeout", {}
    while time.time() < deadline:
        try:
            p = read_status(args)
        except OSError as e:
            offline += 1
            print(time.strftime("%H:%M:%S"), f"无法连接 ({offline}/{MAX_OFFLINE_POLLS}): {e}", flush=True)
            if offline >= MAX_OFFLINE_POLLS:
                outcome = "offline"
                break
            time.sleep(args.interval)
            continue
        offline = 0
        if baseline is None:
            baseline = set(hms_codes(p))  # warnings already showing should not end the watch
        print(time.strftime("%H:%M:%S"), p.get("gcode_state"), f"{p.get('layer_num')}/{p.get('total_layer_num')}",
              f"{p.get('mc_percent')}%", f"剩余 {p.get('mc_remaining_time')} 分", f"热床 {p.get('bed_temper')}",
              f"喷嘴 {p.get('nozzle_temper')}", flush=True)
        current = job_outcome(p, baseline)
        if current:
            outcome = current
            break
        time.sleep(args.interval)
    print(json.dumps({"outcome": outcome, "gcode_state": p.get("gcode_state"), "print_error": error_hex(p),
                      "hms": hms_codes(p)}, ensure_ascii=False), flush=True)
    if args.snapshot:
        try:
            grab_frame(args.ip, args.code, args.snapshot)
            print(f"已保存 {args.snapshot}")
        except OSError as e:
            print(f"摄像头抓图失败: {e}")
    sys.exit(0 if outcome in ("finished", "finished_unload_prompt") else 1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="拓竹打印机局域网控制：状态、摄像头、上传、开始打印、任务控制、监控")
    parser.add_argument("--ip", default=os.environ.get("BAMBU_IP"), help="打印机 IP，默认读 BAMBU_IP")
    parser.add_argument("--serial", default=os.environ.get("BAMBU_SERIAL"), help="序列号，默认读 BAMBU_SERIAL")
    parser.add_argument("--code", default=os.environ.get("BAMBU_ACCESS_CODE"),
                        help="局域网访问码，默认读 BAMBU_ACCESS_CODE；不要写进仓库")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="读取当前状态")
    p.add_argument("--out", help="把完整状态报告保存为 JSON")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cam", help="抓一帧摄像头画面")
    p.add_argument("out", help="输出 .jpg 路径")
    p.set_defaults(func=cmd_cam)

    p = sub.add_parser("upload", help="只上传切片文件到 SD 卡，不开始打印")
    p.add_argument("file", help="切片后的 .3mf")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("print", help="上传并开始打印 (外挂料盘，需 Developer Mode)")
    p.add_argument("file", help="切片后的 .3mf")
    p.add_argument("--plate-clear", action="store_true", help="确认已用 cam 检查：打印板是空的，没有人在操作打印机")
    p.add_argument("--plate", type=int, default=1, help="打印第几块板")
    p.add_argument("--skip-upload", action="store_true", help="文件已在 SD 卡上，不重新上传")
    p.add_argument("--timeout", type=float, default=90, help="等待打印机回复的秒数")
    p.set_defaults(func=cmd_print)

    p = sub.add_parser("control", help="停止 / 暂停 / 继续当前任务 (stop 后不能继续)")
    p.add_argument("action", choices=["stop", "pause", "resume"])
    p.add_argument("--only-if-finished", action="store_true", help="只有最后一层已打完时才发送，用于结束卡在拔料提示上的任务")
    p.add_argument("--timeout", type=float, default=20, help="等待打印机回复的秒数")
    p.set_defaults(func=cmd_control)

    p = sub.add_parser("watch", help="监控任务直到结束或需要处理；打印完成时退出码为 0")
    p.add_argument("--interval", type=float, default=120, help="轮询间隔秒数")
    p.add_argument("--hours", type=float, default=24, help="最长监控小时数")
    p.add_argument("--snapshot", help="结束时保存一帧摄像头画面到这个路径")
    p.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    missing = [flag for flag, value in (("--ip", args.ip), ("--serial", args.serial), ("--code", args.code)) if not value]
    if missing:
        parser.error("缺少 " + "、".join(missing) + " (或对应的环境变量)")
    args.func(args)


if __name__ == "__main__":
    main()
