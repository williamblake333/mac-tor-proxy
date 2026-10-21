#!/usr/bin/env python3
"""
Mac Tor Proxy (mac-tor-proxy) — run a private Tor proxy on macOS and launch only the apps, scripts
and commands you choose through it. Everything else on the Mac connects normally.

https://github.com/williamblake333/mac-tor-proxy

Files live in ~/.mac-tor-proxy:
  config.json          your app list + settings
  torrc, tor-data/     the Tor instance Mac Tor Proxy manages (tor.log is its log)
  profiles/            separate browser profiles used for Tor launches
  logs/                output of scripts/commands run in the background
  proxychains.conf     used by the "proxychains (forced)" route method

Route methods
  chromium     Chrome / Brave / Edge / Vivaldi / Opera: proxy flags + its own profile
  firefox      Firefox family: dedicated profile (SOCKS, remote DNS, WebRTC off)
  electron     Electron apps (Slack, Discord, VS Code, Obsidian …): proxy flags
  env          proxy environment variables, for proxy-aware tools
               (curl, git, pip, Python requests, Go, Node 24+ …)
  proxychains  proxychains-ng forces every TCP connection through Tor; works for
               Homebrew / self-built binaries, not Apple's /usr/bin tools (SIP)
  torbrowser   Tor Browser: just opens it (it brings its own Tor)
"""
from __future__ import annotations

__version__ = "1.0.0"

import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QProcess, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSpinBox, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

# ----------------------------------------------------------------------------
# Paths & defaults
# ----------------------------------------------------------------------------
BASE = Path(os.environ.get("MAC_TOR_PROXY_HOME", str(Path.home() / ".mac-tor-proxy"))).expanduser()
CONFIG = BASE / "config.json"
TORRC = BASE / "torrc"
TOR_DATA = BASE / "tor-data"
PIDFILE = BASE / "tor.pid"
TORLOG = BASE / "tor.log"
START_OUT = BASE / "tor-start.out"
PROFILES = BASE / "profiles"
LOGS = BASE / "logs"
PC_CONF = BASE / "proxychains.conf"

# Apps opened from Finder don't inherit Homebrew's PATH; add it so tor,
# proxychains4, python3, node … resolve to the Homebrew copies.
_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join(
    [p for p in ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin") if p not in _path] + _path
)

DEFAULT_CONFIG = {
    "socks_port": 9050,
    "http_tunnel_port": 9080,
    "control_port": 9051,
    "start_tor_on_open": False,
    "autostart_on_launch": True,
    "stop_tor_on_quit": True,
    "entries": [],
}

APP_METHODS = ["chromium", "firefox", "electron", "env", "torbrowser"]
CLI_METHODS = ["env", "proxychains"]

METHOD_SHORT = {
    "chromium": "Chromium flags + own profile",
    "firefox": "Firefox Tor profile",
    "electron": "Electron proxy flags",
    "env": "Proxy env vars",
    "proxychains": "proxychains (forced)",
    "torbrowser": "Tor Browser (own Tor)",
}

METHOD_HELP = {
    "chromium": "Starts a separate copy of the browser with Tor proxy flags and its own profile, "
                "so your normal browsing is untouched. It fails closed: no Tor, no connection.",
    "firefox": "Starts the browser with a dedicated profile that uses Tor's SOCKS proxy with remote DNS "
               "and WebRTC off. It fails closed: no Tor, no connection.",
    "electron": "Passes Chromium proxy flags to the app. Quit the app first; the flags only apply "
                "to a fresh launch.",
    "env": "Sets ALL_PROXY / HTTPS_PROXY and friends. Works for proxy-aware tools (curl, git, pip, "
           "Python requests, Go, Node 24+). Programs that ignore these variables, including most "
           "native Mac apps, connect directly.",
    "proxychains": "Forces every TCP connection through Tor, even for programs that ignore proxy "
                   "settings. Works with Homebrew and self-built binaries; macOS blocks it for "
                   "Apple's /usr/bin tools and signed apps.",
    "torbrowser": "Tor Browser connects through its own built-in Tor; Mac Tor Proxy just opens it.",
}

CHROMIUM_IDS = ("com.google.chrome", "com.brave.browser", "com.microsoft.edgemac",
                "com.vivaldi.vivaldi", "org.chromium.chromium", "com.operasoftware.opera")
FIREFOX_HINTS = ("org.mozilla.", "librewolf", "waterfox", "zen-browser", "floorp")

INTERPRETERS = {".py": "python3", ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
                ".js": "node", ".mjs": "node", ".cjs": "node", ".rb": "ruby",
                ".pl": "perl", ".php": "php"}

COLORS = {"running": "#2ea44f", "bootstrapping": "#d29922",
          "external": "#3b82f6", "stopped": "#8b949e"}


class TorError(Exception):
    pass


class LaunchError(Exception):
    pass


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def ensure_dirs() -> None:
    for d in (BASE, TOR_DATA):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    PROFILES.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG.read_text())
        if isinstance(data, dict):
            cfg.update(data)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        try:
            CONFIG.replace(CONFIG.with_name("config.broken.json"))
        except OSError:
            pass
    cleaned = []
    for e in cfg.get("entries") or []:
        if not isinstance(e, dict) or not e.get("path") or e.get("kind") not in ("app", "script", "command"):
            continue
        e.setdefault("id", uuid.uuid4().hex[:10])
        e.setdefault("name", Path(e["path"]).name)
        e.setdefault("args", "")
        e.setdefault("terminal", e["kind"] != "app")
        valid = APP_METHODS if e["kind"] == "app" else CLI_METHODS
        if e.get("method") not in valid:
            e["method"] = "env"
        e.setdefault("profile", f"{_slug(e['name'])}-{e['id'][:6]}")
        cleaned.append(e)
    cfg["entries"] = cleaned
    return cfg


