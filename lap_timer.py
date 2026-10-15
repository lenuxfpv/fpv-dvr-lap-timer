#!/usr/bin/env python3
"""FPV DVR Lap Timer - review drone footage, mark gate passes, track lap times.

Videos (.ts / .mp4 / others) stay where they are. The app remembers their paths
and your gate marks in a single SQLite database in your user data folder.
"""
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtCore import QDate, QEvent, QKeyCombination, QProcess, QProcessEnvironment, QSize, QRectF, QSettings, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QFrame, QDateEdit, QDoubleSpinBox, QFormLayout, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QProgressDialog, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionSlider, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

VERSION = "1.19.0"
AUTHOR = "LenuxFPV"
APP_NAME = "FPV DVR Lap Timer"        # shown to users
APP_ID = "fpv-dvr-lap-timer"           # folder / settings name
VIDEO_EXTS = {".ts", ".mp4", ".mov", ".mkv", ".m4v", ".avi"}


def data_dir():
    """Per-user folder for the library database, following each OS's convention."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_ID


def cache_dir():
    """Where the optional smooth-scrub copies live (safe to delete at any time)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_ID / "smooth"


def app_settings():
    return QSettings(APP_ID, "app")


def proxy_path(src):
    """Cache file for a video's smooth-scrub copy.

    Matched by file name + size + modified time, not folder, so moving or renaming the folder (or
    using Fix paths) keeps the copy. The key changes if the file itself is changed."""
    try:
        st = os.stat(src)
    except OSError:
        return cache_dir() / (hashlib.sha1(f"{src}".encode()).hexdigest()[:20] + ".mp4")
    key = f"{vname(src)}|{st.st_size}|{st.st_mtime_ns}"
    return cache_dir() / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".mp4")


# FPV_LAP_TIMER_DB overrides the location (handy for a portable copy or testing)
DB_PATH = Path(os.environ.get("FPV_LAP_TIMER_DB") or data_dir() / "library.db")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def fmt(ms):
    """Milliseconds -> m:ss.mmm"""
    if ms is None:
        return "--"
    m, rem = divmod(int(round(ms)), 60000)
    return f"{m}:{rem / 1000:06.3f}"


