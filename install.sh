#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install melihSS for the current user (no root required).
#
# Usage:
#   ./install.sh                              # default shortcut: Shift+Super+S
#   ./install.sh --shortcut '<Control>less'   # custom shortcut (GTK accelerator syntax)
#   ./install.sh --no-shortcut                # do not touch keyboard shortcuts
set -euo pipefail

APP_ID="io.github.EkizceMelih.melihSS"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
ICON_DIR="${DATA_DIR}/icons/hicolor"
APPS_DIR="${DATA_DIR}/applications"
BIN="${BIN_DIR}/melihss"

SHORTCUT="<Shift><Super>s"
SET_SHORTCUT=1

while [ $# -gt 0 ]; do
    case "$1" in
        --shortcut)    SHORTCUT="${2:?--shortcut needs a value}"; shift 2 ;;
        --no-shortcut) SET_SHORTCUT=0; shift ;;
        -h|--help)     sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }

# --- dependency check -------------------------------------------------------
info "Checking dependencies"
missing=()
python3 - <<'EOF' 2>/dev/null || missing+=("python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1")
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Adw, PangoCairo  # noqa: F401
import cairo  # noqa: F401
EOF
command -v wl-copy   >/dev/null || missing+=("wl-clipboard")
command -v notify-send >/dev/null || missing+=("libnotify-bin")
if [ ${#missing[@]} -gt 0 ]; then
    warn "Missing packages. Install them with:"
    echo "    sudo apt install ${missing[*]}" >&2
    exit 1
fi

# --- files ------------------------------------------------------------------
info "Installing to ${BIN}"
install -Dm755 "${SRC_DIR}/melihss.py" "${BIN}"

info "Installing icons"
install -Dm644 "${SRC_DIR}/data/icons/${APP_ID}.svg" "${ICON_DIR}/scalable/apps/${APP_ID}.svg"
# Raster sizes help docks and app switchers that prefer PNG icons.
python3 - "${ICON_DIR}/scalable/apps/${APP_ID}.svg" "${ICON_DIR}" "${APP_ID}" <<'EOF' || warn "Could not render PNG icons (optional)"
import os, sys
import gi
gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg
import cairo
src, icon_dir, app_id = sys.argv[1:]
handle = Rsvg.Handle.new_from_file(src)
for n in (16, 24, 32, 48, 64, 128, 256, 512):
    out_dir = os.path.join(icon_dir, f"{n}x{n}", "apps")
    os.makedirs(out_dir, exist_ok=True)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, n, n)
    rect = Rsvg.Rectangle()
    rect.x = rect.y = 0
    rect.width = rect.height = n
    handle.render_document(cairo.Context(surface), rect)
    surface.write_to_png(os.path.join(out_dir, f"{app_id}.png"))
EOF
[ -f "${ICON_DIR}/index.theme" ] || cp /usr/share/icons/hicolor/index.theme "${ICON_DIR}/" 2>/dev/null || true
gtk-update-icon-cache -f -t "${ICON_DIR}" >/dev/null 2>&1 || true

info "Installing desktop entry"
mkdir -p "${APPS_DIR}"
sed "s|@BIN@|${BIN}|g" "${SRC_DIR}/data/${APP_ID}.desktop.in" > "${APPS_DIR}/${APP_ID}.desktop"
update-desktop-database "${APPS_DIR}" >/dev/null 2>&1 || true

# --- screenshot permission --------------------------------------------------
# GNOME asks for permission before a non-interactive portal screenshot. Grant it
# up front: "melihss" is the app id GNOME derives when the hotkey launches the
# binary, APP_ID is used when it is started from the desktop entry.
info "Granting screenshot permission"
for id in melihss "${APP_ID}"; do
    gdbus call --session \
        --dest org.freedesktop.impl.portal.PermissionStore \
        --object-path /org/freedesktop/impl/portal/PermissionStore \
        --method org.freedesktop.impl.portal.PermissionStore.SetPermission \
        screenshot true screenshot "${id}" "['yes']" >/dev/null \
        || warn "Could not set screenshot permission for ${id}"
done

# --- keyboard shortcut ------------------------------------------------------
if [ "${SET_SHORTCUT}" -eq 1 ]; then
    info "Binding ${SHORTCUT} to melihSS"
    python3 - "${BIN}" "${SHORTCUT}" <<'EOF'
import sys
from gi.repository import Gio

command, binding = sys.argv[1:]
mk = "org.gnome.settings-daemon.plugins.media-keys"
ck = mk + ".custom-keybinding"
base = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"

root = Gio.Settings.new(mk)
paths = list(root.get_strv("custom-keybindings"))
target = None
for p in paths:
    if "melihss" in Gio.Settings.new_with_path(ck, p).get_string("command"):
        target = p
        break
if target is None:
    i = 0
    while f"{base}custom{i}/" in paths:
        i += 1
    target = f"{base}custom{i}/"
    paths.append(target)
    root.set_strv("custom-keybindings", paths)

s = Gio.Settings.new_with_path(ck, target)
s.set_string("name", "melihSS")
s.set_string("command", command)
s.set_string("binding", binding)
Gio.Settings.sync()
EOF
fi

info "Done. Press ${SHORTCUT} to take a screenshot, or run: melihss --settings"
case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) warn "${BIN_DIR} is not in your PATH; log out and back in, or add it to ~/.profile" ;;
esac
