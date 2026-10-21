#!/bin/bash
# Dry-runs install-mac-tor-proxy.sh in a throwaway $HOME with stand-ins for brew, pip,
# open, sips and curl, then checks what it created, torrun, and --uninstall.
# Safe to run on macOS or Linux; nothing outside the temp folder is touched.
#   bash tests/test_installer.sh
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
REALPY="$(command -v python3)"
SIM="$(mktemp -d "${TMPDIR:-/tmp}/mtp-install-test.XXXXXX")"
FAKE="$SIM/fakebin"; BREWPY="$SIM/brewpy"
mkdir -p "$SIM/home" "$FAKE" "$BREWPY/bin"
fails=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; fails=$((fails + 1)); }
check() { if eval "$1"; then pass "$2"; else fail "$2"; fi; }

# --- stand-ins -------------------------------------------------------------
cat > "$FAKE/uname" <<'EOF'
#!/bin/sh
echo Darwin
EOF
cat > "$FAKE/brew" <<EOF
#!/bin/sh
case "\$1" in
  install) echo "brew install \$*" >> "$SIM/brew.log" ;;
  --prefix) echo "$BREWPY" ;;
  services) echo "Name Status User File"; echo "tor  none" ;;
esac
EOF
cat > "$BREWPY/bin/python3.13" <<EOF
#!/bin/sh
if [ "\$1" = "-m" ] && [ "\$2" = "venv" ]; then
  mkdir -p "\$3/bin"
  cat > "\$3/bin/python" <<'PYEOF'
#!/bin/sh
if [ "\$1" = "-m" ] && [ "\$2" = "pip" ]; then echo "pip \$*" >> "$SIM/pip.log"; exit 0; fi
PYTHONPATH="$REPO/tests/qt_stub" exec "$REALPY" "\$@"
PYEOF
  chmod +x "\$3/bin/python"; exit 0
fi
exec "$REALPY" "\$@"
EOF
cat > "$FAKE/sips" <<'EOF'
#!/bin/sh
exit 1
EOF
cat > "$FAKE/open" <<EOF
#!/bin/sh
echo "open \$*" >> "$SIM/open.log"
EOF
cat > "$FAKE/curl" <<EOF
#!/bin/sh
# Simulated GitHub download: serves the repo's mac_tor_proxy.py unless MTP_FAKE_CURL_FAIL=1
[ "\${MTP_FAKE_CURL_FAIL:-0}" = "1" ] && exit 22
out=""; while [ \$# -gt 0 ]; do [ "\$1" = "-o" ] && out="\$2"; shift; done
cp "$REPO/mac_tor_proxy.py" "\$out"
EOF
if ! command -v plutil >/dev/null 2>&1; then
  cat > "$FAKE/plutil" <<EOF
#!/bin/sh
"$REALPY" -c 'import json,sys; print(json.load(open(sys.argv[2]))[sys.argv[1]])' "\$2" "\$6"
EOF
fi
cat > "$FAKE/proxychains4" <<'EOF'
#!/bin/sh
echo "proxychains4 $*"
EOF
chmod +x "$FAKE"/* "$BREWPY/bin/python3.13"
echo 'alias ll="ls -l"' > "$SIM/home/.zshrc"

export HOME="$SIM/home" PATH="$FAKE:$PATH"
B="$HOME/.mac-tor-proxy"; APP="$HOME/Applications/Mac Tor Proxy.app"

echo "== install from a checkout =="
bash "$REPO/install-mac-tor-proxy.sh" > "$SIM/install1.log" 2>&1; RC=$?
check '[ $RC -eq 0 ] && grep -q "is installed" "$SIM/install1.log"' "installer finishes"
check 'cmp -s "$REPO/mac_tor_proxy.py" "$B/mac_tor_proxy.py"' "app copied from checkout"
check '[ -x "$APP/Contents/MacOS/mac-tor-proxy" ]' "app launcher is executable"
check '"$REALPY" -c "import plistlib,sys; d=plistlib.load(open(sys.argv[1],\"rb\")); assert d[\"CFBundleExecutable\"]==\"mac-tor-proxy\" and d[\"CFBundleName\"]==\"Mac Tor Proxy\"" "$APP/Contents/Info.plist"' "Info.plist is valid"
check 'grep -q "PySide6-Essentials" "$SIM/pip.log"' "PySide6 installed into the venv"
check 'grep -q "tor proxychains-ng python@3.13" "$SIM/brew.log"' "brew installs tor, proxychains-ng, python@3.13"
check '[ "$(grep -c "# Mac Tor Proxy" "$HOME/.zshrc")" = 1 ]' "PATH line added to .zshrc"
check 'grep -q "Mac Tor Proxy.app" "$SIM/open.log"' "installer opens the app"
check '"$B/venv/bin/python" "$B/mac_tor_proxy.py" --version | grep -q "Mac Tor Proxy"' "--version works"

echo "== re-run (update) =="
bash "$REPO/install-mac-tor-proxy.sh" > "$SIM/install2.log" 2>&1
check '[ "$(grep -c "# Mac Tor Proxy" "$HOME/.zshrc")" = 1 ]' "re-run doesn't duplicate the PATH line"

echo "== install piped from curl (downloads the app) =="
rm -f "$B/mac_tor_proxy.py"
(cd "$SIM" && bash < "$REPO/install-mac-tor-proxy.sh" > "$SIM/install3.log" 2>&1)
check 'grep -q "downloading mac_tor_proxy.py" "$SIM/install3.log" && cmp -s "$REPO/mac_tor_proxy.py" "$B/mac_tor_proxy.py"' "app downloaded when run via a pipe"
rm -f "$B/mac_tor_proxy.py"
(cd "$SIM" && MTP_FAKE_CURL_FAIL=1 bash < "$REPO/install-mac-tor-proxy.sh" > "$SIM/install4.log" 2>&1)
check 'grep -q "git clone https://github.com/williamblake333/mac-tor-proxy.git" "$SIM/install4.log" && [ ! -f "$B/mac_tor_proxy.py" ]' "failed download stops with clone instructions"
bash "$REPO/install-mac-tor-proxy.sh" > /dev/null 2>&1

echo "== torrun =="
echo '{"socks_port": 9150, "http_tunnel_port": 9180}' > "$B/config.json"
ENVOUT="$("$B/bin/torrun" env)"
check 'grep -qx "ALL_PROXY=socks5h://127.0.0.1:9150" <<< "$ENVOUT" && grep -qx "HTTPS_PROXY=http://127.0.0.1:9180" <<< "$ENVOUT" && grep -qx "MAC_TOR_PROXY=1" <<< "$ENVOUT"' "torrun sets proxy env from config.json"
check '"$B/bin/torrun" -p python3 x.py | grep -q "proxychains4 -q -f $B/proxychains.conf python3 x.py" && grep -q "socks5 127.0.0.1 9150" "$B/proxychains.conf"' "torrun -p uses proxychains with the configured port"

echo "== uninstall =="
bash "$REPO/install-mac-tor-proxy.sh" --uninstall > /dev/null 2>&1
check '[ ! -e "$B" ] && [ ! -e "$APP" ]' "uninstall removes app and data"
check '! grep -q "# Mac Tor Proxy" "$HOME/.zshrc" && grep -q "alias ll" "$HOME/.zshrc"' "uninstall removes only its .zshrc line"

rm -rf "$SIM"
echo
if [ "$fails" -eq 0 ]; then echo "ALL INSTALLER TESTS PASSED"; else echo "$fails INSTALLER TEST(S) FAILED"; exit 1; fi
