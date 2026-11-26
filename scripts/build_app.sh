#!/bin/zsh
# Builds a self-contained dist/CrateDigger.app: the app code and its own Python
# environment live inside the bundle, so it keeps working if this project folder moves.
#   ./scripts/build_app.sh            build dist/CrateDigger.app
#   ./scripts/build_app.sh --install  build, then install to /Applications/CrateDigger.app
# Rebuild after changing the code. Needs Homebrew Python (the bundle's venv links to it).
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY=$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)
APP="dist/CrateDigger.app"
RES="$APP/Contents/Resources"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/app"

# 1. code
rsync -a --exclude __pycache__ cratedigger nicotine_plugin requirements.txt "$RES/app/"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' cratedigger/__init__.py)
# 2. private Python environment
"$PY" -m venv "$RES/venv"
"$RES/venv/bin/pip" install -q --upgrade pip
"$RES/venv/bin/pip" install -q -r requirements.txt
# 3. icon
"$RES/venv/bin/python" scripts/make_icon.py cratedigger/resources/icon.png
cp cratedigger/resources/icon.png "$RES/app/cratedigger/resources/icon.png"
ICONSET=$(mktemp -d)/icon.iconset; mkdir -p "$ICONSET"
for s in 16 32 64 128 256 512; do
  sips -z $s $s cratedigger/resources/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) cratedigger/resources/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/CrateDigger.icns"
# 4. launcher + Info.plist
cat > "$APP/Contents/MacOS/CrateDigger" <<'LAUNCH'
#!/bin/zsh
# Finder starts apps with a minimal PATH; add Homebrew so ffmpeg/ffprobe are found.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
RES="${0:A:h:h}/Resources"
cd "$RES/app"
exec "$RES/venv/bin/python" -m cratedigger "$@"
LAUNCH
chmod +x "$APP/Contents/MacOS/CrateDigger"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>CrateDigger</string>
  <key>CFBundleDisplayName</key><string>CrateDigger</string>
  <key>CFBundleIdentifier</key><string>local.cratedigger.app</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>CrateDigger</string>
  <key>CFBundleIconFile</key><string>CrateDigger</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
echo "Built $ROOT/$APP ($(du -sh "$APP" | cut -f1))"

if [[ "$1" == "--install" ]]; then
  DEST=/Applications/CrateDigger.app
  if [[ -e "$DEST" ]]; then
    # Only ever replace a previous CrateDigger build, never some other app.
    ID=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$DEST/Contents/Info.plist" 2>/dev/null || true)
    [[ "$ID" == "local.cratedigger.app" ]] || { echo "$DEST exists and is not a CrateDigger build; not touching it."; exit 1; }
    rm -rf "$DEST"
  fi
  ditto "$APP" "$DEST"
  echo "Installed $DEST"
fi
