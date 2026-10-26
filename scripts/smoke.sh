#!/usr/bin/env bash
# Installs the APK on the test phone, opens each tab in light and dark mode,
# takes screenshots, and fails if the app crashed.
set -u
PKG=com.sakib.notionsync
mkdir -p shots

tap_label() {
  adb shell uiautomator dump /sdcard/ui.xml > /dev/null 2>&1
  adb pull /sdcard/ui.xml ui.xml > /dev/null 2>&1
  python3 - "$1" <<'EOF'
import re, sys, subprocess
label = sys.argv[1]
xml = open("ui.xml", encoding="utf-8").read()
for node in re.findall(r"<node [^>]*>", xml):
    if f'content-desc="{label}"' in node or f'text="{label}"' in node:
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        x1, y1, x2, y2 = map(int, b.groups())
        subprocess.run(["adb", "shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2)])
        break
EOF
  sleep 2
}

shoot() {
  adb exec-out screencap -p > "shots/$1.png"
}

adb install -r NotionPhoneSync.apk
adb shell pm grant $PKG android.permission.POST_NOTIFICATIONS || true
adb logcat -c

for mode in light dark; do
  if [ "$mode" = dark ]; then adb shell cmd uimode night yes; else adb shell cmd uimode night no; fi
  adb shell am force-stop $PKG
  adb shell am start -W -n $PKG/.MainActivity
  sleep 6
  tap_label "Backup";   shoot "$mode-1-backup"
  adb shell input swipe 540 1900 540 500 500; sleep 1; shoot "$mode-1b-backup-lower"
  tap_label "Settings"; shoot "$mode-2-settings"
  adb shell input swipe 540 1900 540 400 500; sleep 1; shoot "$mode-2b-settings-lower"
  tap_label "Files";    shoot "$mode-4-files"
  tap_label "Activity"; shoot "$mode-3-activity"
done

if adb logcat -d -b crash | grep -q "Process: $PKG"; then
  echo "::error::The app crashed on the test phone"
  adb logcat -d -b crash | head -80
  exit 1
fi
if ! adb shell pidof $PKG > /dev/null; then
  echo "::error::The app is not running after the test"
  exit 1
fi
echo "Smoke test passed"
