# Install FPV DVR Lap Timer for the current user (Windows).
#   install.bat                 install (Start Menu shortcut)
#   install.bat -Desktop        also add a Desktop shortcut
#   install.bat -Uninstall      remove the shortcuts (keeps your library and videos);
#                               asks whether to also delete the smooth-scrub copies
#   install.bat -Uninstall -ClearCache    same, and delete the copies without asking
# Install also adds an "FPV DVR Lap Timer" entry to Settings > Apps that runs the uninstall.
param([switch]$Desktop, [switch]$Uninstall, [switch]$ClearCache)
$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\FPV DVR Lap Timer.lnk"
$DesktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "FPV DVR Lap Timer.lnk"
# Entry in Settings > Apps (appwiz.cpl). HKCU, so no admin rights are needed.
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FPVLapTimer"

if ($Uninstall) {
    Remove-Item -Force -ErrorAction SilentlyContinue $StartMenu, $DesktopLnk
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $UninstallKey
    Write-Host "Shortcuts removed. Your library ($env:APPDATA\fpv-dvr-lap-timer) and videos were not touched."
    $Cache = Join-Path $env:LOCALAPPDATA "fpv-dvr-lap-timer"
    if (Test-Path $Cache) {
        $mb = [math]::Round((Get-ChildItem $Cache -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB)
        $clear = $ClearCache.IsPresent
        if (-not $clear) {
            $answer = Read-Host "Also delete the smooth-scrub copies in $Cache ($mb MB) to free disk space? They can be rebuilt. [y/N]"
            $clear = ($answer -match '^\s*y')
        }
        if ($clear) {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Cache
            Write-Host "Smooth-scrub copies deleted."
        } else {
            Write-Host "Smooth-scrub copies kept in $Cache ($mb MB) - delete that folder to free the space."
        }
    }
    if (Test-Path "$Dir\.venv") { Write-Host "Delete $Dir\.venv yourself if you no longer need it." }
    exit 0
}

# --- helpers: offer to install a missing prerequisite with winget ----------------------
function Update-SessionPath {
    # a program installed by winget isn't on this session's PATH until we re-read it
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Install-WithWinget($id, $label) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget is not available on this PC, so $label can't be installed automatically." -ForegroundColor Yellow
        return $false
    }
    $answer = Read-Host "Install $label now with winget? [Y/n]"
    if ($answer -match '^\s*n') { return $false }
    winget install --id $id -e --accept-package-agreements --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget could not install $label (exit code $LASTEXITCODE)." -ForegroundColor Yellow
        return $false
    }
    Update-SessionPath
    return $true
}

# --- Python ---------------------------------------------------------------------
function Test-Python($exe, [string[]]$pyArgs) {
    try {
        & $exe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
function Find-Python {
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python "py" @("-3"))) {
        return @{ Exe = "py"; Args = @("-3") }
    }
    if ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python "python" @())) {
        return @{ Exe = "python"; Args = @() }
    }
    return $null
}
$found = Find-Python
if (-not $found) {
    Write-Host "Python 3.9+ was not found." -ForegroundColor Yellow
    if (Install-WithWinget "Python.Python.3.12" "Python 3.12") { $found = Find-Python }
}
if (-not $found) {
    Write-Host "Python 3.9+ is still not available." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host "or run:  winget install Python.Python.3.12   then run this installer again."
    exit 1
}
$PyExe = $found.Exe
$PyArgs = $found.Args

# --- private environment with PyQt6 -------------------------------------------------
$VenvPy = Join-Path $Dir ".venv\Scripts\python.exe"
$VenvPyw = Join-Path $Dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $VenvPyw)) {
    Write-Host "Creating private Python environment in .venv ..."
    & $PyExe @PyArgs -m venv "$Dir\.venv"
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}
Write-Host "Installing PyQt6 (this can take a minute) ..."
& $VenvPy -m pip install --quiet --upgrade pip PyQt6
if ($LASTEXITCODE -ne 0) { throw "pip could not install PyQt6." }
& $VenvPy -c "import PyQt6.QtMultimedia" 2>$null
if ($LASTEXITCODE -ne 0) { throw "PyQt6 installed but Qt Multimedia failed to load." }