def fmt_overlay(ms, decimals=3):
    """Like fmt, but without the minutes while the time is under a minute: s.mmm. `decimals` (0-3) is how many
    digits follow the seconds; extra digits are cut off, not rounded, like the running clock on the video."""
    unit = 10 ** (3 - decimals)                            # ms per last shown digit
    m, rem = divmod(int(round(ms)) // unit * unit, 60000)
    if not m:
        return f"{rem / 1000:.{decimals}f}"
    return f"{m}:{rem / 1000:0{3 + decimals if decimals else 2}.{decimals}f}"


def vname(path):
    """File name of a stored path, whichever OS style (/ or \\) it was saved in."""
    return re.split(r"[\\/]", str(path))[-1]


def find_ffprobe():
    return find_tool("ffprobe")


def find_tool(name):
    """ffmpeg/ffprobe bundled with the app (./bin, ./ffmpeg/bin) first, then on PATH. None if absent."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    roots = [Path(getattr(sys, "_MEIPASS", "") or "."), Path(sys.executable).parent,
             Path(__file__).resolve().parent]
    for root in roots:
        for sub in ("bin", "ffmpeg/bin", "."):
            cand = root / sub / exe
            if cand.is_file():
                return str(cand)
    found = shutil.which(name)
    if found:
        return found
    for d in _winget_tool_dirs() + _mac_tool_dirs():
        cand = d / exe
        if cand.is_file():
            return str(cand)
    return None


def _winget_tool_dirs():
    """Windows: folders where `winget install Gyan.FFmpeg` leaves ffmpeg. The running app's PATH
    doesn't see a program installed after it started, so look there directly."""
    if sys.platform != "win32":
        return []
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    winget = Path(base) / "Microsoft" / "WinGet"
    dirs = [winget / "Links"]
    try:
        dirs += sorted((winget / "Packages").glob("Gyan.FFmpeg*/ffmpeg-*/bin"), reverse=True)
    except OSError:
        pass
    return dirs


def _mac_tool_dirs():
    """macOS: Homebrew's folders. An app opened from Finder doesn't get the shell's PATH, so ffmpeg
    installed with `brew install ffmpeg` would not be found otherwise."""
    return [Path("/opt/homebrew/bin"), Path("/usr/local/bin")] if sys.platform == "darwin" else []


def file_identity(path):
    """(file name, size, modified time): equal for two paths to the same file, or a plain copy of it.
    None if the file can't be read (missing / drive not mounted)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (vname(path), st.st_size, st.st_mtime_ns)


def probe_video(path):
    """Return (fps, duration_ms) using ffprobe; falls back to (30.0, None)."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return 30.0, None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000 if sys.platform == "win32" else 0).stdout   # no console flash on Windows
        data = json.loads(out)
        num, den = data["streams"][0]["avg_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else 30.0
        dur = float(data.get("format", {}).get("duration", 0)) * 1000 or None
        return (fps if 1 < fps < 400 else 30.0), dur
    except Exception:
        return 30.0, None


def _is_windows_style(p):
    return "\\" in p or (len(p) > 1 and p[1] == ":")


def remap_path(path, old, new):
    """Swap the leading folder `old` of `path` for `new`, converting separators to the new
    folder's style (so /mnt/Vault/x.ts -> D:\\Vault\\x.ts works). None if `path` isn't under `old`."""
    norm = lambda t: t.replace("\\", "/").rstrip("/")
    p, o = norm(path), norm(old)
    if not o:
        return None
    ci = _is_windows_style(old) or _is_windows_style(path)      # Windows paths ignore case
    a, b = (p.lower(), o.lower()) if ci else (p, o)
    if a != b and not a.startswith(b + "/"):
        return None
    rest = p[len(o):]
    return new.rstrip("/\\") + rest.replace("/", "\\" if _is_windows_style(new) else "/")


def common_folder(paths):
    """Deepest folder containing all the given file paths ('' if none/too shallow)."""
    parts = [re.split(r"[\\/]", p)[:-1] for p in paths]
    if not parts:
        return ""
    common = []
    for group in zip(*parts):
        if len(set(group)) != 1:
            break
        common.append(group[0])
    if len(common) < 2:
        return ""
    return ("\\" if _is_windows_style(paths[0]) else "/").join(common)


KEEP = object()   # sentinel: "leave this field unchanged"


def effective_date(path, session_date):
    """(date, is_manual). Manual session date wins, else the file's modified date."""
    if session_date:
        try:
            return datetime.strptime(session_date, "%Y-%m-%d").date(), True
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).date(), False
    except OSError:
        return None, False


MIN_RUN, MAX_RUN, DEFAULT_RUN = 2, 10, 3
# DEFAULT_RUN (3) is fixed: a track can have its own run length, but the default for every other track
# is always 3 and can't be changed.


def run_length():
    """The default run length (always 3) for tracks that have no setting of their own."""
    return DEFAULT_RUN


def run_for(run_map, track):
    """The run length for `track`: its own setting if it has one, else the default (always 3)."""
    return (run_map.get(track) if track else None) or DEFAULT_RUN


def lap_stats(laps, n=None):
    """laps: list of (lap_ms, bad) in order.

    Returns dict: best (ms, index) and bestn (total_ms, start_index) for the fastest run of `n`
    consecutive laps (default: the user's setting, 3 unless changed).
    Bad laps are excluded; a bad lap also breaks a run of consecutive laps.
    """
    n = n or DEFAULT_RUN
    best = None
    for i, (ms, bad) in enumerate(laps):
        if not bad and (best is None or ms < best[0]):
            best = (ms, i)
    bestn = None
    for i in range(len(laps) - n + 1):
        window = laps[i:i + n]
        if any(bad for _, bad in window):
            continue
        total = sum(ms for ms, _ in window)
        if bestn is None or total < bestn[0]:
            bestn = (total, i)
    goods = [ms for ms, bad in laps if not bad]
    return {"best": best, "bestn": bestn, "avg": sum(goods) / len(goods) if goods else None,
            "good": sum(1 for _, b in laps if not b), "total": len(laps)}


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
class Library:
    def __init__(self, path=DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                fps REAL DEFAULT 30,
                added TEXT DEFAULT CURRENT_TIMESTAMP
            );
            -- One row per gate pass. bad_lap=1 means the lap ENDING at this mark is excluded.
            CREATE TABLE IF NOT EXISTS marks (
                id INTEGER PRIMARY KEY,
                video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                time_ms INTEGER NOT NULL,
                bad_lap INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS marks_video ON marks(video_id, time_ms);
            -- tracks created by hand before any video is on them (a track otherwise exists only
            -- because some video has its name)
            CREATE TABLE IF NOT EXISTS extra_tracks (name TEXT PRIMARY KEY);
            -- per-track "fastest run of N laps"; a track with no row uses the global default
            CREATE TABLE IF NOT EXISTS track_settings (track TEXT PRIMARY KEY, run_length INTEGER NOT NULL);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(videos)")}
        if "track" not in cols:
            self.db.execute("ALTER TABLE videos ADD COLUMN track TEXT NOT NULL DEFAULT ''")
        if "session_date" not in cols:      # ISO yyyy-mm-dd, NULL = use the file's date
            self.db.execute("ALTER TABLE videos ADD COLUMN session_date TEXT")
        if "proxy_locked" not in cols:      # 1 = never build this video's smooth copy automatically
            self.db.execute("ALTER TABLE videos ADD COLUMN proxy_locked INTEGER NOT NULL DEFAULT 0")
        if "notes" not in cols:             # free text: battery, props, tune...
            self.db.execute("ALTER TABLE videos ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    # --- track / session metadata
    def all_meta(self):
        """{video_id: (track, session_date_or_None)}"""
        return {r[0]: (r[1], r[2]) for r in
                self.db.execute("SELECT id, track, session_date FROM videos")}

    def set_meta(self, vids, track=KEEP, session_date=KEEP):
        for vid in vids:
            if track is not KEEP:
                self.db.execute("UPDATE videos SET track=? WHERE id=?", (track.strip(), vid))
            if session_date is not KEEP:
                self.db.execute("UPDATE videos SET session_date=? WHERE id=?", (session_date, vid))
        self.db.commit()

    def notes_map(self):
        """{video_id: notes text}"""
        return {r[0]: r[1] or "" for r in self.db.execute("SELECT id, notes FROM videos")}

    def set_notes(self, vids, text):
        for vid in vids:
            self.db.execute("UPDATE videos SET notes=? WHERE id=?", (text.strip(), vid))
        self.db.commit()

    def proxy_locked_ids(self):
        """Ids of the videos whose smooth copy is never built automatically."""
        return {r[0] for r in self.db.execute("SELECT id FROM videos WHERE proxy_locked=1")}

    def set_proxy_locked(self, vids, locked):
        for vid in vids:
            self.db.execute("UPDATE videos SET proxy_locked=? WHERE id=?", (1 if locked else 0, vid))
        self.db.commit()

    def tracks(self):
        """Every track name: those on videos plus ones created empty with add_track()."""
        return [r[0] for r in self.db.execute(
            "SELECT track FROM videos WHERE track<>'' UNION SELECT name FROM extra_tracks "
            "ORDER BY 1 COLLATE NOCASE")]

    def add_track(self, name):
        """Create an empty track (no videos yet). Does nothing if the name is already used."""
        name = name.strip()
        if name:
            self.db.execute("INSERT OR IGNORE INTO extra_tracks(name) VALUES(?)", (name,))
            self.db.commit()

    def run_map(self):
        """{track: its own run length} for the tracks that have one."""
        return {t: n for t, n in self.db.execute("SELECT track, run_length FROM track_settings")}

    def set_track_run(self, track, n):
        """Give `track` its own run length (2-10), or None to go back to the global default."""
        if n is None:
            self.db.execute("DELETE FROM track_settings WHERE track=?", (track,))
        else:
            n = max(MIN_RUN, min(MAX_RUN, int(n)))
            self.db.execute("INSERT OR REPLACE INTO track_settings(track, run_length) VALUES(?,?)", (track, n))
        self.db.commit()

    def run_lengths(self, video_ids):
        """Set of the run lengths in force for these videos (their tracks' settings, or the default)."""
        rmap, meta = self.run_map(), self.all_meta()
        return {run_for(rmap, meta[v][0]) for v in video_ids if v in meta}

    def track_video_count(self, name):
        return self.db.execute("SELECT COUNT(*) FROM videos WHERE track=?", (name,)).fetchone()[0]

    def rename_track(self, old, new):
        """Give every video on `old` the name `new` (merging into `new` if it already exists)."""
        new = new.strip()
        if not new or new == old:
            return
        self.db.execute("UPDATE videos SET track=? WHERE track=?", (new, old))
        self.db.execute("DELETE FROM extra_tracks WHERE name=?", (old,))
        self.db.execute("INSERT OR IGNORE INTO extra_tracks(name) VALUES(?)", (new,))
        if self.db.execute("SELECT 1 FROM track_settings WHERE track=?", (new,)).fetchone():
            self.db.execute("DELETE FROM track_settings WHERE track=?", (old,))     # merging: the target keeps its own
        else:
            self.db.execute("UPDATE track_settings SET track=? WHERE track=?", (new, old))
        self.db.commit()

    def delete_track(self, name):
        """Remove a track. Its videos stay in the library, with no track."""
        self.db.execute("UPDATE videos SET track='' WHERE track=?", (name,))
        self.db.execute("DELETE FROM extra_tracks WHERE name=?", (name,))
        self.db.execute("DELETE FROM track_settings WHERE track=?", (name,))
        self.db.commit()

    def video_ids(self, track=None):
        """None = every video, '' = videos with no track, otherwise that track's videos."""
        meta = self.all_meta()
        return {v for v, (t, _) in meta.items() if track is None or t == track}

    def add_video(self, path):
        path = str(Path(path).resolve())
        row = self.db.execute("SELECT id FROM videos WHERE path=?", (path,)).fetchone()
        if row:
            return row[0]
        fps, _ = probe_video(path)
        cur = self.db.execute("INSERT INTO videos(path, fps) VALUES(?,?)", (path, fps))
        self.db.commit()
        return cur.lastrowid

    def videos(self):
        return self.db.execute("SELECT id, path, fps FROM videos ORDER BY path COLLATE NOCASE").fetchall()

    def duplicate_groups(self):
        """[[video_id, ...], ...] - library entries that are the same file (found via different paths,
        or copies with the same name, size and date). Only files that currently exist can be compared."""
        seen = {}
        for vid, path, _ in self.videos():
            ident = file_identity(path)
            if ident:
                seen.setdefault(ident, []).append(vid)
        return [g for g in seen.values() if len(g) > 1]

    def remove_video(self, vid):
        self.db.execute("DELETE FROM videos WHERE id=?", (vid,))
        self.db.commit()

    def plan_prefix_swap(self, old, new):
        """-> (plan [(vid, old_path, new_path)], skipped) for swapping folder `old` with `new`.
        Videos whose new path is already in the library are skipped."""
        taken = {p for _, p, _ in self.videos()}
        plan, skipped = [], 0
        for vid, p, _ in self.videos():
            np_ = remap_path(p, old, new) if new.strip() else None
            if not np_ or np_ == p:
                continue
            if np_ in taken:
                skipped += 1
            else:
                plan.append((vid, p, np_))
        return plan, skipped

    def missing_folders(self):
        """{folder: number of missing videos} for the videos whose file can't be found,
        grouped by the exact folder they were stored in."""
        out = {}
        for _, p, _ in self.videos():
            if not os.path.exists(p):
                folder = re.split(r"[\\/]", p)
                folder = ("\\" if _is_windows_style(p) else "/").join(folder[:-1])
                out[folder] = out.get(folder, 0) + 1
        return out

    def plan_folder_swaps(self, mapping):
        """Like plan_prefix_swap but for several old->new folder pairs at once.
        -> (plan [(vid, old_path, new_path)], skipped)."""
        taken = {p for _, p, _ in self.videos()}
        plan, skipped, done = [], 0, set()
        for old, new in mapping.items():
            if not new.strip():
                continue
            for vid, p, _ in self.videos():
                if vid in done:
                    continue
                np_ = remap_path(p, old, new)
                if not np_ or np_ == p:
                    continue
                done.add(vid)
                if np_ in taken:
                    skipped += 1
                else:
                    taken.add(np_)
                    plan.append((vid, p, np_))
        return plan, skipped

    def apply_path_plan(self, plan):
        for vid, _, np_ in plan:
            self.db.execute("UPDATE videos SET path=? WHERE id=?", (np_, vid))
        self.db.commit()

    def relocate(self, vid, new_path):
        self.db.execute("UPDATE videos SET path=? WHERE id=?", (str(Path(new_path).resolve()), vid))
        self.db.commit()

    def marks(self, vid):
        return self.db.execute(
            "SELECT id, time_ms, bad_lap FROM marks WHERE video_id=? ORDER BY time_ms", (vid,)).fetchall()

    def set_marks(self, vid, rows):
        """Replace every mark of a video with rows [(time_ms, bad)] (what undo / redo put back)."""
        self.db.execute("DELETE FROM marks WHERE video_id=?", (vid,))
        self.db.executemany("INSERT INTO marks(video_id, time_ms, bad_lap) VALUES(?,?,?)",
                            [(vid, int(t), 1 if bad else 0) for t, bad in rows])
        self.db.commit()

    def add_mark(self, vid, t):
        cur = self.db.execute("INSERT INTO marks(video_id, time_ms) VALUES(?,?)", (vid, int(t)))
        self.db.commit()
        return cur.lastrowid

    def delete_mark(self, mid):
        self.db.execute("DELETE FROM marks WHERE id=?", (mid,))
        self.db.commit()

    def move_mark(self, mid, t):
        self.db.execute("UPDATE marks SET time_ms=? WHERE id=?", (int(t), mid))
        self.db.commit()

    def set_bad(self, mid, bad):
        self.db.execute("UPDATE marks SET bad_lap=? WHERE id=?", (1 if bad else 0, mid))
        self.db.commit()

    def marked_ids(self):
        """Ids of the videos that have at least one mark."""
        return {r[0] for r in self.db.execute("SELECT DISTINCT video_id FROM marks")}

    def laps(self, vid):
        """List of (lap_ms, bad) for a video."""
        m = self.marks(vid)
        return [(m[i][1] - m[i - 1][1], bool(m[i][2])) for i in range(1, len(m))]

    # --- backup / restore / merge
    def export_to(self, path):
        """Write a consistent copy of the whole library to `path` (overwrites it)."""
        path = Path(path)
        path.unlink(missing_ok=True)
        dest = sqlite3.connect(path)
        try:
            self.db.backup(dest)
        finally:
            dest.close()

    @staticmethod
    def check_library_file(path):
        """Raises ValueError unless `path` is a lap-timer library database."""
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                src.close()
        except sqlite3.Error as e:
            raise ValueError(f"That isn't a readable database ({e}).")
        if not {"videos", "marks"} <= tables:
            raise ValueError("That database has no videos/marks tables, so it isn't a lap timer library.")

    def replace_from(self, path):
        """Make this library an exact copy of the database at `path`, in place."""
        self.check_library_file(path)
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            src.backup(self.db)
        finally:
            src.close()
        self.upgrade_schema()

    def upgrade_schema(self):
        """Re-apply the schema upgrades so an older library file still works after a restore."""
        self.db.execute("PRAGMA foreign_keys = ON")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(videos)")}
        if "track" not in cols:
            self.db.execute("ALTER TABLE videos ADD COLUMN track TEXT NOT NULL DEFAULT ''")
        if "session_date" not in cols:
            self.db.execute("ALTER TABLE videos ADD COLUMN session_date TEXT")
        if "proxy_locked" not in cols:
            self.db.execute("ALTER TABLE videos ADD COLUMN proxy_locked INTEGER NOT NULL DEFAULT 0")
        if "notes" not in cols:
            self.db.execute("ALTER TABLE videos ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE TABLE IF NOT EXISTS extra_tracks (name TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS track_settings (track TEXT PRIMARY KEY, run_length INTEGER NOT NULL)")
        self.db.commit()

    def merge_from(self, path):
        """Add another library's videos and marks to this one. Videos are matched by path.
        New videos are copied with their marks; for videos in both, marks at times we don't have
        are added, and an empty track / session date is filled in. Nothing here is overwritten.
        -> dict(videos_added, videos_matched, marks_added)"""
        self.check_library_file(path)
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        stats = {"videos_added": 0, "videos_matched": 0, "marks_added": 0}
        try:
            cols = {r[1] for r in src.execute("PRAGMA table_info(videos)")}
            sel = "id, path, fps, " + ("track" if "track" in cols else "''") + ", " + \
                  ("session_date" if "session_date" in cols else "NULL") + ", " + \
                  ("proxy_locked" if "proxy_locked" in cols else "0") + ", " + ("notes" if "notes" in cols else "''")
            theirs = src.execute(f"SELECT {sel} FROM videos").fetchall()
            for sid, vpath, fps, track, sdate, locked, notes in theirs:
                row = self.db.execute("SELECT id, track, session_date, notes FROM videos WHERE path=?", (vpath,)).fetchone()
                if row:
                    vid = row[0]
                    stats["videos_matched"] += 1
                    if not row[1] and track:
                        self.db.execute("UPDATE videos SET track=? WHERE id=?", (track, vid))
                    if not row[2] and sdate:
                        self.db.execute("UPDATE videos SET session_date=? WHERE id=?", (sdate, vid))
                    if not row[3] and notes:
                        self.db.execute("UPDATE videos SET notes=? WHERE id=?", (notes, vid))
                else:
                    vid = self.db.execute("INSERT INTO videos(path, fps, track, session_date, proxy_locked, notes) VALUES(?,?,?,?,?,?)",
                                          (vpath, fps or 30, track or "", sdate, locked or 0, notes or "")).lastrowid
                    stats["videos_added"] += 1
                have = {r[0] for r in self.db.execute("SELECT time_ms FROM marks WHERE video_id=?", (vid,))}
                for t, bad in src.execute("SELECT time_ms, bad_lap FROM marks WHERE video_id=? ORDER BY time_ms", (sid,)):
                    if t not in have:
                        self.db.execute("INSERT INTO marks(video_id, time_ms, bad_lap) VALUES(?,?,?)", (vid, t, bad))
                        have.add(t)
                        stats["marks_added"] += 1
            if src.execute("SELECT 1 FROM sqlite_master WHERE name='extra_tracks'").fetchone():
                for (name,) in src.execute("SELECT name FROM extra_tracks"):       # empty tracks come across too
                    self.db.execute("INSERT OR IGNORE INTO extra_tracks(name) VALUES(?)", (name,))
            if src.execute("SELECT 1 FROM sqlite_master WHERE name='track_settings'").fetchone():
                for track, n in src.execute("SELECT track, run_length FROM track_settings"):   # keep ours if both have one
                    self.db.execute("INSERT OR IGNORE INTO track_settings(track, run_length) VALUES(?,?)", (track, n))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            src.close()
        return stats


def export_csv(lib, video_ids, path):
    """Write one row per lap for the given videos."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:      # BOM so Excel reads non-ASCII names
        w = csv.writer(f)
        w.writerow(["track", "session_date", "video", "lap", "start_s", "end_s",
                    "lap_time_s", "lap_time", "bad_lap", "notes"])
        meta, notes = lib.all_meta(), lib.notes_map()
        for vid, vpath, _ in lib.videos():
            if vid not in video_ids:
                continue
            track, sdate = meta[vid]
            d, _ = effective_date(vpath, sdate)
            d = d.isoformat() if d else ""
            m = lib.marks(vid)
            for i in range(1, len(m)):
                lap = m[i][1] - m[i - 1][1]
                w.writerow([track, d, vname(vpath), i, f"{m[i-1][1]/1000:.3f}", f"{m[i][1]/1000:.3f}",
                            f"{lap/1000:.3f}", fmt(lap), "yes" if m[i][2] else "no", notes.get(vid, "")])


# ----------------------------------------------------------------------------
# Export the fastest N consecutive laps as a video (with lap timer overlay)
# ----------------------------------------------------------------------------
EXPORT_LEAD_MS = 1000     # footage shown before the first gate
EXPORT_TAIL_MS = 2000     # footage held after the last lap, with the final total on screen
EXPORT_PIC_S = 2.0            # how long a start / end picture (sticker) stays on screen
EXPORT_PIC_PCT = 40           # picture height as a percentage of the video height
EXPORT_SEEK_MARGIN_MS = 15000   # how far before the clip we seek (the exact cut is made with trim)


def best_run_window(lib, video_ids, n=None):
    """Fastest run of N consecutive good laps across the given videos; a run never spans two videos.
    N defaults to the run length of the videos' tracks (callers make sure they agree).

    -> dict(vid, path, track, date, marks_ms=[m0..mN], lap_ms=[l1..lN], total_ms) or None."""
    if n is None:
        lengths = lib.run_lengths(video_ids)
        n = lengths.pop() if len(lengths) == 1 else run_length()
    meta = lib.all_meta()
    paths = {v: p for v, p, _ in lib.videos()}
    best = None
    for vid in video_ids:
        marks = lib.marks(vid)
        laps = [(marks[i][1] - marks[i - 1][1], bool(marks[i][2])) for i in range(1, len(marks))]
        b3 = lap_stats(laps, n)["bestn"]
        if b3 and (best is None or b3[0] < best["total_ms"]):
            i = b3[1]
            ms = [marks[i + k][1] for k in range(n + 1)]
            d = effective_date(paths[vid], meta[vid][1])[0]          # the session date, else the file's date
            best = {"vid": vid, "path": paths[vid], "track": meta[vid][0], "date": d.isoformat() if d else "",
                    "marks_ms": ms, "lap_ms": [ms[k + 1] - ms[k] for k in range(n)], "total_ms": b3[0]}
    return best


def hide_console(proc):
    """Windows only: stop a black console window flashing up for each ffmpeg we start (no-op elsewhere)."""
    if sys.platform != "win32" or not hasattr(proc, "setCreateProcessArgumentsModifier"):
        return

    def modifier(args):
        try:
            args.flags = args.flags | 0x08000000          # CREATE_NO_WINDOW
        except Exception:
            pass
    try:
        proc.setCreateProcessArgumentsModifier(modifier)
    except Exception:
        pass


_encoder_cache = {}


def ffmpeg_has_encoder(name="libx264"):
    """True if the ffmpeg we would use can encode with `name`. (Assumes yes if we can't tell.)"""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return False
    key = (ffmpeg, name)
    if key not in _encoder_cache:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20,
                                 creationflags=0x08000000 if sys.platform == "win32" else 0).stdout
            _encoder_cache[key] = name in out
        except Exception:
            _encoder_cache[key] = True
    return _encoder_cache[key]


_filter_cache = {}


def ffmpeg_has_filter(name):
    """True if the ffmpeg we would use has the video/audio filter `name`. (Assumes yes if we can't tell.)"""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return False
    key = (ffmpeg, name)
    if key not in _filter_cache:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20,
                                 creationflags=0x08000000 if sys.platform == "win32" else 0).stdout
            _filter_cache[key] = re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.M) is not None
        except Exception:
            _filter_cache[key] = True
    return _filter_cache[key]


FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"


def ffmpeg_install_hint():
    """One-line, copy-pasteable way to install ffmpeg on this OS (None on Windows: the app can run winget)."""
    if sys.platform == "win32":
        return None
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    return ("your package manager's ffmpeg package, e.g.  sudo apt install ffmpeg  or  sudo pacman -S ffmpeg"
            "  (Fedora: enable RPM Fusion, then  sudo dnf install ffmpeg)")


def find_brew():
    """macOS: path to Homebrew's `brew`, or None. (An app opened from Finder doesn't see the shell's PATH.)"""
    if sys.platform != "darwin":
        return None
    found = shutil.which("brew")
    if found:
        return found
    return next((str(p) for p in map(Path, ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")) if p.is_file()), None)


HOMEBREW_URL = "https://brew.sh"


def x264_help():
    """Plain-language explanation of a missing H.264 encoder, with the fix for this OS."""
    if sys.platform == "win32":
        fix = "Install a full build: run  winget install Gyan.FFmpeg  and restart the app."
    elif sys.platform == "darwin":
        fix = "Install a full build: run  brew install ffmpeg  and restart the app."
    else:
        fix = ("Install the full ffmpeg from your package manager and restart the app. On Fedora, enable RPM Fusion "
               "and run:  sudo dnf swap ffmpeg-free ffmpeg --allowerasing")
    return ("Your ffmpeg can't encode H.264 video (it has no 'libx264' encoder). That is needed for smooth-scrub "
            "copies and for exporting the fastest-laps video.\n\n" + fix)


def drawtext_help():
    """Plain-language explanation of an ffmpeg without the 'drawtext' filter (built without freetype)."""
    if sys.platform == "win32":
        fix = "Install a full build: run  winget install Gyan.FFmpeg  and restart the app."
    elif sys.platform == "darwin":
        fix = "Install a full build: run  brew install ffmpeg  and restart the app."
    else:
        fix = "Install the full ffmpeg from your package manager (not a minimal build) and restart the app."
    return ("Your ffmpeg can't draw text on video (it has no 'drawtext' filter, which needs the freetype library). "
            "That is needed to put the lap timer on the exported video.\n\n" + fix)


def is_drawtext_error(text):
    t = (text or "").lower()
    return "drawtext" in t and ("no such filter" in t or "not found" in t or "freetype" in t or "unknown filter" in t)


def is_x264_error(text):
    t = (text or "").lower()
    return "libx264" in t and ("unknown encoder" in t or "not found" in t or "no such" in t or "encoder" in t)


def find_overlay_font():
    """A bold TrueType font for the timer overlay (ffmpeg's drawtext needs a file). None if not found."""
    cands = []
    if sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        cands = [fonts / "arialbd.ttf", fonts / "segoeuib.ttf", fonts / "arial.ttf"]
    elif sys.platform == "darwin":
        cands = [Path(p) for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                                   "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc")]
    else:
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if out:
                cands.append(Path(out))
        except Exception:
            pass
        cands += [Path(p) for p in (
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")]
    return next((str(c) for c in cands if c.is_file()), None)


def probe_stream(path):
    """Video details needed for the export: width, height, fps, duration_ms, range/matrix tags, has_audio."""
    info = {"width": 1280, "height": 720, "fps": 30.0, "duration_ms": None, "full_range": False,
            "matrix": None, "audio": False}
    ffprobe = find_ffprobe()
    if not ffprobe:
        return info
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                              "stream=codec_type,width,height,avg_frame_rate,pix_fmt,color_range,color_space",
                              "-show_entries", "format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000 if sys.platform == "win32" else 0).stdout
        data = json.loads(out)
        for st in data.get("streams", []):
            if st.get("codec_type") == "audio":
                info["audio"] = True
            elif st.get("codec_type") == "video" and "width" in st and not info.get("_v"):
                info["_v"] = True
                info["width"], info["height"] = int(st["width"]), int(st["height"])
                num, den = st.get("avg_frame_rate", "30/1").split("/")
                info["fps"] = float(num) / float(den) if float(den) else 30.0
                info["full_range"] = st.get("color_range") == "pc" or str(st.get("pix_fmt", "")).startswith("yuvj")
                info["matrix"] = st.get("color_space")
        if data.get("format", {}).get("duration"):
            info["duration_ms"] = float(data["format"]["duration"]) * 1000
    except Exception:
        pass
    return info


def _filter_text(text):
    """Make a string safe inside a single-quoted drawtext text='...' value."""
    return str(text).replace("\\", "").replace("'", "\u2019").replace("%", "").replace("\n", " ")


OVERLAY_DEFAULTS = {"size": 1.0,          # text size multiplier
                    "swap": False,        # lap block on the right and the total block on the left
                    "list": True,         # finished-laps list
                    "track": True,        # track name
                    "total": True,        # "N LAPS" label and the running total
                    "banner": True}       # gold banner along the bottom at the end


def build_export_args(win, src, out, font, info, legacy_sync=False, tail_ms=EXPORT_TAIL_MS, lead_ms=EXPORT_LEAD_MS,
                      no_audio=False, audio_file=None, pictures=None, max_fps=None, decimals=3, preview_at=None, preview_w=640,
                      highlight_fastest=False, intro=None, outro=None, audio_all=False, overlay=None):
    """ffmpeg argv that cuts the fastest-N window from `src` and draws the lap timer overlay.

    intro / outro: optional {"path", "info"} (info from probe_stream) of a video joined before / after the clip,
    scaled and padded to the clip's size and frame rate. They keep their own sound (silence if they have none)
    unless audio_all is set with a replacement audio_file, which then plays under the whole video instead.

    overlay: optional look of the timer, see OVERLAY_DEFAULTS. A win with "custom" set (a chosen lap or range, not the
    fastest run) numbers its laps as in the video ("first_lap", "laps_in_video") instead of "fastest N consecutive".

    Returns (argv, clip_ms): the argument list and the length of the output video (intro and outro included).
    With preview_at (seconds into the clip) it instead renders that one frame, scaled to preview_w pixels wide,
    as a PNG to `out`: the same filters as the export, so the picture matches it exactly."""
    m = win["marks_ms"]
    n = len(win["lap_ms"])                                 # how many laps the window holds
    lead = min(lead_ms, m[0])                              # can't start before the video does
    tail = tail_ms
    if info.get("duration_ms"):
        tail = max(0, min(tail, int(info["duration_ms"] - m[n])))
    c0 = m[0] - lead                                       # clip start in the source (ms)
    clip_ms = (m[n] + tail) - c0
    s = [(x - c0) / 1000.0 for x in m]                     # gate times from the clip start, seconds
    eps = 0.0005                                           # a frame within 0.5 ms of a gate belongs to the next lap
    W, H = info["width"], info["height"]
    ov = dict(OVERLAY_DEFAULTS, **(overlay or {}))
    fs_big, fs_med, fs_small = (round(H * f * ov["size"]) for f in (0.080, 0.043, 0.032))
    mar = round(W * 0.025)
    custom = bool(win.get("custom"))
    lap0 = int(win.get("first_lap", 1))                    # number of the first exported lap in its video
    of_total = win.get("laps_in_video")
    if custom and n == 1:                                  # one chosen lap: its own timer says it all
        ov["list"] = ov["total"] = False
    x_a = mar if not ov["swap"] else f"w-text_w-{mar}"     # the lap block (left unless swapped)
    x_b = f"w-text_w-{mar}" if not ov["swap"] else mar     # the track / total block
    ff = font.replace("\\", "/").replace(":", "\\:")

    def dt(text, x, y, size, color="white", enable=None, box=0.55):
        if "%{" not in text:                               # plain text: a literal ':' must be escaped too
            text = text.replace(":", "\\:")
        opts = [f"fontfile='{ff}'", f"text='{text}'", f"fontsize={size}", f"fontcolor={color}",
                f"x={x}", f"y={y}", "box=1", f"boxcolor=black@{box}", f"boxborderw={max(4, size // 4)}"]
        if enable:
            opts.append(f"enable='{enable}'")
        return "drawtext=" + ":".join(opts)

    def running(start, minutes):                           # time since `start`, live: m:ss.mmm, or s.mmm under a minute
        x = f"(max(t-{start + 0.0006:.6f}\\,0)+0.0004)"      # marks are rounded to 1 ms: the gate frame reads 0:00.000
        secs = f"%{{eif\\:mod(floor({x})\\,60)\\:d\\:{2 if minutes else 1}}}"
        ms = (f".%{{eif\\:mod(floor({x}*{10 ** decimals})\\,{10 ** decimals})\\:d\\:{decimals}}}"
              if decimals else "")
        return (f"%{{eif\\:floor({x}/60)\\:d\\:1}}\\:" if minutes else "") + secs + ms

    def timer(start, en, *args, **kw):                     # running clock; the minutes appear once it reaches 60 s
        t60 = f"{start + 60.0002:.6f}"                     # (x reaches 60 at start + 60.0002)
        return [dt(running(start, False), *args, enable=f"({en})*lt(t,{t60})", **kw),
                dt(running(start, True), *args, enable=f"({en})*gte(t,{t60})", **kw)]

    y_lbl, y_big = mar, mar + round(fs_med * 1.7)
    y_list = y_big + round(fs_big * 1.75)
    # finished-laps list: keep it clear of the bottom banner however many laps there are
    line_h = min(fs_med * 1.6, max(fs_small, (H * 0.84 - y_list) / n))
    fs_list = max(10, min(fs_med, round(line_h / 1.5)))
    def lap_text(k):                                       # "LAP 2 / 3", or the video's own numbering for a chosen range
        if not custom:
            return f"LAP {k + 1} / {n}"
        return f"LAP {lap0 + k}" + (f" / {of_total}" if of_total else "")

    def range_name():                                      # label above the running total
        if not custom:
            return f"{n} LAPS"
        return f"LAPS {lap0}-{lap0 + n - 1}" if n > 1 else f"LAP {lap0}"

    def banner_name():
        if not custom:
            return f"FASTEST {n} CONSECUTIVE LAPS"
        return f"LAPS {lap0}-{lap0 + n - 1}" if n > 1 else f"LAP {lap0}"

    parts = []
    for k in range(n):                                     # lap k+1 running, from its gate to the next gate
        a = -1 if k == 0 else s[k] - eps                   # (lap 1 also covers the lead-in)
        b = s[k + 1] - eps
        en = f"gte(t,{a:.6f})*lt(t,{b:.6f})"
        parts.append(dt(lap_text(k), x_a, y_lbl, fs_med, enable=en))
        parts += timer(s[k], en, x_a, y_big, fs_big)
    en_end = f"gte(t,{s[n] - eps:.6f})"                    # after the last gate: hold the last lap
    parts.append(dt(lap_text(n - 1), x_a, y_lbl, fs_med, enable=en_end))
    parts.append(dt(fmt_overlay(win["lap_ms"][n - 1], decimals), x_a, y_big, fs_big, enable=en_end))
    best = win["lap_ms"].index(min(win["lap_ms"])) if highlight_fastest else -1   # the fastest of these laps (gold)
    for k in range(n if ov["list"] else 0):                # finished laps list, one line appears per lap
        parts.append(dt(f"LAP {lap0 + k}   {fmt_overlay(win['lap_ms'][k], decimals)}" if custom
                        else f"LAP {k + 1}   {fmt_overlay(win['lap_ms'][k], decimals)}", x_a, y_list + round(k * line_h), fs_list,
                        color="#FFD54F" if k == best else "white", enable=f"gte(t,{s[k + 1] - eps:.6f})"))
    # the other corner: track name, then the running total of the laps
    label = _filter_text(win.get("track") or "") if ov["track"] else ""
    if label:
        parts.append(dt(label, x_b, y_lbl, fs_small))
    y_tot = y_lbl + round(fs_small * 1.9) if label else y_lbl
    if ov["total"]:
        parts.append(dt(range_name(), x_b, y_tot, fs_med))
        parts += timer(s[0], f"lt(t,{s[n] - eps:.6f})", x_b, y_tot + round(fs_med * 1.7), fs_big)
        parts.append(dt(fmt_overlay(win["total_ms"], decimals), x_b, y_tot + round(fs_med * 1.7), fs_big,
                        color="#FFD54F", enable=en_end))
    if ov["banner"]:
        parts.append(dt(f"{banner_name()}   {fmt_overlay(win['total_ms'], decimals)}", "(w-text_w)/2", f"h-text_h-{mar}",
                        round(fs_med * 1.25), color="#FFD54F", enable=en_end, box=0.65))

    matrix = {"bt470bg": "bt470", "smpte170m": "smpte170m", "bt709": "bt709", "bt2020nc": "bt2020",
              "bt2020c": "bt2020"}.get(info.get("matrix") or "", "bt709" if H >= 720 else "smpte170m")
    convert = (f"scale=in_range={'pc' if info.get('full_range') else 'tv'}:out_range=tv:"
               f"in_color_matrix={matrix}:out_color_matrix=bt709,format=yuv420p")
    # Seeking inside .ts files can land a keyframe late, so only seek roughly (15 s early, always before the
    # clip) and cut to the exact frame with trim; setpts then makes the overlay clock start at 0.
    at_ms = 0 if preview_at is None else int(max(0, min(preview_at, clip_ms / 1000 - 0.05)) * 1000)
    seek_ms = max(0, c0 + at_ms - EXPORT_SEEK_MARGIN_MS)
    skip = (c0 + at_ms - seek_ms) / 1000.0
    if preview_at is None:
        cut = f"trim=start={skip:.6f}:duration={clip_ms / 1000:.6f},setpts=PTS-STARTPTS"
    else:                                                  # a short piece, with the clock set to its place in the clip
        cut = f"trim=start={skip:.6f}:duration=0.5,setpts=PTS-STARTPTS+{at_ms / 1000:.6f}/TB"
    tags = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
    # Lower frame rate (e.g. 90 -> 60 for YouTube): done after the overlay is drawn, so the timer stays exact.
    # Never raises the rate: a video already at or below the limit is left alone.
    lower = [f"fps={max_fps:g}"] if max_fps and info.get("fps", 0) > max_fps + 0.01 else []
    if preview_at is not None:
        lower = [f"scale={preview_w}:-2:flags=bicubic"]
    vf = ",".join([cut, convert] + parts + [tags, "format=yuv420p"] + lower)
    vf_base = ",".join([cut, convert] + parts + [tags, "format=yuv420p"])
    clip_s = clip_ms / 1000
    if audio_file:                                         # replacement sound track, from its start; padded with
        fade = min(1.0, clip_s)                            # silence if too short, cut (with a 1 s fade) if too long
        audio = ["-af", f"apad=whole_dur={clip_s:.6f},atrim=duration={clip_s:.6f},asetpts=PTS-STARTPTS,"
                        f"afade=t=out:st={clip_s - fade:.6f}:d={fade:.3f}"]
        audio_map = ["-map", "1:a:0"]
    elif no_audio:
        audio, audio_map = [], ["-an"]                     # -an: leave the sound track out
    else:
        audio = (["-af", f"atrim=start={skip:.6f}:duration={clip_s:.6f},asetpts=PTS-STARTPTS"]
                 if info.get("audio") else [])
        audio_map = ["-map", "0:a:0?"]
    audio_enc = [] if (no_audio and not audio_file) else ["-c:a", "aac", "-b:a", "192k"]
    pic_in, extra_in, extra_s, video_args = [], [], 0.0, ["-map", "0:v:0", "-vf", vf]
    pictures = [p for p in (pictures or []) if p.get("path")]
    if preview_at is not None:                             # one frame: work out each picture's fade by hand, so it
        at_s, shown = at_ms / 1000, []                     # is read once instead of looped (slow on a network drive)
        for pic in pictures:
            secs = float(pic.get("secs", EXPORT_PIC_S))
            if pic.get("where") == "start":
                t1 = min(clip_s, secs)
                fade_d = max(0.05, min(0.5, t1))
                alpha = 1.0 if at_s < t1 - fade_d else (max(0.0, t1 - at_s) / fade_d)
            else:
                t0 = max(0.0, clip_s - secs)
                fade_d = max(0.05, min(0.5, clip_s - t0))
                alpha = min(1.0, max(0.0, (at_s - t0) / fade_d))
            if alpha > 0:
                shown.append((pic, alpha))
        pictures = [dict(pic, alpha=a) for pic, a in shown]
    extras = [] if preview_at is not None else [(r, e) for r, e in (("intro", intro), ("outro", outro)) if e]
    if pictures or extras:
        # pictures (e.g. the pilot's sticker) laid over the centre of the video. "end": fades in and stays for
        # the last `secs` seconds; "start": shown from the first frame, fades out after `secs` seconds. Never
        # scaled up past their own size; colours converted to the BT.709 the video uses.
        first = 2 if audio_file else 1                     # input number of the first picture
        parts_g, last = [f"[0:v]{vf_base}[base]"], "base"
        for k, pic in enumerate(pictures):
            idx, secs = first + k, float(pic.get("secs", EXPORT_PIC_S))
            th = max(16, round(H * float(pic.get("pct", EXPORT_PIC_PCT)) / 100))
            max_w = round(W * 0.9)
            if preview_at is not None:
                fade, en = f"colorchannelmixer=aa={pic['alpha']:.3f}", "1"
            elif pic.get("where") == "start":
                t0, t1 = 0.0, min(clip_s, secs)
                fade_d = max(0.05, min(0.5, t1))
                fade = f"fade=t=out:st={t1 - fade_d:.3f}:d={fade_d:.3f}:alpha=1"
                en = f"lt(t,{t1:.3f})"
            else:
                t0 = max(0.0, clip_s - secs)
                fade_d = max(0.05, min(0.5, clip_s - t0))
                fade = f"fade=t=in:st={t0:.3f}:d={fade_d:.3f}:alpha=1"
                en = f"gte(t,{t0:.3f})"
            parts_g.append(f"[{idx}:v]format=rgba,scale=w='min(iw,min({max_w},iw*{th}/ih))':h=-2:flags=lanczos:"
                           f"out_color_matrix=bt709:out_range=tv,format=yuva420p,{fade}[pic{k}]")
            out_label = f"v{k}"
            ending = "eof_action=repeat" if preview_at is not None else "shortest=1"
            parts_g.append(f"[{last}][pic{k}]overlay=x=(W-w)/2:y=(H-h)/2:{ending}:enable='{en}'[{out_label}]")
            last = out_label
            pic_in += ([] if preview_at is not None else ["-loop", "1", "-framerate", "30"]) + ["-i", str(pic["path"])]
        main_v = "mv" if extras else "v"
        parts_g.append(",".join([f"[{last}]format=yuv420p"] + lower + (["setsar=1"] if extras else [])) + f"[{main_v}]")
        video_args = ["-filter_complex", ";".join(parts_g), "-map", "[v]"]
        if extras:
            # intro / outro: each is fitted inside the clip's frame (black bars if the shape differs), brought to the
            # clip's frame rate and colours, then all the pieces are joined with the concat filter.
            fps_out = max_fps if lower else info.get("fps", 30.0)
            norm_a = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
            joined_audio = bool(audio_file) and audio_all and not no_audio
            has_audio = not no_audio or bool(audio_file)
            vparts, aparts, names = [], [], []
            if has_audio and not joined_audio:             # the clip's own sound (or the replacement, clip-length)
                if audio_file:
                    aparts.append(f"[1:a:0]apad=whole_dur={clip_s:.6f},atrim=duration={clip_s:.6f},asetpts=PTS-STARTPTS,"
                                  f"afade=t=out:st={clip_s - min(1.0, clip_s):.6f}:d={min(1.0, clip_s):.3f},{norm_a}[am]")
                elif info.get("audio"):
                    aparts.append(f"[0:a:0]atrim=start={skip:.6f}:duration={clip_s:.6f},asetpts=PTS-STARTPTS,"
                                  f"apad=whole_dur={clip_s:.6f},{norm_a}[am]")
                else:
                    aparts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={clip_s:.6f},asetpts=PTS-STARTPTS[am]")
            idx = first + len(pictures)
            for role, e in extras:
                ei, d = e["info"], e["info"]["duration_ms"] / 1000.0
                eh, ew = ei["height"], ei["width"]
                em = {"bt470bg": "bt470", "smpte170m": "smpte170m", "bt709": "bt709", "bt2020nc": "bt2020",
                      "bt2020c": "bt2020"}.get(ei.get("matrix") or "", "bt709" if eh >= 720 else "smpte170m")
                vparts.append(
                    f"[{idx}:v:0]trim=duration={d:.6f},setpts=PTS-STARTPTS,"
                    f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps_out:g},"
                    f"scale=in_range={'pc' if ei.get('full_range') else 'tv'}:out_range=tv:"
                    f"in_color_matrix={em}:out_color_matrix=bt709,format=yuv420p,{tags}[{role}v]")
                if has_audio and not joined_audio:
                    if ei.get("audio"):
                        aparts.append(f"[{idx}:a:0]apad=whole_dur={d:.6f},atrim=duration={d:.6f},asetpts=PTS-STARTPTS,"
                                      f"{norm_a}[{role}a]")
                    else:
                        aparts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.6f},asetpts=PTS-STARTPTS[{role}a]")
                names.append(role)
                extra_s += d
                idx += 1
                extra_in += ["-i", str(e["path"])]
            vin = (["[introv]"] if "intro" in names else []) + ["[mv]"] + (["[outrov]"] if "outro" in names else [])
            ain = (["[introa]"] if "intro" in names else []) + ["[am]"] + (["[outroa]"] if "outro" in names else [])
            n_seg = len(vin)
            total_s = clip_s + extra_s
            if joined_audio:                               # one replacement track under everything
                aparts.append(f"[1:a:0]apad=whole_dur={total_s:.6f},atrim=duration={total_s:.6f},asetpts=PTS-STARTPTS,"
                              f"afade=t=out:st={total_s - min(1.0, total_s):.6f}:d={min(1.0, total_s):.3f},{norm_a}[a]")
                parts_g += vparts + aparts
                parts_g.append("".join(vin) + f"concat=n={n_seg}:v=1:a=0[v]")
            elif has_audio:
                parts_g += vparts + aparts
                parts_g.append("".join(v + a for v, a in zip(vin, ain)) + f"concat=n={n_seg}:v=1:a=1[v][a]")
            else:
                parts_g += vparts
                parts_g.append("".join(vin) + f"concat=n={n_seg}:v=1:a=0[v]")
            video_args = ["-filter_complex", ";".join(parts_g), "-map", "[v]"]
            if has_audio:
                audio_map, audio = ["-map", "[a]"], []
    if preview_at is not None:
        return ["-y", "-loglevel", "error", "-nostats", "-ss", f"{seek_ms / 1000:.3f}", "-i", str(src), *pic_in,
                *video_args, "-an", "-frames:v", "1", "-f", "image2", "-c:v", "png", str(out)], clip_ms
    argv = ["-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-ss", f"{seek_ms / 1000:.3f}", "-i", str(src),
            *(["-i", str(audio_file)] if audio_file else []),
            *pic_in, *extra_in,
            "-t", f"{clip_ms / 1000 + extra_s + 0.5:.3f}",
            *video_args, *audio_map, *audio,
            *(SYNC_FLAGS_OLD if legacy_sync else SYNC_FLAGS),
            "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            *audio_enc, "-movflags", "+faststart", str(out)]
    return argv, clip_ms + extra_s * 1000


PREVIEW_W = 480                                           # width of the overlay preview in the export window


class PictureRow:
    """A picture chooser for the export dialog: path box + Browse + Clear, and 'Shown for' / 'Size' boxes.
    The picture and the two numbers are remembered between exports (QSettings keys <key>, <key>_s, <key>_pct)."""
    def __init__(self, dialog, label, key, tip, fade_word):
        self.dialog, self.key = dialog, key
        st = dialog.settings
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("none (PNG, JPG, WebP, BMP or GIF)")
        self.edit.setToolTip(tip)
        remembered = str(st.value(key, "") or "")
        self.edit.setText(remembered if remembered and os.path.exists(remembered) else "")
        self.browse = QPushButton("Browse…")
        self.browse.clicked.connect(self.choose)
        self.clear = QPushButton("Clear")
        self.clear.clicked.connect(self.edit.clear)
        self.secs = QDoubleSpinBox()
        self.secs.setRange(0.5, 10)
        self.secs.setSingleStep(0.5)
        self.secs.setDecimals(1)
        self.secs.setSuffix(" s")
        self.secs.setToolTip(f"How long the picture stays on screen ({fade_word}).")
        try:
            self.secs.setValue(float(st.value(key + "_s", EXPORT_PIC_S)))
        except (TypeError, ValueError):
            self.secs.setValue(EXPORT_PIC_S)
        self.size = QSpinBox()
        self.size.setRange(10, 100)
        self.size.setSuffix(" %")
        self.size.setToolTip("Picture height as a percentage of the video height (never enlarged past its own size)")
        try:
            self.size.setValue(int(st.value(key + "_pct", EXPORT_PIC_PCT)))
        except (TypeError, ValueError):
            self.size.setValue(EXPORT_PIC_PCT)
        self.row1 = QHBoxLayout()
        lab = QLabel(label)
        lab.setMinimumWidth(95)
        self.row1.addWidget(lab); self.row1.addWidget(self.edit, 1)
        self.row1.addWidget(self.browse); self.row1.addWidget(self.clear)
        self.opts = QWidget()                              # 'Shown for' / 'Size': only visible once a picture is chosen
        self.row2 = QHBoxLayout(self.opts)
        self.row2.setContentsMargins(0, 0, 0, 0)
        self.row2.addSpacing(95)
        self.row2.addWidget(QLabel("Shown for:")); self.row2.addWidget(self.secs)
        self.row2.addSpacing(16)
        self.row2.addWidget(QLabel("Size:")); self.row2.addWidget(self.size)
        self.row2.addStretch(1)
        self.edit.textChanged.connect(self.sync)
        self.sync()

    def path(self):
        return self.edit.text().strip() or None

    def spec(self, where):
        """Description for build_export_args (path None when no picture is chosen)."""
        return {"path": self.path(), "where": where, "secs": self.secs.value(), "pct": self.size.value()}

    def save(self):
        st = self.dialog.settings
        st.setValue(self.key, self.path() or "")
        st.setValue(self.key + "_s", self.secs.value())
        st.setValue(self.key + "_pct", self.size.value())

    def sync(self, *_):
        has = bool(self.path())
        self.clear.setEnabled(has)
        self.secs.setEnabled(has)
        self.size.setEnabled(has)
        if has != getattr(self, "opts_shown", None):
            self.opts_shown = has
            self.opts.setVisible(has)
            if isinstance(getattr(self.dialog, "scroll", None), QScrollArea):   # dialog built: resize to the rows showing
                self.dialog.fit_to_screen()

    def set_busy(self, busy):
        """While an export runs everything is locked; afterwards the boxes follow whether a picture is chosen."""
        for w in (self.edit, self.browse):
            w.setEnabled(not busy)
        if busy:
            for w in (self.clear, self.secs, self.size):
                w.setEnabled(False)
        else:
            self.sync()

    def choose(self):
        p, _ = QFileDialog.getOpenFileName(
            self.dialog, "Choose a picture", self.edit.text() or str(Path.home()),
            "Pictures (*.png *.jpg *.jpeg *.bmp *.webp *.gif);;All files (*)")
        if p:
            self.edit.setText(p)


class ClipRow:
    """An intro / outro video chooser for the export dialog: label + path box + Browse + Clear.
    The chosen file is remembered between exports (QSettings key <key>)."""
    def __init__(self, dialog, label, key, tip):
        self.dialog, self.key = dialog, key
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("none (any video file)")
        self.edit.setToolTip(tip)
        remembered = str(dialog.settings.value(key, "") or "")
        self.edit.setText(remembered if remembered and os.path.exists(remembered) else "")
        self.browse = QPushButton("Browse…")
        self.browse.clicked.connect(self.choose)
        self.clear = QPushButton("Clear")
        self.clear.clicked.connect(self.edit.clear)
        self.row = QHBoxLayout()
        lab = QLabel(label)
        lab.setMinimumWidth(95)
        self.row.addWidget(lab); self.row.addWidget(self.edit, 1)
        self.row.addWidget(self.browse); self.row.addWidget(self.clear)
        self.edit.textChanged.connect(self.sync)
        self.sync()

    def path(self):
        return self.edit.text().strip() or None

    def save(self):
        self.dialog.settings.setValue(self.key, self.path() or "")

    def sync(self, *_):
        self.clear.setEnabled(bool(self.path()))

    def set_busy(self, busy):
        for w in (self.edit, self.browse):
            w.setEnabled(not busy)
        if busy:
            self.clear.setEnabled(False)
        else:
            self.sync()

    def choose(self):
        p, _ = QFileDialog.getOpenFileName(
            self.dialog, "Choose a video", self.edit.text() or str(Path.home()),
            "Videos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts);;All files (*)")
        if p:
            self.edit.setText(p)


class ExportDialog(QDialog):
    """Shows what will be exported, asks where to save it, then runs ffmpeg with a progress bar."""
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        n = len(win["lap_ms"])
        self.custom = bool(win.get("custom"))
        a0 = int(win.get("first_lap", 1))
        self.range_text = f"laps {a0}-{a0 + n - 1}" if n > 1 else f"lap {a0}"           # "laps 5-8" / "lap 5"
        self.setWindowTitle(f"Export {self.range_text} as video" if self.custom else f"Export fastest {n} laps as video")
        self.proc = None
        self.final = None
        self.part = None
        self.legacy = False
        self.clip_ms = 1
        self.buf = ""
        self.cancelled = False

        lap_no = int(win.get("first_lap", 1)) if win.get("custom") else 1
        laps = " &nbsp;·&nbsp; ".join(f"Lap {lap_no + i}: <b>{fmt(ms)}</b>" for i, ms in enumerate(win["lap_ms"]))
        track = win["track"] or "No track"
        summary = QLabel(f"<b>{html.escape(track)}</b> &nbsp;·&nbsp; {html.escape(vname(win['path']))}<br>{laps}<br>"
                         + (f"{self.range_text.capitalize()}: <b>{fmt(win['total_ms'])}</b>" if self.custom
                            else f"Fastest {n} consecutive: <b>{fmt(win['total_ms'])}</b>"))
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary.setWordWrap(True)
        source_dir = Path(win["path"]).parent                 # save next to the video it was cut from
        default_dir = source_dir if source_dir.is_dir() else Path.home() / "Videos"
        ms = int(round(win["total_ms"]))
        total = f"{ms / 1000:.3f}" if ms < 60000 else fmt(ms)   # under a minute: "25.737", not "0:25.737"
        what = self.range_text if self.custom else f"fastest {n}"
        name = f"{track if win['track'] else Path(vname(win['path'])).stem} {what} {total}.mp4"
        name = re.sub(r'[\\/:*?"<>|]', "-", name)
        self.path_edit = QLineEdit(str((default_dir if default_dir.is_dir() else Path.home()) / name))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.hide()
        self.status = QLabel("Re-encodes the original footage (not the smooth copy) as H.264 MP4 at its original "
                             "size, with the lap timer drawn on it. Frame rate is kept unless you pick a lower one below.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.folder_link = QLabel()                        # shown once the export is done: opens the folder
        self.folder_link.setOpenExternalLinks(True)
        self.folder_link.hide()
        self.status.setStyleSheet("color:gray")
        self.settings = app_settings()
        def padding_spin(key, default_ms, tip):
            spin = QDoubleSpinBox()
            spin.setRange(0, 10)
            spin.setSingleStep(0.5)
            spin.setDecimals(1)
            spin.setSuffix(" s")
            try:
                value = float(self.settings.value(key, default_ms / 1000))
            except (TypeError, ValueError):
                value = default_ms / 1000
            spin.setValue(value)
            spin.setToolTip(tip)
            return spin
        self.lead_spin = padding_spin("export_lead_s", EXPORT_LEAD_MS,
                                      "Footage kept before the first gate, with the lap timer waiting at 0.000")
        self.tail_spin = padding_spin("export_tail_s", EXPORT_TAIL_MS,
                                      "Footage kept after the last lap, with the final total on screen")
        self.no_audio = QCheckBox("Remove audio (whole video, including intro and outro)")
        self.no_audio.setToolTip("Export with no sound track at all: the lap clip, the intro video and the outro video "
                                 "are all silent")
        self.no_audio.setChecked(self.settings.value("export_no_audio", False, type=bool))
        self.audio_edit = QLineEdit()
        self.audio_edit.setPlaceholderText("none: keep the video's own audio")
        self.audio_edit.setToolTip("An audio file to use instead of the video's sound. It starts with the clip, "
                                   "is padded with silence if shorter and faded out at the end if longer.")
        remembered = str(self.settings.value("export_audio", "") or "")      # last audio file, if still there
        if remembered and os.path.exists(remembered) and not self.no_audio.isChecked():
            self.audio_edit.setText(remembered)
        self.audio_edit.textChanged.connect(self.sync_audio_options)
        self.audio_browse = QPushButton("Browse…")
        self.audio_browse.clicked.connect(self.browse_audio)
        self.audio_clear = QPushButton("Clear")
        self.audio_clear.clicked.connect(self.audio_edit.clear)
        self.no_audio.toggled.connect(self.sync_audio_options)
        self.start_pic = PictureRow(self, "Start picture:", "export_start_pic",
                                    "A picture shown in the centre of the video from the first frame, fading out after "
                                    "the time below.", fade_word="fades out over the last half second")
        self.end_pic = PictureRow(self, "End picture:", "export_end_pic",
                                  "A picture shown in the centre of the video at the end, fading in.",
                                  fade_word="fades in over the first half second")
        self.intro = ClipRow(self, "Intro video:", "export_intro",
                             "A video of your own played before the lap clip. It is fitted into the same frame size "
                             "and frame rate (black bars if the shape differs) and keeps its own sound.")
        self.outro = ClipRow(self, "Outro video:", "export_outro",
                             "A video of your own played after the lap clip. It is fitted into the same frame size "
                             "and frame rate (black bars if the shape differs) and keeps its own sound.")
        self.audio_all = QCheckBox("Replacement audio also plays over the intro video and outro video (added above)")
        self.audio_all.setToolTip("Off: the audio file covers only the lap clip, and the intro and outro keep their own "
                                  "sound. On: the audio file plays under the whole video, intro and outro included "
                                  "(their own sound is dropped).")
        self.audio_all.setChecked(self.settings.value("export_audio_all", False, type=bool))
        self.audio_edit.textChanged.connect(self.sync_audio_all)
        self.no_audio.toggled.connect(self.sync_audio_all)
        self.intro.edit.textChanged.connect(self.sync_audio_all)
        self.outro.edit.textChanged.connect(self.sync_audio_all)
        self.fps_combo = QComboBox()
        self.fps_combo.setToolTip("Lower the frame rate of the exported video. YouTube plays at most 60 fps, so a 90 fps "
                                  "export is better converted to 60. A video already at or below the choice is not changed.")
        self.fps_combo.addItem("Original (keep as recorded)", None)
        self.fps_combo.addItem("60 fps", 60)
        self.fps_combo.addItem("30 fps", 30)
        i = self.fps_combo.findData(self.settings.value("export_max_fps", 0, type=int))
        self.fps_combo.setCurrentIndex(max(0, i))
        self.dec_combo = QComboBox()
        self.dec_combo.setToolTip("How many digits after the seconds the lap timer shows on the video")
        for d, label in ((3, "3 (25.737)"), (2, "2 (25.74)"), (1, "1 (25.7)"), (0, "0 (25)")):
            self.dec_combo.addItem(label, d)
        self.dec_combo.setCurrentIndex(max(0, self.dec_combo.findData(self.settings.value("export_decimals", 3, type=int))))
        self.preview_on = QCheckBox("Show preview")
        self.preview_on.setToolTip("Draw one frame of the finished video here, so you can see the overlay before exporting. "
                                   "Turn it off if it slows this window down.")
        self.preview_on.setChecked(self.settings.value("export_preview_on", False, type=bool))
        self.preview_at = QComboBox()
        for label, key in (("End of the clip", "end"), ("During the last lap", "middle"), ("Start of the clip", "start")):
            self.preview_at.addItem(label, key)
        self.preview_img = QLabel()
        self.preview_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_img.setMinimumSize(PREVIEW_W, PREVIEW_W * 9 // 16)
        self.preview_img.setStyleSheet("background:#202020; color:#bbbbbb")
        self.preview_img.setWordWrap(True)
        self.preview_proc = None
        self.preview_file = None
        self.preview_info = None
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(400)                # wait for the changes to settle before drawing
        self.preview_timer.timeout.connect(self.render_preview)
        self.highlight = QCheckBox("Highlight fastest lap")
        self.highlight.setToolTip("Show the fastest of the exported laps in gold in the finished-laps list")
        self.highlight.setChecked(self.settings.value("export_highlight", False, type=bool))
        self.ov_size = QComboBox()
        self.ov_size.setToolTip("Size of the timer text on the video")
        for label, v in (("Small", 0.75), ("Medium", 1.0), ("Large", 1.25)):
            self.ov_size.addItem(label, v)
        i = self.ov_size.findData(float(self.settings.value("export_ov_size", 1.0)))
        self.ov_size.setCurrentIndex(i if i >= 0 else 1)
        def ov_check(key, text, tip, default=True):
            box = QCheckBox(text)
            box.setToolTip(tip)
            box.setChecked(self.settings.value(key, default, type=bool))
            return box
        self.ov_swap = ov_check("export_ov_swap", "Swap sides", "Put the lap timer on the right and the total and track "
                                "name on the left, to keep clear of your DVR's own on-screen display", default=False)
        self.ov_list = ov_check("export_ov_list", "Lap list", "The list of finished laps under the lap timer")
        self.ov_track = ov_check("export_ov_track", "Track name", "The track name above the total")
        self.ov_total = ov_check("export_ov_total", "Total", "The running total of all the laps")
        self.ov_banner = ov_check("export_ov_banner", "Banner", "The gold banner along the bottom at the end")
        self.reset_pad = QPushButton("Reset padding")
        self.reset_pad.setToolTip(f"Back to the defaults: {EXPORT_LEAD_MS / 1000:g} s before, "
                                  f"{EXPORT_TAIL_MS / 1000:g} s after")
        self.reset_pad.clicked.connect(self.reset_padding)
        self.go = QPushButton("Export")
        self.go.setDefault(True)
        self.go.clicked.connect(self.start)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.on_cancel)

        # the options scroll if the screen is too small for them, so the preview never overlaps the fields
        outer = QVBoxLayout(self)
        self.body = QWidget()
        lay = QVBoxLayout(self.body)
        lay.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)
        lay.addWidget(summary)
        prow = QHBoxLayout()
        prow.addWidget(self.preview_on); prow.addWidget(self.preview_at); prow.addStretch(1)
        lay.addLayout(prow)
        lay.addWidget(self.preview_img, 0, Qt.AlignmentFlag.AlignHCenter)
        def section(title):                                 # a bold heading with a thin rule under it
            head = QLabel(f"<b>{title}</b>")
            head.setStyleSheet("margin-top:6px")
            rule = QFrame()
            rule.setFrameShape(QFrame.Shape.HLine)
            rule.setFrameShadow(QFrame.Shadow.Sunken)
            lay.addWidget(head)
            lay.addWidget(rule)

        section("Output")
        row = QHBoxLayout(); row.addWidget(QLabel("Save as:")); row.addWidget(self.path_edit, 1); row.addWidget(browse)
        lay.addLayout(row)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Start padding:")); trow.addWidget(self.lead_spin)
        trow.addSpacing(16)
        trow.addWidget(QLabel("End padding:")); trow.addWidget(self.tail_spin)
        trow.addSpacing(16)
        trow.addWidget(self.reset_pad)
        trow.addStretch(1)
        lay.addLayout(trow)
        frow = QHBoxLayout(); frow.addWidget(QLabel("Frame rate:")); frow.addWidget(self.fps_combo); frow.addStretch(1)
        lay.addLayout(frow)

        section("Timer")
        orow = QHBoxLayout()
        orow.addWidget(QLabel("Text size:")); orow.addWidget(self.ov_size)
        orow.addSpacing(16); orow.addWidget(QLabel("Decimals:")); orow.addWidget(self.dec_combo)
        orow.addSpacing(16); orow.addWidget(self.ov_swap)
        orow.addStretch(1)
        lay.addLayout(orow)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Show:"))
        for box in (self.ov_list, self.ov_track, self.ov_total, self.ov_banner):
            srow.addWidget(box)
        srow.addSpacing(16); srow.addWidget(self.highlight)
        srow.addStretch(1)
        lay.addLayout(srow)

        section("Pictures")
        for pic in (self.start_pic, self.end_pic):
            lay.addLayout(pic.row1)
            lay.addWidget(pic.opts)

        section("Intro, outro and audio")
        lay.addLayout(self.intro.row)
        lay.addLayout(self.outro.row)
        lay.addWidget(self.no_audio)
        arow = QHBoxLayout()
        arow.addWidget(QLabel("Replace audio with:")); arow.addWidget(self.audio_edit, 1)
        arow.addWidget(self.audio_browse); arow.addWidget(self.audio_clear)
        lay.addLayout(arow)
        lay.addWidget(self.audio_all)
        self.sync_audio_options()
        self.sync_audio_all()
        lay.addStretch(1)
        outer.addWidget(self.bar)
        outer.addWidget(self.status)
        outer.addWidget(self.folder_link)
        btns = QHBoxLayout(); btns.addStretch(1); btns.addWidget(self.cancel); btns.addWidget(self.go)
        outer.addLayout(btns)
        self.fit_to_screen()
        for w in (self.lead_spin, self.tail_spin, self.start_pic.secs, self.start_pic.size,
                  self.end_pic.secs, self.end_pic.size):
            w.valueChanged.connect(self.refresh_preview)
        for w in (self.start_pic.edit, self.end_pic.edit):
            w.textChanged.connect(self.refresh_preview)
        self.dec_combo.currentIndexChanged.connect(self.refresh_preview)
        self.ov_size.currentIndexChanged.connect(self.refresh_preview)
        for box in (self.ov_swap, self.ov_list, self.ov_track, self.ov_total, self.ov_banner):
            box.toggled.connect(self.refresh_preview)
        self.highlight.toggled.connect(self.refresh_preview)
        self.preview_at.currentIndexChanged.connect(self.refresh_preview)
        self.preview_on.toggled.connect(self.refresh_preview)
        self.refresh_preview()

    def overlay_options(self):
        return {"size": self.ov_size.currentData(), "swap": self.ov_swap.isChecked(), "list": self.ov_list.isChecked(),
                "track": self.ov_track.isChecked(), "total": self.ov_total.isChecked(),
                "banner": self.ov_banner.isChecked()}

    def fit_to_screen(self):
        """Size the window to its contents, but never taller than the screen (the options scroll instead)."""
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        avail, avail_w = (geo.height(), geo.width()) if geo else (700, 1000)
        want = self.body.sizeHint().height() + 170         # + progress bar, status line and buttons
        # never narrower than the widest row needs (plus the scrollbar and margins), so no button is cut off
        need_w = self.body.minimumSizeHint().width()
        need_w += self.scroll.verticalScrollBar().sizeHint().width() + 40
        need_w = min(need_w, avail_w)
        self.setMinimumWidth(need_w)
        self.resize(max(self.width(), need_w, 660), max(300, min(want, int(avail * 0.9))))

    def refresh_preview(self, *_):
        """Show or hide the preview, and (re)draw it a moment after the last change."""
        on = self.preview_on.isChecked()
        self.preview_img.setVisible(on)
        self.preview_at.setEnabled(on)
        if not on:
            self.preview_timer.stop()
            self.stop_preview()
            self.fit_to_screen()
            return
        self.fit_to_screen()
        self.preview_timer.start()

    def stop_preview(self):
        proc, self.preview_proc = self.preview_proc, None   # cleared first: preview_done ignores a replaced render
        if proc and proc.state() != QProcess.ProcessState.NotRunning:
            proc.kill()
            proc.waitForFinished(2000)

    def preview_message(self, text):
        self.preview_img.setPixmap(QPixmap())
        self.preview_img.setText(text)

    def render_preview(self):
        """Render one frame of the export with the current settings (the same ffmpeg filters as the real export)."""
        if not self.preview_on.isChecked() or self.proc is not None:
            return
        ffmpeg, font = find_tool("ffmpeg"), find_overlay_font()
        if not (ffmpeg and font and ffmpeg_has_encoder("libx264") and ffmpeg_has_filter("drawtext")):
            self.preview_message("Preview needs ffmpeg (with the drawtext filter) and a bold font; see Export.")
            return
        if not os.path.exists(self.win["path"]):
            self.preview_message("The source video can't be found.")
            return
        if self.preview_info is None:                      # first time: ffprobe can take a while on a slow drive
            self.preview_message("Drawing preview…")
            QApplication.processEvents()
            self.preview_info = probe_stream(self.win["path"])
        pics = [pic.spec(where) for pic, where in ((self.start_pic, "start"), (self.end_pic, "end"))
                if pic.path() and os.path.exists(pic.path())]
        at = self.preview_at.currentData()
        lead_ms, tail_ms = int(self.lead_spin.value() * 1000), int(self.tail_spin.value() * 1000)
        if self.preview_file is None:
            fd, self.preview_file = tempfile.mkstemp(suffix=".png", prefix="lap_timer_preview_")
            os.close(fd)
        # the clip length (for the 'end' and 'middle' frames) comes from the same arithmetic as the export
        _, clip_ms = build_export_args(self.win, self.win["path"], self.preview_file, font, self.preview_info,
                                       tail_ms=tail_ms, lead_ms=lead_ms)
        m, n = self.win["marks_ms"], len(self.win["lap_ms"])
        lead = min(lead_ms, m[0])
        when = {"start": 0.0, "end": clip_ms / 1000,
                "middle": (lead + (m[n - 1] + m[n]) / 2 - m[0]) / 1000}[at]     # halfway through the last lap
        argv, _ = build_export_args(self.win, self.win["path"], self.preview_file, font, self.preview_info,
                                    tail_ms=tail_ms, lead_ms=lead_ms, no_audio=True, pictures=pics,
                                    decimals=self.dec_combo.currentData(), preview_at=when, preview_w=PREVIEW_W,
                                    highlight_fastest=self.highlight.isChecked(), overlay=self.overlay_options())
        self.stop_preview()
        self.preview_proc = QProcess(self)
        hide_console(self.preview_proc)
        self.preview_proc.setProgram(ffmpeg)
        self.preview_proc.setArguments(argv)
        self.preview_proc.finished.connect(lambda code, _s, pr=self.preview_proc: self.preview_done(pr, code))
        if not self.preview_img.pixmap() or self.preview_img.pixmap().isNull():
            self.preview_message("Drawing preview…")
        self.preview_proc.start()

    def preview_done(self, proc, code):
        if proc is not self.preview_proc:                  # an older render that was replaced
            return
        pix = QPixmap(self.preview_file) if code == 0 and self.preview_file else QPixmap()
        if pix.isNull():
            err = bytes(proc.readAllStandardError()).decode(errors="ignore").strip().splitlines()
            self.preview_message("Couldn't draw the preview." + (f"\n{err[-1]}" if err else ""))
        else:
            self.preview_img.setText("")
            self.preview_img.setPixmap(pix)
        self.preview_proc = None

    def sync_audio_options(self, *_):
        """'Remove audio' and a replacement audio file are alternatives: choosing one switches the other off."""
        has_file = bool(self.audio_edit.text().strip())
        if has_file and self.no_audio.isChecked():
            self.no_audio.setChecked(False)
        self.no_audio.setEnabled(not has_file)
        self.audio_edit.setEnabled(not self.no_audio.isChecked())
        self.audio_browse.setEnabled(not self.no_audio.isChecked())
        self.audio_clear.setEnabled(has_file)

    def sync_audio_all(self, *_):
        """The 'covers the intro and outro too' choice only matters with a replacement file and an intro or outro."""
        self.audio_all.setEnabled(bool(self.audio_edit.text().strip()) and bool(self.intro.path() or self.outro.path())
                                  and not self.no_audio.isChecked())

    def browse_audio(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Choose audio", self.audio_edit.text() or str(Path.home()),
            "Audio (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus *.wma);;All files (*)")
        if p:
            self.audio_edit.setText(p)

    def reset_padding(self):
        """Put the start and end padding back to their default values."""
        self.lead_spin.setValue(EXPORT_LEAD_MS / 1000)
        self.tail_spin.setValue(EXPORT_TAIL_MS / 1000)

    def browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save video", self.path_edit.text(), "MP4 video (*.mp4)")
        if p:
            self.path_edit.setText(p if p.lower().endswith(".mp4") else p + ".mp4")

    def start(self):
        ffmpeg, font = find_tool("ffmpeg"), find_overlay_font()
        if not ffmpeg:
            QMessageBox.warning(self, "ffmpeg needed", "Exporting a video needs ffmpeg. Install it and try again.")
            return
        if not ffmpeg_has_encoder("libx264"):
            QMessageBox.warning(self, "H.264 encoder missing", x264_help())
            return
        if not ffmpeg_has_filter("drawtext"):
            QMessageBox.warning(self, "Text filter missing", drawtext_help())
            return
        if not font:
            QMessageBox.warning(self, "Font needed", "No bold font file was found for the timer overlay.")
            return
        if not os.path.exists(self.win["path"]):
            QMessageBox.warning(self, "Video not found", f"The source video can't be found:\n{self.win['path']}")
            return
        self.audio_path = self.audio_edit.text().strip() or None
        if self.audio_path:
            if not os.path.exists(self.audio_path):
                QMessageBox.warning(self, "Audio not found", f"The audio file can't be found:\n{self.audio_path}")
                return
            if find_ffprobe() and not probe_stream(self.audio_path)["audio"]:
                QMessageBox.warning(self, "No audio", "That file has no audio track. Pick another file, or Clear it "
                                    "to keep the video's own audio.")
                return
        for pic in (self.start_pic, self.end_pic):
            if pic.path() and not os.path.exists(pic.path()):
                QMessageBox.warning(self, "Picture not found", f"The picture can't be found:\n{pic.path()}")
                return
        self.intro_spec = self.outro_spec = None
        for row, role in ((self.intro, "intro"), (self.outro, "outro")):
            if not row.path():
                continue
            if not os.path.exists(row.path()):
                QMessageBox.warning(self, "Video not found", f"The {role} video can't be found:\n{row.path()}")
                return
            info = probe_stream(row.path())
            if not info.get("_v") or not info.get("duration_ms"):
                QMessageBox.warning(self, "Can't read video", f"The {role} video can't be read (it needs a picture track "
                                    f"and a known length):\n{row.path()}")
                return
            setattr(self, role + "_spec", {"path": row.path(), "info": info})
        self.final = Path(self.path_edit.text().strip())
        if not self.final.name:
            return
        if self.final.exists() and QMessageBox.question(
                self, "Replace file?", f"{self.final.name} already exists. Replace it?") != QMessageBox.StandardButton.Yes:
            return
        self.part = self.final.with_name(self.final.stem + ".part.mp4")
        self.settings.setValue("export_no_audio", self.no_audio.isChecked())
        if not self.no_audio.isChecked():
            self.settings.setValue("export_audio", self.audio_path or "")
        self.start_pic.save()
        self.end_pic.save()
        self.intro.save()
        self.outro.save()
        self.settings.setValue("export_audio_all", self.audio_all.isChecked())
        self.settings.setValue("export_ov_size", self.ov_size.currentData())
        for key, box in (("swap", self.ov_swap), ("list", self.ov_list), ("track", self.ov_track),
                         ("total", self.ov_total), ("banner", self.ov_banner)):
            self.settings.setValue("export_ov_" + key, box.isChecked())
        self.settings.setValue("export_preview_on", self.preview_on.isChecked())
        self.settings.setValue("export_highlight", self.highlight.isChecked())
        self.settings.setValue("export_decimals", self.dec_combo.currentData())
        self.settings.setValue("export_max_fps", self.fps_combo.currentData() or 0)
        self.settings.setValue("export_lead_s", self.lead_spin.value())
        self.settings.setValue("export_tail_s", self.tail_spin.value())
        self.font, self.info = font, probe_stream(self.win["path"])
        self.run_ffmpeg(ffmpeg)

    def run_ffmpeg(self, ffmpeg):
        argv, self.clip_ms = build_export_args(self.win, self.win["path"], self.part, self.font, self.info,
                                               legacy_sync=self.legacy,
                                               tail_ms=int(self.tail_spin.value() * 1000),
                                               lead_ms=int(self.lead_spin.value() * 1000),
                                               no_audio=self.no_audio.isChecked(),
                                               audio_file=self.audio_path,
                                               max_fps=self.fps_combo.currentData(),
                                               decimals=self.dec_combo.currentData(),
                                               highlight_fastest=self.highlight.isChecked(),
                                               pictures=[self.start_pic.spec("start"), self.end_pic.spec("end")],
                                               intro=self.intro_spec, outro=self.outro_spec,
                                               audio_all=self.audio_all.isChecked(), overlay=self.overlay_options())
        self.buf = ""
        self.proc = QProcess(self)
        hide_console(self.proc)
        self.proc.setProgram(ffmpeg)
        self.proc.setArguments(argv)
        self.proc.readyReadStandardOutput.connect(self.on_progress)
        self.proc.finished.connect(self.on_done)
        self.go.setEnabled(False)
        self.path_edit.setEnabled(False)
        self.lead_spin.setEnabled(False)
        self.tail_spin.setEnabled(False)
        self.reset_pad.setEnabled(False)
        self.no_audio.setEnabled(False)
        self.fps_combo.setEnabled(False)
        self.preview_timer.stop()
        self.stop_preview()
        self.dec_combo.setEnabled(False)
        self.highlight.setEnabled(False)
        for w in (self.ov_size, self.ov_swap, self.ov_list, self.ov_track, self.ov_total, self.ov_banner):
            w.setEnabled(False)
        for w in (self.audio_edit, self.audio_browse, self.audio_clear):
            w.setEnabled(False)
        self.start_pic.set_busy(True)
        self.end_pic.set_busy(True)
        self.intro.set_busy(True)
        self.outro.set_busy(True)
        self.audio_all.setEnabled(False)
        self.bar.setValue(0); self.bar.show()
        self.folder_link.hide()
        self.status.setText("Exporting… high quality takes a while (a few minutes for a typical 25-30 s clip, longer at 90 fps).")
        self.proc.start()

    def on_progress(self):
        self.buf += bytes(self.proc.readAllStandardOutput()).decode(errors="ignore")
        lines = self.buf.split("\n")
        self.buf = lines.pop()
        for ln in lines:
            if ln.startswith("out_time_us="):
                try:
                    self.bar.setValue(max(0, min(99, int(int(ln.split("=")[1]) / 1000 / self.clip_ms * 100))))
                except ValueError:
                    pass

    def on_done(self, code, _status):
        err = bytes(self.proc.readAllStandardError()).decode(errors="ignore").strip()
        if self.cancelled:
            self.part.unlink(missing_ok=True)
            return
        if code == 0 and self.part.exists():
            os.replace(self.part, self.final)
            self.bar.setValue(100)
            self.status.setStyleSheet("")
            self.status.setText(f"Done: {self.final}")
            folder = QUrl.fromLocalFile(str(self.final.parent)).toString()
            self.folder_link.setText(f'<a href="{folder}">Open the folder</a>')
            self.folder_link.show()
            self.cancel.setText("Close")
            self.final_ok = True
            return
        self.part.unlink(missing_ok=True)
        if "fps_mode" in err and not self.legacy:                  # old ffmpeg: retry with -vsync
            self.legacy = True
            self.run_ffmpeg(self.proc.program())
            return
        self.status.setStyleSheet("color:#c62828")
        if is_x264_error(err):
            self.status.setText(x264_help())
        elif is_drawtext_error(err):
            self.status.setText(drawtext_help())
        else:
            self.status.setText(f"Export failed: {err[-300:] or 'ffmpeg exited with code ' + str(code)}")
        self.go.setEnabled(True)
        self.path_edit.setEnabled(True)
        self.lead_spin.setEnabled(True)
        self.tail_spin.setEnabled(True)
        self.reset_pad.setEnabled(True)
        for w in (self.ov_size, self.ov_swap, self.ov_list, self.ov_track, self.ov_total, self.ov_banner,
                  self.fps_combo, self.dec_combo, self.highlight):
            w.setEnabled(True)
        self.sync_audio_options()
        self.start_pic.set_busy(False)
        self.end_pic.set_busy(False)
        self.intro.set_busy(False)
        self.outro.set_busy(False)
        self.sync_audio_all()
        self.bar.hide()

    def kill_job(self):
        self.preview_timer.stop()
        self.stop_preview()
        if self.preview_file:
            try:
                os.unlink(self.preview_file)
            except OSError:
                pass
            self.preview_file = None
        if self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.cancelled = True
            self.proc.kill()
            self.proc.waitForFinished(3000)
            if self.part:
                self.part.unlink(missing_ok=True)

    def on_cancel(self):
        self.kill_job()
        self.reject()

    def done(self, result):                                  # Esc, Cancel, Close or window X: tidy up
        self.kill_job()
        super().done(result)

    def closeEvent(self, e):
        self.kill_job()
        super().closeEvent(e)


class LapChart(QWidget):
    """Bar chart of lap times: bad laps grey, fastest lap green, fastest run outlined."""
    def __init__(self, laps, title="", parent=None, n=None):
        super().__init__(parent)
        self.laps, self.title = laps, title
        self.n = n or run_length()
        self.stats = lap_stats(laps, self.n)
        self.setMinimumSize(560, 320)
        self.setMouseTracking(True)
        self._bars = []

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.window())
        fg = pal.windowText().color()
        faint = QColor(fg); faint.setAlpha(60)
        L, R, T, B = 70, 20, 40, 40
        area = QRectF(L, T, self.width() - L - R, self.height() - T - B)
        p.setPen(fg)
        f = QFont(self.font()); f.setBold(True); p.setFont(f)
        p.drawText(QRectF(L, 8, area.width(), 24), Qt.AlignmentFlag.AlignLeft, self.title)
        p.setFont(self.font())
        self._bars = []
        if not self.laps:
            p.drawText(area, Qt.AlignmentFlag.AlignCenter, "No laps yet")
            return
        good = [ms for ms, bad in self.laps if not bad] or [ms for ms, _ in self.laps]
        lo, hi = min(good), max(ms for ms, _ in self.laps)
        pad = max((hi - lo) * 0.15, 500)
        lo, hi = max(0, lo - pad), hi + pad
        y = lambda ms: area.bottom() - (ms - lo) / (hi - lo) * area.height()
        # grid + y labels
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            p.setPen(QPen(faint, 1)); p.drawLine(int(area.left()), int(y(v)), int(area.right()), int(y(v)))
            p.setPen(fg)
            p.drawText(QRectF(0, y(v) - 9, L - 6, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fmt(v))
        n = len(self.laps)
        slot = area.width() / n
        bw = min(slot * 0.7, 60)
        best = self.stats["best"]; b3 = self.stats["bestn"]
        if b3:
            i = b3[1]
            r = QRectF(area.left() + i * slot + 2, area.top(), slot * self.n - 4, area.height())
            c = QColor("#1976d2"); c.setAlpha(35)
            p.fillRect(r, c); p.setPen(QPen(QColor("#1976d2"), 1, Qt.PenStyle.DashLine)); p.drawRect(r)
        for i, (ms, bad) in enumerate(self.laps):
            x = area.left() + i * slot + (slot - bw) / 2
            top = y(ms)
            r = QRectF(x, top, bw, area.bottom() - top)
            col = QColor("#9e9e9e") if bad else QColor("#2e7d32") if best and best[1] == i else QColor("#42a5f5")
            p.fillRect(r, col)
            self._bars.append((r, f"Lap {i+1}: {fmt(ms)}" + (" (bad)" if bad else "")))
            p.setPen(fg)
            p.drawText(QRectF(x - 10, top - 18, bw + 20, 16), Qt.AlignmentFlag.AlignCenter, fmt(ms))
            p.drawText(QRectF(x - 10, area.bottom() + 4, bw + 20, 16), Qt.AlignmentFlag.AlignCenter, str(i + 1))
        p.setPen(fg)
        p.drawText(QRectF(L, self.height() - 20, area.width(), 18), Qt.AlignmentFlag.AlignCenter,
                   f"Lap   ·   green = fastest   grey = bad   dashed blue = fastest {self.n} consecutive")

    def mouseMoveEvent(self, e):
        for r, tip in self._bars:
            if r.contains(e.position()):
                self.setToolTip(tip); return
        self.setToolTip("")


class ChartDialog(QDialog):
    def __init__(self, laps, title, parent=None, n=None):
        super().__init__(parent)
        self.setWindowTitle("Lap times")
        self.resize(820, 440)
        lay = QVBoxLayout(self)
        lay.addWidget(LapChart(laps, title, n=n))


class ProgressChart(QWidget):
    """Line chart across the library: one point per video, oldest recording first."""
    def __init__(self, points, title="Progress across library (per video)", parent=None, run=None):
        super().__init__(parent)
        self.title = title
        self.run = run                       # N for the legend, or None when tracks use different lengths
        # points: [(label, tooltip_name, best_ms|None, bestn_avg_ms|None)]
        self.points = points
        self.setMinimumSize(640, 340)
        self.setMouseTracking(True)
        self._hits = []

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.window())
        fg = pal.windowText().color()
        faint = QColor(fg); faint.setAlpha(60)
        L, R, T, B = 70, 20, 44, 52
        area = QRectF(L, T, self.width() - L - R, self.height() - T - B)
        p.setPen(fg)
        f = QFont(self.font()); f.setBold(True); p.setFont(f)
        p.drawText(QRectF(L, 8, area.width(), 22), Qt.AlignmentFlag.AlignLeft, self.title)
        p.setFont(self.font())
        self._hits = []
        vals = [v for pt in self.points for v in pt[2:] if v is not None]
        if not vals:
            p.drawText(area, Qt.AlignmentFlag.AlignCenter, "No good laps yet")
            return
        lo, hi = min(vals), max(vals)
        pad = max((hi - lo) * 0.2, 500)
        lo, hi = max(0, lo - pad), hi + pad
        y = lambda ms: area.bottom() - (ms - lo) / (hi - lo) * area.height()
        n = len(self.points)
        x = lambda i: area.left() + (area.width() * (i + 0.5) / n)
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            p.setPen(QPen(faint, 1)); p.drawLine(int(area.left()), int(y(v)), int(area.right()), int(y(v)))
            p.setPen(fg)
            p.drawText(QRectF(0, y(v) - 9, L - 6, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fmt(v))
        series = [(2, QColor("#2e7d32"), "Fastest lap"), (3, QColor("#1976d2"), (f"Best {self.run} consecutive (avg per lap)" if self.run else "Best run (avg per lap)"))]
        overall = {idx: min((pt[idx] for pt in self.points if pt[idx] is not None), default=None) for idx, _, _ in series}
        for idx, col, _ in series:
            pts = [(i, pt[idx]) for i, pt in enumerate(self.points) if pt[idx] is not None]
            p.setPen(QPen(col, 2))
            for (i1, v1), (i2, v2) in zip(pts, pts[1:]):
                p.drawLine(int(x(i1)), int(y(v1)), int(x(i2)), int(y(v2)))
            for i, v in pts:
                r = 7 if v == overall[idx] else 4
                p.setBrush(col); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(x(i) - r, y(v) - r, 2 * r, 2 * r))
                self._hits.append((QRectF(x(i) - 9, y(v) - 9, 18, 18),
                                   f"{self.points[i][1]}\n{dict((a, c) for a, _, c in series)[idx]}: {fmt(v)}"))
        step = max(1, n // 12)
        p.setPen(fg)
        for i, pt in enumerate(self.points):
            if i % step == 0:
                p.drawText(QRectF(x(i) - 40, area.bottom() + 4, 80, 16), Qt.AlignmentFlag.AlignCenter, pt[0])
        lx = area.left()
        for _, col, name in series:
            p.setBrush(col); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(QRectF(lx, self.height() - 22, 10, 10))
            p.setPen(fg); p.drawText(QRectF(lx + 14, self.height() - 26, 260, 18), Qt.AlignmentFlag.AlignVCenter, name)
            lx += 270

    def mouseMoveEvent(self, e):
        for r, tip in self._hits:
            if r.contains(e.position()):
                self.setToolTip(tip); return
        self.setToolTip("")


def track_combo(lib, current=None):
    """Combo of 'All tracks' / each track / '(no track)'. itemData: None / name / ''."""
    cb = QComboBox()
    cb.addItem("All tracks", None)
    tracks = lib.tracks()
    for t in tracks:
        cb.addItem(t, t)
    if tracks and any(not t for t, _ in lib.all_meta().values()):
        cb.addItem("(no track)", "")
    if current is not None:
        i = cb.findData(current)
        if i >= 0:
            cb.setCurrentIndex(i)
    return cb


def track_title(track):
    return "All tracks" if track is None else (track or "(no track)")


class ProgressDialog(QDialog):
    def __init__(self, lib, track=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library progress")
        self.resize(900, 460)
        meta, ids = lib.all_meta(), lib.video_ids(track)
        rmap = lib.run_map()
        rows, used = [], set()
        for vid, path, _ in lib.videos():
            if vid not in ids:
                continue
            n = run_for(rmap, meta[vid][0])
            s = lap_stats(lib.laps(vid), n)
            if s["best"]:
                rows.append((effective_date(path, meta[vid][1])[0], path, s, n))
                used.add(n)
        # oldest first; videos with no known date sort last, by name
        rows.sort(key=lambda r: (r[0] is None, r[0] or datetime.min.date(), r[1].lower()))
        points = [(d.isoformat() if d else "?", vname(path),
                   s["best"][0], s["bestn"][0] / n if s["bestn"] else None) for d, path, s, n in rows]
        lay = QVBoxLayout(self)
        lay.addWidget(ProgressChart(points, f"Progress - {track_title(track)}",
                                    run=next(iter(used)) if len(used) == 1 else None))
        note = QLabel("Videos are ordered by session date (set manually, else file date). Hover a point for the "
                      "video name. Bad laps are excluded; larger dots mark the best in this view.")
        note.setStyleSheet("color:gray")
        note.setWordWrap(True)
        lay.addWidget(note)


# ----------------------------------------------------------------------------
# Add folder: choose which of the videos found to add
# ----------------------------------------------------------------------------
class AddVideosDialog(QDialog):
    """Lists the video files about to be added with a tick box and a track each, so the user can add them all
    or untick the ones they don't want, and put each file (or all the ticked ones at once) on a track.
    Files already in the library are shown but not ticked."""
    NEW_TRACK_TEXT = "＋ New track…"

    def __init__(self, entries, tracks, default_track="", parent=None, title="Add videos", intro=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 560)
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen:
            self.setMaximumSize(screen.width(), screen.height())
        self.tracks = list(tracks)                  # track names offered in every dropdown
        # entries: [(path, shown name, size in bytes or None, already in the library)]
        rows = sorted(entries, key=lambda e: e[1].lower())
        self.table = QTableWidget(len(rows), 4)
        self.table.setHorizontalHeaderLabels(["Add", "File", "Size", "Track"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 44); self.table.setColumnWidth(2, 80); self.table.setColumnWidth(3, 200)
        self.combos = []
        for r, (path, shown, size, known) in enumerate(rows):
            tick = QTableWidgetItem()
            name = QTableWidgetItem(shown + ("    - already in the library" if known else ""))
            name.setToolTip(str(path))
            name.setData(Qt.ItemDataRole.UserRole, str(path))
            size_text = ("" if size is None else f"{size / 1e9:.1f} GB" if size >= 1e9 else
                         f"{size / 1e6:,.0f} MB" if size >= 1e6 else "<1 MB")
            size_item = QTableWidgetItem(size_text)
            combo = QComboBox()
            self.fill_combo(combo, default_track)
            combo.activated.connect(lambda _i, c=combo: self.on_combo(c))
            if known:
                for it in (tick, name, size_item):
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                tick.setFlags(tick.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                combo.setEnabled(False)
            else:
                tick.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                tick.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, tick); self.table.setItem(r, 1, name); self.table.setItem(r, 2, size_item)
            self.table.setCellWidget(r, 3, combo)
            self.combos.append(combo)
        self.table.itemChanged.connect(self.update_count)

        self.count = QLabel()
        all_btn, none_btn = QPushButton("Select all"), QPushButton("Select none")
        all_btn.clicked.connect(lambda: self.set_all(Qt.CheckState.Checked))
        none_btn.clicked.connect(lambda: self.set_all(Qt.CheckState.Unchecked))
        self.bulk = QComboBox()
        self.fill_combo(self.bulk, default_track)
        self.bulk.activated.connect(lambda _i: self.on_combo(self.bulk))
        apply_btn = QPushButton("Set track for ticked files")
        apply_btn.clicked.connect(self.apply_bulk)
        self.add = QPushButton()
        self.add.setDefault(True)
        self.add.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        lay = QVBoxLayout(self)
        intro_label = QLabel(intro or "Untick any file you don't want to add, and choose the track for each.")
        intro_label.setWordWrap(True)                # a long folder path must not widen the window
        intro_label.setMinimumWidth(200)
        lay.addWidget(intro_label)
        lay.addWidget(self.table, 1)
        top = QHBoxLayout()
        top.addWidget(all_btn); top.addWidget(none_btn); top.addSpacing(20)
        top.addWidget(QLabel("Track:")); top.addWidget(self.bulk); top.addWidget(apply_btn)
        top.addStretch(1); top.addWidget(self.count)
        lay.addLayout(top)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("optional, e.g. 5S 2200 mAh, 5 in props, new tune")
        self.note_edit.setToolTip("One note, saved on every video you add here. Change it later from the library's "
                                  "right-click menu (Add note… / Edit note…).")
        noterow = QHBoxLayout()
        noterow.addWidget(QLabel("Note for all added videos:")); noterow.addWidget(self.note_edit, 1)
        lay.addLayout(noterow)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(cancel); row.addWidget(self.add)
        lay.addLayout(row)
        self.update_count()

    # ---- track dropdowns
    def fill_combo(self, combo, current):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(no track)", "")
        for t in self.tracks:
            combo.addItem(t, t)
        combo.addItem(self.NEW_TRACK_TEXT, None)
        i = combo.findData(current) if current else 0
        combo.setCurrentIndex(max(i, 0))
        combo.setProperty("last", combo.currentIndex())
        combo.blockSignals(False)

    def on_combo(self, combo):
        if combo.currentData() is None:             # "＋ New track…"
            text, ok = QInputDialog.getText(self, "New track", "Name of the new track:")
            name = " ".join(text.split()) if ok else ""
            if not name:
                combo.setCurrentIndex(combo.property("last") or 0)
                return
            same = next((t for t in self.tracks if t.lower() == name.lower()), None)
            if same is None:
                self.tracks.append(name)
                self.tracks.sort(key=str.lower)
                same = name
            for c in self.combos + [self.bulk]:      # offer it everywhere, keeping each box's own choice
                keep = c.currentData() if c is not combo else same
                self.fill_combo(c, keep)
            return
        combo.setProperty("last", combo.currentIndex())

    def apply_bulk(self):
        track = self.bulk.currentData() or ""
        for r, combo in enumerate(self.combos):
            if combo.isEnabled() and self.table.item(r, 0).checkState() == Qt.CheckState.Checked:
                self.fill_combo(combo, track)

    # ---- ticking
    def checkable_rows(self):
        return [r for r in range(self.table.rowCount())
                if self.table.item(r, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable]

    def set_all(self, state):
        for r in self.checkable_rows():
            self.table.item(r, 0).setCheckState(state)

    def note(self):
        return self.note_edit.text().strip()

    def selected(self):
        """[(path, track)] for the ticked files; track '' means no track."""
        return [(self.table.item(r, 1).data(Qt.ItemDataRole.UserRole), self.combos[r].currentData() or "")
                for r in self.checkable_rows() if self.table.item(r, 0).checkState() == Qt.CheckState.Checked]

    def update_count(self, *_):
        n, total = len(self.selected()), len(self.checkable_rows())
        known = self.table.rowCount() - total
        self.count.setText(f"{n} of {total} selected" + (f"  ({known} already in the library)" if known else ""))
        self.add.setText(f"Add {n} video{'s' if n != 1 else ''}")
        self.add.setEnabled(n > 0)


# ----------------------------------------------------------------------------
# Fix paths dialog
# ----------------------------------------------------------------------------
class FixPathsDialog(QDialog):
    """Re-point missing videos by swapping the folder they were stored under. One row per
    old folder, so videos that lived in different folders can go to different new folders."""
    def __init__(self, lib, parent=None):
        super().__init__(parent)
        self.lib = lib
        self.plan = []
        self.setWindowTitle("Fix video paths")
        self.resize(760, 360)
        folders = lib.missing_folders()

        self.rows = []                      # (old folder, new-folder QLineEdit)
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setColumnStretch(1, 1)
        grid.addWidget(QLabel("<b>Old folder</b>"), 0, 0)
        grid.addWidget(QLabel("<b>New folder where those videos are now</b>"), 0, 1)
        for r, (old, count) in enumerate(sorted(folders.items()), start=1):
            label = QLabel(f"{old}  ({count} video{'s' if count != 1 else ''})")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setToolTip(old)
            label.setWordWrap(True)
            new = QLineEdit()
            new.setPlaceholderText("leave blank to skip this folder")
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, e=new, o=old: self.browse(e, o))
            new.textChanged.connect(self.preview)
            grid.addWidget(label, r, 0)
            grid.addWidget(new, r, 1)
            grid.addWidget(browse, r, 2)
            self.rows.append((old, new))
        grid.setRowStretch(len(folders) + 1, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_host)

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.ok = QPushButton("Update paths")
        self.ok.clicked.connect(self.apply)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)

        lay = QVBoxLayout(self)
        total = sum(folders.values())
        lay.addWidget(QLabel(f"{total} video(s) in the library can't be found, in {len(folders)} folder(s). "
                             "For each old folder, pick the folder they live in now (subfolders are kept). "
                             "Folders can go to different places; leave a row blank to skip it."))
        lay.addWidget(scroll, 1)
        lay.addWidget(self.info)
        btns = QHBoxLayout(); btns.addStretch(1); btns.addWidget(cancel); btns.addWidget(self.ok)
        lay.addLayout(btns)
        self.preview()

    def browse(self, edit, old):
        start = edit.text() or next((n.text() for _, n in self.rows if n.text()), "") or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, f"New folder for {old}", start)
        if d:
            edit.setText(d)

    def preview(self):
        mapping = {old: new.text().strip() for old, new in self.rows}
        self.plan, skipped = self.lib.plan_folder_swaps(mapping)
        found = sum(os.path.exists(n) for _, _, n in self.plan)
        text = f"{len(self.plan)} video(s) would change; {found} of those exist at the new location."
        if skipped:
            text += f" {skipped} skipped (already in the library)."
        self.info.setText(text)
        self.ok.setEnabled(bool(self.plan))

    def apply(self):
        self.lib.apply_path_plan(self.plan)
        self.accept()


# ----------------------------------------------------------------------------
# Track / session details dialog
# ----------------------------------------------------------------------------
class LapRangeDialog(QDialog):
    """Pick which laps of the open video to export: 'from lap A to lap B'."""
    def __init__(self, lap_ms, bad, first=1, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export chosen laps")
        self.lap_ms, self.bad = lap_ms, bad
        n = len(lap_ms)
        self.a = QSpinBox(); self.b = QSpinBox()
        for box in (self.a, self.b):
            box.setRange(1, n)
            box.setValue(min(max(first, 1), n))
            box.valueChanged.connect(self.update_info)
        self.a.valueChanged.connect(lambda v: self.b.setValue(max(self.b.value(), v)))
        self.b.valueChanged.connect(lambda v: self.a.setValue(min(self.a.value(), v)))
        self.info = QLabel()
        self.info.setWordWrap(True)
        form = QFormLayout(self)
        intro = QLabel(f"This video has {n} lap{'s' if n != 1 else ''}. Choose the first and last lap to put in the "
                       "video; for one lap, pick the same number twice.")
        intro.setWordWrap(True)
        form.addRow(intro)
        form.addRow("From lap:", self.a)
        form.addRow("To lap:", self.b)
        form.addRow(self.info)
        ok, cancel = QPushButton("Next…"), QPushButton("Cancel")
        ok.setDefault(True)
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(cancel); row.addWidget(ok)
        form.addRow(row)
        self.update_info()

    def update_info(self, *_):
        a, b = self.a.value(), self.b.value()
        total = sum(self.lap_ms[a - 1:b])
        flagged = [i for i in range(a, b + 1) if self.bad[i - 1]]
        text = f"{b - a + 1} lap{'s' if b != a else ''}, {fmt(total)} in all."
        if flagged:
            text += f" Lap{'s' if len(flagged) > 1 else ''} {', '.join(map(str, flagged))} " \
                    f"{'are' if len(flagged) > 1 else 'is'} flagged bad (still included)."
        self.info.setText(text)

    def values(self):
        return self.a.value(), self.b.value()


class DetailsDialog(QDialog):
    """Edit the track name and session date of one or several videos."""
    def __init__(self, lib, vids, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Track & session date")
        self.vids = vids
        meta = lib.all_meta()
        paths = {v: p for v, p, _ in lib.videos()}
        tracks = {meta[v][0] for v in vids}
        dates = {meta[v][1] for v in vids}
        multi = len(vids) > 1
        self.mixed_track = len(tracks) > 1
        notes = {lib.notes_map().get(v, "") for v in vids}
        self.mixed_notes = len(notes) > 1
        self.notes = QLineEdit("" if self.mixed_notes else next(iter(notes)))
        self.notes.setPlaceholderText("(various - leave blank to keep)" if self.mixed_notes
                                      else "e.g. 5S 2200 mAh, 5 in props, new tune")
        self.notes.setToolTip("Free text about the flight: battery, props, tune. Searchable in the library "
                              "and saved in the CSV export.")

        self.track = QComboBox()
        self.track.setEditable(True)
        self.track.addItems(lib.tracks())
        self.track.setCurrentText("" if self.mixed_track else next(iter(tracks)))
        self.track.lineEdit().setPlaceholderText("(various - leave blank to keep)" if self.mixed_track
                                                 else "Track name, e.g. Backyard Gate")

        self.mode = QComboBox()
        self.mode.addItems(["Keep as is", "Use file date", "Set date:"])
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        first = next(iter(vids))
        d, manual = effective_date(paths[first], meta[first][1])
        self.date.setDate(QDate(d.year, d.month, d.day) if d else QDate.currentDate())
        if multi and len(dates) > 1:
            self.mode.setCurrentIndex(0)
        else:
            self.mode.setCurrentIndex(2 if manual else 1)
        self.mode.currentIndexChanged.connect(lambda: self.date.setEnabled(self.mode.currentIndex() == 2))
        self.date.setEnabled(self.mode.currentIndex() == 2)

        form = QFormLayout(self)
        heading = QLabel(f"{len(vids)} video(s) selected" if multi else vname(paths[first]))
        heading.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(heading)
        form.addRow("Track:", self.track)
        form.addRow("Session date:", self.mode)
        form.addRow("", self.date)
        form.addRow("Notes:", self.notes)
        ok, cancel = QPushButton("Save"), QPushButton("Cancel")
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(cancel); row.addWidget(ok)
        form.addRow(row)

    def values(self):
        text = self.track.currentText().strip()
        track = KEEP if (self.mixed_track and not text) else text
        m = self.mode.currentIndex()
        date = KEEP if m == 0 else None if m == 1 else self.date.date().toString("yyyy-MM-dd")
        text = self.notes.text().strip()
        notes = KEEP if (self.mixed_notes and not text) else text
        return track, date, notes


# ----------------------------------------------------------------------------
# Stats dialog
# ----------------------------------------------------------------------------
class SortItem(QTableWidgetItem):
    """A table cell that sorts by `key` instead of its text (so 1:02.345 follows 25.737 and 10 follows 9).
    A key of None means 'no value' (shown as --): those rows always sort last, whichever way the column goes."""
    def __init__(self, text, key=None):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        a, b = self.key, getattr(other, "key", None)
        if a is None or b is None:
            if a is None and b is None:
                return False
            desc = self.tableWidget().horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
            return desc if a is None else not desc
        return a < b


class StatsDialog(QDialog):
    def __init__(self, lib, parent=None, track=None):
        super().__init__(parent)
        self.lib = lib
        self.setWindowTitle("Library statistics")
        self.resize(960, 640)
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Track:</b>"))
        self.combo = track_combo(lib, track)
        self.combo.currentIndexChanged.connect(self.rebuild)
        top.addWidget(self.combo)
        top.addStretch(1)
        lay.addLayout(top)

        self.head = QLabel()
        self.head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.head)
        self.dup_label = QLabel()
        self.dup_label.setWordWrap(True)
        self.dup_label.setStyleSheet("color:#ef6c00; font-weight:bold")
        lay.addWidget(self.dup_label)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(["Video", "Track", "Session date", "Laps (good/total)",
                                              "Fastest lap", "Run (laps)", "Fastest run", "Avg good lap", "Notes"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 3)

        lay.addWidget(QLabel("<b>By track</b> (all videos, ignoring the filter above)"))
        self.by_track = QTableWidget(0, 7)
        self.by_track.setHorizontalHeaderLabels(["Track", "Videos", "Laps (good/total)", "Fastest lap",
                                                 "Run (laps)", "Fastest run", "Avg good lap"])
        self.by_track.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.by_track, 2)
        for t in (self.table, self.by_track):
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.verticalHeader().setVisible(False)
            t.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)   # start in the natural order
            t.horizontalHeader().setSortIndicatorShown(True)
            t.setSortingEnabled(True)
            t.setToolTip("Click a column heading to sort by it; click again to reverse.")

        prog = QPushButton("📈 Progress chart (this track)")
        prog.clicked.connect(lambda: ProgressDialog(lib, self.combo.currentData(), self).exec())
        exp = QPushButton("Export CSV (this track)…")
        exp.clicked.connect(self.export_selection)
        self.vid_btn = QPushButton()
        self.vid_btn.clicked.connect(self.export_video)
        row = QHBoxLayout(); row.addWidget(prog); row.addWidget(exp)
        lay.addLayout(row)
        lay.addWidget(self.vid_btn)
        self.rebuild()

    @staticmethod
    def summarise(entries, n):
        """entries: [(name, laps_list)], n: run length -> aggregate stats across those videos
        (no fastest run when n is None, i.e. the videos use different run lengths)."""
        best = bestn = None
        good = total = 0
        goods = []
        for name, laps in entries:
            s = lap_stats(laps, n or 2)
            good += s["good"]; total += s["total"]
            goods += [ms for ms, bad in laps if not bad]
            if s["best"] and (best is None or s["best"][0] < best[0]):
                best = (s["best"][0], name)
            if n and s["bestn"] and (bestn is None or s["bestn"][0] < bestn[0]):
                bestn = (s["bestn"][0], name)
        return {"best": best, "bestn": bestn, "good": good, "total": total,
                "avg": sum(goods) / len(goods) if goods else None}

    def rebuild(self):
        lib, track = self.lib, self.combo.currentData()
        meta, ids = lib.all_meta(), lib.video_ids(track)
        notes = lib.notes_map()
        rmap = lib.run_map()
        rows, groups = [], {}
        for vid, path, _ in lib.videos():
            name, laps = vname(path), lib.laps(vid)
            t, sdate = meta[vid]
            d, manual = effective_date(path, sdate)
            groups.setdefault(t, []).append((name, laps))
            if vid in ids:
                rows.append((d, name, t, manual, laps, notes.get(vid, "")))
        lengths = {run_for(rmap, t) for _, _, t, _, _, _ in rows}
        n_head = next(iter(lengths)) if len(lengths) == 1 else (run_for(rmap, track) if not rows else None)
        rows.sort(key=lambda r: (r[0] is None, r[0] or datetime.min.date(), r[1].lower()))

        extras = sum(len([v for v in g if v in ids]) - 1 for g in lib.duplicate_groups()
                     if len([v for v in g if v in ids]) > 1)
        self.dup_label.setText(
            f"⚠ {extras} video(s) here are copies of another entry in the library (same file), so their laps "
            "are counted twice. Remove the extra entries from the library." if extras else "")
        self.dup_label.setVisible(bool(extras))

        agg = self.summarise([(n, l) for _, n, _, _, l, _ in rows], n_head)
        run_line = (f"Fastest {n_head} consecutive laps: {fmt(agg['bestn'][0]) if agg['bestn'] else '--'}"
                    f"{' &nbsp;(' + html.escape(agg['bestn'][1]) + ')' if agg['bestn'] else ''}" if n_head else
                    "Fastest run: each track has its own run length, see the tables below")
        self.head.setText(
            f"<h3>{html.escape(track_title(track))}</h3>"
            f"<h3>Fastest lap: {fmt(agg['best'][0]) if agg['best'] else '--'}"
            f"{' &nbsp;(' + html.escape(agg['best'][1]) + ')' if agg['best'] else ''}<br>{run_line}</h3>")
        self.vid_btn.setText(f"🎬 Export fastest {n_head} video (this track)…" if n_head
                             else "🎬 Export fastest run video (pick a track)…")

        # fill with sorting off (rows would jump around mid-fill), then put back the column the user had chosen
        views = [(t, t.horizontalHeader().sortIndicatorSection(), t.horizontalHeader().sortIndicatorOrder())
                 for t in (self.table, self.by_track)]
        for t, _, _ in views:
            t.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, (d, name, t, manual, laps, note) in enumerate(rows):
            n = run_for(rmap, t)
            s = lap_stats(laps, n)
            ds = (d.isoformat() + ("" if manual else " (file)")) if d else "--"
            vals = [name, t or "--", ds, f"{s['good']}/{s['total']}",
                    fmt(s["best"][0]) if s["best"] else "--", str(n),
                    fmt(s["bestn"][0]) if s["bestn"] else "--",
                    fmt(s["avg"]) if s["avg"] else "--", note]
            keys = [name.lower(), t.lower() if t else None, (d.toordinal(), name.lower()) if d else None,
                    (s["good"], s["total"]), s["best"][0] if s["best"] else None, n,
                    s["bestn"][0] if s["bestn"] else None, s["avg"] or None, note.lower() or None]
            for c, (v, k) in enumerate(zip(vals, keys)):
                self.table.setItem(r, c, SortItem(v, k))

        self.by_track.setRowCount(len(groups))
        for r, t in enumerate(sorted(groups, key=lambda t: (t == "", t.lower()))):
            n = run_for(rmap, t)
            a = self.summarise(groups[t], n)
            vals = [t or "(no track)", str(len(groups[t])), f"{a['good']}/{a['total']}",
                    fmt(a["best"][0]) if a["best"] else "--", str(n),
                    fmt(a["bestn"][0]) if a["bestn"] else "--",
                    fmt(a["avg"]) if a["avg"] else "--"]
            keys = [t.lower() if t else None, len(groups[t]), (a["good"], a["total"]),
                    a["best"][0] if a["best"] else None, n, a["bestn"][0] if a["bestn"] else None, a["avg"] or None]
            for c, (v, k) in enumerate(zip(vals, keys)):
                self.by_track.setItem(r, c, SortItem(v, k))
        for t, sec, order in views:
            t.setSortingEnabled(True)
            if sec >= 0:
                t.sortByColumn(sec, order)

    def export_video(self):
        ids = self.lib.video_ids(self.combo.currentData())
        lengths = self.lib.run_lengths(ids)
        if len(lengths) > 1:
            QMessageBox.information(self, "Pick a track",
                                    "These tracks use different run lengths, so there is no single fastest run. "
                                    "Pick one track in the list above first.")
            return
        n = next(iter(lengths)) if lengths else run_length()
        win = best_run_window(self.lib, ids, n)
        if not win:
            QMessageBox.information(self, "Nothing to export", f"No video here has {n} good laps in a row yet.")
            return
        ExportDialog(win, self).exec()

    def export_selection(self):
        track = self.combo.currentData()
        default = "fpv_laps.csv" if track is None else f"fpv_laps_{track or 'no_track'}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", str(Path.home() / default), "CSV (*.csv)")
        if path:
            export_csv(self.lib, self.lib.video_ids(track), path)


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class VideoView(QVideoWidget):
    """Video widget that reports mouse-wheel turns (used to step through frames)."""
    wheel = pyqtSignal(int, bool)     # notches (+ = forward), shift held

    def wheelEvent(self, e):
        notches = e.angleDelta().y() // 120
        if notches:
            self.wheel.emit(int(notches), bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        e.accept()


RUN_COLOR = "#42a5f5"                         # fastest run of N laps (blue, like the lap chart)
LAP_COLOR = "#FFC107"                         # fastest single lap (gold)


class MarkSlider(QSlider):
    """Seek bar that jumps to where you click and draws a tick for every gate mark."""
    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.marks = []
        self.lap_span = None                  # (start_ms, end_ms) of the fastest lap
        self.run_span = None                  # (start_ms, end_ms) of the fastest run of N laps

    def set_marks(self, times, lap_span=None, run_span=None):
        self.marks = list(times)
        self.lap_span, self.run_span = lap_span, run_span
        self.update()

    def _groove(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                           QStyle.SubControl.SC_SliderGroove, self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            g = self._groove()
            v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                               int(e.position().x()) - g.x(), max(1, g.width()))
            self.setValue(v)
            self.sliderMoved.emit(v)
        super().mousePressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.marks or self.maximum() <= 0:
            return
        p = QPainter(self)
        g = self._groove()
        for span, col in ((self.run_span, RUN_COLOR), (self.lap_span, LAP_COLOR)):
            if span:
                x0, x1 = (int(g.x() + g.width() * min(t, self.maximum()) / self.maximum()) for t in span)
                c = QColor(col); c.setAlpha(150)
                p.fillRect(x0, g.top() - 4, max(x1 - x0, 1), g.height() + 8, c)
        p.setPen(QPen(QColor("#ff9800"), 2))
        for t in self.marks:
            x = int(g.x() + g.width() * min(t, self.maximum()) / self.maximum())
            p.drawLine(x, g.top() - 4, x, g.bottom() + 4)


def jog_speed(v):
    """Shuttle slider position (-100..100) -> signed speed: dead zone, then 0.1x .. 16x (exponential)."""
    a = abs(v)
    if a < 4:
        return 0.0
    r = 0.1 * 160 ** ((a - 4) / 96)
    return r if v > 0 else -r


SPEEDS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


def dark_palette():
    """Fallback dark palette for Qt versions without a built-in colour-scheme switch."""
    from PyQt6.QtGui import QPalette
    pal = QPalette()
    bg, base, text = QColor(45, 45, 48), QColor(30, 30, 32), QColor(230, 230, 230)
    for role, col in ((QPalette.ColorRole.Window, bg), (QPalette.ColorRole.WindowText, text),
                      (QPalette.ColorRole.Base, base), (QPalette.ColorRole.AlternateBase, bg),
                      (QPalette.ColorRole.Text, text), (QPalette.ColorRole.Button, QColor(60, 60, 64)),
                      (QPalette.ColorRole.ButtonText, text), (QPalette.ColorRole.ToolTipBase, base),
                      (QPalette.ColorRole.ToolTipText, text), (QPalette.ColorRole.Highlight, QColor(25, 118, 210)),
                      (QPalette.ColorRole.HighlightedText, QColor(255, 255, 255)),
                      (QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140)),
                      (QPalette.ColorRole.Mid, QColor(110, 110, 115)), (QPalette.ColorRole.Dark, QColor(150, 150, 155)),
                      (QPalette.ColorRole.Light, QColor(75, 75, 80)), (QPalette.ColorRole.Midlight, QColor(65, 65, 70))):
        pal.setColor(role, col)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(120, 120, 120))
    return pal


_original_look = {}
DARK_EXTRA = (
    "QTableView::indicator{width:14px;height:14px;border:1px solid #bdbdbd;border-radius:2px;background:#1e1e20}"
    "QTableView::indicator:checked{background:#42a5f5;border-color:#42a5f5}")


def apply_theme(app, theme):
    """theme: 'system' | 'light' | 'dark'.

    'system' restores whatever look the desktop gave the app. 'light'/'dark' use the Fusion style with
    an explicit palette, which behaves the same on every platform and Qt version."""
    from PyQt6.QtGui import QPalette
    if not _original_look:
        _original_look["style"] = app.style().objectName()
        _original_look["palette"] = QPalette(app.palette())
    hints = app.styleHints()
    if theme == "system":
        if app.style().objectName() != _original_look["style"]:
            app.setStyle(_original_look["style"])
        app.setPalette(_original_look["palette"])
        app.setStyleSheet("")
        if hasattr(hints, "setColorScheme"):
            hints.setColorScheme(Qt.ColorScheme.Unknown)
        return
    app.setStyle("Fusion")
    app.setPalette(dark_palette() if theme == "dark" else app.style().standardPalette())
    # Fusion draws very faint check boxes in table cells on a dark background
    app.setStyleSheet(DARK_EXTRA if theme == "dark" else "")
    if hasattr(hints, "setColorScheme"):           # also darkens window title bars where supported
        hints.setColorScheme(Qt.ColorScheme.Dark if theme == "dark" else Qt.ColorScheme.Light)


# keep source timestamps untouched when building smooth copies: -fps_mode needs ffmpeg 5.1+,
# older versions only know -vsync (we fall back automatically, see _build_done)
SYNC_FLAGS = ["-fps_mode", "passthrough"]
SYNC_FLAGS_OLD = ["-vsync", "0"]

# Every key the user can rebind: (id, what it does, default key). The ids are the settings keys
# ("keys/<id>"); only a changed key is stored, so new defaults reach everyone who never changed it.
KEY_ACTIONS = [
    ("play", "Play / pause", "Space"),
    ("mark", "Mark start/finish gate pass", "Z"),
    ("step_back", "Step one frame back (hold to repeat)", "Left"),
    ("step_fwd", "Step one frame forward (hold to repeat)", "Right"),
    ("hold_back", "Hold to play backward at 2x", "Shift+Left"),
    ("hold_fwd", "Hold to play forward at 2x", "Shift+Right"),
    ("jump_back", "Jump back 5 seconds", "Ctrl+Left"),
    ("jump_fwd", "Jump forward 5 seconds", "Ctrl+Right"),
    ("shuttle_back", "Shuttle backward (repeat to speed up)", "J"),
    ("shuttle_stop", "Stop shuttle", "K"),
    ("shuttle_fwd", "Shuttle forward (repeat to speed up)", "L"),
    ("prev_gate", "Previous gate mark", "PgUp"),
    ("next_gate", "Next gate mark", "PgDown"),
    ("slower", "Slower playback", "["),
    ("faster", "Faster playback", "]"),
    ("bad_lap", "Mark / unmark the lap at the playhead as bad", "X"),
    ("undo", "Undo the last mark change (add, move, delete, bad-lap flag)", "Ctrl+Z"),
    ("redo", "Redo", "Ctrl+Y"),
    ("delete_mark", "Delete the selected mark (when the table has focus)", "Delete"),
]
KEY_DEFAULTS = {kid: key for kid, _, key in KEY_ACTIONS}
KEY_LABELS = {kid: label for kid, label, _ in KEY_ACTIONS}
RESERVED_KEYS = {"Return": "Open the selected video (library list)"}    # fixed, can't be rebound
PORTABLE = QKeySequence.SequenceFormat.PortableText


def key_portable(key):
    """Normalise a key string ('ctrl+g', 'Ctrl+G') to Qt's portable form ('' if empty)."""
    return QKeySequence(key).toString(PORTABLE) if key else ""


class KeysDialog(QDialog):
    """Change any of the app's keys. Blocks duplicates and keys that are fixed (Return)."""
    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(560, 560)
        self.edits = {}
        table = QTableWidget(len(KEY_ACTIONS), 4)
        table.setHorizontalHeaderLabels(["Action", "Key", "", ""])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, (kid, label, default) in enumerate(KEY_ACTIONS):
            table.setItem(r, 0, QTableWidgetItem(label))
            edit = QKeySequenceEdit(QKeySequence(current.get(kid, default)))
            edit.setMaximumSequenceLength(1)         # one key (with optional Ctrl/Shift/Alt), not a chord
            edit.keySequenceChanged.connect(self.check)
            self.edits[kid] = edit
            table.setCellWidget(r, 1, edit)
            reset = QPushButton("Default")
            reset.setToolTip(f"Back to {default}")
            reset.clicked.connect(lambda _=False, e=edit, d=default: e.setKeySequence(QKeySequence(d)))
            clear = QPushButton("Clear")
            clear.setToolTip("No key for this action")
            clear.clicked.connect(edit.clear)
            table.setCellWidget(r, 2, reset)
            table.setCellWidget(r, 3, clear)
        table.setColumnWidth(1, 150)
        table.resizeColumnToContents(2)
        table.resizeColumnToContents(3)
        self.table = table

        self.msg = QLabel()
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color:#ef6c00")
        reset_all = QPushButton("Reset all to defaults")
        reset_all.clicked.connect(self.reset_all)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("Save")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.accept)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Click a key box, then press the key you want (optionally with Ctrl / Shift / Alt, "
                             "e.g. <b>Ctrl+G</b>). Use <i>Clear</i> to leave an action without a key."))
        lay.addWidget(table, 1)
        lay.addWidget(self.msg)
        row = QHBoxLayout(); row.addWidget(reset_all); row.addStretch(1); row.addWidget(cancel); row.addWidget(self.ok)
        lay.addLayout(row)
        self.check()

    def keys(self):
        """{action id: portable key string ('' = none)}"""
        return {kid: e.keySequence().toString(PORTABLE) for kid, e in self.edits.items()}

    def reset_all(self):
        for kid, e in self.edits.items():
            e.setKeySequence(QKeySequence(KEY_DEFAULTS[kid]))

    def conflicts(self):
        """[(key, [what it is used for...])] for every key that is used twice or is reserved."""
        used = {}
        for kid, key in self.keys().items():
            if key:
                used.setdefault(key, []).append(KEY_LABELS[kid])
        out = []
        for key, whats in used.items():
            if key in RESERVED_KEYS:
                whats = whats + [RESERVED_KEYS[key] + " - fixed"]
            if len(whats) > 1:
                out.append((key, whats))
        return out

    def check(self, *_):
        bad = self.conflicts()
        clash = {key for key, _ in bad}
        for kid, key in self.keys().items():
            self.edits[kid].setStyleSheet("background:#ffe0b2; color:black" if key in clash else "")
        if bad:
            key, whats = bad[0]
            self.msg.setText(f"'{key}' is used for more than one thing: " + "; ".join(whats) + ". Pick another key.")
        else:
            self.msg.setText("")
        self.ok.setEnabled(not bad)


