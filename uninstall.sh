#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Remove melihSS for the current user.
#
# Usage:
#   ./uninstall.sh          # keep settings in ~/.config/melihss
#   ./uninstall.sh --purge  # also delete settings
set -euo pipefail

APP_ID="io.github.EkizceMelih.melihSS"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
ICON_DIR="${DATA_DIR}/icons/hicolor"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/melihss"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

info "Removing files"
rm -f "${HOME}/.local/bin/melihss"
rm -f "${DATA_DIR}/applications/${APP_ID}.desktop"
rm -f "${ICON_DIR}/scalable/apps/${APP_ID}.svg"
for n in 16 24 32 48 64 128 256 512; do
    rm -f "${ICON_DIR}/${n}x${n}/apps/${APP_ID}.png"
done
gtk-update-icon-cache -f -t "${ICON_DIR}" >/dev/null 2>&1 || true
update-desktop-database "${DATA_DIR}/applications" >/dev/null 2>&1 || true

info "Removing keyboard shortcut and dock pin"
python3 - "${APP_ID}" <<'EOF'
import sys
from gi.repository import Gio

app_id = sys.argv[1]
mk = "org.gnome.settings-daemon.plugins.media-keys"
ck = mk + ".custom-keybinding"
root = Gio.Settings.new(mk)
paths = list(root.get_strv("custom-keybindings"))
keep = []
for p in paths:
    s = Gio.Settings.new_with_path(ck, p)
    if "melihss" in s.get_string("command"):
        for key in ("name", "command", "binding"):
            s.reset(key)
    else:
        keep.append(p)
if keep != paths:
    root.set_strv("custom-keybindings", keep)

shell = Gio.Settings.new("org.gnome.shell")
favs = list(shell.get_strv("favorite-apps"))
if f"{app_id}.desktop" in favs:
    favs.remove(f"{app_id}.desktop")
    shell.set_strv("favorite-apps", favs)
Gio.Settings.sync()
EOF

info "Revoking screenshot permission"
for id in melihss "${APP_ID}"; do
    gdbus call --session \
        --dest org.freedesktop.impl.portal.PermissionStore \
        --object-path /org/freedesktop/impl/portal/PermissionStore \
        --method org.freedesktop.impl.portal.PermissionStore.DeletePermission \
        screenshot screenshot "${id}" >/dev/null 2>&1 || true
done

if [ "${1:-}" = "--purge" ]; then
    info "Deleting settings in ${CONFIG_DIR}"
    rm -rf "${CONFIG_DIR}"
fi

info "melihSS has been uninstalled"
