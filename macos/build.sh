#!/bin/bash
# Build the remoteexec menu bar app: ./build.sh            -> build/remoteexec.app
#                                    ./build.sh --install  -> also copy to /Applications and launch
# Needs Xcode or the Command Line Tools (xcode-select --install), macOS 13+.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release
BIN="$(swift build -c release --show-bin-path)/RemoteExecBar"
APP="build/remoteexec.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/RemoteExec"
cp Info.plist "$APP/Contents/Info.plist"
[ -f AppIcon.icns ] && cp AppIcon.icns "$APP/Contents/Resources/" && \
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP"   # ad-hoc signature (required on Apple Silicon)

if [ "${1:-}" = "--install" ]; then
    pkill -x RemoteExec 2>/dev/null || true
    rm -rf /Applications/remoteexec.app
    cp -R "$APP" /Applications/
    open /Applications/remoteexec.app
    echo "installed /Applications/remoteexec.app"
else
    echo "built $APP  (run: open $APP)"
fi
