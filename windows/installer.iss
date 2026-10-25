; Installer for Notion Laptop Sync (built by GitHub Actions with Inno Setup 6)
#ifndef AppBuild
  #define AppBuild "0"
#endif

[Setup]
AppId={{6E4B7C2A-9F31-4D8E-A5B2-3C1F0D9E7A64}
AppName=Notion Laptop Sync
AppVersion=1.0.{#AppBuild}
AppPublisher=Sakib
DefaultDirName={localappdata}\Programs\NotionLaptopSync
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=NotionLaptopSync-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\NotionLaptopSync.exe
UninstallDisplayName=Notion Laptop Sync
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\NotionLaptopSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Notion Laptop Sync"; Filename: "{app}\NotionLaptopSync.exe"
Name: "{userdesktop}\Notion Laptop Sync"; Filename: "{app}\NotionLaptopSync.exe"; Tasks: desktopicon

[Run]
; Normal install: offer to open the app at the end.
Filename: "{app}\NotionLaptopSync.exe"; Description: "Open Notion Laptop Sync"; Flags: postinstall nowait skipifsilent
; Silent install (the in-app Update button): reopen quietly in the tray.
Filename: "{app}\NotionLaptopSync.exe"; Parameters: "--minimized --after-update"; Flags: nowait; Check: WizardSilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'NotionLaptopSync');
end;
