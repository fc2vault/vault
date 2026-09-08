#!/bin/bash
# Wrap a built Vault release into a double-clickable macOS Vault.app.
# Usage:  ./make_app.sh [release-dir] [output.app]
#   release-dir : a folder produced by make_release.sh (default: ../../vault, the full build)
#   output.app  : where to write the bundle (default: ../../Vault.app)
#
# The app uses the system python3 and stores writable data (config, cache, DB) in
# ~/Library/Application Support/Vault, so the bundle itself stays read-only (works in /Applications).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
# A shipped release has scan.py next to this script; the dev checkout (webui/) does not.
if [ -f "$HERE/scan.py" ]; then
  SRC="${1:-$HERE}"; APP="${2:-$HERE/../Vault.app}"   # shipped: package this release
else
  ROOT="$(dirname "$HERE")"
  SRC="${1:-$ROOT/../vault}"; APP="${2:-$ROOT/../Vault.app}"   # dev: package the built release
fi

[ -f "$SRC/serve.py" ] || { echo "no release at $SRC — run make_release.sh first"; exit 1; }
echo "Packaging $SRC -> $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

# payload = the release bundle (serve.py, index.html, scan.py, …, data/catalog.db)
cp -R "$SRC"/. "$APP/Contents/Resources/app/"
rm -rf "$APP/Contents/Resources/app/.git" "$APP/Contents/Resources/app/config.json" \
       "$APP/Contents/Resources/app/cache" 2>/dev/null || true

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Vault</string>
  <key>CFBundleDisplayName</key><string>Vault</string>
  <key>CFBundleIdentifier</key><string>local.vault.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>Vault</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>Vault.icns</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/Vault" <<'LAUNCH'
#!/bin/bash
# Vault.app launcher: start the local server, open the browser, stop the server on quit.
APPDIR="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
export VAULT_DATA="$HOME/Library/Application Support/Vault"
mkdir -p "$VAULT_DATA"
CFG="$VAULT_DATA/config.json"
LOG="$VAULT_DATA/server.log"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  osascript -e 'display alert "Vault needs Python 3" message "Install it (e.g. run: xcode-select --install) then reopen Vault."'
  exit 1
fi

# first run: ask for the library folder (native picker) and remember it
if [ ! -f "$CFG" ]; then
  LIB=$(osascript -e 'try
    set f to choose folder with prompt "Select your video library folder for Vault"
    return POSIX path of f
  end try' 2>/dev/null)
  if [ -z "$LIB" ]; then exit 0; fi          # cancelled
  LIB="${LIB%/}"
  printf '{"library": "%s", "port": 8730}\n' "$LIB" > "$CFG"
fi

cd "$APPDIR"
"$PY" serve.py >"$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null; exit 0' TERM INT EXIT

# wait for the port to answer (the first run scans the whole library, which can take a while),
# then open the browser
for i in $(seq 1 300); do
  /usr/bin/curl -s -o /dev/null "http://127.0.0.1:8730/" && break
  # if the server died (e.g. bad library path), surface the log and stop
  kill -0 $SRV 2>/dev/null || { osascript -e "display alert \"Vault could not start\" message \"See $LOG\""; exit 1; }
  sleep 1
done
open "http://127.0.0.1:8730/"
wait $SRV
LAUNCH
chmod +x "$APP/Contents/MacOS/Vault"

[ -f "$HERE/Vault.icns" ] && cp "$HERE/Vault.icns" "$APP/Contents/Resources/Vault.icns"

# refresh Finder's icon/registration
touch "$APP"
echo "Built $APP"
echo "Data (config/cache/DB) will live in ~/Library/Application Support/Vault"
