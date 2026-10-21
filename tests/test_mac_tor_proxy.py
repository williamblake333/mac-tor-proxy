import os, sys, json, socket, threading, tempfile, plistlib, stat, subprocess, time, shlex
from pathlib import Path

"""Test suite for Mac Tor Proxy. Runs without Qt or Tor:  python3 tests/test_mac_tor_proxy.py"""
REPO = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="mtp-test-"))
os.environ["MAC_TOR_PROXY_HOME"] = str(HOME / ".mac-tor-proxy")
sys.path.insert(0, str(REPO / "tests" / "qt_stub"))
sys.path.insert(0, str(REPO))
import mac_tor_proxy as tr

failures = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

# ---------------- fake tor control port ----------------
class FakeTor:
    def __init__(self, ctrl, socks, progress=100):
        self.ctrl, self.socks, self.progress = ctrl, socks, progress
        self.signals = []
        self.cookie = os.urandom(32)
        tr.ensure_dirs()
        (tr.TOR_DATA / "control_auth_cookie").write_bytes(self.cookie)
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", ctrl)); self.srv.listen(8)
        self.ssrv = socket.socket(); self.ssrv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ssrv.bind(("127.0.0.1", socks)); self.ssrv.listen(8)
        self.alive = True
        threading.Thread(target=self.loop, daemon=True).start()
        threading.Thread(target=self.sloop, daemon=True).start()
    def sloop(self):
        while self.alive:
            try: c, _ = self.ssrv.accept(); c.close()
            except OSError: return
    def loop(self):
        while self.alive:
            try: c, _ = self.srv.accept()
            except OSError: return
            f = c.makefile("rb"); authed = False
            try:
                for raw in f:
                    line = raw.decode().strip()
                    if line.startswith("AUTHENTICATE"):
                        if line.split()[1] == self.cookie.hex():
                            authed = True; c.sendall(b"250 OK\r\n")
                        else:
                            c.sendall(b"515 Authentication failed: Wrong length on authentication cookie.\r\n"); break
                    elif not authed:
                        c.sendall(b"514 Authentication required.\r\n"); break
                    elif line == "GETINFO status/bootstrap-phase":
                        tag = "done" if self.progress >= 100 else "conn"
                        summ = "Done" if self.progress >= 100 else "Connecting to a relay"
                        c.sendall(f'250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS={self.progress} TAG={tag} SUMMARY="{summ}"\r\n250 OK\r\n'.encode())
                    elif line.startswith("SIGNAL "):
                        self.signals.append(line.split()[1]); c.sendall(b"250 OK\r\n")
                        if line == "SIGNAL HALT":
                            c.close(); self.shutdown(); return
                    else:
                        c.sendall(b"510 Unrecognized command\r\n")
            except OSError:
                pass
            finally:
                try: c.close()
                except OSError: pass
    def shutdown(self):
        self.alive = False
        for s in (self.srv, self.ssrv):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: s.close()
            except OSError: pass

cfg = tr.load_config()
CTRL, SOCKS, TUN = free_port(), free_port(), free_port()
cfg.update(control_port=CTRL, socks_port=SOCKS, http_tunnel_port=TUN)

print("\n== Tor control protocol ==")
check(not tr.port_open(SOCKS), "socks port closed before start")
try:
    tr.tor_bootstrap(CTRL); check(False, "bootstrap should fail with no tor")
except (OSError, tr.TorError):
    check(True, "bootstrap raises when tor is down")
ft = FakeTor(CTRL, SOCKS, progress=45)
check(tr.tor_bootstrap(CTRL) == (45, "Connecting to a relay"), "bootstrap parses 45% + summary")
ft.progress = 100
check(tr.tor_bootstrap(CTRL) == (100, "Done"), "bootstrap parses 100% Done")
tr.tor_control(CTRL, "SIGNAL NEWNYM")
check(ft.signals == ["NEWNYM"], "NEWNYM delivered")
good = ft.cookie
(tr.TOR_DATA / "control_auth_cookie").write_bytes(b"x" * 32)
try:
    tr.tor_control(CTRL, "GETINFO status/bootstrap-phase"); check(False, "bad cookie should fail")
except tr.TorError as ex:
    check("515" in str(ex), "bad cookie -> TorError 515")