# --- ffmpeg / ffprobe -------------------------------------------------------------
function Get-MissingFfmpegTools {
    $m = @()
    foreach ($tool in @("ffmpeg", "ffprobe")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue) -and
            -not (Test-Path "$Dir\bin\$tool.exe") -and -not (Test-Path "$Dir\ffmpeg\bin\$tool.exe")) {
            $m += $tool
        }
    }
    return $m
}
$missing = @(Get-MissingFfmpegTools)
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host ("Missing: " + ($missing -join ", ") + ". The app runs without it, but can't build smooth-scrub copies") -ForegroundColor Yellow
    Write-Host "(scrubbing only jumps between keyframes) and assumes 30 fps for frame-stepping." -ForegroundColor Yellow
    if (Install-WithWinget "Gyan.FFmpeg" "ffmpeg") { $missing = @(Get-MissingFfmpegTools) }
}
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host ("Warning: " + ($missing -join ", ") + " still not found (a newly installed ffmpeg may need the app reopened).") -ForegroundColor Yellow
    Write-Host "Fix:  winget install Gyan.FFmpeg   (then reopen the app)"
    Write-Host "  or: copy ffmpeg.exe and ffprobe.exe from an ffmpeg download into $Dir\bin"
}

# libx264 is needed for the smooth-scrub copies and the video export
$ffmpegExe = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpegExe -and (Test-Path "$Dir\bin\ffmpeg.exe")) { $ffmpegExe = "$Dir\bin\ffmpeg.exe" }
if ($ffmpegExe) {
    $enc = & $ffmpegExe -hide_banner -encoders 2>$null | Out-String
    if ($enc -notmatch "libx264") {
        Write-Host ""
        Write-Host "Warning: this ffmpeg has no libx264 (H.264) encoder, so smooth-scrub copies and the video export will fail." -ForegroundColor Yellow
        Write-Host "Use a full build, e.g.  winget install Gyan.FFmpeg"
    }
}

# --- shortcuts ---------------------------------------------------------------------------
function New-Shortcut($path) {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($path)
    $lnk.TargetPath = $VenvPyw                       # pythonw = no console window
    $lnk.Arguments = '"' + (Join-Path $Dir "lap_timer.py") + '"'
    $lnk.WorkingDirectory = $Dir
    $lnk.IconLocation = (Join-Path $Dir "fpv-dvr-lap-timer.ico")
    $lnk.Description = "Mark gate passes in FPV footage and track lap times"
    $lnk.Save()
}
New-Shortcut $StartMenu
if ($Desktop) { New-Shortcut $DesktopLnk }

# --- Settings > Apps entry (lets the user uninstall from Windows) ---------------------------
try {
    $version = ((Select-String -Path (Join-Path $Dir "lap_timer.py") -Pattern '^VERSION = "(.*)"' |
                 Select-Object -First 1).Matches.Groups[1].Value)
    $kb = [int](((Get-ChildItem $Dir -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1KB)
    New-Item -Path $UninstallKey -Force | Out-Null
    $props = @{
        DisplayName     = "FPV DVR Lap Timer"
        DisplayVersion  = $version
        Publisher       = "LenuxFPV"
        InstallLocation = $Dir
        DisplayIcon     = (Join-Path $Dir "fpv-dvr-lap-timer.ico")
        UninstallString = 'cmd.exe /c ""' + (Join-Path $Dir "install.bat") + '" -Uninstall"'
    }
    foreach ($k in $props.Keys) { New-ItemProperty -Path $UninstallKey -Name $k -Value $props[$k] -PropertyType String -Force | Out-Null }
    foreach ($k in @("NoModify", "NoRepair")) { New-ItemProperty -Path $UninstallKey -Name $k -Value 1 -PropertyType DWord -Force | Out-Null }
    New-ItemProperty -Path $UninstallKey -Name EstimatedSize -Value $kb -PropertyType DWord -Force | Out-Null
} catch {
    Write-Host "Note: could not add the Settings > Apps uninstall entry ($($_.Exception.Message)). Use install.bat -Uninstall instead." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Installed. Find 'FPV DVR Lap Timer' in the Start Menu (uninstall from Settings > Apps)." -ForegroundColor Green
