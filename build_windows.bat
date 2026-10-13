@echo off
rem Build a standalone FPVLapTimer.exe (no Python needed to run it). Run this on Windows.
rem Optional: put ffmpeg.exe and ffprobe.exe in a "bin" folder here first and they will be bundled
rem (ffmpeg builds the smooth-scrub copies, ffprobe reads the frame rate).
setlocal
cd /d "%~dp0"
py -3 -m venv .buildvenv || goto :err
.buildvenv\Scripts\python -m pip install --upgrade pip PyQt6 pyinstaller || goto :err
set EXTRA=
.buildvenv\Scripts\python make_version_file.py || goto :err
if exist bin\ffprobe.exe set EXTRA=--add-binary "bin\ffprobe.exe;bin"
if exist bin\ffmpeg.exe set EXTRA=%EXTRA% --add-binary "bin\ffmpeg.exe;bin"
.buildvenv\Scripts\pyinstaller --noconfirm --clean --onefile --noconsole --name FPVLapTimer --icon fpv-lap-timer.ico --add-data "fpv-lap-timer.ico;." --version-file version_info.txt %EXTRA% lap_timer.py || goto :err
echo.
echo Done: dist\FPVLapTimer.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