NEW_TRACK = "\x00new-track"      # itemData of the dropdown's "New track…" entry (never a real track name)
RENAME_TRACK = "\x00rename-track"
DELETE_TRACK = "\x00delete-track"
NO_PENDING = object()

ROLE_INFO = Qt.ItemDataRole.UserRole + 1     # dict describing a library entry, painted by LibraryDelegate


class LibraryDelegate(QStyledItemDelegate):
    """Library entry: big track name, a LAPS / FASTEST / FASTEST N row, then a small file name line."""
    HEIGHT = 78

    def sizeHint(self, option, index):
        return QSize(120, self.HEIGHT)          # width: the list stretches entries to its own width

    def paint(self, p, option, index):
        info = index.data(ROLE_INFO)
        if not info:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r, pal = option.rect, option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            p.fillRect(r, pal.highlight())
        fg = pal.highlightedText().color() if selected else pal.text().color()
        muted = QColor(fg); muted.setAlpha(150)
        faint = QColor(fg); faint.setAlpha(45)
        p.setPen(faint)
        p.drawLine(r.left() + 6, r.bottom(), r.right() - 6, r.bottom())

        base = option.font
        def font(scale, bold=False, italic=False):
            f = QFont(base)
            f.setPointSizeF(max(6.0, base.pointSizeF() * scale))
            f.setBold(bold); f.setItalic(italic)
            return f

        x0, w = r.left() + 10, r.width() - 20
        y = r.top() + 5

        # --- row 1: track name (left), status badges (right)
        badges = ("🔒 " if info.get("locked") else "") + ("⚡ " if info["ready"] else "") + ("⚠ duplicate" if info["dup"] else "") + \
                 ("⚠ not found" if not info["exists"] else "")
        p.setFont(font(0.9))
        bw = QFontMetrics(p.font()).horizontalAdvance(badges) + 4 if badges else 0
        if badges:
            p.setPen(QColor("#c62828") if not info["exists"] else QColor("#ff9800") if info["dup"] else muted)
            p.drawText(QRectF(x0 + w - bw, y, bw, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, badges)
        track = info["track"]
        p.setFont(font(1.2, bold=bool(track), italic=not track))
        p.setPen(fg if track else muted)
        shown = QFontMetrics(p.font()).elidedText(track or "No track", Qt.TextElideMode.ElideRight, w - bw - 6)
        p.drawText(QRectF(x0, y, w - bw - 6, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)

        # --- rows 2-3: three stat columns (small caption above bold value)
        trophy = lambda v, on: v + (" 🏆" if on and v != "-" else "")
        cols = (("LAPS", f"{info['good']}/{info['total']}" if info["total"] else "-"),
                ("FASTEST", trophy(fmt(info["best"]) if info["best"] is not None else "-", info.get("pb_lap"))),
                (f"FASTEST {info.get('run') or run_length()}",
                 trophy(fmt(info["bestn"]) if info["bestn"] is not None else "-", info.get("pb_run"))))
        # column widths follow their content (caption or value, whichever is wider), sharing any spare width
        cap_fm, val_fm = QFontMetrics(font(0.72)), QFontMetrics(font(1.05, bold=True))
        need = [max(cap_fm.horizontalAdvance(c), val_fm.horizontalAdvance(v)) + 8 for c, v in cols]
        spare = max(0.0, w - sum(need))
        widths = [n + spare * n / sum(need) for n in need] if spare else [w * n / sum(need) for n in need]
        cx = x0
        for (cap, val), colw in zip(cols, widths):
            p.setFont(font(0.72)); p.setPen(muted)
            p.drawText(QRectF(cx, y + 24, colw, 13), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, cap)
            p.setFont(font(1.05, bold=True)); p.setPen(fg if val != "-" else muted)
            p.drawText(QRectF(cx, y + 36, colw, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val)
            cx += colw

        # --- row 4: small file name (left) and date (right)
        p.setFont(font(0.78))
        date = info["date"] or ""
        dw = QFontMetrics(p.font()).horizontalAdvance(date) + 6 if date else 0
        p.setPen(QColor("#c62828") if not info["exists"] else muted)
        name = QFontMetrics(p.font()).elidedText(info["name"], Qt.TextElideMode.ElideMiddle, w - dw - 8)
        p.drawText(QRectF(x0, y + 56, w - dw - 8, 15), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        if date:
            p.setPen(muted)
            p.drawText(QRectF(x0 + w - dw, y + 56, dw, 15), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, date)
        p.restore()


class ComparePane(QWidget):
    """One side of the lap comparison: a video pane, a video / lap picker and a note when the smooth copy is missing."""
    BIAS = 0.25                                            # seek a quarter frame late, as the main window does

    def __init__(self, dialog, letter):
        super().__init__()
        self.dialog, self.letter = dialog, letter
        self.video_info = None                             # dict of the chosen video, see CompareDialog.load_track
        self.start = self.end = 0                          # the chosen lap, in the video's own time (ms)
        self.ready, self.finished, self.src = False, False, None
        self.player = QMediaPlayer(self)
        audio = QAudioOutput(self)
        audio.setMuted(True)                               # silent: two soundtracks at once would just be noise
        self.player.setAudioOutput(audio)
        self.video = QVideoWidget()
        self.video.setMinimumSize(320, 180)
        self.player.setVideoOutput(self.video)
        self.player.mediaStatusChanged.connect(self.on_status)
        self.video_combo, self.lap_combo = QComboBox(), QComboBox()
        self.video_combo.setToolTip(f"Video for lap {letter}: the videos on this track that have laps marked")
        self.lap_combo.setToolTip(f"Lap {letter}")
        self.video_combo.currentIndexChanged.connect(self.fill_laps)
        self.lap_combo.currentIndexChanged.connect(self.on_lap)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color:#ef6c00; font-weight:bold")
        self.build_btn = QPushButton("Build smooth copy")
        self.build_btn.clicked.connect(lambda: dialog.build_for([self]))
        self.notice.hide(); self.build_btn.hide()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        label = QLabel(f"<b>{letter}</b>")
        head.addWidget(label); head.addWidget(self.video_combo, 1); head.addWidget(self.lap_combo)
        lay.addLayout(head)
        lay.addWidget(self.video, 1)
        lay.addWidget(self.notice)
        lay.addWidget(self.build_btn)

    @property
    def dur(self):
        return max(0, self.end - self.start)

    def set_videos(self, videos, select_vid=None, select_lap=None):
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        for v in videos:
            self.video_combo.addItem(f"{v['name']}  ·  {v['date'] or 'no date'}", v)
        i = next((k for k, v in enumerate(videos) if v["vid"] == select_vid), 0)
        self.video_combo.setCurrentIndex(i)
        self.video_combo.blockSignals(False)
        self.fill_laps(select_lap=select_lap, quiet=True)
        self.on_lap()

    def fill_laps(self, *_, select_lap=None, quiet=False):
        v = self.video_combo.currentData()
        self.lap_combo.blockSignals(True)
        self.lap_combo.clear()
        if v:
            fastest = min((l for l in v["laps"] if not l[3]), key=lambda l: l[2] - l[1], default=None)
            for lap in v["laps"]:
                no, a, b, bad = lap
                self.lap_combo.addItem(f"Lap {no}  ·  {fmt(b - a)}" + ("  (bad)" if bad else "")
                                       + ("  ★" if lap is fastest else ""), lap)
            i = next((k for k, l in enumerate(v["laps"]) if l[0] == select_lap), None)
            if i is None and fastest is not None:
                i = v["laps"].index(fastest)
            self.lap_combo.setCurrentIndex(max(i or 0, 0))
        self.lap_combo.blockSignals(False)
        if not quiet:
            self.on_lap()

    def on_lap(self, *_):
        v, lap = self.video_combo.currentData(), self.lap_combo.currentData()
        self.video_info = v
        if not v or not lap:
            return
        self.start, self.end = lap[1], lap[2]
        self.finished = False
        self.prepare()
        self.dialog.selection_changed(self)

    def prepare(self):
        """Load the video's smooth copy, or ask for one if it has none."""
        v = self.video_info
        proxy = proxy_path(v["path"])
        if not proxy.exists():
            self.ready, self.src = False, None
            self.player.setSource(QUrl())
            self.show_missing()
            return
        self.notice.hide(); self.build_btn.hide()
        if self.src != str(proxy):
            self.ready, self.src = False, str(proxy)
            self.player.setSource(QUrl.fromLocalFile(self.src))
            self.player.pause()                            # (same copy already loaded: keep its state; ready stays as is)

    def show_missing(self):
        b = self.dialog.main.build
        if not find_tool("ffmpeg"):
            self.notice.setText("This video has no smooth copy, and ffmpeg is needed to build one (Help → Install ffmpeg…).")
            self.build_btn.hide()
        elif b and b["path"] == self.video_info["path"]:
            pct = f" {b['pct']}%" if b["pct"] is not None else ""
            self.notice.setText(f"Building the smooth copy…{pct} The comparison starts when it is ready.")
            self.build_btn.hide()
        elif any(p == self.video_info["path"] for _, p in self.dialog.main.build_queue):
            self.notice.setText("The smooth copy is queued; the comparison starts when it is ready.")
            self.build_btn.hide()
        else:
            self.notice.setText("This video has no smooth copy yet. Comparing needs one, so both laps can start on "
                                "exactly their start-gate frame and stay in step.")
            self.build_btn.show()
        self.notice.show()

    def on_status(self, status):
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia)
        if status in loaded and self.src and not self.ready:      # (seeking makes the status flip again: ignore that)
            self.ready = True
            self.dialog.pane_ready(self)                   # seeks to the lap's first frame...
            self.player.pause()                            # ...then holds it, as the main window does

    def seek(self, t):
        """Show the frame `t` ms into the chosen lap (clamped to the lap)."""
        rel = min(max(t, 0), self.dur)
        self.finished = t >= self.dur
        self.player.setPosition(int(self.start + rel + self.BIAS * self.dialog.frame_ms(self)))

    def rel(self):
        return self.player.position() - self.start

    def shutdown(self):
        self.player.stop()
        self.player.setSource(QUrl())


class CompareDialog(QDialog):
    """Play two laps side by side, both starting on their start-gate frame, to see where one gains on the other.
    The laps can come from different videos on the same track. Needs smooth copies (they seek to exact frames)."""
    def __init__(self, main, track=None, parent=None):
        super().__init__(parent or main)
        self.main = main
        self.setWindowTitle("Compare laps")
        self.resize(1100, 640)
        self.setModal(True)
        self.playing = False
        self.elapsed = 0.0
        self.asked = set()                                 # videos we already offered to build a copy for
        self.offer_pending = False
        self.lib = main.lib
        self.tracks = self.find_tracks()
        self.track_combo = QComboBox()
        for t in self.tracks:
            self.track_combo.addItem(t or "(no track)", t)
        i = self.track_combo.findData(track if track in self.tracks else (self.tracks[0] if self.tracks else ""))
        self.track_combo.setCurrentIndex(max(i, 0))
        self.a, self.b = ComparePane(self, "A"), ComparePane(self, "B")
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self.toggle_play)
        back, fwd = QPushButton("◀ Frame"), QPushButton("Frame ▶")
        back.clicked.connect(lambda: self.step(-1)); fwd.clicked.connect(lambda: self.step(1))
        self.speed = QComboBox()
        for sp in (0.1, 0.25, 0.5, 1.0, 2.0):
            self.speed.addItem(f"{sp:g}x", sp)
        self.speed.setCurrentIndex(self.speed.findData(1.0))
        self.speed.currentIndexChanged.connect(self.apply_speed)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1)
        self.slider.sliderMoved.connect(self.scrub)
        self.time_label = QLabel("0:00.000 / 0:00.000")
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.empty = QLabel("Mark at least one lap (two gate marks) in some video first, then compare it with another.")
        self.empty.setWordWrap(True)

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Track:</b>")); top.addWidget(self.track_combo); top.addStretch(1)
        lay.addLayout(top)
        lay.addWidget(self.empty)
        panes = QHBoxLayout()
        panes.addWidget(self.a, 1); panes.addWidget(self.b, 1)
        lay.addLayout(panes, 1)
        lay.addWidget(self.summary)
        self.controls = [self.play_btn, back, fwd, self.slider]          # usable once both laps are loaded
        ctl = QHBoxLayout()
        for w in (self.play_btn, back, fwd, QLabel("Speed:"), self.speed):
            ctl.addWidget(w)
        ctl.addWidget(self.slider, 1); ctl.addWidget(self.time_label)
        lay.addLayout(ctl)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        hint = QLabel(f"{main.key_text('step_back')} / {main.key_text('step_fwd')} step a frame · hold "
                      f"{main.key_text('hold_back')} / {main.key_text('hold_fwd')} to play at 2x · "
                      f"{main.key_text('play')} play / pause")
        hint.setStyleSheet("color:gray")
        row = QHBoxLayout(); row.addWidget(hint); row.addStretch(1); row.addWidget(close)
        lay.addLayout(row)

        for kid, fn in (("play", self.toggle_play), ("step_back", lambda: self.step(-1)), ("step_fwd", lambda: self.step(1))):
            key = main.key_for(kid)
            if key:
                sc = QShortcut(QKeySequence(key), self)
                sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(fn)
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        # hold-to-play keys, as in the main window (default Shift+Left / Shift+Right): 2x while held, stop on release
        self.hold_bindings = {k: sp for kid, sp in (("hold_back", -2.0), ("hold_fwd", 2.0))
                              if (k := main.key_for(kid))}
        self.hold_key, self.hold_mods = None, set()
        self.rev_timer = QTimer(self)                      # plays backwards by seeking, since players can't
        self.rev_timer.setInterval(30)
        self.rev_timer.timeout.connect(self.rev_tick)
        self.rev_speed, self.rev_t = 2.0, 0.0
        QApplication.instance().installEventFilter(self)
        self.copy_timer = QTimer(self)                     # notices when a copy being built is ready
        self.copy_timer.setInterval(700)
        self.copy_timer.timeout.connect(self.check_copies)
        self.copy_timer.start()
        self.track_combo.currentIndexChanged.connect(lambda _: self.load_track())
        self.load_track(first=True)

    # ---- data
    def find_tracks(self):
        out = set()
        meta = self.lib.all_meta()
        for vid, _, _ in self.lib.videos():
            if len(self.lib.marks(vid)) >= 2:
                out.add(meta[vid][0])
        return sorted(out, key=lambda t: (t == "", t.lower()))

    def load_track(self, first=False):
        self.pause()
        track = self.track_combo.currentData()
        videos, meta = [], self.lib.all_meta()
        for vid, path, fps in self.lib.videos():
            if vid not in meta or meta[vid][0] != (track or ""):
                continue
            m = self.lib.marks(vid)
            laps = [(i, m[i - 1][1], m[i][1], bool(m[i][2])) for i in range(1, len(m))]
            if not laps:
                continue
            d = effective_date(path, meta[vid][1])[0]
            videos.append({"vid": vid, "path": path, "name": vname(path), "fps": fps or 30.0,
                           "date": d.isoformat() if d else "", "laps": laps})
        videos.sort(key=lambda v: (v["date"] == "", v["date"], v["name"].lower()))
        self.videos = videos
        has = bool(videos)
        self.empty.setVisible(not has)
        for w in (self.a, self.b):
            w.setVisible(has)
        if not has:
            return
        # A starts on the fastest good lap of the track; B on the open video's lap at the playhead (else its next best)
        good = [(l[2] - l[1], v["vid"], l[0]) for v in videos for l in v["laps"] if not l[3]]
        best = min(good) if good else (0, videos[0]["vid"], videos[0]["laps"][0][0])
        b_vid, b_lap = None, None
        mw = self.main
        if first and mw.vid is not None and any(v["vid"] == mw.vid for v in videos):
            row = mw.lap_at(mw.cursor_ms)
            if row:
                b_vid, b_lap = mw.vid, row
        if b_vid is None or (b_vid, b_lap) == (best[1], best[2]):
            rest = sorted(g for g in good if (g[1], g[2]) != (best[1], best[2]))
            b_vid, b_lap = (rest[0][1], rest[0][2]) if rest else (best[1], best[2])
        self.a.set_videos(videos, best[1], best[2])
        self.b.set_videos(videos, b_vid, b_lap)

    # ---- helpers
    def panes(self):
        return [p for p in (self.a, self.b) if p.video_info]

    def active(self):
        return [p for p in self.panes() if p.ready]

    def maxdur(self):
        return max((p.dur for p in self.panes()), default=0)

    def frame_ms(self, pane):
        return 1000.0 / ((pane.video_info or {}).get("fps") or 30.0)

    def selection_changed(self, pane):
        self.pause()
        self.elapsed = 0.0
        self.slider.setRange(0, int(self.maxdur()))
        self.slider.setValue(0)
        self.update_labels()
        if not self.offer_pending:                         # once, after both panes have settled on their choice
            self.offer_pending = True
            QTimer.singleShot(0, self.offer_build)
        if all(p.ready for p in self.panes()):
            self.seek_all(0)
            self.settle_seek()

    def pane_ready(self, pane):
        self.seek_all(self.elapsed)
        self.settle_seek()

    def settle_seek(self):
        """A seek made the instant media loads (or a lap changes) can be dropped by the player: repeat it once settled."""
        QTimer.singleShot(250, lambda: None if self.playing else self.seek_all(self.elapsed))

    def offer_build(self):
        """Ask whether to build the smooth copies the chosen laps are missing (once per video)."""
        self.offer_pending = False
        panes = [p for p in self.panes() if not proxy_path(p.video_info["path"]).exists()
                 and p.video_info["vid"] not in self.asked]
        if not panes or not find_tool("ffmpeg"):
            return
        names = sorted({p.video_info["name"] for p in panes})
        for p in panes:
            self.asked.add(p.video_info["vid"])
        shown = "\n".join("• " + n for n in names[:6]) + (f"\n…and {len(names) - 6} more" if len(names) > 6 else "")
        box = QMessageBox(self)
        box.setWindowTitle("Smooth copy needed")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Comparing laps needs a smooth-scrub copy of each video, so both laps start on exactly their gate "
                    f"frame and stay in step:\n\n{shown}\n\nBuild {'it' if len(names) == 1 else 'them'} now? "
                    "It runs in the background and the comparison starts by itself when it is ready.")
        build = box.addButton("Build now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is build:
            self.build_for(panes)

    def build_for(self, panes):
        for p in panes:
            v = p.video_info
            self.main.request_proxy(v["vid"], v["path"], front=True, manual=True)
            p.show_missing()
        self.main.update_proxy_ui()

    def check_copies(self):
        for p in self.panes():
            if not p.ready:
                if proxy_path(p.video_info["path"]).exists() and p.src is None:
                    p.prepare()
                elif not proxy_path(p.video_info["path"]).exists():
                    p.show_missing()

    # ---- transport
    def toggle_play(self):
        self.pause() if self.playing else self.play()

    def play(self, rate=None):
        act = self.active()
        if not act or len(act) < len(self.panes()):
            return
        if self.elapsed >= self.maxdur() - 1:
            self.seek_all(0)
        for p in act:
            p.seek(self.elapsed)                           # a seek made while the media was still loading can be lost
            p.player.setPlaybackRate(rate or self.speed.currentData())
            if not p.finished:
                p.player.play()
        self.playing = True
        self.play_btn.setText("⏸ Pause")
        self.timer.start()

    def pause(self):
        self.rev_timer.stop()
        for p in self.panes():
            p.player.pause()
        self.playing = False
        self.play_btn.setText("▶ Play")
        self.timer.stop()

    def apply_speed(self):
        for p in self.panes():
            p.player.setPlaybackRate(self.speed.currentData())

    def seek_all(self, t):
        self.elapsed = min(max(float(t), 0.0), float(self.maxdur()))
        for p in self.active():
            p.seek(self.elapsed)
        self.slider.blockSignals(True)
        self.slider.setValue(int(self.elapsed))
        self.slider.blockSignals(False)
        self.update_labels()

    def scrub(self, v):
        self.pause()
        self.seek_all(v)

    def step(self, n):
        act = self.active()
        if not act:
            return
        self.pause()
        self.seek_all(self.elapsed + n * self.frame_ms(act[0]))

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.KeyPress:
            k = ev.key()
            if self.hold_key is not None and k == self.hold_key:
                return True                                  # swallow auto-repeat while holding
            if self.hold_bindings and QApplication.activeWindow() is self:
                mods = ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
                combo = QKeySequence(QKeyCombination(mods, Qt.Key(k))).toString(PORTABLE)
                speed = self.hold_bindings.get(combo)
                if speed is not None:
                    self.hold_key = k
                    self.hold_mods = {key for flag, key in (
                        (Qt.KeyboardModifier.ShiftModifier, Qt.Key.Key_Shift),
                        (Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_Control),
                        (Qt.KeyboardModifier.AltModifier, Qt.Key.Key_Alt),
                        (Qt.KeyboardModifier.MetaModifier, Qt.Key.Key_Meta)) if mods & flag}
                    self.start_hold(speed)
                    return True
        elif t == QEvent.Type.KeyRelease and self.hold_key is not None and not ev.isAutoRepeat():
            if ev.key() == self.hold_key or ev.key() in self.hold_mods:
                self.hold_key = None
                self.pause()
                return True
        elif t == QEvent.Type.WindowDeactivate and obj is self and self.hold_key is not None:
            self.hold_key = None                             # lost focus: we would never see the release
            self.pause()
        return super().eventFilter(obj, ev)

    def start_hold(self, speed):
        act = self.active()
        if not act or len(act) < len(self.panes()):
            return
        self.pause()
        if speed > 0:
            self.play(rate=speed)
        else:                                                # backwards: step the two videos back in real time
            self.rev_speed, self.rev_t = abs(speed), time.monotonic()
            self.rev_timer.start()

    def rev_tick(self):
        now = time.monotonic()
        dt, self.rev_t = now - self.rev_t, now
        self.seek_all(self.elapsed - self.rev_speed * 1000 * dt)
        if self.elapsed <= 0:
            self.rev_timer.stop()

    def tick(self):
        act = self.active()
        for p in act:                                      # a lap that has reached its finish gate stops on that frame
            if not p.finished and p.rel() >= p.dur:
                p.finished = True
                p.player.pause()
                p.player.setPosition(int(p.end + p.BIAS * self.frame_ms(p)))
        running = [p for p in act if not p.finished]
        if not running:
            self.elapsed = float(self.maxdur())
            self.pause()
        else:
            lead = running[0]
            self.elapsed = float(max(0, lead.rel()))
            for p in running[1:]:                          # keep the two in step (they drift a few ms at most)
                if abs(p.rel() - lead.rel()) > 80:
                    p.player.setPosition(int(p.start + lead.rel() + p.BIAS * self.frame_ms(p)))
        self.slider.blockSignals(True)
        self.slider.setValue(int(self.elapsed))
        self.slider.blockSignals(False)
        self.update_labels()

    def update_labels(self):
        self.time_label.setText(f"{fmt(self.elapsed)} / {fmt(self.maxdur())}")
        ps = self.panes()
        ok = len(ps) == 2 and all(p.ready for p in ps)
        for w in self.controls:
            w.setEnabled(ok)
        if len(ps) == 2:
            da, db = ps[0].dur, ps[1].dur
            diff = db - da
            verdict = ("the same time" if diff == 0 else
                       f"<b>{abs(diff) / 1000:.3f} s {'slower' if diff > 0 else 'faster'}</b> than A")
            self.summary.setText(f"A: <b>{fmt(da)}</b> &nbsp;·&nbsp; B: <b>{fmt(db)}</b> &nbsp;→&nbsp; B is {verdict}. "
                                 "Both start on their start-gate frame; whichever reaches a gate or corner first is ahead.")

    def done(self, result):
        QApplication.instance().removeEventFilter(self)
        self.timer.stop(); self.copy_timer.stop(); self.rev_timer.stop()
        for p in (self.a, self.b):
            p.shutdown()
        super().done(result)


class LapRowDelegate(QStyledItemDelegate):
    """Draws a teal frame round the table row of the lap the playhead is in (`row`; None = no lap)."""
    COLOR = "#00acc1"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.row = None

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.row() != self.row:
            return
        pen = QPen(QColor(self.COLOR))
        pen.setWidth(2)
        painter.save()
        painter.setPen(pen)
        r = option.rect
        painter.drawLine(r.left(), r.top() + 1, r.right(), r.top() + 1)
        painter.drawLine(r.left(), r.bottom() - 1, r.right(), r.bottom() - 1)
        if index.column() == 0:
            painter.drawLine(r.left() + 1, r.top(), r.left() + 1, r.bottom())
        if index.column() == index.model().columnCount() - 1:
            painter.drawLine(r.right() - 1, r.top(), r.right() - 1, r.bottom())
        painter.restore()


class MainWindow(QMainWindow):
    COL_GATE, COL_TIME, COL_LAP, COL_BAD = range(4)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.resize(1400, 800)
        self.setAcceptDrops(True)
        self.lib = Library()
        self.vid = None          # current video id
        self.fps = 30.0
        self.mark_rows = []      # [(mark_id, time_ms, bad)] shown in table
        self._building = False
        # navigation state
        self.cur_path = None
        self.frame_ms = None     # timestamp of the frame actually on screen
        self.compact = False     # View > Compact controls: shorter buttons, no hint line (fits small screens)
        self.undo_stack, self.redo_stack = [], []   # [(video id, what, [(time_ms, bad)] before it)]
        self._lap_key = None     # what the lap indicator last showed, so it only redraws on a change
        self.pending_bad = None  # (video id, last gate time) while X has flagged the lap in progress as bad
        self.cursor_ms = 0.0     # where navigation is heading (may be ahead of the shown frame)
        self._last_seek = 0.0
        self._resume_pos = None  # position to restore once a new source has loaded
        self.shuttle_rate = 0.0  # signed; 0 = not shuttling
        self._rev_t = 0.0
        self.rev_timer = QTimer(self); self.rev_timer.setInterval(30); self.rev_timer.timeout.connect(self._rev_tick)
        self._pending_seek = None
        self.seek_timer = QTimer(self); self.seek_timer.setSingleShot(True); self.seek_timer.setInterval(25)
        self.seek_timer.timeout.connect(self._flush_seek)
        # smooth-scrub copy building
        self.settings = app_settings()
        self.build = None        # dict for the ffmpeg job in progress
        self.build_queue = []    # [(video_id, path)]
        self.legacy_ffmpeg = False   # set when this ffmpeg is too old for -fps_mode
        self.listed = []         # [(video_id, path)] shown in the library list
        self.missing_copies = 0

        # --- player
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.video_widget = VideoView()
        self.video_widget.setMinimumSize(480, 270)
        self.player.setVideoOutput(self.video_widget)
        self.video_widget.wheel.connect(self.on_wheel)
        self.video_widget.videoSink().videoFrameChanged.connect(self.on_frame)
        self.player.mediaStatusChanged.connect(self.on_media_status)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.positionChanged.connect(self.on_position)
        self.player.playbackStateChanged.connect(self.on_state)
        self.player.errorOccurred.connect(lambda e, msg: self.statusBar().showMessage(f"Player error: {msg}", 8000))

        # --- library panel
        self.lib_list = QListWidget()
        self.lib_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.lib_list.setItemDelegate(LibraryDelegate(self.lib_list))
        self.lib_list.setUniformItemSizes(True)
        self.lib_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.track_filter = QComboBox()
        self.track_filter.addItem("All tracks", None)
        self.last_filter = None
        self.pending_filter = NO_PENDING     # track to show after the next rebuild of the dropdown
        self.track_filter.currentIndexChanged.connect(self.on_track_filter_changed)
        self.marked_filter = QComboBox()
        self.marked_filter.setToolTip("Hide videos by whether they have any lap marks yet")
        for label, key in (("Show: all videos", None), ("Show: not marked yet", False), ("Show: marked only", True)):
            self.marked_filter.addItem(label, key)
        self.marked_filter.currentIndexChanged.connect(lambda _: self.refresh_library())
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setAcceptDrops(False)                    # a line edit eats dropped files; let them reach the window
        self.search.setPlaceholderText("Search name, track or notes…")
        self.search.setToolTip("Show only videos whose file name, track or notes contain this text")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)                  # wait for a pause in typing
        self.search_timer.timeout.connect(self.refresh_library)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.sort_box = QComboBox()
        self.sort_box.setToolTip("Order of the videos in the list. Videos with no lap times yet go last.")
        for label, key in (("Sort: date (oldest first)", "date"), ("Sort: date (newest first)", "newest"),
                           ("Sort: fastest lap", "lap"),
                           ("Sort: fastest run", "run")):
            self.sort_box.addItem(label, key)
        i = self.sort_box.findData(self.settings.value("library_sort", "date"))
        self.sort_box.setCurrentIndex(max(i, 0))
        self.sort_box.currentIndexChanged.connect(self.on_sort_changed)
        # Double-click opens a video. (Not itemActivated as well: a double-click emits both, and opening
        # rebuilds the list, so the second call would get an item that no longer exists.)
        self.lib_list.itemDoubleClicked.connect(self.on_item_open_requested)
        self.lib_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lib_list.customContextMenuRequested.connect(self.on_library_menu)
        add_files = QPushButton("Add videos…")
        add_files.clicked.connect(self.add_files)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self.add_folder)
        remove = QPushButton("Remove")
        remove.setToolTip("Remove from library (the video file is not touched)")
        remove.clicked.connect(self.remove_selected)
        stats_btn = QPushButton("Library stats")
        stats_btn.clicked.connect(lambda: StatsDialog(self.lib, self, self.track_filter.currentData()).exec())
        details = QPushButton("Track / date / notes…")
        details.setToolTip("Set the track name, session date and notes of the selected video(s)")
        details.clicked.connect(self.edit_details)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("<b>Video library</b>"))
        ll.addWidget(self.track_filter)
        ll.addWidget(self.sort_box)
        ll.addWidget(self.marked_filter)
        ll.addWidget(self.search)
        ll.addWidget(self.lib_list)
        ll.addWidget(details)
        r1 = QHBoxLayout(); r1.addWidget(add_files); r1.addWidget(add_folder)
        r2 = QHBoxLayout(); r2.addWidget(remove); r2.addWidget(stats_btn)
        fix = QPushButton("Fix paths…")
        fix.setToolTip("Update many videos at once after moving them or copying the library to another PC")
        fix.clicked.connect(self.fix_paths)
        clear = QPushButton("Clear smooth copies…")
        clear.setToolTip("Delete the cached smooth-scrub copies to free disk space (they can be rebuilt)")
        clear.clicked.connect(self.clear_proxies)
        self.build_all_btn = QPushButton("⚡ Build smooth copies")
        self.build_all_btn.setToolTip("Build smooth-scrub copies for every video shown in the list that lacks one")
        self.build_all_btn.clicked.connect(self.on_build_all)
        self.build_on_add = QCheckBox("Build when adding videos")
        self.build_on_add.setToolTip("Queue a smooth-scrub copy for each video as you add it to the library")
        self.build_on_add.setChecked(self.settings.value("build_on_add", True, type=bool))
        self.build_on_add.toggled.connect(lambda v: self.settings.setValue("build_on_add", v))
        self.queue_label = QLabel()
        self.queue_label.setWordWrap(True)
        self.queue_label.setTextFormat(Qt.TextFormat.PlainText)
        self.queue_label.setStyleSheet("color:gray")
        if not find_tool("ffmpeg"):
            self.build_all_btn.setEnabled(False); self.build_on_add.setEnabled(False)
            self.build_all_btn.setToolTip("Needs ffmpeg installed")
        ll.addLayout(r1); ll.addLayout(r2); ll.addWidget(fix)
        ll.addWidget(self.build_all_btn); ll.addWidget(self.build_on_add); ll.addWidget(self.queue_label)
        ll.addWidget(clear)

        # --- transport controls
        self.slider = MarkSlider()
        self.slider.sliderMoved.connect(self.on_slider_moved)
        self.time_label = QLabel("0:00.000 / 0:00.000")
        self.lap_label = QLabel()
        self.lap_label.setToolTip("The lap the playhead is in: this is the lap the Bad lap key / button acts on, "
                                  "and its row is framed in the table")
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self.toggle_play)
        back = QPushButton("◀ Frame"); back.clicked.connect(lambda: self.step(-1))
        fwd = QPushButton("Frame ▶"); fwd.clicked.connect(lambda: self.step(1))
        prev_gate = QPushButton("⏮ Gate"); prev_gate.setToolTip("Previous gate mark (PgUp)")
        prev_gate.clicked.connect(lambda: self.jump_mark(-1))
        next_gate = QPushButton("Gate ⏭"); next_gate.setToolTip("Next gate mark (PgDn)")
        next_gate.clicked.connect(lambda: self.jump_mark(1))
        self.speed = QComboBox()
        for sp in SPEEDS:
            self.speed.addItem(f"{sp:g}x", sp)
        self.speed.setCurrentIndex(SPEEDS.index(1.0))
        self.speed.currentIndexChanged.connect(self.apply_speed)
        self.mute = QCheckBox("Mute"); self.mute.setChecked(True)
        self.mute.toggled.connect(self.audio.setMuted)
        self.mark_btn = QPushButton()
        self.mark_btn.setToolTip("Mark a gate pass on the frame shown. Change the key under View → Keyboard shortcuts…")
        self.mark_btn.setStyleSheet("font-weight:bold; padding:8px 14px; background:#2e7d32; color:white;")
        self.mark_btn.clicked.connect(self.mark_gate)
        self.bad_btn = QPushButton()
        self.bad_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)          # so Space keeps playing / pausing
        self.bad_btn.setToolTip("Flag the lap at the playhead as bad (press again to undo). While you are still marking, "
                                "press it during a lap and then mark the gate that ends it: that lap is saved as bad. "
                                "Change the key under View → Keyboard shortcuts…")
        self.bad_btn.clicked.connect(self.toggle_bad_lap)

        # spring-loaded shuttle: drag right = forward (slow -> fast), left = backward, release = stop
        self.jog = QSlider(Qt.Orientation.Horizontal)
        self.jog.setRange(-100, 100)
        self.jog.setPageStep(0)
        self.jog.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.jog.setTickInterval(25)
        self.jog.setStyleSheet(
            "QSlider::groove:horizontal{height:6px;background:#9e9e9e;border-radius:3px}"
            "QSlider::sub-page:horizontal,QSlider::add-page:horizontal{background:transparent}"
            "QSlider::handle:horizontal{width:20px;margin:-7px 0;background:#1976d2;border-radius:10px}")
        self.jog.valueChanged.connect(self.on_jog)
        self.jog.sliderReleased.connect(lambda: self.jog.setValue(0))
        self.jog.setToolTip("Shuttle: drag right to play forward (slow to fast), left to go backward. Releases to stop.")
        self.jog_label = QLabel("Shuttle: stopped")
        self.jog_label.setMinimumWidth(150)

        # smooth-scrub copy
        self.proxy_label = QLabel()
        self.proxy_btn = QPushButton("⚡ Build smooth-scrub copy")
        self.proxy_btn.clicked.connect(self.on_proxy_button)
        self.use_proxy = QCheckBox("Use smooth copy"); self.use_proxy.setChecked(True)
        self.use_proxy.toggled.connect(lambda: self.load_source(keep_pos=self.cursor_ms) if self.cur_path else None)
        self.auto_proxy = QCheckBox("Auto-build")
        self.auto_proxy.setToolTip("Build the smooth-scrub copy automatically when a video is opened")
        self.auto_proxy.setChecked(self.settings.value("auto_proxy", True, type=bool))
        self.auto_proxy.toggled.connect(lambda v: self.settings.setValue("auto_proxy", v))
        if not find_tool("ffmpeg"):
            self.proxy_btn.setEnabled(False); self.auto_proxy.setEnabled(False)
            self.proxy_btn.setToolTip("Needs ffmpeg installed")

        ctl = QHBoxLayout()
        self.nav_btns = [back, fwd, prev_gate, next_gate]
        back.setToolTip("Step one frame back"); fwd.setToolTip("Step one frame forward")
        self.speed_label = QLabel("Speed:")
        for w in (self.play_btn, back, fwd, prev_gate, next_gate, self.speed_label, self.speed, self.mute):
            ctl.addWidget(w)
        ctl.addStretch(1)
        ctl.addWidget(self.mark_btn)
        jogrow = QHBoxLayout()
        jogrow.addWidget(QLabel("◀◀"))
        jogrow.addWidget(self.jog, 1)
        jogrow.addWidget(QLabel("▶▶"))
        jogrow.addWidget(self.jog_label)
        proxyrow = QHBoxLayout()
        for w in (self.proxy_btn, self.use_proxy, self.auto_proxy):
            proxyrow.addWidget(w)
        proxyrow.addWidget(self.proxy_label, 1)
        proxyrow.addWidget(self.bad_btn)                    # right-aligned, directly under the mark button
        self.title_label = QLabel("Add a video to the library and double-click it to start.")
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.addWidget(self.title_label)
        cl.addWidget(self.video_widget, 1)
        cl.addWidget(self.slider)
        timerow = QHBoxLayout()
        timerow.addWidget(self.time_label)
        timerow.addStretch(1)
        timerow.addWidget(self.lap_label)
        cl.addLayout(timerow)
        cl.addLayout(jogrow)
        cl.addLayout(ctl)
        cl.addLayout(proxyrow)
        self.hint = hint = QLabel()
        hint.setStyleSheet("color:gray")
        hint.setWordWrap(True)
        cl.addWidget(hint)

        # --- laps panel
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Gate", "Video time", "Lap time", "Bad lap"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for col in (0, 3):                      # Gate / Bad lap hug their content; the two time columns share the rest
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.lap_delegate = LapRowDelegate(self.table)
        self.table.setItemDelegate(self.lap_delegate)
        self.table.cellClicked.connect(self.on_row_clicked)
        self.table.itemChanged.connect(self.on_item_changed)
        del_btn = QPushButton("Delete mark")
        del_btn.clicked.connect(lambda: self.delete_selected_mark())
        move_btn = QPushButton("Move mark here")
        move_btn.setToolTip("Set the selected mark to the current video position")
        move_btn.clicked.connect(self.move_selected_mark)
        self.stats_label = QLabel()
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.stats_label.setWordWrap(True)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("<b>Gate passes & laps</b>"))
        rl.addWidget(self.table, 1)
        rr = QHBoxLayout(); rr.addWidget(del_btn); rr.addWidget(move_btn)
        rl.addLayout(rr)
        chart_btn = QPushButton("📊 Lap chart")
        chart_btn.clicked.connect(self.show_chart)
        csv_btn = QPushButton("Export CSV…")
        csv_btn.setToolTip("Export this video's laps")
        csv_btn.clicked.connect(self.export_current)
        self.run_spin = QSpinBox()
        self.run_spin.setRange(MIN_RUN, MAX_RUN)
        self.run_spin.setValue(run_length())
        self.run_spin.setSuffix(" laps")
        self.run_spin.valueChanged.connect(self.on_run_length_changed)
        self.run_label = QLabel()
        self.run_label.setWordWrap(True)                     # wraps instead of being cut off in the narrow compact panel
        self.run_reset = QPushButton("Use default")
        self.run_reset.clicked.connect(self.reset_track_run)
        self.run_btn = QPushButton()
        self.run_btn.clicked.connect(self.export_bestn_video)
        r3 = QHBoxLayout(); r3.addWidget(chart_btn); r3.addWidget(csv_btn)
        rl.addLayout(r3)
        run_row = QHBoxLayout(); run_row.addWidget(self.run_label); run_row.addWidget(self.run_spin)
        run_row.addWidget(self.run_reset); run_row.addStretch(1)
        rl.addLayout(run_row)
        rl.addWidget(self.run_btn)
        self.laps_btn = QPushButton("Export laps…")
        self.laps_btn.setToolTip("Export any lap, or any run of consecutive laps, of this video (not only the fastest)")
        self.laps_btn.clicked.connect(self.export_chosen_laps)
        self.compare_btn = QPushButton("Compare laps…")
        self.compare_btn.setToolTip("Play two laps side by side, even from different videos on the same track "
                                    "(needs smooth copies)")
        self.compare_btn.clicked.connect(self.open_compare)
        lr = QHBoxLayout(); lr.addWidget(self.laps_btn); lr.addWidget(self.compare_btn)
        rl.addLayout(lr)
        self.refresh_run_box()
        rl.addWidget(self.stats_label)

        self.split = split = QSplitter()
        right.setMinimumWidth(320)
        split.addWidget(left); split.addWidget(center); split.addWidget(right)
        split.setSizes([380, 680, 340])
        self.setCentralWidget(split)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 0); split.setStretchFactor(1, 1); split.setStretchFactor(2, 0)   # only the video grows
        for i in (1, 2):
            split.handle(i).setEnabled(False)               # panel widths are fixed: no dragging the dividers
            split.handle(i).setCursor(Qt.CursorShape.ArrowCursor)   # ...and no resize cursor hinting that you can
        split.setHandleWidth(1)
        geo = self.settings.value("window_geometry")
        if geo:
            self.restoreGeometry(geo)

        # --- shortcuts (every key is rebindable: View > Keyboard shortcuts…)
        handlers = {
            "play": self.toggle_play, "mark": self.mark_gate,
            "step_back": lambda: self.step(-1), "step_fwd": lambda: self.step(1),
            "jump_back": lambda: self.seek_rel(-5000), "jump_fwd": lambda: self.seek_rel(5000),
            "shuttle_back": lambda: self.key_shuttle(-1), "shuttle_stop": lambda: self.set_shuttle(0),
            "shuttle_fwd": lambda: self.key_shuttle(1),
            "prev_gate": lambda: self.jump_mark(-1), "next_gate": lambda: self.jump_mark(1),
            "slower": lambda: self.change_speed(-1), "faster": lambda: self.change_speed(1),
            "bad_lap": self.toggle_bad_lap, "undo": self.undo, "redo": self.redo, "delete_mark": self.delete_key,
        }
        self.shortcuts = {}
        for kid, fn in handlers.items():
            s_ = QShortcut(QKeySequence(), self)
            s_.setContext(Qt.ShortcutContext.ApplicationShortcut)
            s_.activated.connect(fn)
            self.shortcuts[kid] = s_
        # hold_back / hold_fwd = hold to play at 2x (handled in eventFilter, which needs the key releases)
        self.hold_key = None
        self.hold_mod_keys = set()
        self.hold_bindings = {}             # portable key string -> shuttle speed
        QApplication.instance().installEventFilter(self)

        enter = QShortcut(QKeySequence(Qt.Key.Key_Return), self.lib_list)
        enter.setContext(Qt.ShortcutContext.WidgetShortcut)
        enter.activated.connect(lambda: self.on_item_open_requested(self.lib_list.currentItem()))

        # --- Library > export / import / merge
        lib_menu = self.menuBar().addMenu("&Library")
        lib_menu.addAction("&Export library…").triggered.connect(self.export_library)
        lib_menu.addAction("&Import library (replace)…").triggered.connect(self.import_library)
        lib_menu.addAction("&Merge library…").triggered.connect(self.merge_library)
        lib_menu.addSeparator()
        lib_menu.addAction("&Compare laps…").triggered.connect(self.open_compare)

        # --- Edit > undo / redo of mark changes
        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_act = edit_menu.addAction("&Undo")
        self.undo_act.triggered.connect(self.undo)
        self.redo_act = edit_menu.addAction("&Redo")
        self.redo_act.triggered.connect(self.redo)

        # --- View > Theme
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction("&Keyboard shortcuts…").triggered.connect(self.choose_keys)
        self.library_act = QAction("Show &library panel", self, checkable=True)
        self.library_act.setChecked(self.settings.value("show_library", True, type=bool))
        self.library_act.toggled.connect(self.set_library_visible)
        view_menu.addAction(self.library_act)
        self.compact_act = QAction("&Compact controls", self, checkable=True)
        self.compact_act.setToolTip("Shorter buttons and no hint line, so the window fits a small screen")
        self.compact_act.setStatusTip("Shorter buttons and no hint line, so the window fits a small screen")
        saved = self.settings.value("compact_layout")
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        self.compact_act.setChecked(saved in (True, "true") if saved is not None
                                    else (screen.width() < 1400 or screen.height() < 800))   # first run: auto
        self.compact_act.toggled.connect(self.set_compact)
        view_menu.addAction(self.compact_act)
        self.compact = self.compact_act.isChecked()
        self.apply_compact()
        self.split.widget(0).setVisible(self.library_act.isChecked())
        theme_menu = view_menu.addMenu("&Theme")
        group = QActionGroup(self)
        self.theme_actions = {}
        for key, label in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            act = QAction(label, self, checkable=True)
            act.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            group.addAction(act); theme_menu.addAction(act)
            self.theme_actions[key] = act
        theme = self.settings.value("theme", "system")
        self.theme_actions.get(theme, self.theme_actions["system"]).setChecked(True)
        apply_theme(QApplication.instance(), theme)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Install &ffmpeg…").triggered.connect(self.offer_ffmpeg)
        help_menu.addAction("&About").triggered.connect(self.show_about)

        self.apply_keys()
        self.refresh_library()
        self.fit_to_screen()

    def fit_to_screen(self):
        """Never open bigger than the screen: a size saved on a larger monitor, or the 1400 x 800 default."""
        avail = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        self.resize(min(self.width(), avail.width()), min(self.height(), avail.height() - 40))
        if self.x() + self.width() > avail.right() or self.y() + self.height() > avail.bottom():
            self.move(max(avail.left(), min(self.x(), avail.right() - self.width())),
                      max(avail.top(), min(self.y(), avail.bottom() - self.height() - 40)))

    def set_library_visible(self, on):
        self.settings.setValue("show_library", on)
        self.split.widget(0).setVisible(on)

    def set_compact(self, on):
        self.settings.setValue("compact_layout", on)
        self.compact = on
        self.apply_compact()
        self.apply_keys()
        self.fit_to_screen()

    def apply_compact(self):
        """Compact controls trade a little text for width and height, so the window fits a small or scaled screen."""
        c = self.compact
        self.speed_label.setVisible(not c)
        for b, text in zip(self.nav_btns, ("◀", "▶", "⏮", "⏭") if c else ("◀ Frame", "Frame ▶", "⏮ Gate", "Gate ⏭")):
            b.setText(text)
            b.setFixedWidth(36) if c else (b.setMinimumWidth(0), b.setMaximumWidth(16777215))
        self.video_widget.setMinimumSize(*((320, 180) if c else (480, 270)))
        self.use_proxy.setText("Smooth copy" if c else "Use smooth copy")
        self.update_proxy_ui()
        self.split.widget(2).setMinimumWidth(250 if c else 320)

    def show_about(self):
        def folder_line(label, folder):
            name = html.escape(str(folder))
            if folder.is_dir():
                return f'{label}: <a href="{QUrl.fromLocalFile(str(folder)).toString()}">{name}</a>'
            return f"{label}: {name} (not created yet)"
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<b>{html.escape(APP_NAME)} {html.escape(VERSION)}</b><br><br>Author: {html.escape(AUTHOR)}<br><br>"
                    + folder_line("Library folder (library.db)", DB_PATH.parent) + "<br>"
                    + folder_line("Smooth copies folder", cache_dir()))
        box.exec()

    # ---- ffmpeg check -------------------------------------------------------
    def missing_ffmpeg_tools(self):
        return [t for t in ("ffmpeg", "ffprobe") if not find_tool(t)]

    def offer_ffmpeg_on_start(self):
        """At start-up: if ffmpeg/ffprobe are missing, offer to install them (unless told not to ask again)."""
        if self.missing_ffmpeg_tools() and not self.settings.value("ffmpeg_prompt_off", False, type=bool):
            self.offer_ffmpeg()

    def offer_ffmpeg(self):
        missing = self.missing_ffmpeg_tools()
        if not missing:
            QMessageBox.information(self, "ffmpeg", f"ffmpeg is installed:\n{find_tool('ffmpeg')}")
            return
        can_winget = sys.platform == "win32" and shutil.which("winget") is not None
        brew = find_brew()
        hint = ffmpeg_install_hint()
        text = (f"{' and '.join(missing)} {'was' if len(missing) == 1 else 'were'} not found. The app runs without "
                f"{'it' if len(missing) == 1 else 'them'}, but it can't build smooth-scrub copies (scrubbing only jumps between keyframes), it assumes "
                "30 fps for frame-stepping, and it can't export videos.")
        if can_winget:
            text += "\n\nInstall ffmpeg now with winget? It is a free download and takes about a minute."
        elif brew:
            text += ("\n\nHomebrew is installed, so the app can run  brew install ffmpeg  for you. "
                     "It can take several minutes.")
        elif sys.platform == "darwin":
            text += (f"\n\nThe easiest way to install ffmpeg on a Mac is Homebrew ({HOMEBREW_URL}). Install Homebrew "
                     "first, then come back to Help → Install ffmpeg… (or run  brew install ffmpeg  in Terminal).")
        elif hint:
            text += f"\n\nTo install it, use {hint}, then restart the app."
        else:
            text += "\n\nwinget isn't available on this PC. Download a full build (with libx264) and put " \
                    "ffmpeg.exe and ffprobe.exe in a 'bin' folder next to the app."
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("ffmpeg needed")
        box.setText(text)
        install = None
        if can_winget:
            install = box.addButton("Install with winget", QMessageBox.ButtonRole.AcceptRole)
        elif brew:
            install = box.addButton("Install with Homebrew", QMessageBox.ButtonRole.AcceptRole)
        no_brew = sys.platform == "darwin" and not brew
        web = box.addButton("Get Homebrew" if no_brew else "Open download page", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        never = QCheckBox("Don't ask again at start-up (Help → Install ffmpeg… still works)")
        box.setCheckBox(never)
        box.exec()
        self.settings.setValue("ffmpeg_prompt_off", never.isChecked())
        clicked = box.clickedButton()
        if install is not None and clicked is install:
            self.install_ffmpeg(brew)
        elif clicked is web:
            QDesktopServices.openUrl(QUrl(HOMEBREW_URL if no_brew else FFMPEG_DOWNLOAD_URL))

    def install_ffmpeg(self, brew=None):
        """Install ffmpeg with winget (Windows) or the given Homebrew `brew` (macOS), showing a busy dialog."""
        if brew:
            tool, program, args = "Homebrew", brew, ["install", "ffmpeg"]
            how = "This can take several minutes."
            manual = "Try running  brew install ffmpeg  in Terminal."
        else:
            tool, program = "winget", "winget"
            args = ["install", "--id", "Gyan.FFmpeg", "-e", "--accept-package-agreements", "--accept-source-agreements"]
            how = "This can take a minute."
            manual = ("Try running  winget install Gyan.FFmpeg  in a terminal, or download ffmpeg yourself and put "
                      "ffmpeg.exe and ffprobe.exe in a 'bin' folder next to the app.")
        progress = QProgressDialog(f"Installing ffmpeg with {tool}…\n{how}", "", 0, 0, self)
        progress.setWindowTitle("Installing ffmpeg")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        hide_console(proc)

        reported = []

        def finished(code, _status):
            if reported:                       # a crash can raise both errorOccurred and finished
                return
            reported.append(True)
            progress.close()
            output = bytes(proc.readAll()).decode("utf-8", "replace").strip()
            proc.deleteLater()
            self.refresh_ffmpeg_state()
            if not self.missing_ffmpeg_tools():
                self.statusBar().showMessage("ffmpeg installed", 6000)
            else:
                QMessageBox.warning(self, "ffmpeg", f"{tool} could not install ffmpeg"
                                    + (f" (exit code {code})" if code else "")
                                    + f".\n\n{manual}"
                                    + (f"\n\n{output[-600:]}" if output else ""))

        def failed(_err):
            if proc.state() == QProcess.ProcessState.NotRunning:
                finished(-1, None)

        proc.finished.connect(finished)
        proc.errorOccurred.connect(failed)
        if brew:
            env = QProcessEnvironment.systemEnvironment()
            env.insert("HOMEBREW_NO_ENV_HINTS", "1")
            proc.setProcessEnvironment(env)
        proc.start(program, args)
        progress.show()

    def refresh_ffmpeg_state(self):
        """Re-enable the ffmpeg-dependent controls after ffmpeg appeared while the app was running."""
        _encoder_cache.clear()
        if find_tool("ffmpeg"):
            for w in (self.build_on_add, self.auto_proxy, self.proxy_btn):
                w.setEnabled(True)
            for w in (self.build_all_btn, self.proxy_btn):
                if w.toolTip() == "Needs ffmpeg installed":
                    w.setToolTip("")
            self.build_all_btn.setToolTip("Build smooth-scrub copies for every video shown in the list that lacks one")
            self.update_queue_ui()
            self.update_proxy_ui()

    # ---- library backup / restore / merge ----------------------------------
    def export_library(self):
        default = Path.home() / f"library-{datetime.now():%Y-%m-%d}.db"
        p, _ = QFileDialog.getSaveFileName(self, "Export library", str(default), "Library database (*.db)")
        if not p:
            return
        if not p.lower().endswith(".db"):
            p += ".db"
        try:
            self.lib.export_to(p)
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QMessageBox.information(self, "Library exported", f"Saved a copy of your library to:\n{p}")

    def release_current_video(self):
        """Stop playback and drop anything tied to the current library rows (ids change on import)."""
        self.set_shuttle(0)
        self.player.stop()
        self.player.setSource(QUrl())
        self.vid = None
        self.cur_path = None
        self.clear_undo()
        self.load_marks()
        self.update_proxy_ui()

    def pick_library_file(self, title):
        p, _ = QFileDialog.getOpenFileName(self, title, str(Path.home()), "Library database (*.db);;All files (*)")
        if not p:
            return None
        try:
            Library.check_library_file(p)
        except ValueError as e:
            QMessageBox.warning(self, "Not a library", str(e))
            return None
        return p

    def import_library(self):
        p = self.pick_library_file("Import library (replaces your current one)")
        if not p:
            return
        if QMessageBox.question(
                self, "Replace library?",
                "This replaces ALL videos, marks and tracks in this app with the ones in the file.\n"
                "A backup of your current library is saved first.\n\nContinue?") != QMessageBox.StandardButton.Yes:
            return
        backup = DB_PATH.with_name(f"library-before-import-{datetime.now():%Y%m%d-%H%M%S}.db")
        try:
            self.lib.export_to(backup)
            if self.build:
                self.build["cancelled"] = True
                self.build["proc"].kill()
            self.build_queue = []
            self.release_current_video()
            self.lib.replace_from(p)
        except Exception as e:
            QMessageBox.warning(self, "Import failed", f"{e}\n\nYour previous library is backed up at:\n{backup}")
            self.refresh_library()
            return
        self.refresh_library()
        QMessageBox.information(self, "Library imported", f"Library replaced.\nYour previous one is backed up at:\n{backup}")

    def merge_library(self):
        p = self.pick_library_file("Merge library into this one")
        if not p:
            return
        try:
            st = self.lib.merge_from(p)
        except Exception as e:
            QMessageBox.warning(self, "Merge failed", str(e))
            return
        self.load_marks()
        self.refresh_library()
        QMessageBox.information(
            self, "Library merged",
            f"{st['videos_added']} new video(s) added, {st['videos_matched']} already in your library "
            f"(matched by file path), {st['marks_added']} new mark(s) added.\n\n"
            "Videos from another computer keep that computer's file paths; use \"Fix paths…\" to point them at "
            "your copies.")

    # ---- library -----------------------------------------------------------
    def on_track_filter_changed(self):
        action = self.track_filter.currentData()
        if action == NEW_TRACK:
            self.new_empty_track()
        elif action == RENAME_TRACK:
            self.rename_current_track()
        elif action == DELETE_TRACK:
            self.delete_current_track()
        else:
            self.refresh_library()

    def rename_current_track(self):
        old = self.pending_filter = self.last_filter
        if not old:
            return self.refresh_library()
        text, ok = QInputDialog.getText(self, "Rename track", f"New name for '{old}':", text=old)
        new = " ".join(text.split()) if ok else ""
        if new and new != old:
            same = next((t for t in self.lib.tracks() if t.lower() == new.lower() and t != old), None)
            if same is not None:
                n = self.lib.track_video_count(old)
                if QMessageBox.question(
                        self, "Merge tracks",
                        f"A track named '{same}' already exists.\n\nMerge '{old}' ({n} video(s)) into it?"
                        ) != QMessageBox.StandardButton.Yes:
                    return self.refresh_library()
                new = same
            self.lib.rename_track(old, new)
            self.pending_filter = new
            self.statusBar().showMessage(f"Track renamed to '{new}'", 5000)
        self.refresh_library()

    def delete_current_track(self):
        name = self.pending_filter = self.last_filter
        if not name:
            return self.refresh_library()
        n = self.lib.track_video_count(name)
        what = (f"Delete the empty track '{name}'?" if n == 0 else
                f"Delete the track '{name}'?\n\nIts {n} video(s) stay in the library (and keep their marks) "
                "but will have no track.")
        if QMessageBox.question(self, "Delete track", what) == QMessageBox.StandardButton.Yes:
            self.lib.delete_track(name)
            self.pending_filter = None                       # back to All tracks
            self.statusBar().showMessage(f"Track '{name}' deleted", 5000)
        self.refresh_library()

    def new_empty_track(self):
        """The dropdown's '＋ New track…' entry: make a track with no videos yet and switch to it, so
        the videos added next (Add videos…, Add folder…, drag and drop) land on it."""
        text, ok = QInputDialog.getText(self, "New track", "Name of the new track:")
        name = " ".join(text.split()) if ok else ""
        target = self.last_filter
        if name:
            same = next((t for t in self.lib.tracks() if t.lower() == name.lower()), None)
            if same:
                target = same
                self.statusBar().showMessage(f"Track '{same}' already exists - switched to it", 5000)
            else:
                self.lib.add_track(name)
                target = name
                self.statusBar().showMessage(
                    f"Track '{name}' created - add videos now and they will join it", 8000)
        self.pending_filter = target
        self.refresh_library()

    def on_sort_changed(self):
        self.settings.setValue("library_sort", self.sort_box.currentData())
        self.refresh_library()

    def refresh_library(self, select_id=None):
        select_id = select_id or self.vid
        # rebuild the track filter without triggering another refresh
        current = self.track_filter.currentData()
        if self.pending_filter is not NO_PENDING:
            current, self.pending_filter = self.pending_filter, NO_PENDING
        self.track_filter.blockSignals(True)
        self.track_filter.clear()
        self.track_filter.addItem("All tracks", None)
        for t in self.lib.tracks():
            self.track_filter.addItem(t, t)
        meta = self.lib.all_meta()
        if self.lib.tracks() and any(not t for t, _ in meta.values()):
            self.track_filter.addItem("(no track)", "")
        self.track_filter.insertSeparator(self.track_filter.count())
        self.track_filter.addItem("＋ New track…", NEW_TRACK)
        if current and current in self.lib.tracks():             # a real track is showing: offer to change it
            self.track_filter.addItem("✎ Rename this track…", RENAME_TRACK)
            self.track_filter.addItem("🗑 Delete this track…", DELETE_TRACK)
        i = self.track_filter.findData(current) if current is not None else 0
        self.track_filter.setCurrentIndex(max(i, 0))
        self.track_filter.blockSignals(False)
        show = self.track_filter.currentData()
        self.last_filter = show

        self.lib_list.clear()
        rmap = self.lib.run_map()
        everything = []
        for vid, path, fps in self.lib.videos():
            n = run_for(rmap, meta[vid][0])
            everything.append((effective_date(path, meta[vid][1])[0], vid, path, fps, n,
                               lap_stats(self.lib.laps(vid), n)))
        # personal bests: fastest lap / fastest run across every video of a named track (not just the visible ones)
        pb = {}
        for _, vid, _, _, _, st in everything:
            track = meta[vid][0]
            for field in ("best", "bestn"):
                if track and st[field] and st[field][0] < pb.get((track, field), (1 << 62,))[0]:
                    pb[(track, field)] = (st[field][0],)
        want_marked = self.marked_filter.currentData()
        marked = self.lib.marked_ids()
        notes = self.lib.notes_map()
        query = self.search.text().strip().lower()
        entries = [e for e in everything
                   if (show is None or meta[e[1]][0] == show)
                   and (want_marked is None or (e[1] in marked) == want_marked)
                   and (not query or query in f"{vname(e[2])} {meta[e[1]][0]} {notes.get(e[1], '')}".lower())]
        by_date = lambda e: (e[0] is None, e[0] or datetime.min.date(), e[2].lower())
        sort_key = self.sort_box.currentData()
        if sort_key in ("lap", "run"):
            field = "best" if sort_key == "lap" else "bestn"
            # fastest first; videos without a time go last, then the usual date order
            entries.sort(key=lambda e: (e[5][field] is None, e[5][field][0] if e[5][field] else 0, by_date(e)))
        elif sort_key == "newest":
            # undated videos still go last; same-day videos stay in name order
            entries.sort(key=lambda e: (e[0] is None, -(e[0].toordinal() if e[0] else 0), e[2].lower()))
        else:
            entries.sort(key=by_date)
        missing_copies = 0
        self.listed = [(e[1], e[2]) for e in entries]
        dup_of = {}                                  # video id -> paths of its twin entries
        for group in self.lib.duplicate_groups():
            for v in group:
                dup_of[v] = [pth for vv, pth, _ in self.lib.videos() if vv in group and vv != v]
        locked = self.lib.proxy_locked_ids()
        for d, vid, path, fps, n, s in entries:
            exists = os.path.exists(path)
            ready = exists and proxy_path(path).exists()
            if exists and not ready and vid not in locked:
                missing_copies += 1
            item = QListWidgetItem(vname(path))             # plain text for accessibility; painted by the delegate
            item.setData(ROLE_INFO, {
                "track": meta[vid][0], "run": n, "good": s["good"], "total": s["total"],
                "best": s["best"][0] if s["best"] else None, "bestn": s["bestn"][0] if s["bestn"] else None,
                "pb_lap": bool(s["best"]) and bool(meta[vid][0]) and s["best"][0] == pb[(meta[vid][0], "best")][0],
                "pb_run": bool(s["bestn"]) and bool(meta[vid][0]) and s["bestn"][0] == pb[(meta[vid][0], "bestn")][0],
                "name": vname(path), "date": d.isoformat() if d else "", "ready": ready,
                "dup": vid in dup_of, "exists": exists, "locked": vid in locked})
            item.setData(Qt.ItemDataRole.UserRole, (vid, path, fps))
            item.setToolTip(path + (f"\n📝 {notes[vid]}" if notes.get(vid) else "")
                            + ("" if exists else "\n(file not found - double-click to locate)")
                            + ("\n🔒 Smooth copy is not built automatically (right-click to change)" if vid in locked else "")
                            + ("\n⚠ Same file is also in the library as: " + "; ".join(dup_of[vid])
                               + "\nIts laps are counted twice in stats. Remove one of them." if vid in dup_of else ""))
            self.lib_list.addItem(item)
            if vid == select_id:
                self.lib_list.setCurrentItem(item)
        self.missing_copies = missing_copies
        self.update_queue_ui()
        self.refresh_run_box()

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add videos", str(Path.home()), "Video (*.ts *.mp4 *.mov *.mkv *.m4v *.avi);;All files (*)")
        if files:
            self.add_with_preview(files, what="in the selection", title="Add videos")

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add videos from folder", str(Path.home()))
        if folder:
            paths = [p for p in Path(folder).rglob("*") if p.suffix.lower() in VIDEO_EXTS]
            self.add_with_preview(paths, root=Path(folder), what=f"in {folder}")

    def add_with_preview(self, paths, root=None, what="", title="Add videos from folder"):
        """Show the video files that would be added (picked files, or a folder and its subfolders), let the user
        untick the ones they don't want and choose a track for each, then add the rest."""
        known_paths = {str(Path(p).resolve()) for _, p, _ in self.lib.videos()}
        known_ids = {i for i in (file_identity(p) for _, p, _ in self.lib.videos()) if i}
        entries = []
        for p in paths:
            p = Path(p)
            if p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            try:
                shown = str(p.relative_to(root)) if root else p.name
            except ValueError:
                shown = p.name
            known = str(p.resolve()) in known_paths or file_identity(p) in known_ids
            entries.append((p, shown, size, known))
        if not entries:
            QMessageBox.information(self, "No videos found",
                                    f"No video files ({', '.join(sorted(VIDEO_EXTS))}) were found {what}.")
            return
        n_sub = len({Path(shown).parent for _, shown, _, _ in entries if Path(shown).parent != Path(".")})
        intro = (f"{len(entries)} video file(s) found {what}" + (f", in {n_sub} subfolder(s)" if n_sub else "") +
                 ". Untick any you don't want to add, and choose the track for each file (or for all the ticked "
                 "files at once with the Track box below). The note, if you type one, is saved on every video you add.")
        shown_track = self.track_filter.currentData()
        default_track = shown_track if isinstance(shown_track, str) and shown_track not in (NEW_TRACK, RENAME_TRACK, DELETE_TRACK) else ""
        dlg = AddVideosDialog(entries, self.lib.tracks(), default_track, self, title, intro)
        if dlg.exec():
            chosen = dlg.selected()
            used = {t for _, t in chosen}
            if shown_track is not None and shown_track not in used:      # keep the new videos visible
                self.pending_filter = next(iter(used)) if len(used) == 1 else None
            self.add_paths([p for p, _ in chosen], tracks={str(Path(p).resolve()): t for p, t in chosen},
                           note=dlg.note())

    def add_paths(self, paths, tracks=None, note=""):
        """Add videos to the library. `tracks` ({resolved path: track name, '' = none}) sets each new video's
        track; without it new videos join the track being viewed. `note` is saved on every new video."""
        paths = [p for p in paths if Path(p).suffix.lower() in VIDEO_EXTS]
        if not paths:
            return
        known = {v[0] for v in self.lib.videos()}
        # what is already in the library, by file identity, so a second copy of a file is caught
        existing = {}
        for vid, vpath, _ in self.lib.videos():
            ident = file_identity(vpath)
            if ident:
                existing.setdefault(ident, vpath)
        new_videos, duplicates = [], []
        track = self.track_filter.currentData()     # new videos join the track being viewed
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for p in paths:
                resolved = str(Path(p).resolve())
                ident = file_identity(resolved)      # follows symlinks to the real file
                if ident in existing:                # same file already in the library (any path)
                    duplicates.append((resolved, existing[ident]))
                    continue
                vid = self.lib.add_video(p)
                if vid not in known:
                    known.add(vid)
                    new_videos.append((vid, resolved))
                    own = tracks.get(resolved, track) if tracks is not None else track
                    if own:
                        self.lib.set_meta([vid], track=own)
                    if note:
                        self.lib.set_notes([vid], note)
                if ident:
                    existing[ident] = resolved
        finally:
            QApplication.restoreOverrideCursor()
        if self.build_on_add.isChecked() and self.build_on_add.isEnabled():
            for vid, path in new_videos:
                self.request_proxy(vid, path)
        self.refresh_library()
        msg = f"Added {len(new_videos)} video(s)"
        if new_videos and self.build_on_add.isChecked() and self.build_on_add.isEnabled():
            msg += ("; building smooth copies in the background" if ffmpeg_has_encoder("libx264")
                    else "; smooth copies skipped: your ffmpeg has no H.264 (libx264) encoder, see the README")
        if not find_ffprobe():
            msg += " - ffprobe not found, assuming 30 fps (frame-stepping may be off). Install ffmpeg."
        self.statusBar().showMessage(msg, 10000)
        if duplicates:
            self.warn_duplicates(duplicates)

    def open_paths(self, paths):
        """Open files given on the command line / by the file manager: add the new ones, and
        open the first (files already in the library just open, with no duplicate warning)."""
        paths = [str(Path(p).resolve()) for p in paths if Path(p).suffix.lower() in VIDEO_EXTS]
        if not paths:
            return
        known = {path for _, path, _ in self.lib.videos()}
        self.add_paths([p for p in paths if p not in known])
        by_path = {path: vid for vid, path, _ in self.lib.videos()}
        first_vid = next((by_path[p] for p in paths if p in by_path), None)
        self.track_filter.setCurrentIndex(0)                 # make sure it is visible in the list
        for i in range(self.lib_list.count()):
            item = self.lib_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole)[0] == first_vid:
                self.open_item(item)               # rebuilds the list, so scroll to the new current item
                if self.lib_list.currentItem():
                    self.lib_list.scrollToItem(self.lib_list.currentItem())
                break

    def warn_duplicates(self, duplicates):
        lines = [f"• {vname(new)}\n    already in the library as:  {old}" if new != old
                 else f"• {vname(new)}  (already in the library)" for new, old in duplicates[:8]]
        more = f"\n…and {len(duplicates) - 8} more" if len(duplicates) > 8 else ""
        QMessageBox.warning(
            self, "Already in your library",
            f"{len(duplicates)} file(s) were not added because the same file is already in the library "
            "(same name, size and date):\n\n" + "\n".join(lines) + more +
            "\n\nAdding it twice would count its laps twice in your stats.")

    def remove_selected(self, vids=None):
        """Remove the selected videos (and their lap marks) from the library; the video files are not deleted."""
        paths = {v: p for v, p, _ in self.lib.videos()}
        if vids is None:
            vids = self.selected_vids()
            if not vids and self.lib_list.currentItem():
                vids = [self.lib_list.currentItem().data(Qt.ItemDataRole.UserRole)[0]]
        vids = [v for v in vids if v in paths]
        if not vids:
            return
        what = f"'{vname(paths[vids[0]])}'" if len(vids) == 1 else f"{len(vids)} videos"
        if QMessageBox.question(self, "Remove", f"Remove {what} and the lap marks from the library?\n"
                                "(The video files themselves are not deleted.)") != QMessageBox.StandardButton.Yes:
            return
        gone = set(vids)
        if self.vid in gone:
            self.set_shuttle(0)
            self.player.stop()
            self.player.setSource(QUrl())
            self.vid = None
            self.cur_path = None
            self.clear_undo()
            self.load_marks()
            self.update_proxy_ui()
        self.build_queue = [q for q in self.build_queue if q[0] not in gone]
        if self.build and self.build["vid"] in gone:
            self.build["cancelled"] = True
            self.build["proc"].kill()
        for vid in vids:
            mine = proxy_path(paths[vid])
            shared = any(proxy_path(p) == mine for v, p, _ in self.lib.videos() if v not in gone)   # a duplicate entry
            if not shared:
                mine.unlink(missing_ok=True)
            self.lib.remove_video(vid)
        self.refresh_library()
        self.statusBar().showMessage(f"Removed {len(vids)} video(s) from the library", 4000)

    def key_for(self, kid):
        """The key bound to action `kid` (portable string, '' = none): the saved one, else the default."""
        saved = self.settings.value(f"keys/{kid}")
        return key_portable(KEY_DEFAULTS[kid] if saved is None else str(saved))

    def key_text(self, kid):
        """Key for display ('–' if none)."""
        key = self.key_for(kid)
        if not key:
            return "–"
        return QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText).replace("Left", "←").replace("Right", "→")

    def apply_keys(self):
        """(Re)bind every shortcut from the saved settings and update the labels that show them."""
        for kid, sc_ in self.shortcuts.items():
            sc_.setKey(QKeySequence(self.key_for(kid)))
        self.hold_bindings = {k: sp for kid, sp in (("hold_back", -2.0), ("hold_fwd", 2.0))
                              if (k := self.key_for(kid))}
        self.mark_btn.setText(f"🏁 MARK GATE ({self.key_text('mark')})" if self.compact
                              else f"🏁 MARK START/FINISH GATE ({self.key_text('mark')})")
        self.mark_btn.setStyleSheet("font-weight:bold; padding:%s; background:#2e7d32; color:white;"
                                    % ("4px 10px" if self.compact else "8px 14px"))
        self.refresh_bad_btn()
        self.refresh_undo_actions()
        back, fwd = self.key_text("step_back"), self.key_text("step_fwd")
        hold_back, hold_fwd = self.key_text("hold_back"), self.key_text("hold_fwd")
        self.hint.setVisible(not self.compact)
        self.hint.setText(f"Tip: {back} / {fwd} step one frame, hold {hold_back} / {hold_fwd} to play at 2x · mouse wheel "
                          "also steps (Shift ×10) · all keys: View → Keyboard shortcuts…")

    def choose_keys(self):
        dlg = KeysDialog({kid: self.key_for(kid) for kid in KEY_DEFAULTS}, self)
        if dlg.exec():
            for kid, key in dlg.keys().items():
                if key == key_portable(KEY_DEFAULTS[kid]):
                    self.settings.remove(f"keys/{kid}")          # default again: store nothing
                else:
                    self.settings.setValue(f"keys/{kid}", key)
            self.apply_keys()
            self.statusBar().showMessage("Keyboard shortcuts saved", 4000)

    def set_theme(self, theme):
        self.settings.setValue("theme", theme)
        apply_theme(QApplication.instance(), theme)
        self.update()

    def fix_paths(self):
        if FixPathsDialog(self.lib, self).exec():
            self.refresh_library()

    # ---- selection helpers + right-click menu --------------------------------
    def selected_vids(self):
        return [i.data(Qt.ItemDataRole.UserRole)[0] for i in self.lib_list.selectedItems()]

    def reselect(self, vids):
        """Re-select entries after the list was rebuilt (refresh_library clears the selection)."""
        vids = set(vids)
        for i in range(self.lib_list.count()):
            item = self.lib_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole)[0] in vids:
                item.setSelected(True)

    def assign_track(self, vids, track):
        """Put the videos on `track` ('' = no track) and keep them selected."""
        self.lib.set_meta(vids, track=track)
        self.refresh_library()
        self.reselect(vids)
        what = f"'{track}'" if track else "no track"
        self.statusBar().showMessage(f"{len(vids)} video(s) set to {what}", 4000)

    def new_track_for(self, vids):
        meta = self.lib.all_meta()
        current = {meta[v][0] for v in vids}
        text, ok = QInputDialog.getText(self, "New track", "Track name:",
                                        text=next(iter(current)) if len(current) == 1 else "")
        if ok and text.strip():
            self.assign_track(vids, text.strip())

    def make_library_menu(self, vids):
        meta = self.lib.all_meta()
        current = {meta[v][0] for v in vids}
        paths = {v: p for v, p, _ in self.lib.videos()}
        menu = QMenu(self)
        title = menu.addAction(f"{len(vids)} videos selected" if len(vids) > 1 else vname(paths[vids[0]]))
        title.setEnabled(False)
        menu.addSeparator()
        sub = menu.addMenu("Assign to track")
        for t in self.lib.tracks():
            act = sub.addAction(t)
            act.setCheckable(True)
            act.setChecked(current == {t})               # ticked when every selected video is on it
            act.triggered.connect(lambda _=False, t=t: self.assign_track(vids, t))
        if self.lib.tracks():
            sub.addSeparator()
        sub.addAction("New track…").triggered.connect(lambda: self.new_track_for(vids))
        clear = menu.addAction("Remove from track")
        clear.setEnabled(current != {""})
        clear.triggered.connect(lambda: self.assign_track(vids, ""))
        menu.addSeparator()
        notes = self.lib.notes_map()
        same = {notes.get(v, "") for v in vids}
        menu.addAction("Edit note…" if same != {""} else "Add note…").triggered.connect(lambda: self.edit_notes(vids))
        menu.addAction("Track, date && notes…").triggered.connect(self.edit_details)
        menu.addSeparator()
        menu.addAction("Remove from library" if len(vids) == 1 else f"Remove {len(vids)} videos from library"
                       ).triggered.connect(lambda: self.remove_selected(vids))
        menu.addSeparator()
        copies = [v for v in vids if proxy_path(paths[v]).exists()]
        delete = menu.addAction("Delete smooth copy and lock" if len(vids) == 1
                                else f"Delete smooth copies and lock ({len(copies)})")
        delete.setEnabled(bool(copies))
        delete.triggered.connect(lambda: self.delete_proxies(vids))
        locked = self.lib.proxy_locked_ids()
        all_locked = all(v in locked for v in vids)
        lock = menu.addAction("🔒 Don't auto-build smooth copy")
        lock.setCheckable(True)
        lock.setChecked(all_locked)
        lock.triggered.connect(lambda: self.set_proxy_lock(vids, not all_locked))
        return menu

    def delete_proxies(self, vids):
        """Delete the smooth copies of the given videos (the videos and their marks are untouched) and lock the
        videos, so Auto-build doesn't make the copies again."""
        paths = {v: p for v, p, _ in self.lib.videos()}
        files = {proxy_path(paths[v]) for v in vids if v in paths}
        files = [f for f in files if f.exists()]
        if not files:
            return
        size = sum(f.stat().st_size for f in files) / 1e6
        if QMessageBox.question(self, "Delete smooth copy",
                                f"Delete {len(files)} smooth-scrub cop{'y' if len(files) == 1 else 'ies'} "
                                f"({size:.0f} MB)?\nYour videos and marks are not affected; copies can be rebuilt.\n\n"
                                "The video will also be locked (🔒) so Auto-build doesn't make the copy again. "
                                "Untick 🔒 in the right-click menu to undo that."
                                ) != QMessageBox.StandardButton.Yes:
            return
        doomed = {paths[v] for v in vids if v in paths}
        self.build_queue = [q for q in self.build_queue if q[1] not in doomed]
        if self.build and self.build["path"] in doomed:
            self.build["cancelled"] = True
            self.build["proc"].kill()
        if self.cur_path in doomed:
            self.player.setSource(QUrl())          # release the file before deleting it
        for f in files:
            f.unlink(missing_ok=True)
        self.lib.set_proxy_locked(vids, True)
        if self.cur_path in doomed:
            self.load_source(keep_pos=self.cursor_ms)
        self.refresh_library()
        self.reselect(vids)
        self.update_proxy_ui()
        self.statusBar().showMessage(f"Deleted {len(files)} smooth cop{'y' if len(files) == 1 else 'ies'} and locked "
                                     "the video(s) so they are not rebuilt automatically", 8000)

    def set_proxy_lock(self, vids, locked):
        """Lock (or unlock) automatic smooth-copy building for the given videos."""
        self.lib.set_proxy_locked(vids, locked)
        if locked:
            paths = {p for v, p, _ in self.lib.videos() if v in vids}
            self.build_queue = [q for q in self.build_queue if q[1] not in paths]
            if self.build and self.build["path"] in paths:
                self.build["cancelled"] = True
                self.build["proc"].kill()
        self.refresh_library()
        self.reselect(vids)
        self.update_proxy_ui()
        self.statusBar().showMessage(f"{len(vids)} video(s): smooth copy will "
                                     + ("not be built automatically" if locked else "be built automatically again"), 5000)

    def on_library_menu(self, pos):
        item = self.lib_list.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():                        # right-click on an unselected entry selects just it
            self.lib_list.clearSelection()
            item.setSelected(True)
        self.make_library_menu(self.selected_vids()).exec(self.lib_list.viewport().mapToGlobal(pos))

    def edit_notes(self, vids):
        """Quick note for the given videos (right-click menu). With videos whose notes differ, the box starts empty
        and leaving it empty keeps them as they are."""
        notes = self.lib.notes_map()
        existing = {notes.get(v, "") for v in vids}
        mixed = len(existing) > 1
        what = f"{len(vids)} videos" if len(vids) > 1 else vname({v: p for v, p, _ in self.lib.videos()}[vids[0]])
        text, ok = QInputDialog.getText(
            self, "Note", f"Note for {what}" + (" (they have different notes; leave empty to keep them)" if mixed else "")
            + ":\nbattery, props, tune…", QLineEdit.EchoMode.Normal, "" if mixed else next(iter(existing)))
        if not ok or (mixed and not text.strip()):
            return
        self.lib.set_notes(vids, text)
        self.update_title()
        self.refresh_library()
        self.reselect(vids)
        self.statusBar().showMessage("Note saved" if text.strip() else "Note cleared", 3000)

    def edit_details(self):
        vids = self.selected_vids()
        if not vids:
            self.statusBar().showMessage("Select one or more videos first", 3000)
            return
        dlg = DetailsDialog(self.lib, vids, self)
        if dlg.exec():
            track, date, notes = dlg.values()
            self.lib.set_meta(vids, track, date)
            if notes is not KEEP:
                self.lib.set_notes(vids, notes)
            self.update_title()
            self.refresh_library()
            self.reselect(vids)

    def on_item_open_requested(self, item):
        """Slot for double-click / Enter. Opening rebuilds the list, so do it after this signal has finished."""
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        QTimer.singleShot(0, lambda: self.open_video(*data))

    def open_item(self, item):
        if item is not None:
            self.open_video(*item.data(Qt.ItemDataRole.UserRole))

    def update_title(self):
        """The line above the video: its name, and its notes in grey."""
        if self.vid is None or not self.cur_path:
            return
        note = self.lib.notes_map().get(self.vid, "")
        self.title_label.setText(f"<b>{html.escape(vname(self.cur_path))}</b>"
                                 + (f" &nbsp;<span style='color:gray'>{html.escape(note)}</span>" if note else ""))

    def open_video(self, vid, path, fps):
        if not os.path.exists(path):
            new, _ = QFileDialog.getOpenFileName(self, f"Locate {vname(path)}", str(Path(path).parent),
                                                 "Video (*.ts *.mp4 *.mov *.mkv *.m4v *.avi);;All files (*)")
            if not new:
                return
            self.lib.relocate(vid, new)
            path = str(Path(new).resolve())
        if vid != self.vid:
            self.clear_undo()
        self.vid, self.fps = vid, fps or 30.0
        self.update_title()
        self.cur_path = path
        self.set_shuttle(0)
        self.cursor_ms = 0.0
        self.load_source()
        self.load_marks()
        self.refresh_library(vid)
        if self.auto_proxy.isChecked() and self.auto_proxy.isEnabled():
            self.request_proxy(vid, path, front=True)
        self.update_proxy_ui()

    # ---- source / smooth-scrub copy -------------------------------------------
    def load_source(self, keep_pos=None):
        """(Re)load the player from the smooth copy if it exists and is enabled, else the original."""
        if not self.cur_path:
            return
        proxy = proxy_path(self.cur_path)
        use = self.use_proxy.isChecked() and proxy.exists()
        self.frame_ms = None
        self._resume_pos = keep_pos
        self.player.setSource(QUrl.fromLocalFile(str(proxy if use else self.cur_path)))
        self.apply_speed()
        self.player.pause()
        self.update_proxy_ui()

    def on_media_status(self, status):
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia)
        if self._resume_pos is not None and status in loaded:
            pos, self._resume_pos = self._resume_pos, None
            self.seek(pos)
            self.player.pause()

    def update_proxy_ui(self):
        self.update_queue_ui()
        path = self.cur_path
        if not path:
            self.proxy_label.setText(""); self.proxy_btn.hide(); self.use_proxy.setEnabled(False)
            return
        have = proxy_path(path).exists()
        b = self.build
        self.use_proxy.setEnabled(have)
        if b and b["path"] == path:
            pct = f" {b['pct']}%" if b["pct"] is not None else ""
            self.proxy_label.setText(f"Building smooth-scrub copy…{pct} (you can keep working)")
            self.proxy_btn.setText("Cancel build"); self.proxy_btn.show()
        elif any(p == path for _, p in self.build_queue):
            self.proxy_label.setText("Smooth-scrub copy queued…")
            self.proxy_btn.hide()
        elif have:
            on = self.use_proxy.isChecked()
            self.proxy_label.setText("⚡ Smooth scrub active: every frame is seekable." if on
                                     else "Using the original video (scrubbing jumps between keyframes).")
            self.proxy_btn.hide()
        else:
            off = "🔒 Auto-build is off for this video. " if self.vid in self.lib.proxy_locked_ids() else ""
            self.proxy_label.setText(off + "Original video: scrubbing only jumps between keyframes, so the picture "
                                     "lags. Build a smooth copy for exact frames.")
            self.proxy_btn.setText("⚡ Build copy" if self.compact else "⚡ Build smooth-scrub copy"); self.proxy_btn.show()

    def on_proxy_button(self):
        if self.build and self.build["path"] == self.cur_path:
            self.build["cancelled"] = True
            self.build["proc"].kill()
        elif self.cur_path:
            self.request_proxy(self.vid, self.cur_path, manual=True)
            self.update_proxy_ui()

    def request_proxy(self, vid, path, front=False, manual=False):
        """Queue a smooth copy. front=True moves it ahead of the other waiting videos. A locked video is skipped
        unless the user asked for this one directly (manual=True)."""
        if (proxy_path(path).exists() or not find_tool("ffmpeg") or not os.path.exists(path)
                or (self.build and self.build["path"] == path)
                or (not manual and vid in self.lib.proxy_locked_ids())):
            return
        self.build_queue = [q for q in self.build_queue if q[1] != path]
        self.build_queue.insert(0, (vid, path)) if front else self.build_queue.append((vid, path))
        self._next_build()
        self.update_queue_ui()

    def on_build_all(self):
        if self.build or self.build_queue:               # button doubles as "cancel all"
            self.cancel_builds()
            return
        for vid, path in self.listed:
            self.request_proxy(vid, path)
        self.update_proxy_ui()

    def cancel_builds(self):
        self.build_queue.clear()
        if self.build:
            self.build["cancelled"] = True
            self.build["proc"].kill()
        self.update_proxy_ui()

    def update_queue_ui(self):
        busy = bool(self.build or self.build_queue)
        if busy:
            self.build_all_btn.setText("✖ Cancel all builds")
            self.build_all_btn.setEnabled(True)
            b = self.build
            now = f"Building {vname(b['path'])}" + (f" {b['pct']}%" if b["pct"] is not None else "…") if b else "Starting…"
            more = len(self.build_queue)
            self.queue_label.setText(now + (f"  ·  {more} more waiting" if more else ""))
        else:
            n = self.missing_copies
            self.build_all_btn.setText(f"⚡ Build smooth copies ({n})" if n else "⚡ All shown videos have smooth copies")
            self.build_all_btn.setEnabled(bool(n) and bool(find_tool("ffmpeg")))
            self.queue_label.setText("")

    def _next_build(self):
        if self.build or not self.build_queue:
            return
        if not ffmpeg_has_encoder("libx264"):               # every build would fail: say why once, drop the queue
            self.build_queue.clear()
            self.statusBar().showMessage(x264_help().split("\n\n")[0] + " See the README for the fix.", 30000)
            if not getattr(self, "_x264_warned", False):
                self._x264_warned = True
                QMessageBox.warning(self, "H.264 encoder missing", x264_help())
            self.update_proxy_ui()
            return
        vid, path = self.build_queue.pop(0)
        final = proxy_path(path)
        final.parent.mkdir(parents=True, exist_ok=True)
        part = final.with_name(final.stem + ".part.mp4")
        _, dur = probe_video(path)
        proc = QProcess(self)
        hide_console(proc)
        proc.setProgram(find_tool("ffmpeg"))
        proc.setArguments([
            "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=-2:'min(480,ih)'", *(SYNC_FLAGS_OLD if self.legacy_ffmpeg else SYNC_FLAGS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-g", "1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(part)])
        self.build = {"proc": proc, "vid": vid, "path": path, "part": part, "final": final,
                      "dur": dur, "pct": None, "cancelled": False, "buf": ""}
        proc.readyReadStandardOutput.connect(self._build_progress)
        proc.finished.connect(self._build_done)
        proc.start()
        self.update_proxy_ui()

    def _build_progress(self):
        b = self.build
        if not b:
            return
        b["buf"] += bytes(b["proc"].readAllStandardOutput()).decode(errors="ignore")
        lines = b["buf"].split("\n")
        b["buf"] = lines.pop()
        for ln in lines:
            if ln.startswith("out_time_us=") and b["dur"]:
                try:
                    b["pct"] = max(0, min(99, int(int(ln.split("=")[1]) / 1000 / b["dur"] * 100)))
                except ValueError:
                    pass
        self.update_proxy_ui()

    def _build_done(self, code, _status):
        b, self.build = self.build, None
        err = bytes(b["proc"].readAllStandardError()).decode(errors="ignore").strip()
        if code == 0 and b["part"].exists():
            os.replace(b["part"], b["final"])
            self.mark_ready(b["vid"])
            self.statusBar().showMessage(f"Smooth-scrub copy ready: {vname(b['path'])}", 6000)
            if b["path"] == self.cur_path and self.use_proxy.isChecked():
                self.load_source(keep_pos=self.cursor_ms)
        elif "fps_mode" in err and not self.legacy_ffmpeg and not b["cancelled"]:
            b["part"].unlink(missing_ok=True)                # old ffmpeg: retry this one with -vsync
            self.legacy_ffmpeg = True
            self.build_queue.insert(0, (b["vid"], b["path"]))
        else:
            b["part"].unlink(missing_ok=True)
            if not b["cancelled"]:
                if is_x264_error(err):
                    self.statusBar().showMessage(x264_help().split("\n\n")[0], 30000)
                else:
                    self.statusBar().showMessage(f"Smooth copy failed for {vname(b['path'])}: {err[-200:]}", 12000)
        self.update_proxy_ui()
        self._next_build()

    def mark_ready(self, vid):
        """Show the ⚡ on a list entry without rebuilding the list (keeps selection)."""
        for i in range(self.lib_list.count()):
            item = self.lib_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole)[0] == vid:
                info = dict(item.data(ROLE_INFO) or {})
                info["ready"] = True
                item.setData(ROLE_INFO, info)
        self.missing_copies = max(0, self.missing_copies - 1)

    def clear_proxies(self):
        files = list(cache_dir().glob("*.mp4")) if cache_dir().exists() else []
        size = sum(f.stat().st_size for f in files) / 1e6
        if not files and not self.build:
            self.statusBar().showMessage("No smooth-scrub copies to clear", 4000)
            return
        if QMessageBox.question(self, "Clear smooth copies",
                                f"Delete {len(files)} smooth-scrub cop{'y' if len(files) == 1 else 'ies'} "
                                f"({size:.0f} MB)?\nYour videos and marks are not affected; copies can be rebuilt.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.build_queue.clear()
        if self.build:
            self.build["cancelled"] = True
            self.build["proc"].kill()
        self.player.setSource(QUrl())          # release the file before deleting it
        for f in files:
            f.unlink(missing_ok=True)
        self.load_source(keep_pos=self.cursor_ms)
        self.refresh_library()

    def eventFilter(self, obj, ev):
        """Hold-to-play keys (default Shift+Left/Right): play at 2x while held, stop on release."""
        t = ev.type()
        if t == QEvent.Type.KeyPress:
            k = ev.key()
            if self.hold_key is not None and k == self.hold_key:
                return True                                  # swallow auto-repeat while holding
            if self.hold_bindings and self.vid is not None and not QApplication.activeModalWidget():
                if isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox, QKeySequenceEdit)):
                    return super().eventFilter(obj, ev)      # typing in a text box: not ours
                mods = ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
                combo = QKeySequence(QKeyCombination(mods, Qt.Key(k))).toString(PORTABLE)
                speed = self.hold_bindings.get(combo)
                if speed is not None:
                    self.hold_key = k
                    self.hold_mod_keys = {key for flag, key in (
                        (Qt.KeyboardModifier.ShiftModifier, Qt.Key.Key_Shift),
                        (Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_Control),
                        (Qt.KeyboardModifier.AltModifier, Qt.Key.Key_Alt),
                        (Qt.KeyboardModifier.MetaModifier, Qt.Key.Key_Meta)) if mods & flag}
                    self.set_shuttle(speed)
                    return True
        elif t == QEvent.Type.KeyRelease and self.hold_key is not None and not ev.isAutoRepeat():
            if ev.key() == self.hold_key or ev.key() in self.hold_mod_keys:
                self.hold_key = None
                self.set_shuttle(0)
                return True
        elif t == QEvent.Type.WindowDeactivate and obj is self and self.hold_key is not None:
            self.hold_key = None                             # lost focus: we'd never see the release
            self.set_shuttle(0)
        return super().eventFilter(obj, ev)

    def closeEvent(self, e):
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.build_queue.clear()
        build = self.build        # killing the process runs its finish handler, which clears self.build
        if build:
            build["cancelled"] = True
            build["proc"].kill()
            build["proc"].waitForFinished(2000)
            build["part"].unlink(missing_ok=True)
        super().closeEvent(e)

    # ---- transport ---------------------------------------------------------
    def toggle_play(self):
        if self.vid is None:
            return
        if self.shuttle_rate:
            self.set_shuttle(0)
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("⏸ Pause" if playing else "▶ Play")

    def on_position(self, pos):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self.update_time_label()

    def on_frame(self, frame):
        """Called for every frame shown; remember its real timestamp."""
        if not frame.isValid() or frame.startTime() < 0:
            return
        self.frame_ms = frame.startTime() / 1000.0
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if playing or time.monotonic() - self._last_seek > 0.15:
            self.cursor_ms = self.frame_ms      # nothing pending: trust what is on screen
        self.update_time_label()

    def update_time_label(self):
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        shown = self.player.position() if (playing or self.frame_ms is None) else self.frame_ms
        self.time_label.setText(f"{fmt(shown)} / {fmt(self.player.duration())}")
        self.update_lap_indicator()

    def frame_dur(self):
        return 1000.0 / (self.fps or 30.0)

    def seek(self, ms, bias=0.0):
        """Move to `ms` (clamped); remembers where we are heading. `bias` nudges only the request."""
        d = self.player.duration()
        ms = max(0.0, min(float(ms), d - 1 if d else float(ms)))
        self.cursor_ms = ms
        self._last_seek = time.monotonic()
        self.player.setPosition(int(ms + bias))
        self.update_lap_indicator()

    def on_slider_moved(self, v):
        # coalesce a fast drag into at most one seek every ~25 ms (latest position wins)
        self.set_shuttle(0)
        self.cursor_ms = float(v)
        self._last_seek = time.monotonic()
        self.update_lap_indicator()
        self._pending_seek = v
        if not self.seek_timer.isActive():
            self._flush_seek()
            self.seek_timer.start()

    def _flush_seek(self):
        if self._pending_seek is not None:
            self.player.setPosition(int(self._pending_seek))
            self._pending_seek = None

    def step(self, n):
        """Move n frames (negative = back). Exact on the smooth copy."""
        if self.vid is None:
            return
        self.set_shuttle(0)
        self.player.pause()
        d = self.frame_dur()
        # Seeks land on the frame at or before the request, so ask a quarter frame late. The bias is
        # not stored in the cursor, otherwise it would add up and skip frames when stepping fast.
        self.seek(self.cursor_ms + n * d, bias=0.25 * d)

    def on_wheel(self, notches, shift):
        self.step(notches * (10 if shift else 1))

    def seek_rel(self, delta):
        if self.vid is not None:
            self.set_shuttle(0)
            self.seek(self.cursor_ms + delta)

    def jump_mark(self, direction):
        """Go to the previous (-1) / next (+1) gate mark."""
        if self.vid is None or not self.mark_rows:
            return
        here = self.cursor_ms
        times = [m[1] for m in self.mark_rows]
        cand = [t for t in times if t < here - 100] if direction < 0 else [t for t in times if t > here + 100]
        if cand:
            self.set_shuttle(0)
            self.player.pause()
            self.seek(cand[-1] if direction < 0 else cand[0])

    def refresh_bad_btn(self):
        """Label the Bad lap button with its key; lit orange while the lap in progress is flagged to end bad."""
        pending = (self.vid is not None and self.pending_bad is not None and bool(self.mark_rows)
                   and self.pending_bad == (self.vid, self.mark_rows[-1][1]))
        key = self.key_text("bad_lap")
        if self.compact:
            self.bad_btn.setText(f"✖ BAD ({key})" if pending else f"✖ Bad lap ({key})")
        else:
            self.bad_btn.setText(f"✖ Lap will be BAD ({key})" if pending else f"✖ Bad lap ({key})")
        pad = "2px 8px" if self.compact else "4px 14px"
        self.bad_btn.setStyleSheet(
            f"font-weight:bold; padding:{pad}; background:#ef6c00; color:white; border:1px solid #ef6c00;" if pending
            else f"padding:{pad}; color:#ef6c00; border:1px solid #ef6c00;")

    # ---- undo / redo of mark edits ------------------------------------------
    def mark_snapshot(self):
        return [(m[1], m[2]) for m in self.lib.marks(self.vid)]

    def begin_edit(self, what):
        """Call before changing the open video's marks: remembers how they were so Ctrl+Z can put them back."""
        if self.vid is None:
            return
        self.undo_stack.append((self.vid, what, self.mark_snapshot()))
        del self.undo_stack[:-100]
        self.redo_stack.clear()
        self.refresh_undo_actions()

    def clear_undo(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.refresh_undo_actions()

    def undo(self):
        self.step_history(self.undo_stack, self.redo_stack, "undo")

    def redo(self):
        self.step_history(self.redo_stack, self.undo_stack, "redo")

    def step_history(self, src, dst, verb):
        if self.vid is None or not src or src[-1][0] != self.vid:
            self.statusBar().showMessage(f"Nothing to {verb}", 3000)
            return
        vid, what, snap = src.pop()
        dst.append((vid, what, self.mark_snapshot()))
        self.lib.set_marks(vid, snap)
        self.pending_bad = None
        self.load_marks()
        self.refresh_library()
        self.refresh_undo_actions()
        self.statusBar().showMessage(f"{'Undid' if verb == 'undo' else 'Redid'}: {what}", 4000)

    def refresh_undo_actions(self):
        """Edit menu: name what Undo / Redo would do, with the key."""
        if not hasattr(self, "undo_act"):
            return
        mine = lambda st: st[-1] if st and st[-1][0] == self.vid else None
        for act, st, word, kid in ((self.undo_act, self.undo_stack, "Undo", "undo"),
                                   (self.redo_act, self.redo_stack, "Redo", "redo")):
            top = mine(st)
            act.setEnabled(top is not None)
            act.setText(f"&{word}{': ' + top[1] if top else ''}\t{self.key_text(kid)}")

    def lap_at(self, ms):
        """Table row of the gate that ends the lap containing `ms` (a frame on a gate starts the next lap).
        0: before the first gate; None: after the last gate (the lap in progress, or no marks at all)."""
        return next((i for i, m in enumerate(self.mark_rows) if m[1] > ms + 1), None)

    def update_lap_indicator(self):
        """Show which lap the playhead is in, next to the time, and frame its row in the table."""
        if not hasattr(self, "lap_label"):
            return
        row, bad, text = None, False, ""
        if self.vid is not None:
            n = len(self.mark_rows) - 1                     # finished laps
            i = self.lap_at(self.cursor_ms)
            if not self.mark_rows:
                text = "No gates marked yet"
            elif i == 0:
                text = "Before the start gate"
            elif i is None:
                bad = self.pending_bad == (self.vid, self.mark_rows[-1][1])
                text = f"Lap {n + 1} in progress" + (" · will be BAD" if bad else "")
            else:
                row, bad = i, bool(self.mark_rows[i][2])
                text = f"Lap {i} / {n} · {fmt(self.mark_rows[i][1] - self.mark_rows[i - 1][1])}" + (" · BAD" if bad else "")
        if (text, bad, row) == self._lap_key:
            return
        moved = (self._lap_key or (None, None, None))[2] != row
        self._lap_key = (text, bad, row)
        self.lap_label.setText(text)
        self.lap_label.setStyleSheet("font-weight:bold; color:#ef6c00;" if bad else "font-weight:bold;")
        self.lap_delegate.row = row
        self.table.viewport().update()
        if moved and row is not None and self.table.item(row, 0) is not None:
            self.table.scrollToItem(self.table.item(row, 0), QTableWidget.ScrollHint.EnsureVisible)

    def toggle_bad_lap(self):
        """Flag (or un-flag) the lap the playhead is in as bad. A frame on a gate belongs to the lap starting there."""
        if self.vid is None:
            self.statusBar().showMessage("Open a video first", 3000)
            return
        here = self.cursor_ms
        row = self.lap_at(here)
        if row is None and self.mark_rows:                  # past the last mark: the lap in progress, not ended yet
            last = self.mark_rows[-1][1]
            if self.pending_bad == (self.vid, last):
                self.pending_bad = None
                self.refresh_bad_btn()
                self.update_lap_indicator()
                self.statusBar().showMessage(f"Lap {len(self.mark_rows)} is good again", 4000)
            else:
                self.pending_bad = (self.vid, last)
                self.refresh_bad_btn()
                self.update_lap_indicator()
                self.statusBar().showMessage(f"Lap {len(self.mark_rows)} will be marked bad when you mark the gate "
                                             f"that ends it (press {self.shortcuts['bad_lap'].key().toString()} "
                                             "again to cancel)", 8000)
            return
        if not row:                                         # no marks yet, or before the first one
            self.statusBar().showMessage("The playhead isn't inside a lap yet: mark the start gate first", 4000)
            return
        item = self.table.item(row, self.COL_BAD)
        bad = item.checkState() != Qt.CheckState.Checked
        item.setCheckState(Qt.CheckState.Checked if bad else Qt.CheckState.Unchecked)   # saves and redraws
        self.statusBar().showMessage(f"Lap {row} marked bad" if bad else f"Lap {row} is good again", 3000)

    def apply_speed(self):
        if not self.shuttle_rate:
            self.player.setPlaybackRate(self.speed.currentData())

    def change_speed(self, d):
        i = min(max(self.speed.currentIndex() + d, 0), self.speed.count() - 1)
        self.speed.setCurrentIndex(i)

    # ---- shuttle (variable-speed forward / backward) -------------------------
    def set_shuttle(self, rate):
        """rate > 0: play forward at that speed; rate < 0: step backward; 0: stop (pause)."""
        if self.vid is None and rate:
            return
        self.shuttle_rate = rate
        self.rev_timer.stop()
        if rate > 0:
            self.player.setPlaybackRate(rate)
            self.player.play()
        elif rate < 0:
            self.player.pause()
            self._rev_t = time.monotonic()
            self.rev_timer.start()
        else:
            self.player.pause()
            self.player.setPlaybackRate(self.speed.currentData())
        self.jog_label.setText("Shuttle: stopped" if not rate
                               else f"Shuttle: {'▶▶' if rate > 0 else '◀◀'} {abs(rate):.2g}x")
        if not rate and self.jog.value():
            self.jog.blockSignals(True); self.jog.setValue(0); self.jog.blockSignals(False)

    def on_jog(self, v):
        self.set_shuttle(jog_speed(v))

    def key_shuttle(self, direction):
        """J/L: each press in the same direction doubles the speed (1x, 2x, 4x … 16x)."""
        r = self.shuttle_rate
        rate = direction * (1.0 if r * direction <= 0 else min(abs(r) * 2, 16.0))
        self.set_shuttle(rate)
        self.jog.blockSignals(True)
        self.jog.setValue(0)
        self.jog.blockSignals(False)

    def _rev_tick(self):
        now = time.monotonic()
        dt, self._rev_t = now - self._rev_t, now
        self.seek(self.cursor_ms - abs(self.shuttle_rate) * 1000 * dt)
        if self.cursor_ms <= 0:
            self.set_shuttle(0)

    def mark_time(self):
        """Time of the frame actually on screen (falls back to the player position)."""
        return int(round(self.frame_ms if self.frame_ms is not None else self.player.position()))

    # ---- marks / laps ------------------------------------------------------
    def mark_gate(self):
        if self.vid is None:
            self.statusBar().showMessage("Open a video first", 3000)
            return
        t = self.mark_time()
        if any(abs(t - m[1]) < 300 for m in self.mark_rows):
            self.statusBar().showMessage("There is already a mark at this spot", 3000)
            return
        before = max((m[1] for m in self.mark_rows), default=None)      # the gate this lap started at
        self.begin_edit("mark gate")
        mid = self.lib.add_mark(self.vid, t)
        flagged = self.pending_bad == (self.vid, before) and before is not None and t > before
        self.pending_bad = None
        if flagged:                                         # X was pressed during this lap: it ends bad
            self.lib.set_bad(mid, True)
        self.load_marks()
        self.refresh_library()
        if flagged:
            self.statusBar().showMessage(f"Lap {len(self.mark_rows) - 1} marked bad", 3000)

    def load_marks(self):
        self._building = True
        self.mark_rows = self.lib.marks(self.vid) if self.vid else []
        self.table.setRowCount(len(self.mark_rows))
        laps = [(t - self.mark_rows[i][1], bool(bad)) for i, (_, t, bad) in enumerate(self.mark_rows[1:])]
        n = self.video_run()
        st = lap_stats(laps, n)
        fast_row = st["best"][1] + 1 if st["best"] else None           # lap k sits on row k + 1
        run_rows = range(st["bestn"][1] + 1, st["bestn"][1] + n + 1) if st["bestn"] else range(0)
        times = [m[1] for m in self.mark_rows]
        lap_span = (times[fast_row - 1], times[fast_row]) if fast_row else None
        run_span = (times[run_rows[0] - 1], times[run_rows[-1]]) if run_rows else None
        for i, (mid, t, bad) in enumerate(self.mark_rows):
            lap = t - self.mark_rows[i - 1][1] if i else None
            cells = [QTableWidgetItem("Start" if i == 0 else f"Gate {i}"),
                     QTableWidgetItem(fmt(t)),
                     QTableWidgetItem(fmt(lap) if lap is not None else "")]
            chk = QTableWidgetItem()
            if i:
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked if bad else Qt.CheckState.Unchecked)
            else:
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            cells.append(chk)
            for c, it in enumerate(cells):
                if c != self.COL_BAD:
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if bad and i:
                    it.setForeground(QColor("#9e9e9e"))
                    f = it.font(); f.setStrikeOut(c == self.COL_LAP); it.setFont(f)
                if i == fast_row:
                    gold = QColor(LAP_COLOR); gold.setAlpha(110)
                    it.setBackground(gold)
                    f = it.font(); f.setBold(True); it.setFont(f)
                elif i in run_rows:
                    blue = QColor(RUN_COLOR); blue.setAlpha(70)
                    it.setBackground(blue)
                self.table.setItem(i, c, it)
        self._building = False
        self.slider.set_marks(times, lap_span, run_span)
        self.update_stats(laps)
        if hasattr(self, "bad_btn"):
            self.refresh_bad_btn()
        self._lap_key = None
        self.update_lap_indicator()

    def run_track(self):
        """The track the 'Fastest run' box applies to: the open video's, else the one picked in the library
        filter; '' when neither is a real track (the box is then disabled: the default is fixed at 3)."""
        if self.vid is not None:
            return self.lib.all_meta().get(self.vid, ("", None))[0]
        shown = self.track_filter.currentData()
        return shown if isinstance(shown, str) and shown and shown not in (NEW_TRACK, RENAME_TRACK, DELETE_TRACK) else ""

    def video_run(self, vid=None):
        """Run length in force for a video (its track's own setting, else the default)."""
        vid = self.vid if vid is None else vid
        return run_for(self.lib.run_map(), self.lib.all_meta().get(vid, ("", None))[0])

    def refresh_run_box(self):
        """Show the run length for the current track, and label what the box would change."""
        if not hasattr(self, "run_spin"):
            return
        track = self.run_track()
        rmap = self.lib.run_map()
        self.run_spin.blockSignals(True)
        self.run_spin.setValue(run_for(rmap, track))
        self.run_spin.blockSignals(False)
        self.run_spin.setEnabled(bool(track))               # no track: the default (3) is fixed
        if track:
            own = track in rmap
            self.run_label.setText(f"Fastest run for '{track}':")
            self.run_reset.setVisible(True)
            self.run_reset.setEnabled(own)
            self.run_reset.setToolTip(f"Go back to the default ({run_length()} laps)" if own
                                      else "This track already uses the default")
            self.run_spin.setToolTip(f"How many laps in a row count as the fastest run on the track '{track}' "
                                     f"({MIN_RUN}-{MAX_RUN}). Other tracks are not affected.")
        else:
            self.run_label.setText("Fastest run (default):")
            self.run_reset.setVisible(False)
            self.run_spin.setToolTip(f"Tracks use {run_length()} laps unless you give them their own. "
                                     "Open a video on a track, or pick a track in the library list, to change it.")
        n = self.video_run() if self.vid is not None else run_for(rmap, "")
        self.run_btn.setText(f"🎬 Export fastest {n} video…")
        self.run_btn.setToolTip(f"Save a video of this video's fastest {n} consecutive laps with a lap timer on it")

    def on_run_length_changed(self, n):
        """The 'Fastest run' box: sets the current track's own run length (the default is fixed at 3)."""
        track = self.run_track()
        if track:
            self.lib.set_track_run(track, n)
            self.after_run_change()

    def reset_track_run(self):
        track = self.run_track()
        if track:
            self.lib.set_track_run(track, None)
            self.after_run_change()

    def after_run_change(self):
        if self.vid is not None:
            self.update_stats(self.lib.laps(self.vid))
        self.refresh_library()                     # also refreshes the box and the export button

    def update_stats(self, laps):
        n = self.video_run()
        s = lap_stats(laps, n)
        if not laps:
            self.stats_label.setText("<i>Mark each pass through the start/finish gate. "
                                     "The first mark starts lap 1.</i>")
            return
        best = f"{fmt(s['best'][0])} <small>(lap {s['best'][1] + 1})</small>" if s["best"] else "--"
        if s["bestn"]:
            i = s["bestn"][1]
            bestn = f"{fmt(s['bestn'][0])} <small>(laps {i + 1}–{i + n})</small>"
        else:
            bestn = f"-- <small>(needs {n} good laps in a row)</small>"
        self.stats_label.setText(
            f"<div style='font-size:15px'>Laps: <b>{s['good']}</b> good / {s['total']} total<br>"
            f"Fastest lap: <b>{best}</b><br>Fastest {n} consecutive: <b>{bestn}</b></div>")

    def on_row_clicked(self, row, col):
        if col != self.COL_BAD and 0 <= row < len(self.mark_rows):
            self.set_shuttle(0)
            self.seek(self.mark_rows[row][1])

    def on_item_changed(self, item):
        if self._building or item.column() != self.COL_BAD or item.row() == 0:
            return
        mid = self.mark_rows[item.row()][0]
        self.begin_edit("bad-lap flag")
        self.lib.set_bad(mid, item.checkState() == Qt.CheckState.Checked)
        row = item.row()
        self.load_marks()
        self.table.selectRow(row)
        self.refresh_library()

    def selected_row(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def delete_selected_mark(self):
        r = self.selected_row()
        if r is None:
            self.statusBar().showMessage("Click a gate in the table first, then delete it", 4000)
            return
        self.begin_edit("delete gate")
        self.lib.delete_mark(self.mark_rows[r][0])
        self.load_marks()
        if self.mark_rows:                                   # keep a row selected so you can delete several in a row
            self.table.selectRow(min(r, len(self.mark_rows) - 1))
        self.refresh_library()
        self.statusBar().showMessage("Mark deleted", 3000)

    def delete_key(self):
        """The Delete key only deletes while the table has focus (so it can't fire by accident)."""
        if self.table.hasFocus():
            self.delete_selected_mark()

    def move_selected_mark(self):
        r = self.selected_row()
        if r is None:
            self.statusBar().showMessage("Select a mark in the table first", 3000)
            return
        mid, old_t = self.mark_rows[r][0], self.mark_rows[r][1]
        t = self.mark_time()
        if abs(t - old_t) < 1:
            self.statusBar().showMessage(
                "The video is already at this mark - scrub to the new spot, then press Move mark here", 5000)
            return
        self.begin_edit("move gate")
        self.lib.move_mark(mid, t)
        self.load_marks()
        new_row = next((i for i, m in enumerate(self.mark_rows) if m[0] == mid), None)
        if new_row is not None:                              # marks re-sort by time, so follow the moved one
            self.table.selectRow(new_row)
        self.refresh_library()
        self.statusBar().showMessage(f"Mark moved to {fmt(t)}", 3000)

    def export_bestn_video(self):
        if self.vid is None:
            self.statusBar().showMessage("Open a video first", 3000)
            return
        n = self.video_run()
        win = best_run_window(self.lib, {self.vid}, n)
        if not win:
            QMessageBox.information(self, "Nothing to export",
                                    f"This video needs {n} good laps in a row (bad laps break the run) first.")
            return
        ExportDialog(win, self).exec()

    def open_compare(self):
        track = None
        if self.vid is not None:
            track = self.lib.all_meta().get(self.vid, ("", None))[0]
        else:
            shown = self.track_filter.currentData()
            track = shown if isinstance(shown, str) and shown not in (NEW_TRACK, RENAME_TRACK, DELETE_TRACK) else None
        self.set_shuttle(0)
        self.player.pause()
        CompareDialog(self, track).exec()

    def export_chosen_laps(self):
        """Export laps A to B of the open video, picked by the user, with the same export window as the fastest run."""
        if self.vid is None or len(self.mark_rows) < 2:
            self.statusBar().showMessage("Mark at least two gates (one lap) in the open video first", 4000)
            return
        marks = self.mark_rows
        lap_ms = [marks[i][1] - marks[i - 1][1] for i in range(1, len(marks))]
        row = self.lap_at(self.cursor_ms)                    # start on the lap at the playhead (else the last one)
        dlg = LapRangeDialog(lap_ms, [bool(m[2]) for m in marks[1:]], row if row else len(lap_ms), self)
        if not dlg.exec():
            return
        a, b = dlg.values()
        ms = [marks[k][1] for k in range(a - 1, b + 1)]
        meta = self.lib.all_meta().get(self.vid, ("", None))
        d = effective_date(self.cur_path, meta[1])[0]
        win = {"vid": self.vid, "path": self.cur_path, "track": meta[0], "date": d.isoformat() if d else "",
               "marks_ms": ms, "lap_ms": [ms[k + 1] - ms[k] for k in range(len(ms) - 1)], "total_ms": ms[-1] - ms[0],
               "custom": True, "first_lap": a, "laps_in_video": len(lap_ms)}
        ExportDialog(win, self).exec()

    def show_chart(self):
        if self.vid is None:
            return
        name = vname(self.cur_path) if self.cur_path else ""
        ChartDialog(self.lib.laps(self.vid), name, self, n=self.video_run()).exec()

    def export_current(self):
        if self.vid is None:
            return
        default = str(Path.home() / (vname(self.cur_path) + ".csv")) if self.cur_path else str(Path.home() / "laps.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", default, "CSV (*.csv)")
        if path:
            export_csv(self.lib, {self.vid}, path)
            self.statusBar().showMessage(f"Exported to {path}", 5000)

    # ---- drag & drop -------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths, folders = [], []
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_dir():
                folders.append(p)
                paths += [q for q in p.rglob("*")]
            else:
                paths.append(p)
        if folders:
            self.add_with_preview(paths, root=folders[0].parent if len(folders) > 1 else folders[0],
                                  what="in the dropped folder(s)")
        else:
            self.add_paths(paths)


def install_error_handler(get_window):
    """PyQt aborts the whole program when a slot raises. Log the error instead and keep the app open."""
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(text)
        try:
            log = DB_PATH.parent / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n")
            win = get_window()
            if win:
                win.statusBar().showMessage(f"Internal error: {exc} (details in {log})", 20000)
        except Exception:
            pass

    sys.excepthook = hook


def app_icon():
    """The window / title-bar / taskbar icon: fpv-dvr-lap-timer.ico, bundled next to the script or inside the
    PyInstaller exe (sys._MEIPASS). An empty QIcon if it can't be found."""
    for root in (Path(getattr(sys, "_MEIPASS", "") or "."), Path(__file__).resolve().parent):
        for name in ("fpv-dvr-lap-timer.ico", "fpv-dvr-lap-timer.svg"):
            path = root / name
            if path.is_file():
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
    return QIcon()


def main():
    if sys.platform == "win32":
        try:        # own taskbar identity, so the taskbar groups/shows this app's icon even when run via Python
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LenuxFPV.FPVDVRLapTimer")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    win = MainWindow()
    install_error_handler(lambda: win)
    win.show()
    QTimer.singleShot(600, win.offer_ffmpeg_on_start)      # after the window is up
    # allow: lap_timer.py video1.ts video2.mp4
    if len(sys.argv) > 1:
        win.open_paths(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
