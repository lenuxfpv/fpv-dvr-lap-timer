#!/usr/bin/env python3
"""FPV Lap Timer - review drone footage, mark gate passes, track lap times.

Videos (.ts / .mp4 / others) stay where they are. The app remembers their paths
and your gate marks in a single SQLite database in your user data folder.
"""
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtCore import QDate, QEvent, QProcess, QSize, QRectF, QSettings, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDateEdit, QDoubleSpinBox, QFormLayout, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSlider, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionSlider, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

VERSION = "1.1.2"
VIDEO_EXTS = {".ts", ".mp4", ".mov", ".mkv", ".m4v", ".avi"}


def data_dir():
    """Per-user folder for the library database, following each OS's convention."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fpv-lap-timer"


def cache_dir():
    """Where the optional smooth-scrub copies live (safe to delete at any time)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "fpv-lap-timer" / "smooth"


def proxy_path(src):
    """Cache file for a video's smooth-scrub copy.

    Matched by file name + size + modified time, not folder, so moving or renaming the folder (or
    using Fix paths) keeps the copy. The key changes if the file itself is changed."""
    try:
        st = os.stat(src)
    except OSError:
        return cache_dir() / (hashlib.sha1(f"{src}".encode()).hexdigest()[:20] + ".mp4")
    key = f"{vname(src)}|{st.st_size}|{st.st_mtime_ns}"
    path = cache_dir() / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".mp4")
    if not path.exists():
        # adopt a copy built by an earlier version, which keyed on the full path
        legacy = cache_dir() / (hashlib.sha1(f"{src}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20] + ".mp4")
        if legacy.exists():
            try:
                legacy.rename(path)
            except OSError:
                return legacy
    return path


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
    return shutil.which(name)


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


def lap_stats(laps):
    """laps: list of (lap_ms, bad) in order.

    Returns dict: best (ms, index) and best3 (total_ms, start_index).
    Bad laps are excluded; a bad lap also breaks a run of consecutive laps.
    """
    best = None
    for i, (ms, bad) in enumerate(laps):
        if not bad and (best is None or ms < best[0]):
            best = (ms, i)
    best3 = None
    for i in range(len(laps) - 2):
        window = laps[i:i + 3]
        if any(bad for _, bad in window):
            continue
        total = sum(ms for ms, _ in window)
        if best3 is None or total < best3[0]:
            best3 = (total, i)
    goods = [ms for ms, bad in laps if not bad]
    return {"best": best, "best3": best3, "avg": sum(goods) / len(goods) if goods else None,
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
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(videos)")}
        if "track" not in cols:
            self.db.execute("ALTER TABLE videos ADD COLUMN track TEXT NOT NULL DEFAULT ''")
        if "session_date" not in cols:      # ISO yyyy-mm-dd, NULL = use the file's date
            self.db.execute("ALTER TABLE videos ADD COLUMN session_date TEXT")
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

    def tracks(self):
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT track FROM videos WHERE track<>'' ORDER BY track COLLATE NOCASE")]

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

    def add_mark(self, vid, t):
        self.db.execute("INSERT INTO marks(video_id, time_ms) VALUES(?,?)", (vid, int(t)))
        self.db.commit()

    def delete_mark(self, mid):
        self.db.execute("DELETE FROM marks WHERE id=?", (mid,))
        self.db.commit()

    def move_mark(self, mid, t):
        self.db.execute("UPDATE marks SET time_ms=? WHERE id=?", (int(t), mid))
        self.db.commit()

    def set_bad(self, mid, bad):
        self.db.execute("UPDATE marks SET bad_lap=? WHERE id=?", (1 if bad else 0, mid))
        self.db.commit()

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
                  ("session_date" if "session_date" in cols else "NULL")
            theirs = src.execute(f"SELECT {sel} FROM videos").fetchall()
            for sid, vpath, fps, track, sdate in theirs:
                row = self.db.execute("SELECT id, track, session_date FROM videos WHERE path=?", (vpath,)).fetchone()
                if row:
                    vid = row[0]
                    stats["videos_matched"] += 1
                    if not row[1] and track:
                        self.db.execute("UPDATE videos SET track=? WHERE id=?", (track, vid))
                    if not row[2] and sdate:
                        self.db.execute("UPDATE videos SET session_date=? WHERE id=?", (sdate, vid))
                else:
                    vid = self.db.execute("INSERT INTO videos(path, fps, track, session_date) VALUES(?,?,?,?)",
                                          (vpath, fps or 30, track or "", sdate)).lastrowid
                    stats["videos_added"] += 1
                have = {r[0] for r in self.db.execute("SELECT time_ms FROM marks WHERE video_id=?", (vid,))}
                for t, bad in src.execute("SELECT time_ms, bad_lap FROM marks WHERE video_id=? ORDER BY time_ms", (sid,)):
                    if t not in have:
                        self.db.execute("INSERT INTO marks(video_id, time_ms, bad_lap) VALUES(?,?,?)", (vid, t, bad))
                        have.add(t)
                        stats["marks_added"] += 1
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            src.close()
        return stats


def export_csv(lib, video_ids, path):
    """Write one row per lap for the given videos."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["track", "session_date", "video", "lap", "start_s", "end_s",
                    "lap_time_s", "lap_time", "bad_lap"])
        meta = lib.all_meta()
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
                            f"{lap/1000:.3f}", fmt(lap), "yes" if m[i][2] else "no"])


# ----------------------------------------------------------------------------
# Export the fastest 3 consecutive laps as a video (with lap timer overlay)
# ----------------------------------------------------------------------------
EXPORT_LEAD_MS = 1000     # footage shown before the first gate
EXPORT_TAIL_MS = 2000     # footage held after the third lap, with the final total on screen
EXPORT_SEEK_MARGIN_MS = 15000   # how far before the clip we seek (the exact cut is made with trim)


def best_three_window(lib, video_ids):
    """Fastest 3 consecutive good laps across the given videos (a window never spans two videos).

    -> dict(vid, path, track, date, marks_ms=[m0..m3], lap_ms=[l1,l2,l3], total_ms) or None."""
    meta = lib.all_meta()
    paths = {v: p for v, p, _ in lib.videos()}
    best = None
    for vid in video_ids:
        marks = lib.marks(vid)
        laps = [(marks[i][1] - marks[i - 1][1], bool(marks[i][2])) for i in range(1, len(marks))]
        b3 = lap_stats(laps)["best3"]
        if b3 and (best is None or b3[0] < best["total_ms"]):
            i = b3[1]
            ms = [marks[i + k][1] for k in range(4)]
            best = {"vid": vid, "path": paths[vid], "track": meta[vid][0], "date": meta[vid][1],
                    "marks_ms": ms, "lap_ms": [ms[k + 1] - ms[k] for k in range(3)], "total_ms": b3[0]}
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
            "copies and for exporting the fastest-3 video.\n\n" + fix)


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


def build_export_args(win, src, out, font, info, legacy_sync=False, tail_ms=EXPORT_TAIL_MS):
    """ffmpeg argv that cuts the fastest-3 window from `src` and draws the lap timer overlay.

    Returns (argv, clip_ms): the argument list and the length of the output clip."""
    m = win["marks_ms"]
    lead = min(EXPORT_LEAD_MS, m[0])
    tail = tail_ms
    if info.get("duration_ms"):
        tail = max(0, min(tail, int(info["duration_ms"] - m[3])))
    c0 = m[0] - lead                                       # clip start in the source (ms)
    clip_ms = (m[3] + tail) - c0
    s = [(x - c0) / 1000.0 for x in m]                     # gate times from the clip start, seconds
    eps = 0.0005                                           # a frame within 0.5 ms of a gate belongs to the next lap
    W, H = info["width"], info["height"]
    fs_big, fs_med, fs_small = round(H * 0.080), round(H * 0.043), round(H * 0.032)
    mar = round(W * 0.025)
    ff = font.replace("\\", "/").replace(":", "\\:")

    def dt(text, x, y, size, color="white", enable=None, box=0.55):
        if "%{" not in text:                               # plain text: a literal ':' must be escaped too
            text = text.replace(":", "\\:")
        opts = [f"fontfile='{ff}'", f"text='{text}'", f"fontsize={size}", f"fontcolor={color}",
                f"x={x}", f"y={y}", "box=1", f"boxcolor=black@{box}", f"boxborderw={max(4, size // 4)}"]
        if enable:
            opts.append(f"enable='{enable}'")
        return "drawtext=" + ":".join(opts)

    def running(start):                                    # m:ss.mmm of the time since `start`, live
        x = f"(max(t-{start + 0.0006:.6f}\\,0)+0.0004)"      # marks are rounded to 1 ms: the gate frame reads 0:00.000
        return (f"%{{eif\\:floor({x}/60)\\:d\\:1}}\\:%{{eif\\:mod(floor({x})\\,60)\\:d\\:2}}"
                f".%{{eif\\:mod(floor({x}*1000)\\,1000)\\:d\\:3}}")

    y_lbl, y_big = mar, mar + round(fs_med * 1.7)
    y_list = y_big + round(fs_big * 1.75)
    parts = []
    for k in range(3):                                     # lap k+1 running, from its gate to the next gate
        a = -1 if k == 0 else s[k] - eps                   # (lap 1 also covers the lead-in)
        b = s[k + 1] - eps
        en = f"gte(t,{a:.6f})*lt(t,{b:.6f})"
        parts.append(dt(f"LAP {k + 1} / 3", mar, y_lbl, fs_med, enable=en))
        parts.append(dt(running(s[k]), mar, y_big, fs_big, enable=en))
    en_end = f"gte(t,{s[3] - eps:.6f})"                    # after the last gate: hold lap 3
    parts.append(dt("LAP 3 / 3", mar, y_lbl, fs_med, enable=en_end))
    parts.append(dt(fmt(win["lap_ms"][2]), mar, y_big, fs_big, enable=en_end))
    for k in range(3):                                     # finished laps list, one line appears per lap
        parts.append(dt(f"LAP {k + 1}   {fmt(win['lap_ms'][k])}", mar, y_list + round(k * fs_med * 1.6), fs_med,
                        enable=f"gte(t,{s[k + 1] - eps:.6f})"))
    # top right: track name, then the running 3-lap total
    label = _filter_text(" \u00b7 ".join(x for x in (win.get("track") or "", win.get("date") or "") if x))
    if label:
        parts.append(dt(label, "w-text_w-%d" % mar, y_lbl, fs_small))
    y_tot = y_lbl + round(fs_small * 1.9) if label else y_lbl
    parts.append(dt("3 LAPS", "w-text_w-%d" % mar, y_tot, fs_med))
    parts.append(dt(running(s[0]), "w-text_w-%d" % mar, y_tot + round(fs_med * 1.7), fs_big,
                    enable=f"lt(t,{s[3] - eps:.6f})"))
    parts.append(dt(fmt(win["total_ms"]), "w-text_w-%d" % mar, y_tot + round(fs_med * 1.7), fs_big,
                    color="#FFD54F", enable=en_end))
    parts.append(dt(f"FASTEST 3 CONSECUTIVE LAPS   {fmt(win['total_ms'])}", "(w-text_w)/2", f"h-text_h-{mar}",
                    round(fs_med * 1.25), color="#FFD54F", enable=en_end, box=0.65))

    matrix = {"bt470bg": "bt470", "smpte170m": "smpte170m", "bt709": "bt709", "bt2020nc": "bt2020",
              "bt2020c": "bt2020"}.get(info.get("matrix") or "", "bt709" if H >= 720 else "smpte170m")
    convert = (f"scale=in_range={'pc' if info.get('full_range') else 'tv'}:out_range=tv:"
               f"in_color_matrix={matrix}:out_color_matrix=bt709,format=yuv420p")
    # Seeking inside .ts files can land a keyframe late, so only seek roughly (15 s early, always before the
    # clip) and cut to the exact frame with trim; setpts then makes the overlay clock start at 0.
    seek_ms = max(0, c0 - EXPORT_SEEK_MARGIN_MS)
    skip = (c0 - seek_ms) / 1000.0
    cut = f"trim=start={skip:.6f}:duration={clip_ms / 1000:.6f},setpts=PTS-STARTPTS"
    tags = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
    vf = ",".join([cut, convert] + parts + [tags, "format=yuv420p"])
    audio = (["-af", f"atrim=start={skip:.6f}:duration={clip_ms / 1000:.6f},asetpts=PTS-STARTPTS"]
             if info.get("audio") else [])
    argv = ["-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-ss", f"{seek_ms / 1000:.3f}", "-i", str(src), "-t", f"{clip_ms / 1000 + 0.5:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, *audio,
            *(SYNC_FLAGS_OLD if legacy_sync else SYNC_FLAGS),
            "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    return argv, clip_ms


class ExportDialog(QDialog):
    """Shows what will be exported, asks where to save it, then runs ffmpeg with a progress bar."""
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setWindowTitle("Export fastest 3 laps as video")
        self.resize(640, 300)
        self.proc = None
        self.final = None
        self.part = None
        self.legacy = False
        self.clip_ms = 1
        self.buf = ""
        self.cancelled = False

        laps = "<br>".join(f"Lap {i + 1}: <b>{fmt(ms)}</b>" for i, ms in enumerate(win["lap_ms"]))
        track = win["track"] or "No track"
        summary = QLabel(f"<b>{track}</b> &nbsp;·&nbsp; {vname(win['path'])}<br><br>{laps}<br><br>"
                         f"Fastest 3 consecutive: <b>{fmt(win['total_ms'])}</b>")
        summary.setTextFormat(Qt.TextFormat.RichText)
        default_dir = Path.home() / "Videos"
        name = f"{track if win['track'] else Path(vname(win['path'])).stem} fastest 3 {fmt(win['total_ms'])}.mp4"
        name = re.sub(r'[\\/:*?"<>|]', "-", name)
        self.path_edit = QLineEdit(str((default_dir if default_dir.is_dir() else Path.home()) / name))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.hide()
        self.status = QLabel("Re-encodes the original footage (not the smooth copy) as H.264 MP4 at its original "
                             "size and frame rate, with the lap timer drawn on it.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:gray")
        self.settings = QSettings("fpv-lap-timer", "app")
        self.tail_spin = QDoubleSpinBox()
        self.tail_spin.setRange(0, 10)
        self.tail_spin.setSingleStep(0.5)
        self.tail_spin.setDecimals(1)
        self.tail_spin.setSuffix(" s")
        try:
            tail_s = float(self.settings.value("export_tail_s", EXPORT_TAIL_MS / 1000))
        except (TypeError, ValueError):
            tail_s = EXPORT_TAIL_MS / 1000
        self.tail_spin.setValue(tail_s)
        self.tail_spin.setToolTip("Footage kept after the third lap, with the final total on screen")
        self.go = QPushButton("Export")
        self.go.setDefault(True)
        self.go.clicked.connect(self.start)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.on_cancel)

        lay = QVBoxLayout(self)
        lay.addWidget(summary)
        row = QHBoxLayout(); row.addWidget(QLabel("Save as:")); row.addWidget(self.path_edit, 1); row.addWidget(browse)
        lay.addLayout(row)
        trow = QHBoxLayout(); trow.addWidget(QLabel("End padding:")); trow.addWidget(self.tail_spin); trow.addStretch(1)
        lay.addLayout(trow)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        btns = QHBoxLayout(); btns.addStretch(1); btns.addWidget(self.cancel); btns.addWidget(self.go)
        lay.addLayout(btns)

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
        if not font:
            QMessageBox.warning(self, "Font needed", "No bold font file was found for the timer overlay.")
            return
        if not os.path.exists(self.win["path"]):
            QMessageBox.warning(self, "Video not found", f"The source video can't be found:\n{self.win['path']}")
            return
        self.final = Path(self.path_edit.text().strip())
        if not self.final.name:
            return
        if self.final.exists() and QMessageBox.question(
                self, "Replace file?", f"{self.final.name} already exists. Replace it?") != QMessageBox.StandardButton.Yes:
            return
        self.part = self.final.with_name(self.final.stem + ".part.mp4")
        self.settings.setValue("export_tail_s", self.tail_spin.value())
        self.font, self.info = font, probe_stream(self.win["path"])
        self.run_ffmpeg(ffmpeg)

    def run_ffmpeg(self, ffmpeg):
        argv, self.clip_ms = build_export_args(self.win, self.win["path"], self.part, self.font, self.info,
                                               legacy_sync=self.legacy,
                                               tail_ms=int(self.tail_spin.value() * 1000))
        self.buf = ""
        self.proc = QProcess(self)
        hide_console(self.proc)
        self.proc.setProgram(ffmpeg)
        self.proc.setArguments(argv)
        self.proc.readyReadStandardOutput.connect(self.on_progress)
        self.proc.finished.connect(self.on_done)
        self.go.setEnabled(False)
        self.path_edit.setEnabled(False)
        self.tail_spin.setEnabled(False)
        self.bar.setValue(0); self.bar.show()
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
        else:
            self.status.setText(f"Export failed: {err[-300:] or 'ffmpeg exited with code ' + str(code)}")
        self.go.setEnabled(True)
        self.path_edit.setEnabled(True)
        self.tail_spin.setEnabled(True)
        self.bar.hide()

    def kill_job(self):
        if self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.cancelled = True
            self.proc.kill()
            self.proc.waitForFinished(3000)
            if self.part:
                self.part.unlink(missing_ok=True)

    def on_cancel(self):
        self.kill_job()
        self.reject()

    def closeEvent(self, e):
        self.kill_job()
        super().closeEvent(e)


class LapChart(QWidget):
    """Bar chart of lap times: bad laps grey, fastest lap green, best-3 run outlined."""
    def __init__(self, laps, title="", parent=None):
        super().__init__(parent)
        self.laps, self.title = laps, title
        self.stats = lap_stats(laps)
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
        best = self.stats["best"]; b3 = self.stats["best3"]
        if b3:
            i = b3[1]
            r = QRectF(area.left() + i * slot + 2, area.top(), slot * 3 - 4, area.height())
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
                   "Lap   ·   green = fastest   grey = bad   dashed blue = fastest 3 consecutive")

    def mouseMoveEvent(self, e):
        for r, tip in self._bars:
            if r.contains(e.position()):
                self.setToolTip(tip); return
        self.setToolTip("")


class ChartDialog(QDialog):
    def __init__(self, laps, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lap times")
        self.resize(820, 440)
        lay = QVBoxLayout(self)
        lay.addWidget(LapChart(laps, title))


class ProgressChart(QWidget):
    """Line chart across the library: one point per video, oldest recording first."""
    def __init__(self, points, title="Progress across library (per video)", parent=None):
        super().__init__(parent)
        self.title = title
        # points: [(label, tooltip_name, best_ms|None, best3_avg_ms|None)]
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
        series = [(2, QColor("#2e7d32"), "Fastest lap"), (3, QColor("#1976d2"), "Best 3 consecutive (avg per lap)")]
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
        rows = []
        for vid, path, _ in lib.videos():
            if vid not in ids:
                continue
            s = lap_stats(lib.laps(vid))
            if s["best"]:
                rows.append((effective_date(path, meta[vid][1])[0], path, s))
        # oldest first; videos with no known date sort last, by name
        rows.sort(key=lambda r: (r[0] is None, r[0] or datetime.min.date(), r[1].lower()))
        points = [(d.isoformat() if d else "?", vname(path),
                   s["best"][0], s["best3"][0] / 3 if s["best3"] else None) for d, path, s in rows]
        lay = QVBoxLayout(self)
        lay.addWidget(ProgressChart(points, f"Progress - {track_title(track)}"))
        note = QLabel("Videos are ordered by session date (set manually, else file date). Hover a point for the "
                      "video name. Bad laps are excluded; larger dots mark the best in this view.")
        note.setStyleSheet("color:gray")
        note.setWordWrap(True)
        lay.addWidget(note)


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
        form.addRow(QLabel(f"{len(vids)} video(s) selected" if multi else vname(paths[first])))
        form.addRow("Track:", self.track)
        form.addRow("Session date:", self.mode)
        form.addRow("", self.date)
        ok, cancel = QPushButton("Save"), QPushButton("Cancel")
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(cancel); row.addWidget(ok)
        form.addRow(row)

    def values(self):
        text = self.track.currentText().strip()
        track = KEEP if (self.mixed_track and not text) else text
        m = self.mode.currentIndex()
        date = KEEP if m == 0 else None if m == 1 else self.date.date().toString("yyyy-MM-dd")
        return track, date


# ----------------------------------------------------------------------------
# Stats dialog
# ----------------------------------------------------------------------------
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

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Video", "Track", "Session date", "Laps (good/total)",
                                              "Fastest lap", "Fastest 3 laps", "Avg good lap"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 3)

        lay.addWidget(QLabel("<b>By track</b> (all videos, ignoring the filter above)"))
        self.by_track = QTableWidget(0, 6)
        self.by_track.setHorizontalHeaderLabels(["Track", "Videos", "Laps (good/total)", "Fastest lap",
                                                 "Fastest 3 laps", "Avg good lap"])
        self.by_track.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.by_track, 2)
        for t in (self.table, self.by_track):
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.verticalHeader().setVisible(False)

        prog = QPushButton("📈 Progress chart (this track)")
        prog.clicked.connect(lambda: ProgressDialog(lib, self.combo.currentData(), self).exec())
        exp = QPushButton("Export CSV (this track)…")
        exp.clicked.connect(self.export_selection)
        vid_btn = QPushButton("🎬 Export fastest 3 video (this track)…")
        vid_btn.clicked.connect(self.export_video)
        row = QHBoxLayout(); row.addWidget(prog); row.addWidget(exp)
        lay.addLayout(row)
        lay.addWidget(vid_btn)
        self.rebuild()

    @staticmethod
    def summarise(entries):
        """entries: [(name, laps_list)] -> aggregate stats across those videos."""
        best = best3 = None
        good = total = 0
        goods = []
        for name, laps in entries:
            s = lap_stats(laps)
            good += s["good"]; total += s["total"]
            goods += [ms for ms, bad in laps if not bad]
            if s["best"] and (best is None or s["best"][0] < best[0]):
                best = (s["best"][0], name)
            if s["best3"] and (best3 is None or s["best3"][0] < best3[0]):
                best3 = (s["best3"][0], name)
        return {"best": best, "best3": best3, "good": good, "total": total,
                "avg": sum(goods) / len(goods) if goods else None}

    def rebuild(self):
        lib, track = self.lib, self.combo.currentData()
        meta, ids = lib.all_meta(), lib.video_ids(track)
        rows, groups = [], {}
        for vid, path, _ in lib.videos():
            name, laps = vname(path), lib.laps(vid)
            t, sdate = meta[vid]
            d, manual = effective_date(path, sdate)
            groups.setdefault(t, []).append((name, laps))
            if vid in ids:
                rows.append((d, name, t, manual, laps))
        rows.sort(key=lambda r: (r[0] is None, r[0] or datetime.min.date(), r[1].lower()))

        extras = sum(len([v for v in g if v in ids]) - 1 for g in lib.duplicate_groups()
                     if len([v for v in g if v in ids]) > 1)
        self.dup_label.setText(
            f"⚠ {extras} video(s) here are copies of another entry in the library (same file), so their laps "
            "are counted twice. Remove the extra entries from the library." if extras else "")
        self.dup_label.setVisible(bool(extras))

        agg = self.summarise([(n, l) for _, n, _, _, l in rows])
        self.head.setText(
            f"<h3>{track_title(track)}</h3>"
            f"<h3>Fastest lap: {fmt(agg['best'][0]) if agg['best'] else '--'}"
            f"{' &nbsp;(' + agg['best'][1] + ')' if agg['best'] else ''}<br>"
            f"Fastest 3 consecutive laps: {fmt(agg['best3'][0]) if agg['best3'] else '--'}"
            f"{' &nbsp;(' + agg['best3'][1] + ')' if agg['best3'] else ''}</h3>")

        self.table.setRowCount(len(rows))
        for r, (d, name, t, manual, laps) in enumerate(rows):
            s = lap_stats(laps)
            ds = (d.isoformat() + ("" if manual else " (file)")) if d else "--"
            vals = [name, t or "--", ds, f"{s['good']}/{s['total']}",
                    fmt(s["best"][0]) if s["best"] else "--",
                    fmt(s["best3"][0]) if s["best3"] else "--",
                    fmt(s["avg"]) if s["avg"] else "--"]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))

        self.by_track.setRowCount(len(groups))
        for r, t in enumerate(sorted(groups, key=lambda t: (t == "", t.lower()))):
            a = self.summarise(groups[t])
            vals = [t or "(no track)", str(len(groups[t])), f"{a['good']}/{a['total']}",
                    fmt(a["best"][0]) if a["best"] else "--",
                    fmt(a["best3"][0]) if a["best3"] else "--",
                    fmt(a["avg"]) if a["avg"] else "--"]
            for c, v in enumerate(vals):
                self.by_track.setItem(r, c, QTableWidgetItem(v))

    def export_video(self):
        win = best_three_window(self.lib, self.lib.video_ids(self.combo.currentData()))
        if not win:
            QMessageBox.information(self, "Nothing to export", "No video here has three good laps in a row yet.")
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


class MarkSlider(QSlider):
    """Seek bar that jumps to where you click and draws a tick for every gate mark."""
    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.marks = []

    def set_marks(self, times):
        self.marks = list(times)
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

DEFAULT_MARK_KEY = "Z"


class HotkeyDialog(QDialog):
    """Pick the key that marks a gate pass. Warns if it is already used for something else."""
    def __init__(self, current, taken, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Mark gate hotkey")
        self.taken = taken                       # {QKeySequence portable string: what it does}
        self.edit = QKeySequenceEdit(QKeySequence(current))
        self.edit.setMaximumSequenceLength(1)    # one key (with optional Ctrl/Shift/Alt), not a chord
        self.msg = QLabel()
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color:#ef6c00")
        reset = QPushButton(f"Reset to {DEFAULT_MARK_KEY}")
        reset.clicked.connect(lambda: self.edit.setKeySequence(QKeySequence(DEFAULT_MARK_KEY)))
        ok, cancel = QPushButton("Save"), QPushButton("Cancel")
        ok.setDefault(True)
        ok.clicked.connect(self.try_accept)
        cancel.clicked.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Click the box, then press the key you want to use for <b>Mark gate</b>:"))
        lay.addWidget(self.edit)
        lay.addWidget(self.msg)
        row = QHBoxLayout(); row.addWidget(reset); row.addStretch(1); row.addWidget(cancel); row.addWidget(ok)
        lay.addLayout(row)

    def key(self):
        return self.edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    def try_accept(self):
        key = self.key()
        if not key:
            self.msg.setText("Press a key first.")
        elif key in self.taken:
            self.msg.setText(f"'{key}' is already used for: {self.taken[key]}. Pick another key.")
        else:
            self.accept()


ROLE_INFO = Qt.ItemDataRole.UserRole + 1     # dict describing a library entry, painted by LibraryDelegate


class LibraryDelegate(QStyledItemDelegate):
    """Library entry: big track name, a LAPS / FASTEST / FASTEST 3 row, then a small file name line."""
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
        badges = ("⚡ " if info["ready"] else "") + ("⚠ duplicate" if info["dup"] else "") + \
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
        colw = w / 3
        cols = (("LAPS", f"{info['good']}/{info['total']}" if info["total"] else "-"),
                ("FASTEST", fmt(info["best"]) if info["best"] is not None else "-"),
                ("FASTEST 3", fmt(info["best3"]) if info["best3"] is not None else "-"))
        for i, (cap, val) in enumerate(cols):
            cx = x0 + i * colw
            p.setFont(font(0.72)); p.setPen(muted)
            p.drawText(QRectF(cx, y + 24, colw, 13), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, cap)
            p.setFont(font(1.05, bold=True)); p.setPen(fg if val != "-" else muted)
            p.drawText(QRectF(cx, y + 36, colw, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val)

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


class MainWindow(QMainWindow):
    COL_GATE, COL_TIME, COL_LAP, COL_BAD = range(4)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FPV Lap Timer {VERSION}")
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
        self.settings = QSettings("fpv-lap-timer", "app")
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
        self.track_filter.currentIndexChanged.connect(lambda: self.refresh_library())
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
        details = QPushButton("Track / date…")
        details.setToolTip("Set the track name and session date of the selected video(s)")
        details.clicked.connect(self.edit_details)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("<b>Video library</b>"))
        ll.addWidget(self.track_filter)
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
        self.mark_btn.setToolTip("Mark a gate pass on the frame shown. Change the key under View → Mark gate hotkey…")
        self.mark_btn.setStyleSheet("font-weight:bold; padding:8px 14px; background:#2e7d32; color:white;")
        self.mark_btn.clicked.connect(self.mark_gate)

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
        for w in (self.play_btn, back, fwd, prev_gate, next_gate, QLabel("Speed:"), self.speed, self.mute):
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
        self.title_label = QLabel("Add a video to the library and double-click it to start.")
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.addWidget(self.title_label)
        cl.addWidget(self.video_widget, 1)
        cl.addWidget(self.slider)
        cl.addWidget(self.time_label)
        cl.addLayout(jogrow)
        cl.addLayout(ctl)
        cl.addLayout(proxyrow)
        self.hint = hint = QLabel()
        self._hint_tail = ("←/→ frame (hold to repeat) · wheel on video = frame (Shift ×10) · "
                      "hold Shift+←/→ = 2x play · Ctrl+←/→ ±5s · J/K/L shuttle · PgUp/PgDn prev/next gate · [ ] speed")
        hint.setStyleSheet("color:gray")
        hint.setWordWrap(True)
        cl.addWidget(hint)

        # --- laps panel
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Gate", "Video time", "Lap time", "Bad lap"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
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
        vid_btn = QPushButton("🎬 Export fastest 3 video…")
        vid_btn.setToolTip("Save a video of this video's fastest 3 consecutive laps with a lap timer on it")
        vid_btn.clicked.connect(self.export_best3_video)
        r3 = QHBoxLayout(); r3.addWidget(chart_btn); r3.addWidget(csv_btn)
        rl.addLayout(r3)
        rl.addWidget(vid_btn)
        rl.addWidget(self.stats_label)

        split = QSplitter()
        split.addWidget(left); split.addWidget(center); split.addWidget(right)
        split.setSizes([310, 740, 360])
        self.setCentralWidget(split)

        # --- shortcuts
        self.key_uses = {}                  # portable key string -> description, for conflict warnings
        def sc(key, fn, what=None):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.ShortcutContext.ApplicationShortcut)
            s.activated.connect(fn)
            if what:
                self.key_uses[QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)] = what
            return s
        sc("Space", self.toggle_play, "Play / pause")
        self.mark_shortcut = sc(self.settings.value("mark_key", DEFAULT_MARK_KEY), self.mark_gate)
        sc("Left", lambda: self.step(-1), "Step one frame back")
        sc("Right", lambda: self.step(1), "Step one frame forward")
        # Shift+←/→ = hold to play at 2x backward/forward (handled in eventFilter, needs key releases)
        self.hold_key = None
        QApplication.instance().installEventFilter(self)
        sc("Ctrl+Left", lambda: self.seek_rel(-5000), "Jump back 5 seconds")
        sc("Ctrl+Right", lambda: self.seek_rel(5000), "Jump forward 5 seconds")
        sc("J", lambda: self.key_shuttle(-1), "Shuttle backward")
        sc("K", lambda: self.set_shuttle(0), "Stop shuttle")
        sc("L", lambda: self.key_shuttle(1), "Shuttle forward")
        sc("PgUp", lambda: self.jump_mark(-1), "Previous gate")
        sc("PgDown", lambda: self.jump_mark(1), "Next gate")
        sc("[", lambda: self.change_speed(-1), "Slower playback")
        sc("]", lambda: self.change_speed(1), "Faster playback")
        sc("Delete", self.delete_key, "Delete selected mark")
        self.key_uses.update({"Shift+Left": "Hold to play backward at 2x", "Shift+Right": "Hold to play forward at 2x",
                              "Return": "Open the selected video"})

        enter = QShortcut(QKeySequence(Qt.Key.Key_Return), self.lib_list)
        enter.setContext(Qt.ShortcutContext.WidgetShortcut)
        enter.activated.connect(lambda: self.on_item_open_requested(self.lib_list.currentItem()))

        # --- Library > export / import / merge
        lib_menu = self.menuBar().addMenu("&Library")
        lib_menu.addAction("&Export library…").triggered.connect(self.export_library)
        lib_menu.addAction("&Import library (replace)…").triggered.connect(self.import_library)
        lib_menu.addAction("&Merge library…").triggered.connect(self.merge_library)

        # --- View > Theme
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction("Mark gate &hotkey…").triggered.connect(self.choose_mark_key)
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
        help_menu.addAction("&About").triggered.connect(
            lambda: QMessageBox.about(self, "About FPV Lap Timer", f"FPV Lap Timer {VERSION}"))

        self.refresh_mark_key_labels()
        self.refresh_library()

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
    def refresh_library(self, select_id=None):
        select_id = select_id or self.vid
        # rebuild the track filter without triggering another refresh
        current = self.track_filter.currentData()
        self.track_filter.blockSignals(True)
        self.track_filter.clear()
        self.track_filter.addItem("All tracks", None)
        for t in self.lib.tracks():
            self.track_filter.addItem(t, t)
        meta = self.lib.all_meta()
        if self.lib.tracks() and any(not t for t, _ in meta.values()):
            self.track_filter.addItem("(no track)", "")
        i = self.track_filter.findData(current) if current is not None else 0
        self.track_filter.setCurrentIndex(max(i, 0))
        self.track_filter.blockSignals(False)
        show = self.track_filter.currentData()

        self.lib_list.clear()
        entries = []
        for vid, path, fps in self.lib.videos():
            if show is not None and meta[vid][0] != show:
                continue
            entries.append((effective_date(path, meta[vid][1])[0], vid, path, fps))
        entries.sort(key=lambda e: (e[0] is None, e[0] or datetime.min.date(), e[2].lower()))
        missing_copies = 0
        self.listed = [(vid, path) for _, vid, path, _ in entries]
        dup_of = {}                                  # video id -> paths of its twin entries
        for group in self.lib.duplicate_groups():
            for v in group:
                dup_of[v] = [pth for vv, pth, _ in self.lib.videos() if vv in group and vv != v]
        for d, vid, path, fps in entries:
            s = lap_stats(self.lib.laps(vid))
            exists = os.path.exists(path)
            ready = exists and proxy_path(path).exists()
            if exists and not ready:
                missing_copies += 1
            item = QListWidgetItem(vname(path))             # plain text for accessibility; painted by the delegate
            item.setData(ROLE_INFO, {
                "track": meta[vid][0], "good": s["good"], "total": s["total"],
                "best": s["best"][0] if s["best"] else None, "best3": s["best3"][0] if s["best3"] else None,
                "name": vname(path), "date": d.isoformat() if d else "", "ready": ready,
                "dup": vid in dup_of, "exists": exists})
            item.setData(Qt.ItemDataRole.UserRole, (vid, path, fps))
            item.setToolTip(path + ("" if exists else "\n(file not found - double-click to locate)")
                            + ("\n⚠ Same file is also in the library as: " + "; ".join(dup_of[vid])
                               + "\nIts laps are counted twice in stats. Remove one of them." if vid in dup_of else ""))
            self.lib_list.addItem(item)
            if vid == select_id:
                self.lib_list.setCurrentItem(item)
        self.missing_copies = missing_copies
        self.update_queue_ui()

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add videos", str(Path.home()), "Video (*.ts *.mp4 *.mov *.mkv *.m4v *.avi);;All files (*)")
        self.add_paths(files)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add all videos in folder", str(Path.home()))
        if folder:
            paths = [p for p in Path(folder).rglob("*") if p.suffix.lower() in VIDEO_EXTS]
            self.add_paths(paths)

    def add_paths(self, paths):
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
                    if track:
                        self.lib.set_meta([vid], track=track)
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

    def remove_selected(self):
        item = self.lib_list.currentItem()
        if not item:
            return
        vid, path, _ = item.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(self, "Remove", f"Remove '{vname(path)}' and its lap marks from the library?\n"
                                "(The video file itself is not deleted.)") != QMessageBox.StandardButton.Yes:
            return
        if vid == self.vid:
            self.set_shuttle(0)
            self.player.stop()
            self.player.setSource(QUrl())
            self.vid = None
            self.cur_path = None
            self.load_marks()
            self.update_proxy_ui()
        self.build_queue = [q for q in self.build_queue if q[0] != vid]
        if self.build and self.build["vid"] == vid:
            self.build["cancelled"] = True
            self.build["proc"].kill()
        proxy_path(path).unlink(missing_ok=True)
        self.lib.remove_video(vid)
        self.refresh_library()

    def mark_key_text(self):
        return QKeySequence(self.settings.value("mark_key", DEFAULT_MARK_KEY)).toString(
            QKeySequence.SequenceFormat.NativeText)

    def refresh_mark_key_labels(self):
        key = self.mark_key_text()
        self.mark_btn.setText(f"🏁 MARK GATE ({key})")
        self.hint.setText(f"Space play/pause · {key} mark · " + self._hint_tail)

    def choose_mark_key(self):
        current = self.settings.value("mark_key", DEFAULT_MARK_KEY)
        taken = {k: v for k, v in self.key_uses.items()}
        dlg = HotkeyDialog(current, taken, self)
        if dlg.exec():
            key = dlg.key()
            self.settings.setValue("mark_key", key)
            self.mark_shortcut.setKey(QKeySequence(key))
            self.refresh_mark_key_labels()
            self.statusBar().showMessage(f"Mark gate hotkey is now {self.mark_key_text()}", 5000)

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
        menu.addAction("Track && session date…").triggered.connect(self.edit_details)
        return menu

    def on_library_menu(self, pos):
        item = self.lib_list.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():                        # right-click on an unselected entry selects just it
            self.lib_list.clearSelection()
            item.setSelected(True)
        self.make_library_menu(self.selected_vids()).exec(self.lib_list.viewport().mapToGlobal(pos))

    def edit_details(self):
        vids = self.selected_vids()
        if not vids:
            self.statusBar().showMessage("Select one or more videos first", 3000)
            return
        dlg = DetailsDialog(self.lib, vids, self)
        if dlg.exec():
            track, date = dlg.values()
            self.lib.set_meta(vids, track, date)
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

    def open_video(self, vid, path, fps):
        if not os.path.exists(path):
            new, _ = QFileDialog.getOpenFileName(self, f"Locate {vname(path)}", str(Path(path).parent),
                                                 "Video (*.ts *.mp4 *.mov *.mkv *.m4v *.avi);;All files (*)")
            if not new:
                return
            self.lib.relocate(vid, new)
            path = str(Path(new).resolve())
        self.vid, self.fps = vid, fps or 30.0
        self.title_label.setText(f"<b>{vname(path)}</b>")
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
            self.proxy_label.setText("Original video: scrubbing only jumps between keyframes, so the picture "
                                     "lags. Build a smooth copy for exact frames.")
            self.proxy_btn.setText("⚡ Build smooth-scrub copy"); self.proxy_btn.show()

    def on_proxy_button(self):
        if self.build and self.build["path"] == self.cur_path:
            self.build["cancelled"] = True
            self.build["proc"].kill()
        elif self.cur_path:
            self.request_proxy(self.vid, self.cur_path)
            self.update_proxy_ui()

    def request_proxy(self, vid, path, front=False):
        """Queue a smooth copy. front=True moves it ahead of the other waiting videos."""
        if (proxy_path(path).exists() or not find_tool("ffmpeg") or not os.path.exists(path)
                or (self.build and self.build["path"] == path)):
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
        """Shift+Left/Right: play at 2x (backward/forward) while the keys are held, stop on release."""
        t = ev.type()
        if t == QEvent.Type.KeyPress:
            k, mods = ev.key(), ev.modifiers()
            if self.hold_key is not None and k == self.hold_key:
                return True                                  # swallow auto-repeat while holding
            if (k in (Qt.Key.Key_Left, Qt.Key.Key_Right) and mods & Qt.KeyboardModifier.ShiftModifier
                    and not mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
                    and self.vid is not None and not QApplication.activeModalWidget()):
                self.hold_key = k
                self.set_shuttle(2.0 if k == Qt.Key.Key_Right else -2.0)
                return True
        elif t == QEvent.Type.KeyRelease and self.hold_key is not None and not ev.isAutoRepeat():
            if ev.key() in (self.hold_key, Qt.Key.Key_Shift):
                self.hold_key = None
                self.set_shuttle(0)
                return True
        elif t == QEvent.Type.WindowDeactivate and obj is self and self.hold_key is not None:
            self.hold_key = None                             # lost focus: we'd never see the release
            self.set_shuttle(0)
        return super().eventFilter(obj, ev)

    def closeEvent(self, e):
        self.build_queue.clear()
        if self.build:
            self.build["cancelled"] = True
            self.build["proc"].kill()
            self.build["proc"].waitForFinished(2000)
            self.build["part"].unlink(missing_ok=True)
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

    def frame_dur(self):
        return 1000.0 / (self.fps or 30.0)

    def seek(self, ms, bias=0.0):
        """Move to `ms` (clamped); remembers where we are heading. `bias` nudges only the request."""
        d = self.player.duration()
        ms = max(0.0, min(float(ms), d - 1 if d else float(ms)))
        self.cursor_ms = ms
        self._last_seek = time.monotonic()
        self.player.setPosition(int(ms + bias))

    def on_slider_moved(self, v):
        # coalesce a fast drag into at most one seek every ~25 ms (latest position wins)
        self.set_shuttle(0)
        self.cursor_ms = float(v)
        self._last_seek = time.monotonic()
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
        self.lib.add_mark(self.vid, t)
        self.load_marks()
        self.refresh_library()

    def load_marks(self):
        self._building = True
        self.mark_rows = self.lib.marks(self.vid) if self.vid else []
        self.table.setRowCount(len(self.mark_rows))
        laps = []
        for i, (mid, t, bad) in enumerate(self.mark_rows):
            lap = t - self.mark_rows[i - 1][1] if i else None
            if lap is not None:
                laps.append((lap, bool(bad)))
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
                self.table.setItem(i, c, it)
        self._building = False
        self.slider.set_marks([m[1] for m in self.mark_rows])
        self.update_stats(laps)

    def update_stats(self, laps):
        s = lap_stats(laps)
        if not laps:
            self.stats_label.setText("<i>Mark each pass through the start gate. "
                                     "The first mark starts lap 1.</i>")
            return
        best = f"{fmt(s['best'][0])} <small>(lap {s['best'][1] + 1})</small>" if s["best"] else "--"
        if s["best3"]:
            i = s["best3"][1]
            best3 = f"{fmt(s['best3'][0])} <small>(laps {i + 1}–{i + 3})</small>"
        else:
            best3 = "-- <small>(needs 3 good laps in a row)</small>"
        self.stats_label.setText(
            f"<div style='font-size:15px'>Laps: <b>{s['good']}</b> good / {s['total']} total<br>"
            f"Fastest lap: <b>{best}</b><br>Fastest 3 consecutive: <b>{best3}</b></div>")

    def on_row_clicked(self, row, col):
        if col != self.COL_BAD and 0 <= row < len(self.mark_rows):
            self.set_shuttle(0)
            self.seek(self.mark_rows[row][1])

    def on_item_changed(self, item):
        if self._building or item.column() != self.COL_BAD or item.row() == 0:
            return
        mid = self.mark_rows[item.row()][0]
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
        self.lib.move_mark(self.mark_rows[r][0], self.mark_time())
        self.load_marks()
        self.refresh_library()

    def export_best3_video(self):
        if self.vid is None:
            self.statusBar().showMessage("Open a video first", 3000)
            return
        win = best_three_window(self.lib, {self.vid})
        if not win:
            QMessageBox.information(self, "Nothing to export",
                                    "This video needs three good laps in a row (bad laps break the run) first.")
            return
        ExportDialog(win, self).exec()

    def show_chart(self):
        if self.vid is None:
            return
        name = Path(self.title_label.text().replace("<b>", "").replace("</b>", "")).name
        ChartDialog(self.lib.laps(self.vid), name, self).exec()

    def export_current(self):
        if self.vid is None:
            return
        default = str(Path.home() / (self.title_label.text().replace("<b>", "").replace("</b>", "") + ".csv"))
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", default, "CSV (*.csv)")
        if path:
            export_csv(self.lib, {self.vid}, path)
            self.statusBar().showMessage(f"Exported to {path}", 5000)

    # ---- drag & drop -------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            paths += [q for q in p.rglob("*")] if p.is_dir() else [p]
        self.add_paths(paths)


def install_error_handler(get_window):
    """PyQt aborts the whole program when a slot raises. Log the error instead and keep the app open."""
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(text)
        try:
            log = data_dir() / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a") as f:
                f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n")
            win = get_window()
            if win:
                win.statusBar().showMessage(f"Internal error: {exc} (details in {log})", 20000)
        except Exception:
            pass

    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    install_error_handler(lambda: win)
    win.show()
    # allow: lap_timer.py video1.ts video2.mp4
    if len(sys.argv) > 1:
        win.open_paths(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
