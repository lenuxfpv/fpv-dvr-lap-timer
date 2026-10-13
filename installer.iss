; Inno Setup script for the FPV DVR Lap Timer Windows installer (FPVDVRLapTimer-Setup.exe).
; Built by the "Build Windows exe" workflow after PyInstaller has produced dist\FPVDVRLapTimer.exe:
;   iscc /DMyAppVersion=1.2.3 installer.iss
; Per-user install (no admin rights): the app goes in %LOCALAPPDATA%\Programs, with a Start Menu
; shortcut, an optional Desktop shortcut, and an entry in Settings > Apps.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{B7C5D2E1-6F3A-4C8B-9E2D-5A1F4D7C3B90}
AppName=FPV DVR Lap Timer
AppVersion={#MyAppVersion}
AppPublisher=LenuxFPV
DefaultDirName={autopf}\FPV DVR Lap Timer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=FPVDVRLapTimer-Setup
SetupIconFile=fpv-dvr-lap-timer.ico
UninstallDisplayIcon={app}\FPVDVRLapTimer.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\FPVDVRLapTimer.exe"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; leftovers of older versions (the old exe name and the old "FPV Lap Timer" shortcuts), so an upgrade is clean
Type: files; Name: "{app}\FPVLapTimer.exe"
Type: files; Name: "{autoprograms}\FPV Lap Timer.lnk"
Type: files; Name: "{autodesktop}\FPV Lap Timer.lnk"

[Icons]
Name: "{autoprograms}\FPV DVR Lap Timer"; Filename: "{app}\FPVDVRLapTimer.exe"
Name: "{autodesktop}\FPV DVR Lap Timer"; Filename: "{app}\FPVDVRLapTimer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FPVDVRLapTimer.exe"; Description: "Launch FPV DVR Lap Timer"; Flags: nowait postinstall skipifsilent

[Code]
// Uninstall never touches the library or the videos. Offer to free the disk space used by the
// smooth-scrub copies (they can be rebuilt); default is to keep them. Skipped for silent uninstalls.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Cache, OldCache: String;
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    Cache := ExpandConstant('{localappdata}\fpv-dvr-lap-timer');
    OldCache := ExpandConstant('{localappdata}\fpv-lap-timer');     // left by versions before 1.10
    if DirExists(Cache) or DirExists(OldCache) then
      if MsgBox('Also delete the smooth-scrub copies in' + #13#10 + ExpandConstant('{localappdata}') + '\fpv-dvr-lap-timer' + #13#10 +
                'to free disk space? They can be rebuilt.' + #13#10#13#10 +
                'Your library and videos are not touched either way.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(Cache, True, True, True);
        DelTree(OldCache, True, True, True);
      end;
  end;
end;
