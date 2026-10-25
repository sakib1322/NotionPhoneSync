# Notion Phone Sync

An Android app that backs up everything on your phone (photos, videos,
voice recordings, documents, downloads, WhatsApp media) to a Notion page.

- Every phone folder becomes a Notion sub-page with the same name
  (e.g. DCIM → Camera, Pictures → Screenshots).
- Runs automatically every 15 minutes in the background.
- No duplicates: each file is uploaded once. If a file is edited later,
  the old copy in Notion is replaced.
- Files deleted from the phone stay in Notion (it is a backup).

## Building

Every time files are uploaded to the `main` branch of this repository,
GitHub builds the app automatically (Actions tab, about 5 minutes).
The finished APK appears under **Releases** as `NotionPhoneSync.apk`.

Keep this repository **private**: it contains the app's signing key.

## Notion limits

- Up to 5 GB per file (paid plans).
- Notion refuses some file types (for example .apk or .amr). These are
  listed in the app under "Show skipped & failed files".
- Notion allows about 3 requests per second, so the first full backup
  can take hours or days. Later runs only upload new files.

---

# Notion Laptop Sync (Windows 11)

The `windows/` folder holds the Windows version: it backs up chosen folders
(by default Desktop, Documents, Downloads, Pictures, Videos, Music) to a
separate Notion page such as **Laptop Backup**, with the same folder
mirroring, duplicate protection, auto-sync timer, speed meter and in-app
updates. It runs in the system tray and can start with Windows.

GitHub builds `NotionLaptopSync-Setup.exe` whenever `windows/` changes
(releases tagged `win-build-N`). Settings and progress live in
`%APPDATA%\NotionLaptopSync` and survive updates and reinstalls.
