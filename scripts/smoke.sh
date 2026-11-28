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

adb logcat -c
# Install the previous release first, open it, then update over it (like a real update).
if [ -f prev/NotionPhoneSync.apk ]; then
  adb install prev/NotionPhoneSync.apk
  adb shell am start -W -n $PKG/.MainActivity
  sleep 6
  shoot "0-previous-version"
  adb shell am force-stop $PKG
fi
adb install -r NotionPhoneSync.apk
adb shell pm grant $PKG android.permission.POST_NOTIFICATIONS || true

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

# Flashcards screen: opens, add-cards dialog opens, a card can be added and studied.
adb shell cmd uimode night no
adb shell am start -W -n $PKG/.FlashcardsActivity
sleep 4
shoot "flash-1-home"
tap_label "+  Add cards"
adb shell input text "What%sdoes%sTRIPS%sstand%sfor?"
tap_label "Back (the answer)"
adb shell input text "Trade-Related%sAspects%sof%sIP%sRights"
shoot "flash-2-add"
adb shell input keyevent 4      # hide the keyboard
sleep 1
tap_label "Add"
tap_label "Close"
sleep 1
shoot "flash-3-home-with-deck"
tap_label "My first deck"
sleep 2
shoot "flash-4-question"
tap_label "Show answer"
shoot "flash-5-answer"
for f in shots/flash-*.png; do echo "SHOT $(basename "$f") $(base64 -w0 "$f")"; done

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
