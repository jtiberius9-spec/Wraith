#!/bin/bash
# Build Wraith.app + Wraith-mac.dmg ON A MAC.  (Windows can't cross-build this.)
#
# Prereqs: Homebrew (https://brew.sh) and Python 3.11+.
# Usage:   chmod +x build_mac.sh && ./build_mac.sh
#
# Produces:  dist/Wraith.app  and  Wraith-0.3.0-mac.dmg
set -e
cd "$(dirname "$0")"
VERSION=0.3.0

echo "==> 1/6  Homebrew deps (scrcpy gives the server jar; plus adb + ffmpeg)"
brew list scrcpy >/dev/null 2>&1            || brew install scrcpy
brew list ffmpeg >/dev/null 2>&1            || brew install ffmpeg
brew list android-platform-tools >/dev/null 2>&1 || brew install --cask android-platform-tools

echo "==> 2/6  Python venv + deps"
python3 -m venv .venv-mac
source .venv-mac/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller pillow

echo "==> 3/6  Stage bundled binaries into bin/  (mac adb + ffmpeg + scrcpy-server)"
rm -rf bin && mkdir bin
cp "$(command -v adb)"    bin/adb
cp "$(command -v ffmpeg)" bin/ffmpeg
# scrcpy-server jar from the Homebrew scrcpy install (version must match the app: 4.0)
SRV="$(brew --prefix scrcpy)/share/scrcpy/scrcpy-server"
cp "$SRV" bin/scrcpy-server
chmod +x bin/adb bin/ffmpeg

echo "==> 4/6  App icon (.icns) from the ghost PNG"
ICONSET=wraith.iconset; rm -rf "$ICONSET"; mkdir "$ICONSET"
for s in 16 32 64 128 256 512; do
  sips -z $s $s wraith_icon_preview.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s*2)); sips -z $d $d wraith_icon_preview.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o wraith.icns
rm -rf "$ICONSET"

echo "==> 5/6  PyInstaller build (Wraith.app)"
rm -rf build dist
pyinstaller --noconfirm --clean Wraith-mac.spec

echo "==> 6/6  Package Wraith-${VERSION}-mac.dmg"
rm -f "Wraith-${VERSION}-mac.dmg"
hdiutil create -volname "Wraith" -srcfolder "dist/Wraith.app" -ov -format UDZO \
  "Wraith-${VERSION}-mac.dmg"

echo
echo "DONE -> Wraith-${VERSION}-mac.dmg   (also dist/Wraith.app)"
echo "Upload the .dmg to your GitHub release alongside the Windows installer."
echo
echo "NOTE: it's unsigned, so first launch needs right-click -> Open (Gatekeeper)."
