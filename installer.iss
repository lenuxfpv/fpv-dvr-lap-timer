; Inno Setup script for the FPV Lap Timer Windows installer (FPVLapTimer-Setup.exe).
; Built by the "Build Windows exe" workflow after PyInstaller has produced dist\FPVLapTimer.exe:
;   iscc /DMyAppVersion=1.2.3 installer.iss
; Per-user install (no admin rights): the app goes in %LOCALAPPDATA%\Programs, with a Start Menu
; shortcut, an optional Desktop shortcut, and an entry in Settings > Apps.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{B7C5D2E1-6F3A-4C8B-9E2D-5A1F4D7C3B90}
AppName=FPV Lap Timer
AppVersion={#MyAppVersion}
AppPublisher=FPV Lap Timer
DefaultDirName={autopf}\FPV Lap Timer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=FPVLapTimer-Setup
SetupIconFile=fpv-lap-timer.ico
UninstallDisplayIcon={app}\FPVLapTimer.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\FPVLapTimer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\FPV Lap Timer"; Filename: "{app}\FPVLapTimer.exe"
Name: "{autodesktop}\FPV Lap Timer"; Filename: "{app}\FPVLapTimer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FPVLapTimer.exe"; Description: "Launch FPV Lap Timer"; Flags: nowait postinstall skipifsilent

[Code]
// Uninstall never touches the library or the videos. Offer to free the disk space used by the
// smooth-scrub copies (they can be rebuilt); default is to keep them. Skipped for silent uninstalls.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Cache: String;
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    Cache := ExpandConstant('{localappdata}\fpv-lap-timer');
    if DirExists(Cache) then
      if MsgBox('Also delete the smooth-scrub copies in' + #13#10 + Cache + #13#10 +
                'to free disk space? They can be rebuilt.' + #13#10#13#10 +
                'Your library and videos are not touched either way.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Cache, True, True, True);
  end;
end;