(tr.TOR_DATA / "control_auth_cookie").write_bytes(good)
check(tr.port_open(SOCKS), "socks port reported open")
tr.tor_stop(cfg)
time.sleep(0.2)
check("HALT" in ft.signals, "tor_stop sends SIGNAL HALT")
check(not tr.port_open(CTRL), "control port closed after HALT")
tr.tor_stop(cfg)  # already stopped -> no exception
check(True, "tor_stop on stopped tor is a no-op")

print("\n== torrc / proxychains ==")
tr.write_torrc(cfg)
torrc = tr.TORRC.read_text()
for frag in (f"SocksPort 127.0.0.1:{SOCKS}", f"HTTPTunnelPort 127.0.0.1:{TUN}", f"ControlPort 127.0.0.1:{CTRL}",
             "CookieAuthentication 1", f"DataDirectory {tr.TOR_DATA}", "RunAsDaemon 1", f"Log notice file {tr.TORLOG}"):
    check(frag in torrc, f"torrc has {frag!r}")
check(oct(tr.TOR_DATA.stat().st_mode & 0o777) == "0o700", "tor-data is 0700")
tr.write_proxychains_conf(cfg)
check(f"socks5 127.0.0.1 {SOCKS}" in tr.PC_CONF.read_text(), "proxychains conf uses socks port")

print("\n== tor_spawn with fake tor binary ==")
binx = HOME / "bin"; binx.mkdir()
(binx / "tor").write_text("#!/bin/sh\necho \"fake tor $@\"\nexit 0\n"); (binx / "tor").chmod(0o755)
os.environ["PATH"] = f"{binx}:{os.environ['PATH']}"
p = tr.tor_spawn(cfg); p.wait(5)
check(p.returncode == 0 and "fake tor -f" in tr.START_OUT.read_text(), "tor_spawn runs `tor -f torrc`")
blocker = socket.socket(); blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
blocker.bind(("127.0.0.1", SOCKS)); blocker.listen(1)
try:
    tr.tor_spawn(cfg); check(False, "spawn should refuse busy port")
except tr.TorError as ex:
    check("already in use" in str(ex) and "brew services stop tor" in str(ex), "busy port -> helpful error")
blocker.close()

