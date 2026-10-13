# FPV Lap Timer

Version 1.2.0 (shown in the window title and **Help → About**).

Review FPV drone footage (`.ts`, `.mp4`, also `.mov .mkv .m4v .avi`), mark each pass through the
start gate, and get lap times, fastest lap and fastest 3 consecutive laps. Videos are kept in a
library grouped by track and session date.

## What's new in 1.2.0

- **Every keyboard shortcut can be changed**, not just Mark gate: **View → Keyboard shortcuts…**
  (clashing keys are blocked). See [Changing the keys](#changing-the-keys).
- **Windows installer**: download `FPVLapTimer-Setup.exe` from the
  [Releases page](https://github.com/lenuxfpv/fpv-lap-timer/releases). No Python or admin rights needed, and
  it adds a Settings > Apps entry to uninstall (with an option to delete the smooth-scrub cache).
- **ffmpeg help**: if ffmpeg is missing, the app offers to install it (winget on Windows).
- **Fix paths…** handles videos from several folders at once.

Older releases are listed in the [Changelog](#changelog).

## Install

On Windows the easiest route is the `FPVLapTimer-Setup.exe` installer (see *Windows* below), which needs
nothing else installed. To run from source (Linux, or Windows without the installer) you need Python 3.9+,
**PyQt6** and (strongly recommended) **ffmpeg**, which provides two programs:
`ffprobe` reads each video's frame rate (without it 30 fps is assumed and frame-stepping is off on other
frame rates) and `ffmpeg` builds the smooth-scrub copies (without it scrubbing only jumps between
keyframes, see *Navigating the video*). The app works without ffmpeg, just less smoothly. Your ffmpeg
must include the **libx264** encoder (used for the smooth copies and the video export); Fedora's default
`ffmpeg-free` does not, so on Fedora enable RPM Fusion and run `sudo dnf swap ffmpeg-free ffmpeg
--allowerasing`. The Debian/Ubuntu `ffmpeg` and the Windows `winget install Gyan.FFmpeg` builds include it.
The video export also needs a bold font file, normally already installed (DejaVu Sans or Liberation on
Linux, Arial on Windows). The install
scripts check all of this for you and add a menu launcher pointing at the folder you run them from. Keep
the project folder where it is after installing; if you move it, run the installer again.

### Linux

```
./install.sh               # menu launcher; uses system PyQt6, or creates a private .venv
./install.sh --desktop     # also a Desktop shortcut
./install.sh --uninstall   # remove launcher + icon (library and videos untouched); asks about the smooth copies
./install.sh --uninstall --clear-cache   # ...and delete the smooth copies without asking
```

If `ffmpeg` / `ffprobe` is missing, ffmpeg has no libx264, or no bold font is found, the script warns
you and prints the fix for your distro. Uninstalling leaves your library alone and asks whether to also delete the cached smooth copies (`~/.cache/fpv-lap-timer`) to free disk space.

### Windows

**Easiest:** download **`FPVLapTimer-Setup.exe`** from the
[Releases page](https://github.com/lenuxfpv/fpv-lap-timer/releases) and run it. No Python needed and no
admin rights: it installs to `%LOCALAPPDATA%\Programs\FPV Lap Timer`, adds a Start Menu shortcut (and a
Desktop one if you tick it) and an "FPV Lap Timer" entry in Settings > Apps, where you can uninstall it. The
uninstaller asks whether to also delete the smooth-scrub copies; your library and videos are never touched.
The release also has the bare `FPVLapTimer.exe` if you prefer a portable copy (no Apps entry). If ffmpeg is
missing the app offers to install it (see below). The rest of this section covers running from source.

Double-click **`install.bat`** (or run `install.bat -Desktop` for a Desktop shortcut too, and
`install.bat -Uninstall` to remove them). It creates a private `.venv`, installs PyQt6, and adds a
Start Menu shortcut and an "FPV Lap Timer" entry in Settings > Apps (Add/Remove Programs) that runs the uninstall. Needs Python 3.9+; if Python or ffmpeg is missing it offers to install them with winget (or run `winget install Python.Python.3.12` yourself). For frame rates install
ffmpeg (`winget install Gyan.FFmpeg`) or copy `ffmpeg.exe` and `ffprobe.exe` into a `bin` folder next
to the app. Uninstalling removes the shortcuts and leaves your library alone; it asks whether to also delete the
cached smooth copies (`%LOCALAPPDATA%\fpv-lap-timer`) to free disk space (`install.bat -Uninstall -ClearCache`
deletes them without asking). The installer also warns if your ffmpeg lacks libx264.

To build the single `.exe` yourself, run **`build_windows.bat`** on Windows to produce `dist\FPVLapTimer.exe`
with PyInstaller (no Python needed to run it; put `ffmpeg.exe` and `ffprobe.exe` in `bin` first to
bundle them). The build also stamps the app's version (`VERSION` in `lap_timer.py`) into the exe's
Properties > Details tab.

The exe and the installer (`installer.iss`, built with Inno Setup) are built automatically by the **Build Windows exe** GitHub Actions workflow
(`.github/workflows/build-windows.yml`): run it from the repo's Actions tab, or push a tag like `vX.Y.Z` (matching `VERSION` in `lap_timer.py`)
to also publish a release. It starts the exe as a smoke test, checks the stamped version, and test-installs and
uninstalls the installer silently, checking the Apps entry appears and goes away. ffmpeg is not
bundled in that build, so put it in a `bin` folder next to the exe or on PATH. If ffmpeg or ffprobe can't be
found when the app starts, it offers to install ffmpeg with winget (Windows) or points you to the install
command / download page; tick *Don't ask again* to stop the prompt, and use **Help → Install ffmpeg…** any
time. The app picks up a freshly installed ffmpeg without a restart.

> Tested on a real Windows machine: `FPVLapTimer-Setup.exe` (install, the Settings > Apps entry, uninstall),
> `FPVLapTimer.exe`, the ffmpeg install prompt and the Keyboard shortcuts dialog. Not yet tried there: the
> `install.bat` / `install.ps1` route (run from source) since its Settings > Apps entry was added, and
> playback and exports beyond what's listed. If a script fails, the error message should say which step; the
> app itself needs only `pip install PyQt6`.

### Run without installing

```
python3 lap_timer.py                          # open the app
python3 lap_timer.py flight1.ts flight2.mp4   # open and add these videos to the library
```

You can also drag video files or folders onto the window.

## Where your data lives

Everything is stored in one SQLite database - Linux `~/.local/share/fpv-lap-timer/library.db`,
Windows `%APPDATA%\fpv-lap-timer\library.db`, macOS `~/Library/Application Support/fpv-lap-timer/`.
Set the environment variable `FPV_LAP_TIMER_DB` to use a different file. It holds each video's path,
gate marks, bad-lap flags, track name and session date. **Nothing is written next to your videos**,
and the app never modifies or deletes video files. Back up that one file to keep your history.
Preferences (theme, keyboard shortcuts, the *Auto-build* / *Build when adding* checkboxes) are stored
separately by Qt (Linux `~/.config/fpv-lap-timer/app.conf`, Windows registry) and are not part of the
library file.

**Duplicate protection.** Adding a file that is already in the library (the same file reached through a
different path, a symlink, or a copy with the same name, size and date) is refused with a warning
listing what was skipped, because the same laps would otherwise be counted twice in your stats. If
duplicates are already in the library they show as **⚠ duplicate** (orange) in the list, and the
Library stats window shows a warning for the track/view that contains them. Remove the extra entry
with *Remove*; the video file and the shared smooth copy are not touched.

If a video is moved or renamed it shows in red; double-click it and pick the new location. Your
marks stay attached.

### Backing up, moving and merging the library

The **Library** menu works on the whole database, so you don't have to find `library.db` yourself:

- **Export library…** saves a copy to a `.db` file you choose (named with today's date by default).
- **Import library (replace)…** swaps your library for an exported file. Your current library is saved
  first as `library-before-import-<date-time>.db` next to `library.db`.
- **Merge library…** adds another library to yours without overwriting anything. Videos are matched by
  file path: new videos come across with their marks, track and session date; for videos you both have,
  marks at times you don't already have are added and an empty track or session date is filled in
  (your own marks and settings are kept). A summary shows what was added.

To move to another computer, export on the old one and import (or merge) on the new one. You can also
copy `library.db` to the new machine's location above. Video paths are stored as full paths, so they
will show in red if the videos live somewhere else there. Click **Fix paths…** in the library panel:
it lists every folder that has missing videos, one row each; pick the new folder for each (they can
be different places, and a blank row is skipped) and the videos are re-pointed at once. It also converts between Windows (`D:\Drone`) and
Linux (`/mnt/Drone`) style paths, and shows how many of the new locations actually exist before you
apply it.

## The video library list

Each video in the left panel is shown as a card:

```
RaceGOW6 Track 1                  ⚡
LAPS      FASTEST     FASTEST 3
6/7       0:25.737    1:22.591
20261010-155234.ts       2026-10-10
```

- **Track name** in large bold type (*No track* in grey italics if none is set).
- **LAPS** = good / total laps marked, **FASTEST** = best good lap, **FASTEST 3** = best three good laps in a
  row (a dash until there are three in a row). Bad laps are excluded, as everywhere else.
- The **file name** is on a small line at the bottom (shortened in the middle if long), with the session
  date at its right.
- Badges at the top right: **⚡** smooth-scrub copy ready, **⚠ duplicate** (orange) same file listed twice,
  **⚠ not found** (red) the file is missing - double-click it to locate it.
- Hover a card for its full path.

## Workflow

1. **Add videos** with *Add videos…*, *Add folder…* (scans subfolders), or drag and drop.
2. Select videos and click **Track / date…** to set a track name and session date (see below).
3. Double-click a video to open it.
4. Play or scrub to the moment you pass through the gate and press **Z** (or the green button; you can change the key, see below).
   The first mark starts lap 1; every later mark ends a lap and starts the next.
5. Review the table on the right and flag any bad laps.

### Navigating the video

**Smooth-scrub copy.** FPV recordings (especially `.ts`) have a keyframe only every second or two, and
a player can only show those frames when you jump around, so scrubbing looks stuck and
single-frame stepping doesn't work. The app fixes this by building a small 480p copy of each video in
which every frame is a keyframe (ffmpeg; builds at roughly 5x real time). Each copy takes about
17-19% of the original's size, i.e. 0.5 MB per second of 60 fps footage and 0.8 MB per second at 90 fps
(a 2.5 minute video is ~100-130 MB). Any ffmpeg version works; 5.1+ is preferred and older ones are
handled automatically.
It is built in the background, after which the app switches to it automatically and keeps your
position. When copies get built:

- **When you add videos** (checkbox *Build when adding videos* in the library panel, on by default):
  every new video is queued and built one after another, so a whole folder is ready by the time you
  start reviewing. Videos you already have are not rebuilt.
- **Build smooth copies (N)** button: queues every video in the current list (it respects the track
  filter) that doesn't have a copy yet. While anything is building it becomes **Cancel all builds**,
  and a line under it shows the current video, its percentage and how many are waiting.
- **When you open a video** (*Auto-build* under the player): a video you open that has no copy jumps to
  the front of the queue.
- A **⚡** badge on a video's card in the library list means its smooth copy is ready. Opening a video
  while its copy is still building works fine; it switches over when it finishes.

Once a copy is in use, scrubbing, stepping and reverse are fluid and frame-exact. Marks are saved as
times, so they are identical whichever version you used. More about the copies:

- The copies live in a cache folder (Linux `~/.cache/fpv-lap-timer/smooth`, Windows
  `%LOCALAPPDATA%\fpv-lap-timer\smooth`), never next to your videos. **Clear smooth copies…**
  deletes them to free space; they can be rebuilt. *Remove* also deletes that video's copy.
- A copy is found again by the video's **file name, size and modified date**, not its folder, so
  moving or reorganising your videos (or using *Fix paths…*) does not trigger a rebuild. It is rebuilt
  only if the file itself changes or you clear the cache. Two different videos with the same name,
  size and date would share a copy, which in practice does not happen.
- Untick *Use smooth copy* to go back to the original at any time.
- Without ffmpeg (or before the copy is ready) the original is used and the picture can only move
  between keyframes. Marks always use the time of the frame **actually on screen**.

**Slow to fast:** (the keys named here are the defaults; every one can be changed, see *Changing the keys*)

- **Shuttle slider** (under the seek bar): drag right to play forward from 0.1x up to 16x, drag left
  to go backward; the further you drag, the faster. Let go and it springs back and stops. This is the
  quickest way to cruise along a lap and then slow down at the gate.
- **Frame stepping:** ←/→ (hold to repeat), the Frame buttons, or the mouse wheel over the video
  (Shift = 10 frames per notch).
- **Seek bar:** click or drag anywhere; orange ticks show where your gate marks are.
- **Fixed speeds:** 0.1x - 8x in the Speed box (`[` / `]`).
- **J / K / L:** J = backward, L = forward (press again to double: 1x, 2x, 4x … 16x), K = stop.
- **PgUp / PgDn** (or the Gate buttons) jump to the previous / next gate mark.

### Controls

These are the defaults; every one can be changed (see *Changing the keys* below).

| Key | Action |
|---|---|
| Space | Play / pause (also stops a shuttle) |
| Z | Mark gate pass on the frame shown |
| ← / → | Step one frame back / forward (hold to repeat) |
| Mouse wheel on video | Step frames (Shift = 10) |
| Hold Shift + ← / → | Play backward / forward at 2x while held; stops on release |
| Ctrl + ← / → | Jump 5 seconds |
| J / K / L | Shuttle backward / stop / forward (repeat to speed up) |
| PgUp / PgDn | Previous / next gate mark |
| `[` / `]` | Slower / faster playback (0.1x - 8x) |
| Delete | Delete the selected mark (when the table has focus) |

Audio is muted by default (checkbox under the video).

### Changing the keys

Every key in the table above can be changed: choose **View → Keyboard shortcuts…**, click the box next to an
action and press the key you want (a single key, optionally with Ctrl / Shift / Alt, e.g. `Ctrl+G`). *Default*
puts one action back, *Clear* leaves it without a key, and *Reset all to defaults* restores everything.
Giving two actions the same key (or using Return, which always opens the selected video in the library) is
blocked: the clashing rows turn orange and *Save* stays disabled until you fix it. The choices are
remembered, and the **MARK GATE** button label shows your Mark gate key. (The line under the video just points
back to this dialog.) A Mark gate key
you set in an older version carries over.

### Dark mode

**View → Theme** offers *Follow system*, *Light* and *Dark*. The choice is remembered. Charts and
dialogs follow the theme too.

### Marks and laps table

- Each row is a gate pass. The lap time on a row is the time since the previous mark.
- Click a row to jump the video to that mark.
- Tick **Bad lap** to exclude that lap (the lap *ending* at that mark). Bad laps are greyed out,
  never counted as a fastest lap, and **break a run of 3**, so "fastest 3 consecutive" is always three
  good laps in a row.
- **Delete mark** removes a mark (the two neighbouring laps merge).
- **Move mark here** moves the selected mark to the current video position, for fixing a mark that
  was placed slightly off.

## Tracks and session dates

- **Track** groups videos from the same course. It is an editable dropdown: pick an existing track
  or type a new name.
- **Session date** controls ordering in the library list and charts. Choose *Use file date* (the
  file's modified date), *Set date* (manual), or *Keep as is*. Set it manually for copied or
  re-saved files whose file date is wrong.
- **Right-click** a video in the library for the quickest way: **Assign to track** lists your existing
  tracks (ticked if the selected videos are already on it) plus **New track…**; **Remove from track**
  clears it; **Track & session date…** opens the full dialog. It works on a multi-selection, and
  right-clicking an unselected video acts on just that one.
- Select several videos (Shift/Ctrl-click) to tag them all at once. Fields that differ between the
  selected videos are left alone unless you change them.
- The dropdown above the library list filters it by track. Videos you add while a track is selected
  join that track.

## Reports

- **Per video** (right panel): good / total laps, fastest lap, fastest 3 consecutive laps.
- **Lap chart** (right panel): bar chart of one video's laps. Green = fastest, grey = bad, dashed blue
  box = fastest 3 consecutive. Hover a bar for details.
- **Library stats** (left panel): pick a track (or *All tracks*) to see its fastest lap and fastest 3,
  a per-video table, and a **By track** table comparing every track.
- **Progress chart** (in Library stats): one point per video in session-date order, showing fastest lap
  and best-3 average per lap for the selected track, to see improvement over time.
- **Export CSV**: from the right panel (current video) or Library stats (selected track). Columns:
  `track, session_date, video, lap, start_s, end_s, lap_time_s, lap_time, bad_lap`. Bad laps are
  included and marked `yes`, so you can filter them in a spreadsheet.

Fastest-3 is calculated within a single video; it never spans two videos.

### Export the fastest 3 laps as a video

Makes a shareable video of your fastest 3 consecutive laps with a lap timer drawn on it.

- **From the open video:** **🎬 Export fastest 3 video…** in the right panel exports that video's fastest
  3 consecutive laps. **From a track:** **Library stats → 🎬 Export fastest 3 video (this track)…** exports
  the single fastest 3 across all videos on the selected track (or *All tracks*), whichever video they
  are in. Choose the file name and click *Export*; a progress bar shows how far it is and *Cancel*
  removes the partial file.
- **What you get:** an MP4 (H.264) of the **original footage**, not the smooth copy, at its original
  resolution and frame rate (a 90 fps video stays 90 fps), cut on the exact frames you marked, with
  1 s before the first gate and, by default, 2 s after the last one (set "End padding" in the export dialog, 0–10 s). It is re-encoded at high quality
  (x264 CRF 16, slow preset) with BT.709 colours so it plays correctly on YouTube, Discord and phones.
  Audio is kept if the source has any.
- **The overlay:** top left, the current lap number and a running lap timer that restarts at each gate,
  with each finished lap's time listed underneath; top right, the track name and date and the running
  3-lap total; after the last gate the final lap and total are held on screen and a gold
  *FASTEST 3 CONSECUTIVE LAPS m:ss.mmm* banner appears.
- **Notes:** if your ffmpeg has no H.264 encoder (libx264) the app tells you so in plain words, with the fix for
  your system, before it starts (for exports and for smooth-scrub copies). It needs ffmpeg with libx264 and a bold font file (found automatically: DejaVu Sans Bold / Liberation on
  Linux, Arial on Windows and macOS). Exporting is slow because it is a high-quality re-encode: about 30 s for a 28 s 60 fps clip on a fast machine and a few minutes for a 90 fps clip read from a network share, so leave it running. The source video
  must be reachable (a network share that is offline shows an error). YouTube plays up to 60 fps, so
  a 90 fps export will be shown at 60 there; the file itself is left untouched.

## Notes

- If something unexpected goes wrong the app shows a message in the status bar and keeps running
  instead of closing; the details are written to `errors.log` next to the library database.
- *Remove* only deletes the video from the library (and its marks); the file is untouched.
- Playback of `.ts` files uses Qt's ffmpeg backend. If a file won't play, check the status bar for
  the player error.

## Changelog

Releases and their downloads (`FPVLapTimer-Setup.exe` and `FPVLapTimer.exe`) are on the
[Releases page](https://github.com/lenuxfpv/fpv-lap-timer/releases).

### 1.2.0
- **View → Keyboard shortcuts…**: every key (play/pause, frame stepping, hold-to-play, 5-second jumps,
  shuttle, gate navigation, speed, delete mark) can now be changed, not just Mark gate. Clashes are blocked.

### 1.1.6
- New **`FPVLapTimer-Setup.exe`** installer on the Releases page: per-user install (no admin), Start Menu
  and optional Desktop shortcuts, a Settings > Apps entry to uninstall, and an uninstall prompt to delete
  the smooth-scrub cache. The bare `FPVLapTimer.exe` is still published.

### 1.1.5
- The app checks for ffmpeg/ffprobe on start-up and offers to install ffmpeg with winget (Windows) or
  shows the install command / download page; **Help → Install ffmpeg…** does the same any time, and a
  freshly installed ffmpeg is picked up without restarting.
- `install.bat` adds an "FPV Lap Timer" entry to Settings > Apps so it can be uninstalled from Windows.
- Fixed `install.ps1` hiding winget's output and treating failed installs as successful.

### 1.1.4
- README: added this changelog and refreshed the example release tag. No changes to the app itself.

### 1.1.3
- **Fix paths…** now lists every folder that has missing videos, one row each, so videos stored in
  different folders can be re-pointed to different new folders in one go (blank rows are skipped).
- Uninstalling (`install.sh --uninstall` / `install.bat -Uninstall`) asks whether to also delete the
  cached smooth-scrub copies to free disk space; `--clear-cache` / `-ClearCache` skips the question.

### 1.1.2
- Build workflow updated to current GitHub Actions versions (no more Node 20 deprecation warning).

### 1.1.1
- The Windows installer (`install.bat`) now offers to install a missing Python or ffmpeg with winget.

### 1.1.0
- First release.
