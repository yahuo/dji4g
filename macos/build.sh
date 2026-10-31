#!/usr/bin/env bash
# Builds macos/build/DJISMS.app; `--install` also copies it to /Applications.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
app="$here/build/DJISMS.app"

install=0
for arg in "$@"; do
  case "$arg" in
    --install) install=1 ;;
    *) echo "usage: $0 [--install]" >&2; exit 2 ;;
  esac
done

swift build -c release --package-path "$here"
binary="$(swift build -c release --package-path "$here" --show-bin-path)/DJISMS"

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  printf '%s' "$value"
}

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$binary" "$app/Contents/MacOS/DJISMS"

# AppIcon.icon is an Icon Composer document. actool compiles it to Assets.car and
# also writes AppIcon.icns for macOS 14 and 15; it ships with Xcode 26+, not the
# command line tools.
if xcrun --find actool >/dev/null 2>&1; then
  if ! out="$(xcrun actool "$here/AppIcon.icon" --compile "$app/Contents/Resources" \
      --platform macosx --minimum-deployment-target 14.0 --app-icon AppIcon \
      --output-partial-info-plist "$here/build/AppIcon.plist" \
      --output-format human-readable-text --errors --warnings 2>&1)"; then
    printf '%s\n' "$out" >&2
    exit 1
  fi
else
  echo "actool not found (needs Xcode 26+); building without the app icon" >&2
fi

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>io.github.yahuo.dji4g.sms</string>
  <key>CFBundleName</key>
  <string>DJI 短信</string>
  <key>CFBundleDisplayName</key>
  <string>DJI 短信</string>
  <key>CFBundleExecutable</key>
  <string>DJISMS</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIconName</key>
  <string>AppIcon</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>LSMinimumSystemVersion</key>
  <string>14.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key>
      <string>io.github.yahuo.dji4g.sms</string>
      <key>CFBundleURLSchemes</key>
      <array>
        <string>dji4g</string>
      </array>
    </dict>
  </array>
  <key>DJI4GEngineRoot</key>
  <string>$(xml_escape "$root")</string>
</dict>
</plist>
PLIST
plutil -lint "$app/Contents/Info.plist" >/dev/null

codesign --force --sign - --timestamp=none "$app"
echo "built $app"

if [ "$install" -eq 1 ]; then
  target="/Applications/DJISMS.app"
  rm -rf "$target"
  ditto "$app" "$target"
  echo "installed $target"
fi
