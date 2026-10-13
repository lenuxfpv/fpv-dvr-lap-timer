# Building FPV DVR Lap Timer

## Windows exe

Run **`build_windows.bat`** on Windows to produce `dist\FPVDVRLapTimer.exe` with PyInstaller. No Python is needed to
run the result. Put `ffmpeg.exe` and `ffprobe.exe` in a `bin` folder first if you want to bundle them.

The build stamps the app's version (`VERSION` in `lap_timer.py`) into the exe's Properties > Details tab.

## Installer

The installer is built from `installer.iss` with Inno Setup.

## GitHub Actions

Both are built automatically by the **Build Windows exe** workflow (`.github/workflows/build-windows.yml`). Run it
from the repo's Actions tab, or push a tag like `vX.Y.Z` (matching `VERSION` in `lap_timer.py`) to also publish a
release.

The workflow:

- starts the exe as a smoke test and checks the stamped version;
- test-installs and uninstalls the installer silently, checking that the Settings > Apps entry appears and goes
  away;
- on a tag, publishes `FPVDVRLapTimer.exe` and `FPVDVRLapTimer-Setup.exe` as a release.
