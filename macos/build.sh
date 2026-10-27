#!/bin/bash
# Build the remoteexec menu bar app: ./build.sh            -> build/remoteexec.app
#                                    ./build.sh --install  -> also copy to /Applications and launch
# Needs Xcode or the Command Line Tools (xcode-select --install), macOS 13+.
set -euo pipefail
cd "$(dirname "$0")"

# SwiftPM's default build system fails to initialise on some Command Line Tools-only
# setups ("Unknown error parsing property list"); fall back to the native one.
BS=()
swift build -c release || { BS=(--build-system native); swift build -c release "${BS[@]}"; }
BIN="$(swift build -c release ${BS[@]+"${BS[@]}"} --show-bin-path)/RemoteExecBar"
APP="build/remoteexec.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/RemoteExec"
cp Info.plist "$APP/Contents/Info.plist"
cp AppIcon.icns "$APP/Contents/Resources/"   # regenerate/replace: see make_icon.py
codesign --force --deep --sign - "$APP"   # ad-hoc signature (required on Apple Silicon)

if [ "${1:-}" = "--install" ]; then
    pkill -x RemoteExec 2>/dev/null || true
    rm -rf /Applications/remoteexec.app
    cp -R "$APP" /Applications/
    touch /Applications/remoteexec.app   # nudge Finder/Dock to drop a cached icon
    open /Applications/remoteexec.app
    echo "installed /Applications/remoteexec.app"
else
    echo "built $APP  (run: open $APP)"
fi
