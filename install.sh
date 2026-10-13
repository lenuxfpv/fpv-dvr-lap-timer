#!/usr/bin/env bash
# Install FPV Lap Timer for the current user (Linux): checks dependencies and adds an
# application-menu launcher pointing at THIS folder.
#
#   ./install.sh                 install (menu launcher)
#   ./install.sh --desktop       also put a shortcut on the Desktop
#   ./install.sh --uninstall     remove the launcher and icon (keeps your library and videos);
#                                asks whether to also delete the smooth-scrub copies
#   ./install.sh --uninstall --clear-cache    same, and delete the copies without asking
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/scalable/apps"
DESKTOP_FILE="$APPS/fpv-lap-timer.desktop"
ICON_FILE="$ICONS/fpv-lap-timer.svg"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/fpv-lap-timer"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

desktop_dir() {
    local d=""
    command -v xdg-user-dir >/dev/null 2>&1 && d="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    [[ -n "$d" && -d "$d" ]] && echo "$d" || echo "$HOME/Desktop"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$DESKTOP_FILE" "$ICON_FILE" "$(desktop_dir)/fpv-lap-timer.desktop"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
    say "Launcher removed. Your library ($DATA/fpv-lap-timer) and videos were not touched."
    if [[ -d "$CACHE" ]]; then
        size="$(du -sh "$CACHE" 2>/dev/null | cut -f1)"
        clear=no
        if [[ "${2:-}" == "--clear-cache" ]]; then
            clear=yes
        elif [[ -t 0 ]]; then
            read -r -p "Also delete the smooth-scrub copies in $CACHE (${size:-unknown size}) to free disk space? They can be rebuilt. [y/N] " ans
            [[ "$ans" =~ ^[Yy] ]] && clear=yes
        fi
        if [[ "$clear" == yes ]]; then
            rm -rf "$CACHE"
            say "Smooth-scrub copies deleted."
        else
            echo "Smooth-scrub copies kept in $CACHE (${size:-unknown size}) - delete that folder to free the space."
        fi
    fi
    [[ -d "$DIR/.venv" ]] && echo "Delete $DIR/.venv yourself if you no longer need it."
    exit 0
fi

# --- Python + PyQt6 -----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.9+ first."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9 or newer is required."

if python3 -c 'import PyQt6.QtMultimedia, PyQt6.QtMultimediaWidgets' >/dev/null 2>&1; then
    PYBIN="$(command -v python3)"
    say "Using system PyQt6 ($PYBIN)"
else
    say "PyQt6 not found system-wide; creating a private environment in .venv ..."
    python3 -m venv "$DIR/.venv" || die "Could not create a virtual environment (on Debian/Ubuntu: sudo apt install python3-venv)."
    "$DIR/.venv/bin/python" -m pip install --quiet --upgrade pip PyQt6
    PYBIN="$DIR/.venv/bin/python"
    "$PYBIN" -c 'import PyQt6.QtMultimedia' || die "PyQt6 installed but Qt Multimedia failed to load."
fi

# --- ffmpeg / ffprobe (frame rate + smooth-scrub copies) -------------------------
missing=()
for tool in ffmpeg ffprobe; do
    command -v "$tool" >/dev/null 2>&1 || [[ -x "$DIR/bin/$tool" ]] || missing+=("$tool")
done
if (( ${#missing[@]} )); then
    hint="install ffmpeg with your package manager"
    if [[ -r /etc/os-release ]]; then
        . /etc/os-release
        case "${ID:-} ${ID_LIKE:-}" in
            *fedora*|*rhel*) hint="enable RPM Fusion, then: sudo dnf swap ffmpeg-free ffmpeg --allowerasing  (or sudo dnf install ffmpeg)";;
            *debian*|*ubuntu*) hint="sudo apt install ffmpeg";;
            *arch*) hint="sudo pacman -S ffmpeg";;
            *suse*) hint="sudo zypper install ffmpeg";;
        esac
    fi
    warn "${missing[*]} not found. The app still runs, but it can't build smooth-scrub copies (scrubbing will only"
    warn "jump between keyframes) and it assumes 30 fps for frame-stepping. To fix: $hint"
fi

# libx264 is needed for the smooth-scrub copies and the video export; some builds leave it out
# (notably Fedora's default ffmpeg-free).
encoders=""
command -v ffmpeg >/dev/null 2>&1 && encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"   # (not piped into grep -q: pipefail)
if command -v ffmpeg >/dev/null 2>&1 && [[ "$encoders" != *libx264* ]]; then
    warn "Your ffmpeg has no libx264 (H.264) encoder, so smooth-scrub copies and the fastest-3 video export will fail."
    warn "Fedora: enable RPM Fusion and run: sudo dnf swap ffmpeg-free ffmpeg --allowerasing"
fi

# a bold font is needed for the timer drawn on exported videos
if [[ -z "$("$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); import lap_timer; print(lap_timer.find_overlay_font() or "")' "$DIR" 2>/dev/null)" ]]; then
    warn "No bold font file was found, so the fastest-3 video export can't draw its timer."
    warn "Install one, e.g. DejaVu Sans (Fedora: sudo dnf install dejavu-sans-fonts, Debian/Ubuntu: sudo apt install fonts-dejavu-core)."
fi

# --- launcher -----------------------------------------------------------------
# Escape for the Exec key: quote the path, escape "$`\ , double backslashes, and %.
esc() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//\$/\\\$}"; s="${s//\`/\\\`}"
    s="${s//\\/\\\\}"; s="${s//%/%%}"
    printf '%s' "$s"
}

mkdir -p "$APPS" "$ICONS"
cp "$DIR/fpv-lap-timer.svg" "$ICON_FILE"
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=FPV Lap Timer
Comment=Mark gate passes in FPV footage and track lap times
Exec="$(esc "$PYBIN")" "$(esc "$DIR/lap_timer.py")" %F
Path=$DIR
Icon=fpv-lap-timer
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=lap_timer.py
DESKTOP
chmod +x "$DESKTOP_FILE"

if [[ "${1:-}" == "--desktop" ]]; then
    cp "$DESKTOP_FILE" "$(desktop_dir)/fpv-lap-timer.desktop"
    chmod +x "$(desktop_dir)/fpv-lap-timer.desktop"
    command -v gio >/dev/null 2>&1 && gio set "$(desktop_dir)/fpv-lap-timer.desktop" metadata::trusted true 2>/dev/null || true
    say "Desktop shortcut added (you may need to right-click it and choose 'Allow Launching')."
fi

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
command -v desktop-file-validate >/dev/null 2>&1 && desktop-file-validate "$DESKTOP_FILE" || true
say "Installed. Search for 'FPV Lap Timer' in your application menu (log out/in if the icon is missing)."