print("\n== app detection ==")
apps = HOME / "Applications"; apps.mkdir()
def mkapp(name, bid, electron=False, framework=None):
    a = apps / f"{name}.app"; (a / "Contents" / "MacOS").mkdir(parents=True)
    with open(a / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": bid, "CFBundleName": name}, fh)
    if electron: (a / "Contents" / "Frameworks" / "Electron Framework.framework").mkdir(parents=True)
    if framework: (a / "Contents" / "Frameworks" / framework).mkdir(parents=True)
    return str(a)
chrome = mkapp("Google Chrome", "com.google.Chrome")
brave = mkapp("Brave Browser", "com.brave.Browser")
thorium = mkapp("Thorium", "org.chromium.Thorium.custom", framework="Thorium Chromium Framework.framework")
ff = mkapp("Firefox", "org.mozilla.firefox")
slack = mkapp("Slack", "com.tinyspeck.slackmacgap", electron=True)
torb = mkapp("Tor Browser", "org.torproject.torbrowser")
notes = mkapp("Notes", "com.apple.Notes")
for path, want in ((chrome, "chromium"), (brave, "chromium"), (thorium, "chromium"), (ff, "firefox"),
                   (slack, "electron"), (torb, "torbrowser"), (notes, "env")):
    check(tr.detect_app_method(path) == want, f"{Path(path).name} -> {want}")

print("\n== launch commands ==")
runs, spawns = [], []
class R: returncode = 0; stdout = ""; stderr = ""
real_run = subprocess.run
def fake_run(cmd, *a, **k):
    if cmd[0] in ("/usr/bin/open",):
        runs.append(cmd); return R()
    if cmd[0] == "/bin/ps":
        r = R(); r.stdout = fake_ps; return r
    return real_run(cmd, *a, **k)
fake_ps = ""
tr.subprocess.run = fake_run
real_spawn = tr._spawn
tr._spawn = lambda argv, **kw: spawns.append((argv, kw))

def entry(kind, path, method=None, **kw):
    e = tr.new_entry(kind, path)
    if method: e["method"] = method
    e.update(kw); return e

e_chrome = entry("app", chrome)
msg = tr.launch_entry(e_chrome, cfg)
c = runs[-1]
check(c[:4] == ["/usr/bin/open", "-n", "-a", chrome], "chrome: open -n -a <app>")
check(f"--proxy-server=socks5://127.0.0.1:{SOCKS}" in c, "chrome: proxy-server flag")
check("--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1" in c, "chrome: host resolver rules as one arg")
check(any(a.startswith("--user-data-dir=" + str(tr.PROFILES)) for a in c), "chrome: separate user-data-dir")
check(c.index("--args") == 4, "chrome: flags after --args")

e_ff = entry("app", ff)
tr.launch_entry(e_ff, cfg)
c = runs[-1]
prof = tr.PROFILES / e_ff["profile"]
check(c[-3:] == ["-profile", str(prof), "-new-instance"], "firefox: -profile <dir> -new-instance")
uj = (prof / "user.js").read_text()
check(f'user_pref("network.proxy.socks_port", {SOCKS});' in uj and 'user_pref("network.proxy.socks_remote_dns", true);' in uj
      and 'user_pref("media.peerconnection.enabled", false);' in uj and 'user_pref("network.proxy.socks", "127.0.0.1");' in uj,
      "firefox: user.js prefs valid")
fake_ps = f"/Applications/Firefox.app/Contents/MacOS/firefox -profile {prof} -new-instance\n"
n = len(runs); m = tr.launch_entry(e_ff, cfg)
check(len(runs) == n and "already open" in m, "firefox: no relaunch when profile running")
fake_ps = ""

e_slack = entry("app", slack, args="--enable-logging")
tr.launch_entry(e_slack, cfg)
c = runs[-1]
check(f"--proxy-server=socks5://127.0.0.1:{SOCKS}" in c and c[-1] == "--enable-logging", "electron: flags + extra args")
fake_ps = f"{slack}/Contents/MacOS/Slack\n"
check(tr.app_running(slack), "app_running detects running Slack")
fake_ps = ""
check(not tr.app_running(slack), "app_running false when not running")

e_notes = entry("app", notes)
tr.launch_entry(e_notes, cfg)
c = runs[-1]
check(f"ALL_PROXY=socks5h://127.0.0.1:{SOCKS}" in c and f"HTTPS_PROXY=http://127.0.0.1:{TUN}" in c and c[c.index("ALL_PROXY=socks5h://127.0.0.1:%d" % SOCKS) - 1] == "--env",
      "env app: open --env vars")
check("--args" not in c, "env app: no --args when none given")

e_tb = entry("app", torb)
tr.launch_entry(e_tb, cfg)
check(runs[-1] == ["/usr/bin/open", "-a", torb], "tor browser: plain open")

# scripts
scripts = HOME / "my scripts"; scripts.mkdir()
py = scripts / "fetch prices.py"; py.write_text("print('hi')\n")
sh = scripts / "run.sh"; sh.write_text("echo hi\n")
exe = scripts / "tool"; exe.write_text("#!/bin/sh\necho hi\n"); exe.chmod(0o755)
other = scripts / "thing.txt"; other.write_text("echo hi\n")
check(tr.script_argv(py)[1:] == [str(py)] and tr.script_argv(py)[0].endswith("python3"), "script_argv .py -> python3")
check(tr.script_argv(exe) == [str(exe)], "script_argv executable -> direct")
check(tr.script_argv(other) == ["/bin/zsh", str(other)], "script_argv unknown -> zsh")

e_py = entry("script", str(py), args="--coin BTC")
m = tr.launch_entry(e_py, cfg)
argv, kw = spawns[-1]
check(argv[0] == "/usr/bin/osascript", "script (terminal): osascript")
do = argv[argv.index("-e", 3) + 1]
check(do.startswith('do script "') and do.endswith('"'), "script (terminal): AppleScript do script string")
inner = do[len('do script "'):-1].replace('\\"', '"').replace("\\\\", "\\")
check(f"export ALL_PROXY=socks5h://127.0.0.1:{SOCKS}" in inner, "script (terminal): exports proxy env")
check(f"cd {shlex.quote(str(scripts))}" in inner, "script (terminal): cd into script dir (quoted, has space)")
check(inner.endswith(shlex.join(tr.script_argv(py) + ['--coin', 'BTC'])), "script (terminal): runs interpreter + script + args")
# run the generated shell line for real (minus osascript) to prove it's valid shell
probe = inner.replace(shlex.join(tr.script_argv(py) + ['--coin', 'BTC']), 'env | grep -E "^(ALL_PROXY|HTTPS_PROXY|NODE_USE_ENV_PROXY)=" | sort; pwd')
probe = probe.replace("clear; ", "")
out = real_run(["/bin/bash", "-c", probe], capture_output=True, text=True).stdout
check(f"ALL_PROXY=socks5h://127.0.0.1:{SOCKS}" in out and f"HTTPS_PROXY=http://127.0.0.1:{TUN}" in out and str(scripts) in out,
      "script (terminal): generated shell line executes correctly")

e_bg = entry("script", str(sh), terminal=False)
m = tr.launch_entry(e_bg, cfg)
argv, kw = spawns[-1]
check(argv[1:] == [str(sh)] and argv[0].endswith("bash"), "script (background): bash run.sh")
check(kw["env"]["ALL_PROXY"] == f"socks5h://127.0.0.1:{SOCKS}" and kw["cwd"] == str(scripts), "script (background): env + cwd")
check("Output:" in m and (tr.LOGS / f"{e_bg['profile']}.log").exists(), "script (background): log file created")

# proxychains missing, then present
e_pc = entry("script", str(py), method="proxychains")
try:
    tr.launch_entry(e_pc, cfg); check(False, "proxychains missing should raise")
except tr.LaunchError as ex:
    check("brew install proxychains-ng" in str(ex), "proxychains missing -> install hint")
(binx / "proxychains4").write_text("#!/bin/sh\nexit 0\n"); (binx / "proxychains4").chmod(0o755)
tr.launch_entry(e_pc, cfg)
argv, kw = spawns[-1]
do = argv[argv.index("-e", 3) + 1]
check(f"{binx}/proxychains4 -q -f {tr.PC_CONF}" in do and "export ALL_PROXY" not in do, "proxychains (terminal): prefix, no env exports")

e_cmd = entry("command", "curl -s https://check.torproject.org/api/ip", terminal=False, method="proxychains")
tr.launch_entry(e_cmd, cfg)
argv, kw = spawns[-1]
check(argv[:2] == ["/bin/zsh", "-lc"] and argv[2].startswith(f"{binx}/proxychains4 -q -f") and argv[2].endswith("curl -s https://check.torproject.org/api/ip"),
      "command (background, proxychains): zsh -lc 'proxychains4 … curl …'")
e_cmd2 = entry("command", "git ls-remote https://github.com/torproject/tor.git", terminal=True)
tr.launch_entry(e_cmd2, cfg)
check(e_cmd2["name"] == "git", "command entry named after first word")

e_bad = entry("script", str(py), args='--x "unterminated')
try:
    tr.launch_entry(e_bad, cfg); check(False, "bad args should raise")
except tr.LaunchError:
    check(True, "bad shell args -> LaunchError")

# AppleScript escaping with quotes/backslashes in path
weird = scripts / 'say "hi" \\ there.sh'; weird.write_text("echo\n")
tr.launch_entry(entry("script", str(weird)), cfg)
argv, _ = spawns[-1]
do = argv[argv.index("-e", 3) + 1]
body = do[len('do script "'):-1]
import re
check(re.search(r'(?<!\\)"', body) is None, "AppleScript string: no unescaped double quotes")

print("\n== config round trip ==")
cfg2 = dict(cfg); cfg2["entries"] = [e_chrome, e_py, {"kind": "bogus", "path": "x"}, {"kind": "app", "path": chrome, "method": "proxychains"}]
tr.save_config(cfg2)
back = tr.load_config()
check(len(back["entries"]) == 3 and back["entries"][2]["method"] == "env", "load_config drops bad entries and fixes invalid methods")
tr.CONFIG.write_text("{not json")
back = tr.load_config()
check(back["entries"] == [] and (tr.BASE / "config.broken.json").exists(), "corrupt config is set aside")

print("\n== GUI smoke test (stubbed Qt) ==")
tr._spawn = lambda argv, **kw: spawns.append((argv, kw))
cfg_gui = tr.load_config(); cfg_gui.update(control_port=CTRL, socks_port=SOCKS, http_tunnel_port=TUN); tr.save_config(cfg_gui)
w = tr.MainWindow()
check(w.state == "stopped" and "stopped" in w.status_lbl.text(), "window starts with Tor stopped")
w.add_path(chrome); w.add_path(str(py)); w.add_path(slack)
check(w.table.rowCount() == 3 and [e["method"] for e in w.cfg["entries"]] == ["chromium", "env", "electron"], "add_path adds rows with detected methods")
check("Electron proxy flags" in w.info_lbl.text() and "Quit the app first" in w.info_lbl.text(), "info label explains detected method")
combo = w.table.cellWidget(1, 1)
combo.setCurrentIndex(combo.findData("proxychains"))
check(json.loads(tr.CONFIG.read_text())["entries"][1]["method"] == "proxychains", "method combo change persists")
it = w.table.item(0, 2); it.setText("--incognito"); w.on_item_changed(it)
check(w.cfg["entries"][0]["args"] == "--incognito", "editing args persists")
it = w.table.item(1, 3); it.setCheckState(tr.Qt.CheckState.Unchecked); w.on_item_changed(it)
check(w.cfg["entries"][1]["terminal"] is False, "terminal checkbox persists")
it = w.table.item(0, 0); it.setText("Chrome (Tor)"); w.on_item_changed(it)
check(w.cfg["entries"][0]["name"] == "Chrome (Tor)", "rename persists")

# launch while stopped with autostart -> spawns tor, queues; then fake tor comes up -> launch fires
(binx / "tor").write_text("#!/bin/sh\nexit 0\n")
w.table.selectRow(0)
n = len(runs)
w.launch_selected()
check(w.start_proc is not None and w.pending == [w.cfg["entries"][0]["id"]], "launch while stopped: starts Tor and queues app")
w.start_proc.wait(5)
w.refresh_status()
check(w.state == "bootstrapping", "while starting: state bootstrapping")
ft2 = FakeTor(CTRL, SOCKS, progress=60)
w.refresh_status()
check(w.state == "bootstrapping" and "60%" in w.status_lbl.text() and len(runs) == n, "60%: still waiting, nothing launched")
ft2.progress = 100
w.refresh_status()
check(w.state == "running" and len(runs) == n + 1 and w.pending == [], "100%: queued app launched")
check("--user-data-dir=" in " ".join(runs[-1]), "queued launch used chromium method")
w.new_identity()
check("NEWNYM" in ft2.signals, "New identity button sends NEWNYM")
# electron already running -> asks; answer No -> no launch
fake_ps = f"{slack}/Contents/MacOS/Slack\n"
n = len(runs); w.request_launch(w.cfg["entries"][2]["id"])
check(len(runs) == n, "electron already running + 'No' -> not launched")
tr.QMessageBox.question = staticmethod(lambda *a, **k: tr.QMessageBox.StandardButton.Yes)
w.request_launch(w.cfg["entries"][2]["id"])
check(len(runs) == n + 1, "electron already running + 'Yes' -> launched")
fake_ps = ""
w.toggle_tor()
time.sleep(0.2); w.refresh_status()
check("HALT" in ft2.signals and w.state == "stopped", "toggle stops Tor")
w.table.selectRow(1); w.remove_selected()
check(w.table.rowCount() == 2 and all(e["kind"] != "script" for e in w.cfg["entries"]), "remove_selected removes row")
# external tor on port
ext = socket.socket(); ext.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); ext.bind(("127.0.0.1", SOCKS)); ext.listen(4)
w.refresh_status()
check(w.state == "external", "foreign listener on SOCKS port -> external state")
ext.close(); w.refresh_status()
check(w.state == "stopped", "back to stopped")
w.open_tor_terminal()
check(spawns[-1][0][0] == "/usr/bin/osascript" and "Tor is off" in w.info_lbl.text(), "Open Tor Terminal works and warns when Tor is off")
w.quit_app()
check(w._shut_down, "quit runs shutdown")

print("\n" + ("ALL TESTS PASSED" if not failures else f"{len(failures)} FAILURE(S): " + "; ".join(failures)))
sys.exit(1 if failures else 0)
