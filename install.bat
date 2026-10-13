@echo off
rem Double-click to install FPV DVR Lap Timer (Start Menu shortcut). Use "install.bat -Desktop" to add a Desktop shortcut too.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
