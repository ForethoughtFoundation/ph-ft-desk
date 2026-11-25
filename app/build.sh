#!/bin/bash
# Build Forethought Desk.app and install it in /Applications.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp -d)"
app="$out/Forethought Desk.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
swiftc -O -o "$app/Contents/MacOS/ForethoughtDesk" "$here/main.swift"
cp "$here/Info.plist" "$app/Contents/Info.plist"
# Icon: the orange Forethought mark on off-white.
iconset="$out/AppIcon.iconset"; mkdir -p "$iconset"
for s in 16 32 64 128 256 512 1024; do
  inner=$((s * 62 / 100)); pad=$(((s - inner) / 2)); r=$((s * 22 / 100))
  rsvg-convert -h "$inner" "path/to/your-icon.svg" -o "$out/mark.png"
  magick -size ${s}x${s} xc:none -fill '#FBFAF4' -draw "roundrectangle 0,0,$((s-1)),$((s-1)),$r,$r" \
    "$out/mark.png" -gravity center -composite "$out/i$s.png"
done
for s in 16 32 128 256 512; do
  cp "$out/i$s.png" "$iconset/icon_${s}x${s}.png"; cp "$out/i$((s*2)).png" "$iconset/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
# A stable local identity keeps macOS permission grants across rebuilds.
codesign -s "-" --deep "$app"
if [ -d "/Applications/Forethought Desk.app" ]; then
  osascript -e 'quit app "Forethought Desk"' 2>/dev/null || true
  trash "/Applications/Forethought Desk.app"
fi
mv "$app" /Applications/
echo "/Applications/Forethought Desk.app"
