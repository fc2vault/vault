#!/bin/bash
# Build Vault-<version>-mac-<arch>.dmg: Vault.app with its own Python and ffmpeg, nothing to install.
#   build_mac.sh <release-dir> <arm64|x86_64> <out-dir> [downloads-dir]
# <release-dir> is a folder made by make_release.sh. Runs on any Mac with the command-line tools
# (swiftc, hdiutil); the downloads are cached in [downloads-dir] (default <out-dir>/_downloads).
set -euo pipefail
REL="$(cd "$1" && pwd)"; ARCH="$2"; OUT="$(mkdir -p "$3" && cd "$3" && pwd)"
DL="${4:-$OUT/_downloads}"; mkdir -p "$DL"
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$REL/VERSION" 2>/dev/null || echo 0)"
[ -f "$REL/serve.py" ] || { echo "no release at $REL"; exit 1; }

# Pinned inputs: standalone CPython (astral-sh/python-build-standalone) and static ffmpeg builds
# (ffmpeg.martin-riedl.de, signed and notarized). The checksums keep a changed download out.
PB=https://github.com/astral-sh/python-build-standalone/releases/download/20261110
case "$ARCH" in
  arm64)
    PY_URL="$PB/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only_stripped.tar.gz"
    PY_SHA=2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd
    FF=https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2
    FFMPEG_SHA=c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924
    FFPROBE_SHA=fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6
    TARGET=arm64-apple-macos11 ;;
  x86_64)
    PY_URL="$PB/cpython-3.12.16%2B20261110-x86_64-apple-darwin-install_only_stripped.tar.gz"
    PY_SHA=b78a4c479f1964ec484880bee567568266f68da102269fb93a05fe8716b45f91
    FF=https://ffmpeg.martin-riedl.de/download/macos/amd64/1789931006_9.0.2
    FFMPEG_SHA=7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4
    FFPROBE_SHA=2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd
    TARGET=x86_64-apple-macos10.15 ;;
  *) echo "arch must be arm64 or x86_64"; exit 1 ;;
esac

fetch() {   # url file sha256
  local f="$DL/$2"
  [ -s "$f" ] || curl -fsSL --retry 3 -o "$f" "$1"
  echo "$3  $f" | shasum -a 256 -c --quiet - || { echo "checksum mismatch: $2"; rm -f "$f"; exit 1; }
}
fetch "$PY_URL" "mac-$ARCH-python.tar.gz" "$PY_SHA"
fetch "$FF/ffmpeg.zip" "mac-$ARCH-ffmpeg.zip" "$FFMPEG_SHA"
fetch "$FF/ffprobe.zip" "mac-$ARCH-ffprobe.zip" "$FFPROBE_SHA"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
APP="$WORK/Vault.app"
R="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$R/app" "$R/bin"

cp -R "$REL"/. "$R/app/"
rm -rf "$R/app/.git" "$R/app/.github" "$R/app/config.json" "$R/app/cache" "$R/app/tests" \
       "$R/app/start.command" "$R/app/make_app.sh" "$R/app/packaging"
tar -xzf "$DL/mac-$ARCH-python.tar.gz" -C "$R"            # -> python/
rm -rf "$R/python/lib/python3.12/test" "$R/python/lib/python3.12/idlelib" \
       "$R/python/lib/python3.12/tkinter" "$R/python/lib/python3.12/turtledemo" "$R/python/share"
unzip -q -o "$DL/mac-$ARCH-ffmpeg.zip" -d "$R/bin"
unzip -q -o "$DL/mac-$ARCH-ffprobe.zip" -d "$R/bin"
chmod +x "$R/bin/ffmpeg" "$R/bin/ffprobe"
cp "$REL/Vault.icns" "$R/Vault.icns" 2>/dev/null || true
cp "$HERE/THIRD-PARTY.txt" "$R/THIRD-PARTY.txt"

swiftc -O -target "$TARGET" -o "$APP/Contents/MacOS/Vault" "$HERE/VaultLauncher.swift"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Vault</string>
  <key>CFBundleDisplayName</key><string>Vault</string>
  <key>CFBundleIdentifier</key><string>io.github.fc2vault.vault</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>Vault</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>Vault.icns</string>
  <key>LSMinimumSystemVersion</key><string>$( [ "$ARCH" = arm64 ] && echo 11.0 || echo 10.15 )</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Free software. Python and ffmpeg are included under their own licenses (THIRD-PARTY.txt).</string>
</dict></plist>
PLIST

# Ad-hoc signature (no developer identity): Apple Silicon refuses to run unsigned code at all.
# The ffmpeg binaries keep their own notarized signatures.
find "$R/python" -type f \( -name "*.so" -o -name "*.dylib" \) -exec codesign --force -s - {} \; 2>/dev/null
codesign --force -s - "$R/python/bin/python3.12"
codesign --force -s - "$APP"

DMGDIR="$WORK/dmg"; mkdir -p "$DMGDIR"
mv "$APP" "$DMGDIR/"
ln -s /Applications "$DMGDIR/Applications"
NAME="Vault-$VERSION-mac-$( [ "$ARCH" = arm64 ] && echo apple-silicon || echo intel ).dmg"
rm -f "$OUT/$NAME"
hdiutil create -quiet -volname "Vault $VERSION" -srcfolder "$DMGDIR" -fs HFS+ -format UDZO -imagekey zlib-level=9 "$OUT/$NAME"
echo "$OUT/$NAME  $(du -h "$OUT/$NAME" | cut -f1)"