def save_config(cfg: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "item"


# ----------------------------------------------------------------------------
# Tor process & control port
# ----------------------------------------------------------------------------
def write_torrc(cfg: dict) -> None:
    TORRC.write_text("\n".join([
        "# Generated by Mac Tor Proxy; rewritten every time Tor starts",
        f"SocksPort 127.0.0.1:{cfg['socks_port']}",
        f"HTTPTunnelPort 127.0.0.1:{cfg['http_tunnel_port']}",
        f"ControlPort 127.0.0.1:{cfg['control_port']}",
        "CookieAuthentication 1",
        f"DataDirectory {TOR_DATA}",
        f"PidFile {PIDFILE}",
        f"Log notice file {TORLOG}",
        "RunAsDaemon 1",
        "AvoidDiskWrites 1",
        "ClientOnly 1",
        "",
    ]))


def write_proxychains_conf(cfg: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    PC_CONF.write_text(
        "# Generated by Mac Tor Proxy\n"
        "strict_chain\nproxy_dns\nremote_dns_subnet 224\n"
        "tcp_read_time_out 15000\ntcp_connect_time_out 8000\n"
        "localnet 127.0.0.0/255.0.0.0\n"
        f"[ProxyList]\nsocks5 127.0.0.1 {cfg['socks_port']}\n"
    )


def port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _read_reply(reader) -> list[str]:
    lines = []
    while True:
        raw = reader.readline()
        if not raw:
            raise TorError("Tor closed the control connection")
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(line)
        if len(line) >= 4 and line[3] == " ":
            if not line.startswith("2"):
                raise TorError(line)
            return lines


def tor_control(port: int, *commands: str, timeout: float = 2.0) -> list[list[str]]:
    """Authenticate with Mac Tor Proxy's cookie and run control-port commands."""
    cookie = (TOR_DATA / "control_auth_cookie").read_bytes().hex()
    with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
        with s.makefile("rb") as reader:
            s.sendall(f"AUTHENTICATE {cookie}\r\n".encode())
            _read_reply(reader)
            replies = []
            for cmd in commands:
                s.sendall(cmd.encode() + b"\r\n")
                replies.append(_read_reply(reader))
            return replies


def tor_bootstrap(port: int) -> tuple[int, str]:
    line = tor_control(port, "GETINFO status/bootstrap-phase")[0][0]
    m = re.search(r"PROGRESS=(\d+)", line)
    s = re.search(r'SUMMARY="([^"]*)"', line)
    return (int(m.group(1)) if m else 0), (s.group(1) if s else "")


def our_tor_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid


def tor_spawn(cfg: dict) -> subprocess.Popen:
    tor = shutil.which("tor")
    if not tor:
        raise TorError("Tor isn't installed. In Terminal run:\n\nbrew install tor")
    for key in ("socks_port", "http_tunnel_port", "control_port"):
        if port_open(cfg[key]):
            raise TorError(
                f"Port {cfg[key]} is already in use.\n\nIf Homebrew's Tor service is running, stop it with:\n\n"
                "brew services stop tor\n\nor choose different ports in Mac Tor Proxy."
            )
    ensure_dirs()
    write_torrc(cfg)
    TORLOG.write_text("")
    with open(START_OUT, "wb") as out:
        return subprocess.Popen([tor, "-f", str(TORRC)], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)


def tor_stop(cfg: dict) -> None:
    try:
        tor_control(cfg["control_port"], "SIGNAL HALT")
        return
    except (OSError, TorError):
        pass
    pid = our_tor_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return
    if not port_open(cfg["control_port"]) and not port_open(cfg["socks_port"]):
        return  # already stopped
    raise TorError("Couldn't reach Mac Tor Proxy's Tor instance to stop it.")


def log_tail(n: int = 15) -> str:
    out: list[str] = []
    for f in (START_OUT, TORLOG):
        try:
            out += f.read_text(errors="replace").strip().splitlines()
        except OSError:
            pass
    return "\n".join(out[-n:])


# ----------------------------------------------------------------------------
# App detection
# ----------------------------------------------------------------------------
def app_info(app_path: str | Path) -> dict:
    try:
        with open(Path(app_path) / "Contents" / "Info.plist", "rb") as fh:
            data = plistlib.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def detect_app_method(app_path: str | Path) -> str:
    bid = str(app_info(app_path).get("CFBundleIdentifier", "")).lower()
    fw = Path(app_path) / "Contents" / "Frameworks"
    if bid.startswith("org.torproject."):
        return "torbrowser"
    if any(h in bid for h in FIREFOX_HINTS):
        return "firefox"
    if (fw / "Electron Framework.framework").exists():
        return "electron"
    if bid.startswith(CHROMIUM_IDS):
        return "chromium"
    try:
        for child in fw.iterdir():
            n = child.name.lower()
            if n.endswith(" framework.framework") and re.search(r"chrom|brave|edge|vivaldi|opera", n):
                return "chromium"
    except OSError:
        pass
    return "env"


def new_entry(kind: str, path: str, name: str | None = None) -> dict:
    eid = uuid.uuid4().hex[:10]
    if kind == "app":
        info = app_info(path)
        name = name or info.get("CFBundleDisplayName") or info.get("CFBundleName") or Path(path).stem
        method = detect_app_method(path)
    elif kind == "script":
        name = name or Path(path).name
        method = "env"
    else:
        try:
            first = shlex.split(path)[0]
        except (ValueError, IndexError):
            first = "command"
        name = name or Path(first).name
        method = "env"
    name = str(name)
    return {"id": eid, "name": name, "kind": kind, "path": path, "method": method,
            "args": "", "terminal": kind != "app", "profile": f"{_slug(name)}-{eid[:6]}"}


# ----------------------------------------------------------------------------
# Launching
# ----------------------------------------------------------------------------
def proxy_env(cfg: dict) -> dict:
    socks = f"socks5h://127.0.0.1:{cfg['socks_port']}"
    tunnel = f"http://127.0.0.1:{cfg['http_tunnel_port']}"
    local = "localhost,127.0.0.1,::1"
    return {
        "ALL_PROXY": socks, "all_proxy": socks,
        "HTTP_PROXY": socks, "http_proxy": socks,
        "HTTPS_PROXY": tunnel, "https_proxy": tunnel,
        "NO_PROXY": local, "no_proxy": local,
        "NODE_USE_ENV_PROXY": "1",
        "MAC_TOR_PROXY": "1",
    }


def chromium_flags(cfg: dict) -> list[str]:
    return [
        f"--proxy-server=socks5://127.0.0.1:{cfg['socks_port']}",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-quic",
    ]


def write_firefox_profile(profile: Path, cfg: dict) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    prefs = {
        "network.proxy.type": 1,
        "network.proxy.socks": "127.0.0.1",
        "network.proxy.socks_port": int(cfg["socks_port"]),
        "network.proxy.socks_version": 5,
        "network.proxy.socks_remote_dns": True,
        "network.proxy.socks5_remote_dns": True,
        "network.proxy.no_proxies_on": "",
        "network.proxy.failover_direct": False,
        "network.trr.mode": 5,
        "media.peerconnection.enabled": False,
        "network.dns.disablePrefetch": True,
        "network.prefetch-next": False,
        "network.predictor.enabled": False,
        "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False,
    }
    lines = ["// Written by Mac Tor Proxy on every launch; edits here are overwritten."]
    lines += [f'user_pref("{k}", {json.dumps(v)});' for k, v in prefs.items()]
    (profile / "user.js").write_text("\n".join(lines) + "\n")


def _spawn(argv: list[str], **kw) -> subprocess.Popen:
    kw.setdefault("stdin", subprocess.DEVNULL)
    kw.setdefault("stdout", subprocess.DEVNULL)
    kw.setdefault("stderr", subprocess.DEVNULL)
    kw.setdefault("start_new_session", True)
    return subprocess.Popen(argv, **kw)


def open_app(app: str | Path, args: list[str] | None = None, env: dict | None = None,
             new_instance: bool = True) -> None:
    cmd = ["/usr/bin/open"]
    if new_instance:
        cmd.append("-n")
    cmd += ["-a", str(app)]
    for k, v in (env or {}).items():
        cmd += ["--env", f"{k}={v}"]
    if args:
        cmd += ["--args", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as ex:
        raise LaunchError(f"Couldn't run 'open': {ex}") from ex
    if r.returncode != 0:
        raise LaunchError((r.stderr or r.stdout).strip() or f"'open' failed with code {r.returncode}")


def run_in_terminal(shell_cmd: str) -> None:
    esc = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "terminal-launch.err", "ab") as err:
        _spawn(["/usr/bin/osascript",
                "-e", 'tell application "Terminal"',
                "-e", f'do script "{esc}"',
                "-e", "activate",
                "-e", "end tell"], stderr=err)


def _ps_lines() -> list[str]:
    try:
        return subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True,
                              text=True, timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def app_running(app_path: str | Path) -> bool:
    needle = str(Path(app_path)) + "/Contents/MacOS/"
    return any(needle in line for line in _ps_lines())


def profile_in_use(profile: Path) -> bool:
    needle = str(profile)
    return any(needle in line for line in _ps_lines())


def script_argv(path: str | Path) -> list[str]:
    p = Path(path)
    interp = INTERPRETERS.get(p.suffix.lower())
    if interp:
        return [shutil.which(interp) or interp, str(p)]
    if os.access(p, os.X_OK):
        return [str(p)]
    return ["/bin/zsh", str(p)]


def proxychains_prefix(cfg: dict) -> list[str]:
    pc = shutil.which("proxychains4")
    if not pc:
        raise LaunchError("proxychains isn't installed. In Terminal run:\n\nbrew install proxychains-ng")
    write_proxychains_conf(cfg)
    return [pc, "-q", "-f", str(PC_CONF)]


def terminal_exports(env: dict) -> str:
    return "; ".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())


def launch_entry(e: dict, cfg: dict) -> str:
    """Launch one entry through Tor. Returns a status message; raises LaunchError."""
    name, method = e["name"], e["method"]
    try:
        extra = shlex.split(e.get("args") or "")
    except ValueError as ex:
        raise LaunchError(f"The extra args for {name} aren't valid shell syntax: {ex}") from ex

    if e["kind"] == "app":
        app = Path(e["path"])
        if not app.exists():
            raise LaunchError(f"{app} no longer exists.")
        profile = PROFILES / e["profile"]
        if method == "torbrowser":
            open_app(app, extra or None, new_instance=False)
            return f"Opened {name}. Tor Browser runs its own Tor, separate from Mac Tor Proxy's."
        if method == "chromium":
            profile.mkdir(parents=True, exist_ok=True)
            open_app(app, chromium_flags(cfg) + [f"--user-data-dir={profile}", "--no-first-run",
                                                 "--no-default-browser-check"] + extra)
            return (f"Launched {name} through Tor in its own profile (separate from your normal one). "
                    "Open check.torproject.org in it to confirm.")
        if method == "firefox":
            if profile_in_use(profile):
                return f"{name}'s Tor profile is already open; switch to that window."
            write_firefox_profile(profile, cfg)
            open_app(app, ["-profile", str(profile), "-new-instance"] + extra)
            return f"Launched {name} with its Tor profile. Open check.torproject.org in it to confirm."
        if method == "electron":
            open_app(app, chromium_flags(cfg) + extra)
            return f"Launched {name} with Tor proxy flags."
        if method == "env":
            open_app(app, extra or None, env=proxy_env(cfg))
            return (f"Launched {name} with proxy environment variables. Native Mac apps often ignore "
                    "these, so confirm it's really using Tor before trusting it.")
        raise LaunchError(f"{METHOD_SHORT.get(method, method)} isn't available for apps.")

    if method not in CLI_METHODS:
        raise LaunchError(f"{METHOD_SHORT.get(method, method)} isn't available for scripts.")
    env = proxy_env(cfg) if method == "env" else {}
    pc = proxychains_prefix(cfg) if method == "proxychains" else []

    if e["kind"] == "script":
        script = Path(e["path"])
        if not script.exists():
            raise LaunchError(f"{script} no longer exists.")
        argv = pc + script_argv(script) + extra
        shell_line = shlex.join(argv)
        cwd = script.parent
    else:
        cmd = e["path"].strip()
        if not cmd:
            raise LaunchError("The command is empty.")
        args_raw = (e.get("args") or "").strip()
        shell_line = (shlex.join(pc) + " " if pc else "") + cmd + (" " + args_raw if args_raw else "")
        argv = ["/bin/zsh", "-lc", shell_line]
        cwd = Path.home()

    if e.get("terminal", True):
        banner = shlex.quote(f"[Mac Tor Proxy] {name} -> Tor via {METHOD_SHORT[method]}")
        parts = [terminal_exports(env)] if env else []
        parts += [f"cd {shlex.quote(str(cwd))}", "clear", f"echo {banner}", shell_line]
        run_in_terminal("; ".join(parts))
        return (f"Opened {name} in Terminal through Tor. (The first time, macOS asks whether "
                "Python (Mac Tor Proxy) may control Terminal; allow it.)")

    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{e['profile']}.log"
    with open(log, "ab") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  {name} ===\n".encode())
        fh.flush()
        _spawn(argv, cwd=str(cwd), env={**os.environ, **env}, stdout=fh, stderr=subprocess.STDOUT)
    return f"Running {name} in the background through Tor. Output: {log}"


# ----------------------------------------------------------------------------
# Icons
# ----------------------------------------------------------------------------
def tor_pixmap(color: str, size: int = 64, bg: str | None = None) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 64.0
    if bg:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(QRectF(6 * s, 6 * s, 52 * s, 52 * s), 12 * s, 12 * s)
        inset, span = 16 * s, 32 * s
    else:
        inset, span = 6 * s, 52 * s
    pen = QPen(QColor(color))
    pen.setWidthF(span * 0.09)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for frac in (0.0, 0.2):
        o, d = inset + span * frac, span * (1 - 2 * frac)
        p.drawEllipse(QRectF(o, o, d, d))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    o, d = inset + span * 0.38, span * 0.24
    p.drawEllipse(QRectF(o, o, d, d))
    p.end()
    return pm


def dot_pixmap(color: str, d: int = 14) -> QPixmap:
    pm = QPixmap(d * 2, d * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(1, 1, d * 2 - 2, d * 2 - 2))
    p.end()
    pm.setDevicePixelRatio(2.0)
    return pm


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    COLS = ["Name", "Route method", "Extra args", "Terminal", "Path / command"]

    def __init__(self):
        super().__init__()
        ensure_dirs()
        self.cfg = load_config()
        save_config(self.cfg)
        write_proxychains_conf(self.cfg)
        self.state, self.progress, self.summary = "stopped", 0, ""
        self._last_state = None
        self.start_proc: subprocess.Popen | None = None
        self.start_deadline = 0.0
        self.pending: list[str] = []
        self.check_proc = None
        self._populating = False
        self._quitting = False
        self._shut_down = False
        self._tray_hint_shown = False
        self.tray = None
        self.tray_status = self.tray_toggle = self.tray_newnym = None

        self.setWindowTitle("Mac Tor Proxy")
        self.setMinimumSize(860, 540)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_tray()
        self.populate_table()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(1500)
        self.refresh_status()
        if self.cfg["start_tor_on_open"] and self.state == "stopped":
            self.start_tor()

    # ---- layout -------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)

        tor_box = QGroupBox("Tor proxy")
        tl = QVBoxLayout(tor_box)
        top = QHBoxLayout()
        self.dot = QLabel()
        self.dot.setFixedSize(16, 16)
        self.status_lbl = QLabel("Tor is stopped")
        f = self.status_lbl.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        self.status_lbl.setFont(f)
        top.addWidget(self.dot)
        top.addWidget(self.status_lbl, 1)
        self.toggle_btn = QPushButton("Start Tor")
        self.toggle_btn.clicked.connect(self.toggle_tor)
        self.newnym_btn = QPushButton("New identity")
        self.newnym_btn.clicked.connect(self.new_identity)
        self.check_btn = QPushButton("Check exit IP")
        self.check_btn.clicked.connect(self.check_exit)
        self.term_btn = QPushButton("Open Tor Terminal")
        self.term_btn.clicked.connect(self.open_tor_terminal)
        for b in (self.toggle_btn, self.newnym_btn, self.check_btn, self.term_btn):
            top.addWidget(b)
        tl.addLayout(top)

        self.info_lbl = QLabel(" ")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tl.addWidget(self.info_lbl)

        ports = QHBoxLayout()
        self.port_spins = {}
        for key, label in (("socks_port", "SOCKS port"), ("http_tunnel_port", "HTTP tunnel port"),
                           ("control_port", "Control port")):
            sp = QSpinBox()
            sp.setRange(1024, 65535)
            sp.setValue(int(self.cfg[key]))
            sp.valueChanged.connect(lambda v, k=key: self.set_port(k, v))
            ports.addWidget(QLabel(label))
            ports.addWidget(sp)
            self.port_spins[key] = sp
        ports.addStretch(1)
        tl.addLayout(ports)

        opts = QHBoxLayout()
        for key, label in (("start_tor_on_open", "Start Tor when Mac Tor Proxy opens"),
                           ("autostart_on_launch", "Start Tor automatically when launching"),
                           ("stop_tor_on_quit", "Stop Tor when Mac Tor Proxy quits")):
            cb = QCheckBox(label)
            cb.setChecked(bool(self.cfg[key]))
            cb.toggled.connect(lambda on, k=key: self.set_option(k, on))
            opts.addWidget(cb)
        opts.addStretch(1)
        tl.addLayout(opts)
        outer.addWidget(tor_box)

        apps_box = QGroupBox("Apps, scripts and commands routed through Tor (you can drag .app bundles or scripts here)")
        al = QVBoxLayout(apps_box)
        t = self.table = QTableWidget(0, len(self.COLS))
        t.setHorizontalHeaderLabels(self.COLS)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                          | QAbstractItemView.EditTrigger.EditKeyPressed)
        t.verticalHeader().setVisible(False)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        t.setColumnWidth(0, 170)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        t.setColumnWidth(2, 140)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        t.itemChanged.connect(self.on_item_changed)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(self.table_menu)
        al.addWidget(t)

        btns = QHBoxLayout()
        for label, slot in (("Add App…", self.add_app), ("Add Script…", self.add_script),
                            ("Add Command…", self.add_command), ("Remove", self.remove_selected)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        self.launch_btn = QPushButton("Launch through Tor")
        self.launch_btn.setDefault(True)
        self.launch_btn.clicked.connect(self.launch_selected)
        btns.addWidget(self.launch_btn)
        al.addLayout(btns)
        outer.addWidget(apps_box, 1)

        hint = QLabel('Only things launched from here use Tor; the rest of your Mac connects normally. '
                      'Verify inside an app at <a href="https://check.torproject.org">check.torproject.org</a>.')
        hint.setOpenExternalLinks(True)
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self.setCentralWidget(root)

    def _build_tray(self):
        self.tray_menu = QMenu(self)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon(tor_pixmap(COLORS["stopped"])), self)
            self.tray.setToolTip("Mac Tor Proxy")
            self.tray.setContextMenu(self.tray_menu)
            self.tray.show()

    def rebuild_tray_menu(self):
        m = self.tray_menu
        m.clear()
        self.tray_status = m.addAction(self.status_lbl.text())
        self.tray_status.setEnabled(False)
        self.tray_toggle = m.addAction("Start Tor")
        self.tray_toggle.triggered.connect(lambda _=False: self.toggle_tor())
        self.tray_newnym = m.addAction("New identity")
        self.tray_newnym.triggered.connect(lambda _=False: self.new_identity())
        m.addSeparator()
        sub = m.addMenu("Launch through Tor")
        if not self.cfg["entries"]:
            sub.addAction("(nothing added yet)").setEnabled(False)
        for e in self.cfg["entries"]:
            a = sub.addAction(e["name"])
            a.triggered.connect(lambda _=False, eid=e["id"]: self.request_launch(eid))
        m.addAction("Open Tor Terminal").triggered.connect(lambda _=False: self.open_tor_terminal())
        m.addSeparator()
        m.addAction("Show Mac Tor Proxy").triggered.connect(lambda _=False: self.show_window())
        m.addAction("Quit Mac Tor Proxy").triggered.connect(lambda _=False: self.quit_app())
        self._last_state = None  # force labels/icons to refresh
        self._apply_state(self.state, self.progress, self.summary)

    # ---- table --------------------------------------------------------------
    def populate_table(self):
        self._populating = True
        t = self.table
        t.setRowCount(0)
        for e in self.cfg["entries"]:
            r = t.rowCount()
            t.insertRow(r)
            name = QTableWidgetItem(e["name"])
            name.setData(Qt.ItemDataRole.UserRole, e["id"])
            name.setToolTip(METHOD_HELP[e["method"]])
            t.setItem(r, 0, name)

            combo = QComboBox()
            for m in (APP_METHODS if e["kind"] == "app" else CLI_METHODS):
                combo.addItem(METHOD_SHORT[m], m)
            combo.setCurrentIndex(max(combo.findData(e["method"]), 0))
            combo.currentIndexChanged.connect(lambda _i, eid=e["id"], c=combo: self.on_method_changed(eid, c))
            t.setCellWidget(r, 1, combo)

            t.setItem(r, 2, QTableWidgetItem(e.get("args", "")))

            term = QTableWidgetItem()
            if e["kind"] == "app":
                term.setText("—")
                term.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            else:
                term.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                              | Qt.ItemFlag.ItemIsUserCheckable)
                term.setCheckState(Qt.CheckState.Checked if e.get("terminal", True) else Qt.CheckState.Unchecked)
                term.setToolTip("Checked: open in a Terminal window. Unchecked: run in the background "
                                "and write output to ~/.mac-tor-proxy/logs.")
            t.setItem(r, 3, term)

            p = QTableWidgetItem(e["path"])
            p.setToolTip(e["path"])
            if e["kind"] != "command":
                p.setFlags(p.flags() & ~Qt.ItemFlag.ItemIsEditable)
            t.setItem(r, 4, p)
        self._populating = False
        self.rebuild_tray_menu()

    def find_entry(self, eid):
        return next((e for e in self.cfg["entries"] if e["id"] == eid), None)

    def entry_at_row(self, row):
        item = self.table.item(row, 0)
        return self.find_entry(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def selected_ids(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for r in rows:
            e = self.entry_at_row(r)
            if e:
                out.append(e["id"])
        return out

    def on_item_changed(self, item):
        if self._populating:
            return
        e = self.entry_at_row(item.row())
        if not e:
            return
        col = item.column()
        if col == 0:
            e["name"] = item.text().strip() or e["name"]
        elif col == 2:
            e["args"] = item.text()
        elif col == 3 and e["kind"] != "app":
            e["terminal"] = item.checkState() == Qt.CheckState.Checked
        elif col == 4 and e["kind"] == "command":
            e["path"] = item.text().strip()
        save_config(self.cfg)
        if col == 0:
            self.rebuild_tray_menu()

    def on_method_changed(self, eid, combo):
        e = self.find_entry(eid)
        if not e:
            return
        e["method"] = combo.currentData()
        save_config(self.cfg)
        self.info(f"{e['name']}: {METHOD_HELP[e['method']]}")

    def table_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        e = self.entry_at_row(row)
        if not e:
            return
        m = QMenu(self)
        m.addAction("Launch through Tor").triggered.connect(lambda _=False: self.request_launch(e["id"]))
        if e["kind"] != "command":
            m.addAction("Show in Finder").triggered.connect(
                lambda _=False: _spawn(["/usr/bin/open", "-R", e["path"]]))
        log = LOGS / f"{e['profile']}.log"
        if e["kind"] != "app" and log.exists():
            m.addAction("Open background output log").triggered.connect(
                lambda _=False: _spawn(["/usr/bin/open", "-a", "Console", str(log)]))
        m.addSeparator()
        m.addAction("Remove").triggered.connect(lambda _=False: self.remove_selected())
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ---- adding / removing --------------------------------------------------
    def add_app(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose an app to route through Tor", "/Applications",
                                              "Applications (*.app);;All files (*)")
        if path:
            self.add_path(path)

    def add_script(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a script or program to run through Tor",
                                              str(Path.home()))
        if path:
            self.add_path(path)

    def add_command(self):
        cmd, ok = QInputDialog.getText(self, "Add command", "Command to run through Tor:",
                                       QLineEdit.EchoMode.Normal, "curl https://check.torproject.org/api/ip")
        if ok and cmd.strip():
            self._add_entry(new_entry("command", cmd.strip()))

    def add_path(self, path: str):
        path = path.rstrip("/")
        p = Path(path)
        if path.endswith(".app") and p.is_dir():
            e = new_entry("app", path)
        elif p.is_file():
            e = new_entry("script", path)
        else:
            QMessageBox.warning(self, "Mac Tor Proxy", f"Can't add {path}: choose an .app, a script or a program.")
            return
        self._add_entry(e)

    def _add_entry(self, e: dict):
        self.cfg["entries"].append(e)
        save_config(self.cfg)
        self.populate_table()
        self.table.selectRow(self.table.rowCount() - 1)
        msg = f"Added {e['name']} using {METHOD_SHORT[e['method']]}. {METHOD_HELP[e['method']]}"
        if e["kind"] == "app" and e["method"] == "env":
            msg += (" If this app must be forced through Tor, a per-app proxy tool such as Proxifier "
                    "is the reliable option.")
        self.info(msg)

    def remove_selected(self):
        ids = set(self.selected_ids())
        if not ids:
            return
        self.cfg["entries"] = [e for e in self.cfg["entries"] if e["id"] not in ids]
        self.pending = [i for i in self.pending if i not in ids]
        save_config(self.cfg)
        self.populate_table()

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            if url.isLocalFile():
                self.add_path(url.toLocalFile())

    # ---- launching ----------------------------------------------------------
    def launch_selected(self):
        ids = self.selected_ids()
        if not ids:
            self.info("Select an app or script in the list first.")
            return
        for eid in ids:
            self.request_launch(eid)

    def request_launch(self, eid):
        e = self.find_entry(eid)
        if not e:
            return
        if (e["kind"] == "app" and e["method"] == "torbrowser") or self.state in ("running", "external"):
            self.do_launch(e)
            return
        if self.state == "stopped":
            if not self.cfg["autostart_on_launch"]:
                ans = QMessageBox.question(self, "Tor is off",
                                           f"Tor isn't running. Start it and then launch {e['name']}?")
                if ans != QMessageBox.StandardButton.Yes:
                    return
            if not self.start_tor():
                return
        if eid not in self.pending:
            self.pending.append(eid)
        self.info(f"Waiting for Tor to finish connecting, then launching {e['name']}…")

    def do_launch(self, e):
        if e["kind"] == "app" and e["method"] in ("electron", "env") and app_running(e["path"]):
            ans = QMessageBox.question(
                self, "Already running",
                f"{e['name']} is already running, so it won't pick up the Tor settings. "
                "Quit it first, then launch it again from here.\n\nLaunch a second copy anyway?")
            if ans != QMessageBox.StandardButton.Yes:
                return
        try:
            self.info(launch_entry(e, self.cfg))
        except LaunchError as ex:
            QMessageBox.warning(self, f"Couldn't launch {e['name']}", str(ex))

    # ---- Tor control --------------------------------------------------------
    def toggle_tor(self):
        if self.state in ("running", "bootstrapping"):
            self.stop_tor()
        elif self.state == "stopped":
            self.start_tor()
        else:
            QMessageBox.information(
                self, "Mac Tor Proxy",
                f"Another program is already listening on port {self.cfg['socks_port']}, so Mac Tor Proxy "
                "can't start or stop it. If it's Homebrew's Tor, run this in Terminal:\n\n"
                "brew services stop tor\n\nApps you launch from here will use whatever is on that port.")

    def start_tor(self) -> bool:
        if self.start_proc is not None:
            return True
        try:
            self.start_proc = tor_spawn(self.cfg)
        except TorError as ex:
            QMessageBox.warning(self, "Couldn't start Tor", str(ex))
            return False
        self.start_deadline = time.time() + 90
        self.info("Starting Tor… the first connection can take 10 to 30 seconds.")
        QTimer.singleShot(300, self.refresh_status)
        return True

    def stop_tor(self):
        self.pending.clear()
        self.start_proc = None
        try:
            tor_stop(self.cfg)
            self.info("Tor stopped. Apps launched through it can't connect until you start it again.")
        except TorError as ex:
            QMessageBox.warning(self, "Mac Tor Proxy", str(ex))
        QTimer.singleShot(600, self.refresh_status)

    def _start_failed(self, why: str):
        self.pending.clear()
        QMessageBox.warning(self, "Tor didn't start", f"{why}\n\nLast log lines:\n{log_tail() or '(no log output)'}")

    def refresh_status(self):
        c = self.cfg
        state, progress, summary = "stopped", 0, ""
        try:
            progress, summary = tor_bootstrap(c["control_port"])
            state = "running" if progress >= 100 else "bootstrapping"
            if self.start_proc is not None and self.start_proc.poll() is not None:
                self.start_proc = None
        except (OSError, TorError):
            if self.start_proc is not None:
                rc = self.start_proc.poll()
                if rc not in (None, 0):
                    self.start_proc = None
                    self._start_failed(f"Tor exited with code {rc}.")
                elif time.time() > self.start_deadline:
                    self.start_proc = None
                    self._start_failed("Tor didn't open its control port in time.")
                else:
                    state, summary = "bootstrapping", "starting"
            if state == "stopped" and port_open(c["socks_port"]):
                state = "external"
        self._apply_state(state, progress, summary)

    def _apply_state(self, state, progress, summary):
        self.state, self.progress, self.summary = state, progress, summary
        c = self.cfg
        text = {
            "running": f"Tor is running  ·  SOCKS 127.0.0.1:{c['socks_port']}",
            "bootstrapping": f"Tor is connecting… {progress}%" + (f" ({summary})" if summary else ""),
            "external": f"Port {c['socks_port']} is used by another Tor/proxy (not managed here)",
            "stopped": "Tor is stopped",
        }[state]
        managed = state in ("running", "bootstrapping")
        self.status_lbl.setText(text)
        self.toggle_btn.setText("Stop Tor" if managed else "Start Tor")
        self.toggle_btn.setEnabled(state != "external")
        self.newnym_btn.setEnabled(state == "running")
        self.check_btn.setEnabled(state in ("running", "external"))
        for sp in self.port_spins.values():
            sp.setEnabled(state == "stopped")
        if self.tray_status is not None:
            self.tray_status.setText(text)
            self.tray_toggle.setText("Stop Tor" if managed else "Start Tor")
            self.tray_toggle.setEnabled(state != "external")
            self.tray_newnym.setEnabled(state == "running")
        if state != self._last_state:
            self._last_state = state
            self.dot.setPixmap(dot_pixmap(COLORS[state]))
            if self.tray is not None:
                self.tray.setIcon(QIcon(tor_pixmap(COLORS[state])))
        if self.tray is not None:
            self.tray.setToolTip(f"Mac Tor Proxy: {text}")
        if state in ("running", "external") and self.pending:
            queued, self.pending = self.pending, []
            for eid in queued:
                e = self.find_entry(eid)
                if e:
                    self.do_launch(e)

    def new_identity(self):
        try:
            tor_control(self.cfg["control_port"], "SIGNAL NEWNYM")
            self.info("New identity requested: new connections use fresh circuits (Tor allows this about "
                      "once every 10 seconds). Restart an app if you want its open connections switched too.")
        except (OSError, TorError) as ex:
            self.info(f"Couldn't request a new identity: {ex}")

    def check_exit(self):
        if self.check_proc is not None and self.check_proc.state() != QProcess.ProcessState.NotRunning:
            return
        self.info("Checking the exit IP through Tor…")
        proc = self.check_proc = QProcess(self)
        proc.finished.connect(self._check_done)
        proc.start("/usr/bin/curl", ["-s", "--max-time", "30", "--socks5-hostname",
                                     f"127.0.0.1:{self.cfg['socks_port']}",
                                     "https://check.torproject.org/api/ip"])

    def _check_done(self, code, _status=None):
        out = bytes(self.check_proc.readAllStandardOutput()).decode(errors="replace").strip()
        try:
            data = json.loads(out)
        except ValueError:
            self.info(f"The check didn't get an answer (curl exit code {code}). Tor may still be connecting.")
            return
        if data.get("IsTor"):
            self.info(f"✅ Traffic through port {self.cfg['socks_port']} exits the Tor network at {data.get('IP')}.")
        else:
            self.info(f"⚠️ Port {self.cfg['socks_port']} is NOT exiting through Tor (IP {data.get('IP')}).")

    def open_tor_terminal(self):
        env = proxy_env(self.cfg)
        msg = shlex.quote("[Mac Tor Proxy] Proxy-aware tools in this window use Tor. "
                          "Try: curl https://check.torproject.org/api/ip")
        run_in_terminal(f"{terminal_exports(env)}; clear; echo {msg}")
        if self.state not in ("running", "external"):
            self.info("Opened a Tor Terminal, but Tor is off; start it before running anything there.")

    # ---- settings -----------------------------------------------------------
    def set_port(self, key, value):
        value = int(value)
        if self.cfg[key] == value:
            return
        self.cfg[key] = value
        save_config(self.cfg)
        write_proxychains_conf(self.cfg)
        self._last_state = None
        self.refresh_status()

    def set_option(self, key, on):
        self.cfg[key] = bool(on)
        save_config(self.cfg)

    def info(self, msg: str):
        self.info_lbl.setText(msg)

    # ---- window / quit ------------------------------------------------------
    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._quitting = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        if self._shut_down:
            return
        self._shut_down = True
        if self.cfg.get("stop_tor_on_quit") and self.state in ("running", "bootstrapping"):
            try:
                tor_stop(self.cfg)
            except TorError:
                pass

    def closeEvent(self, ev):
        # The red close button hides the window; Mac Tor Proxy stays in the menu bar.
        if self.tray is not None and not self._quitting and ev.spontaneous():
            ev.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage("Mac Tor Proxy", "Still running in the menu bar. Use its menu to "
                                      "show this window or quit.", QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        self._shutdown()
        ev.accept()


def main():
    if sys.argv[1:2] == ["--version"]:
        print(f"Mac Tor Proxy {__version__}")
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--make-icon":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = QApplication(sys.argv[:1])
        tor_pixmap("#ffffff", 1024, bg="#59316b").save(sys.argv[2], "PNG")
        return
    app = QApplication(sys.argv)
    app.setApplicationName("Mac Tor Proxy")

    # Single instance: if Mac Tor Proxy is already open, ask it to show its window and exit.
    server_name = f"mac-tor-proxy-{os.getuid()}"
    probe = QLocalSocket()
    probe.connectToServer(server_name)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    server.listen(server_name)

    app.setWindowIcon(QIcon(tor_pixmap("#ffffff", 256, bg="#59316b")))
    app.setQuitOnLastWindowClosed(not QSystemTrayIcon.isSystemTrayAvailable())
    win = MainWindow()
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.show_window()))
    app.aboutToQuit.connect(win._shutdown)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
