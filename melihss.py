#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Melih Ekizce
"""melihSS - a fast, Lightshot-style screenshot tool for GNOME on Wayland.

Bind it to a hotkey. It freezes the screen, lets you drag a region, and shows
annotation tools right next to the selection (pen, line, arrow, rectangle,
ellipse, highlighter, text, colors).

Usage:
    melihss              Take a screenshot
    melihss --settings   Open the settings window

Settings are stored in ~/.config/melihss/config.json.
Fixed keys: Esc closes (or finishes the text being typed), Enter copies,
the mouse wheel changes stroke width / text size.
"""
import copy
import datetime
import io
import json
import math
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
import cairo  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

__version__ = "1.0.0"

APP_ID = "io.github.EkizceMelih.melihSS"
SCRIPT_PATH = os.path.realpath(__file__)
CONFIG_PATH = os.path.join(GLib.get_user_config_dir(), "melihss", "config.json")

PALETTE = [
    (0.93, 0.18, 0.18),  # red
    (1.00, 0.55, 0.00),  # orange
    (0.98, 0.84, 0.10),  # yellow
    (0.20, 0.75, 0.30),  # green
    (0.20, 0.52, 0.96),  # blue
    (0.62, 0.30, 0.90),  # purple
    (1.00, 1.00, 1.00),  # white
    (0.00, 0.00, 0.00),  # black
]

TOOLS = ["pen", "line", "arrow", "rect", "ellipse", "marker", "text"]
ACTION_NAMES = {
    "copy": "Copy and close",
    "save": "Save and close",
    "undo": "Undo",
    "redo": "Redo",
    "pen": "Pen",
    "line": "Line",
    "arrow": "Arrow",
    "rect": "Rectangle",
    "ellipse": "Ellipse",
    "marker": "Highlighter",
    "text": "Text",
}

DEFAULT_GLOBAL_SHORTCUT = "<Shift><Super>s"
DEFAULTS = {
    "color": list(PALETTE[0]),
    "width": 3,
    "font": "Sans",
    "dim": 0.45,
    "save_dir": "~/Pictures/Screenshots",
    "notify_copy": False,
    "save_also_copy": False,
    "keys": {
        "copy": "<Control>c",
        "save": "<Control>s",
        "undo": "<Control>z",
        "redo": "<Control>y",
        "pen": "p",
        "line": "l",
        "arrow": "a",
        "rect": "r",
        "ellipse": "e",
        "marker": "m",
        "text": "t",
    },
}

MOD_MASK = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
            | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK)
# Modifiers that turn a key press into a command even while typing text.
STRONG_MODS = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
MODIFIER_KEYVALS = {
    Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock,
    Gdk.KEY_Num_Lock, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
}

CSS = """
window.shot { background: black; }
.bar {
  background-color: rgba(28, 28, 30, 0.94);
  border-radius: 9px;
  padding: 4px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
}
.bar button, .bar menubutton > button {
  min-width: 30px; min-height: 30px;
  padding: 3px;
  border-radius: 6px;
  border: none;
  outline: none;
  box-shadow: none;
  background-image: none;
  background-color: transparent;
}
.bar button:hover, .bar menubutton > button:hover { background-color: rgba(255, 255, 255, 0.14); }
.bar button.active { background-color: #3584e4; }
.bar separator { background-color: rgba(255, 255, 255, 0.18); margin: 3px; min-width: 1px; min-height: 1px; }
.swatch { min-width: 26px; min-height: 26px; padding: 2px; }
.shortcut-btn { min-width: 110px; }
"""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def load_config():
    """Return the user configuration merged over the defaults."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        keys = data.pop("keys", {})
        for k, v in data.items():
            if k in cfg:
                cfg[k] = v
        for k, v in keys.items():
            if k in cfg["keys"]:
                cfg["keys"][k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    """Write the configuration atomically."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def display_path(path):
    """Show paths under the home directory as ~/..."""
    path = os.path.expanduser(path)
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def parse_accel(accel):
    """Parse a GTK accelerator string into a (lowercase keyval, modifiers) pair."""
    if not accel:
        return None
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    if not ok or keyval == 0:
        return None
    return Gdk.keyval_to_lower(keyval), int(mods & MOD_MASK)


def accel_label(accel):
    """Human-readable label for an accelerator, e.g. 'Ctrl+C'."""
    p = parse_accel(accel)
    if not p:
        return "None"
    return Gtk.accelerator_get_label(p[0], Gdk.ModifierType(p[1]))


# --- GNOME global shortcut (stored as a gsettings custom keybinding) ---
MK_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CK_SCHEMA = MK_SCHEMA + ".custom-keybinding"
CK_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def find_keybinding(create=False):
    """Return the Gio.Settings of melihSS's custom keybinding, optionally creating it."""
    root = Gio.Settings.new(MK_SCHEMA)
    paths = list(root.get_strv("custom-keybindings"))
    for p in paths:
        s = Gio.Settings.new_with_path(CK_SCHEMA, p)
        if "melihss" in s.get_string("command"):
            return s
    if not create:
        return None
    i = 0
    while f"{CK_BASE}custom{i}/" in paths:
        i += 1
    p = f"{CK_BASE}custom{i}/"
    s = Gio.Settings.new_with_path(CK_SCHEMA, p)
    s.set_string("name", "melihSS")
    s.set_string("command", shutil.which("melihss") or SCRIPT_PATH)
    s.set_string("binding", "")
    paths.append(p)
    root.set_strv("custom-keybindings", paths)
    Gio.Settings.sync()
    return s


def get_global_shortcut():
    s = find_keybinding()
    return s.get_string("binding") if s else ""


def set_global_shortcut(accel):
    s = find_keybinding(create=True)
    s.set_string("binding", accel)
    Gio.Settings.sync()


# --------------------------------------------------------------------------
# Screen capture (xdg-desktop-portal)
# --------------------------------------------------------------------------
def capture_screen(timeout=10):
    """Grab the full screen through the Screenshot portal and return a cairo surface.

    Wayland does not let regular clients read the screen, so the request goes
    through org.freedesktop.portal.Screenshot in non-interactive mode.
    """
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    token = "melihss%d" % (time.monotonic_ns() % 10**9)
    sender = bus.get_unique_name()[1:].replace(".", "_")
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    loop = GLib.MainLoop()
    result = {}

    def on_response(_conn, _sender, _path, _iface, _signal, params):
        code, data = params.unpack()
        result["code"], result["data"] = code, data
        loop.quit()

    # Subscribe before calling to avoid missing a fast response.
    sub = bus.signal_subscribe(
        "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
        "Response", req_path, None, Gio.DBusSignalFlags.NONE, on_response)
    bus.call_sync(
        "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot", "Screenshot",
        GLib.Variant("(sa{sv})", ("", {
            "handle_token": GLib.Variant("s", token),
            "interactive": GLib.Variant("b", False),
        })),
        None, Gio.DBusCallFlags.NONE, -1, None)

    def on_timeout():
        loop.quit()
        return False

    tid = GLib.timeout_add_seconds(timeout, on_timeout)
    loop.run()
    bus.signal_unsubscribe(sub)
    if "code" not in result:
        raise RuntimeError("the screenshot portal did not respond")
    GLib.source_remove(tid)
    if result["code"] != 0:
        raise RuntimeError("screenshot permission denied (code %d)" % result["code"])

    file_path = unquote(urlparse(result["data"]["uri"]).path)
    surface = cairo.ImageSurface.create_from_png(file_path)
    try:
        os.remove(file_path)  # the portal drops a file in ~/Pictures; clean it up
    except OSError:
        pass
    return surface


def notify(msg):
    """Show a desktop notification, falling back to stderr."""
    try:
        subprocess.Popen(["notify-send", "-a", "melihSS", "-i", APP_ID, "melihSS", msg])
    except OSError:
        print(msg, file=sys.stderr)


def copy_png_to_clipboard(data):
    """Put PNG bytes on the clipboard.

    wl-copy / xclip keep serving the data after this process exits, which a
    GTK clipboard owner could not do.
    """
    try:
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            subprocess.run(["wl-copy", "--type", "image/png"], input=data, check=True, timeout=5)
        elif shutil.which("xclip"):
            subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png"],
                           input=data, check=True, timeout=5)
        else:
            notify("Could not copy to clipboard: install wl-clipboard or xclip.")
            return False
    except (OSError, subprocess.SubprocessError) as e:
        notify(f"Could not copy to clipboard: {e}")
        return False
    return True


# --------------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------------
class Shape:
    """A single annotation. Coordinates are in widget (logical) pixels."""

    def __init__(self, kind, color, width, p1=(0, 0), p2=None, size=20, font="Sans"):
        self.kind = kind
        self.color = color
        self.width = width
        self.p1 = p1
        self.p2 = p2 if p2 is not None else p1
        self.points = [p1]
        self.text = ""
        self.size = size
        self.font = font

    def is_meaningful(self):
        """False for accidental clicks that should not create a shape."""
        if self.kind in ("pen", "marker"):
            return len(self.points) >= 1
        if self.kind == "text":
            return bool(self.text.strip())
        return math.hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1]) >= 3


def text_layout(ctx, shape):
    layout = PangoCairo.create_layout(ctx)
    desc = Pango.FontDescription.from_string(shape.font or "Sans")
    desc.set_weight(Pango.Weight.BOLD)
    desc.set_absolute_size(shape.size * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_text(shape.text, -1)
    return layout


def draw_arrow(ctx, p1, p2, width):
    (x1, y1), (x2, y2) = p1, p2
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 1:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    head = max(12.0, width * 3.5)
    spread = math.radians(26)
    base = head * math.cos(spread)
    ang = math.atan2(y2 - y1, x2 - x1)
    # Stop the shaft at the base of the head so the tip stays sharp.
    if length > base:
        ctx.set_line_width(width)
        ctx.move_to(x1, y1)
        ctx.line_to(x2 - ux * base, y2 - uy * base)
        ctx.stroke()
    ctx.move_to(x2, y2)
    ctx.line_to(x2 - head * math.cos(ang - spread), y2 - head * math.sin(ang - spread))
    ctx.line_to(x2 - head * math.cos(ang + spread), y2 - head * math.sin(ang + spread))
    ctx.close_path()
    ctx.fill()


def draw_shape(ctx, s, editing=False):
    r, g, b = s.color
    ctx.save()
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.set_source_rgb(r, g, b)
    k = s.kind
    if k in ("pen", "marker"):
        if k == "marker":
            ctx.set_source_rgba(r, g, b, 0.42)
            ctx.set_line_width(s.width * 4 + 6)
        else:
            ctx.set_line_width(s.width)
        x0, y0 = s.points[0]
        ctx.move_to(x0, y0)
        if len(s.points) == 1:
            ctx.line_to(x0 + 0.01, y0)  # draw a dot for a single click
        for x, y in s.points[1:]:
            ctx.line_to(x, y)
        ctx.stroke()
    elif k == "line":
        ctx.set_line_width(s.width)
        ctx.move_to(*s.p1)
        ctx.line_to(*s.p2)
        ctx.stroke()
    elif k == "arrow":
        draw_arrow(ctx, s.p1, s.p2, s.width)
    elif k == "rect":
        ctx.set_line_join(cairo.LINE_JOIN_MITER)
        ctx.set_line_width(s.width)
        (x1, y1), (x2, y2) = s.p1, s.p2
        ctx.rectangle(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        ctx.stroke()
    elif k == "ellipse":
        (x1, y1), (x2, y2) = s.p1, s.p2
        rx, ry = abs(x2 - x1) / 2, abs(y2 - y1) / 2
        if rx >= 1 and ry >= 1:
            ctx.save()
            ctx.translate((x1 + x2) / 2, (y1 + y2) / 2)
            ctx.scale(rx, ry)
            ctx.arc(0, 0, 1, 0, 2 * math.pi)
            ctx.restore()  # restore before stroking so the line width is not scaled
            ctx.set_line_width(s.width)
            ctx.stroke()
    elif k == "text":
        layout = text_layout(ctx, s)
        x, y = s.p1
        ctx.move_to(x, y)
        PangoCairo.show_layout(ctx, layout)
        if editing:
            # Dashed frame and caret while the text is being typed.
            _ink, logical = layout.get_pixel_extents()
            w = max(logical.width, 4)
            h = max(logical.height, s.size * 1.2)
            ctx.set_line_width(1)
            ctx.set_dash([4, 3])
            ctx.set_source_rgba(1, 1, 1, 0.85)
            ctx.rectangle(x - 5.5, y - 3.5, w + 11, h + 7)
            ctx.stroke()
            ctx.set_dash([])
            strong, _weak = layout.get_cursor_pos(len(s.text.encode("utf-8")))
            cx = x + strong.x / Pango.SCALE
            cy = y + strong.y / Pango.SCALE
            ch = max(strong.height / Pango.SCALE, s.size)
            ctx.set_source_rgb(r, g, b)
            ctx.set_line_width(2)
            ctx.move_to(cx + 1, cy)
            ctx.line_to(cx + 1, cy + ch)
            ctx.stroke()
    ctx.restore()


def render_region(img, sel, shapes, factor):
    """Render the selection with its annotations at full image resolution.

    `sel` is in widget coordinates; `factor` converts widget pixels to image
    pixels (it is >1 on HiDPI screens).
    """
    iw, ih = img.get_width(), img.get_height()
    if sel is None:
        sel = (0, 0, iw / factor, ih / factor)
    x, y, w, h = sel
    px = max(0, min(iw - 1, round(x * factor)))
    py = max(0, min(ih - 1, round(y * factor)))
    pw = max(1, min(iw - px, round(w * factor)))
    ph = max(1, min(ih - py, round(h * factor)))
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
    ctx = cairo.Context(out)
    ctx.set_source_surface(img, -px, -py)
    ctx.paint()
    ctx.translate(-px, -py)
    ctx.scale(factor, factor)
    for s in shapes:
        draw_shape(ctx, s)
    return out


# --------------------------------------------------------------------------
# Toolbar icons (drawn with cairo so they look the same on every theme)
# --------------------------------------------------------------------------
def draw_icon(ctx, kind, w, h, color=None):
    ctx.translate((w - 20) / 2, (h - 20) / 2)
    ctx.set_source_rgba(1, 1, 1, 0.93)
    ctx.set_line_width(1.8)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    if kind == "pen":
        ctx.move_to(3, 15)
        ctx.curve_to(6, 6, 9, 18, 12, 9)
        ctx.curve_to(13.5, 5.5, 15.5, 4.5, 17, 4)
        ctx.stroke()
    elif kind == "line":
        ctx.move_to(4, 16)
        ctx.line_to(16, 4)
        ctx.stroke()
    elif kind == "arrow":
        ctx.move_to(4, 16)
        ctx.line_to(15, 5)
        ctx.stroke()
        ctx.move_to(16, 4)
        ctx.line_to(8.5, 5.5)
        ctx.line_to(14.5, 11.5)
        ctx.close_path()
        ctx.fill()
    elif kind == "rect":
        ctx.rectangle(3.5, 5.5, 13, 9.5)
        ctx.stroke()
    elif kind == "ellipse":
        ctx.save()
        ctx.translate(10, 10)
        ctx.scale(7, 5)
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
        ctx.restore()
        ctx.stroke()
    elif kind == "marker":
        ctx.set_source_rgba(1, 1, 1, 0.5)
        ctx.set_line_width(6)
        ctx.move_to(4, 13)
        ctx.line_to(16, 7)
        ctx.stroke()
    elif kind == "text":
        ctx.set_line_width(2.2)
        ctx.move_to(4.5, 4.5)
        ctx.line_to(15.5, 4.5)
        ctx.move_to(10, 4.5)
        ctx.line_to(10, 16)
        ctx.stroke()
    elif kind == "undo":
        ctx.move_to(5, 8)
        ctx.curve_to(8, 3.5, 16, 4, 16, 10.5)
        ctx.curve_to(16, 15, 12.5, 16.5, 9, 16)
        ctx.stroke()
        ctx.move_to(4.5, 3.5)
        ctx.line_to(5, 8.5)
        ctx.line_to(9.5, 7.5)
        ctx.stroke()
    elif kind == "copy":
        ctx.rectangle(7.5, 3.5, 9, 10)
        ctx.stroke()
        ctx.rectangle(3.5, 7.5, 9, 9)
        ctx.set_source_rgba(0.11, 0.11, 0.12, 1)
        ctx.fill_preserve()
        ctx.set_source_rgba(1, 1, 1, 0.93)
        ctx.stroke()
    elif kind == "save":
        ctx.move_to(10, 3)
        ctx.line_to(10, 12.5)
        ctx.move_to(6, 9)
        ctx.line_to(10, 13)
        ctx.line_to(14, 9)
        ctx.move_to(3.5, 13)
        ctx.line_to(3.5, 16.5)
        ctx.line_to(16.5, 16.5)
        ctx.line_to(16.5, 13)
        ctx.stroke()
    elif kind == "settings":
        ctx.translate(10, 10)
        for i in range(8):
            ctx.save()
            ctx.rotate(i * math.pi / 4)
            ctx.rectangle(-1.7, -8.5, 3.4, 4)
            ctx.fill()
            ctx.restore()
        ctx.set_line_width(2.6)
        ctx.arc(0, 0, 5.4, 0, 2 * math.pi)
        ctx.stroke()
    elif kind == "close":
        ctx.move_to(5, 5)
        ctx.line_to(15, 15)
        ctx.move_to(15, 5)
        ctx.line_to(5, 15)
        ctx.stroke()
    elif kind == "color":
        ctx.arc(10, 10, 7.5, 0, 2 * math.pi)
        ctx.set_source_rgb(*color)
        ctx.fill_preserve()
        ctx.set_source_rgba(1, 1, 1, 0.95)
        ctx.set_line_width(1.6)
        ctx.stroke()


def icon_widget(kind, color_getter=None, size=20):
    area = Gtk.DrawingArea()
    area.set_content_width(size)
    area.set_content_height(size)
    area.set_can_target(False)
    area.set_draw_func(
        lambda _a, c, w, h: draw_icon(c, kind, w, h, color_getter() if color_getter else None))
    return area


# --------------------------------------------------------------------------
# Capture / annotation window
# --------------------------------------------------------------------------
class ShotWindow(Gtk.ApplicationWindow):
    """Fullscreen window showing the frozen screen, the selection and the toolbars."""

    def __init__(self, app, image, cfg):
        super().__init__(application=app, title="melihSS")
        self.cfg = cfg
        self.img = image
        self.img_w, self.img_h = image.get_width(), image.get_height()
        self.shapes, self.redo_stack = [], []
        self.current = None      # shape being drawn
        self.editing = None      # text being typed
        self.sel = None          # (x, y, w, h) in widget coordinates
        self.tool = None
        self.color = tuple(cfg["color"])
        self.width = int(cfg["width"])
        self.font = cfg["font"]
        self.dim = float(cfg["dim"])
        self.drag_mode = None    # "select" | "move" | "resize" | "draw"
        self.drag_start = (0, 0)
        self.drag_orig = None
        self.drag_handle = None
        self.pointer = (0, 0)
        self.hint_until = 0.0
        self.scroll_acc = 0.0
        self.tool_buttons = {}

        self.keymap = {}
        for action, accel in cfg["keys"].items():
            p = parse_accel(accel)
            if p and p not in self.keymap:
                self.keymap[p] = action
        redo_alt = (Gdk.KEY_z, int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
        self.keymap.setdefault(redo_alt, "redo")

        self.set_decorated(False)
        self.add_css_class("shot")

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self.on_draw)
        overlay = Gtk.Overlay()
        overlay.set_child(self.area)
        self.set_child(overlay)

        # Toolbars are overlay children positioned with margins, so they only
        # cover their own area and the canvas still gets the other clicks.
        self.vbar = self.build_tool_bar()
        self.hbar = self.build_action_bar()
        for bar in (self.vbar, self.hbar):
            bar.set_halign(Gtk.Align.START)
            bar.set_valign(Gtk.Align.START)
            bar.set_visible(False)
            overlay.add_overlay(bar)

        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.area.add_controller(drag)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        self.area.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self.on_scroll)
        self.area.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
        self.fullscreen()

    def key_hint(self, action):
        accel = self.cfg["keys"].get(action, "")
        return f"{ACTION_NAMES[action]} ({accel_label(accel)})" if accel else ACTION_NAMES[action]

    # ---------------- toolbars ----------------
    def make_button(self, kind, tooltip, callback, color_getter=None):
        btn = Gtk.Button()
        btn.set_child(icon_widget(kind, color_getter))
        btn.set_tooltip_text(tooltip)
        # Never take keyboard focus, otherwise Space/Enter would hit the button.
        btn.set_focusable(False)
        btn.set_focus_on_click(False)
        btn.connect("clicked", callback)
        return btn

    def build_tool_bar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("bar")
        for kind in TOOLS:
            btn = self.make_button(kind, self.key_hint(kind), lambda _b, k=kind: self.select_tool(k))
            self.tool_buttons[kind] = btn
            box.append(btn)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Color picker: palette popover plus a custom color dialog.
        self.color_icon = icon_widget("color", lambda: self.color)
        color_btn = Gtk.MenuButton()
        color_btn.set_child(self.color_icon)
        color_btn.set_tooltip_text("Color  •  Stroke width: mouse wheel")
        color_btn.set_focusable(False)
        color_btn.set_direction(Gtk.ArrowType.LEFT)
        pop = Gtk.Popover()
        grid = Gtk.Grid(row_spacing=4, column_spacing=4, margin_top=4,
                        margin_bottom=4, margin_start=4, margin_end=4)
        for i, c in enumerate(PALETTE):
            sw = Gtk.Button()
            sw.add_css_class("swatch")
            sw.set_child(icon_widget("color", lambda c=c: c, 22))
            sw.set_focusable(False)
            sw.connect("clicked", lambda _b, c=c: (self.set_color(c), pop.popdown()))
            grid.attach(sw, i % 4, i // 4, 1, 1)
        custom = Gtk.Button(label="Custom color…")
        custom.set_focusable(False)
        custom.connect("clicked", lambda _b: (pop.popdown(), self.choose_custom_color()))
        grid.attach(custom, 0, 2, 4, 1)
        pop.set_child(grid)
        color_btn.set_popover(pop)
        box.append(color_btn)

        box.append(self.make_button("undo", self.key_hint("undo"), lambda _b: self.undo()))
        return box

    def build_action_bar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        box.add_css_class("bar")
        box.append(self.make_button("settings", "Settings and shortcuts", lambda _b: self.open_settings()))
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        box.append(self.make_button("copy", self.key_hint("copy"), lambda _b: self.copy_and_quit()))
        box.append(self.make_button("save", self.key_hint("save"), lambda _b: self.save_and_quit()))
        box.append(self.make_button("close", "Close (Esc)", lambda _b: self.quit()))
        return box

    def update_bars(self):
        """Place the tool bar to the right of the selection and the action bar below it,
        flipping sides when there is no room on screen."""
        show = self.sel is not None and self.drag_mode not in ("select", "move", "resize")
        self.vbar.set_visible(show)
        self.hbar.set_visible(show)
        if not show:
            return
        W, H = self.area.get_width(), self.area.get_height()
        x, y, w, h = self.sel
        gap = 8
        vw = self.vbar.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        vh = self.vbar.measure(Gtk.Orientation.VERTICAL, vw)[1]
        hw = self.hbar.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        hh = self.hbar.measure(Gtk.Orientation.VERTICAL, hw)[1]

        vx = x + w + gap
        if vx + vw > W:
            vx = x - vw - gap
            if vx < 0:
                vx = x + w - vw - gap
        vy = min(max(0, y + h - vh), max(0, H - vh))

        hy = y + h + gap
        if hy + hh > H:
            hy = y - hh - gap
            if hy < 0:
                hy = y + h - hh - gap
        hx = min(max(0, x + w - hw), max(0, W - hw))
        # Keep the two bars from overlapping.
        if hx + hw > vx and hx < vx + vw and hy + hh > vy and hy < vy + vh:
            hx = max(0, vx - hw - gap)

        self.vbar.set_margin_start(int(max(0, vx)))
        self.vbar.set_margin_top(int(vy))
        self.hbar.set_margin_start(int(hx))
        self.hbar.set_margin_top(int(max(0, hy)))

    # ---------------- state ----------------
    def factor(self):
        """Image pixels per widget pixel."""
        return self.img_w / max(1, self.area.get_width())

    def text_size(self):
        return 12 + self.width * 3

    def select_tool(self, kind):
        self.commit_text()
        self.tool = None if self.tool == kind else kind  # clicking again deselects
        for k, btn in self.tool_buttons.items():
            if k == self.tool:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")
        self.update_cursor(*self.pointer)
        self.area.queue_draw()

    def set_color(self, c):
        self.color = c
        if self.editing:
            self.editing.color = c
        self.color_icon.queue_draw()
        self.area.queue_draw()

    def choose_custom_color(self):
        dlg = Gtk.ColorDialog()
        dlg.set_with_alpha(False)
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue = self.color
        rgba.alpha = 1.0

        def done(d, res):
            try:
                c = d.choose_rgba_finish(res)
            except GLib.Error:
                return  # cancelled
            self.set_color((c.red, c.green, c.blue))

        dlg.choose_rgba(self, rgba, None, done)

    def commit_text(self):
        if self.editing is not None:
            if self.editing.is_meaningful():
                self.shapes.append(self.editing)
                self.redo_stack.clear()
            self.editing = None
            self.area.queue_draw()

    def undo(self):
        if self.editing is not None:
            self.editing = None
        elif self.shapes:
            self.redo_stack.append(self.shapes.pop())
        self.area.queue_draw()

    def redo(self):
        if self.redo_stack:
            self.shapes.append(self.redo_stack.pop())
            self.area.queue_draw()

    def in_sel(self, x, y):
        if not self.sel:
            return False
        sx, sy, sw, sh = self.sel
        return sx <= x <= sx + sw and sy <= y <= sy + sh

    def hit_handle(self, x, y):
        """Return the edge/corner under the pointer ('n', 'se', ...) or None."""
        if not self.sel:
            return None
        x0, y0, w, h = self.sel
        x1, y1 = x0 + w, y0 + h
        t = 7
        in_x = x0 - t <= x <= x1 + t
        in_y = y0 - t <= y <= y1 + t
        v = "n" if (abs(y - y0) <= t and in_x) else "s" if (abs(y - y1) <= t and in_x) else ""
        hz = "w" if (abs(x - x0) <= t and in_y) else "e" if (abs(x - x1) <= t and in_y) else ""
        return (v + hz) or None

    @staticmethod
    def norm(x1, y1, x2, y2):
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))

    # ---------------- mouse ----------------
    def update_cursor(self, x, y):
        name = "crosshair"
        if self.sel:
            if self.tool == "text" and self.in_sel(x, y):
                name = "text"
            elif self.tool is None:
                h = self.hit_handle(x, y)
                if h in ("nw", "se"):
                    name = "nwse-resize"
                elif h in ("ne", "sw"):
                    name = "nesw-resize"
                elif h in ("n", "s"):
                    name = "ns-resize"
                elif h in ("e", "w"):
                    name = "ew-resize"
                elif self.in_sel(x, y):
                    name = "move"
        self.area.set_cursor(Gdk.Cursor.new_from_name(name))

    def on_motion(self, _ctrl, x, y):
        self.pointer = (x, y)
        if self.drag_mode is None:
            self.update_cursor(x, y)
        if time.monotonic() < self.hint_until:
            self.area.queue_draw()

    def on_drag_begin(self, _g, x, y):
        self.drag_start = (x, y)
        self.drag_mode = None
        if self.sel and self.tool:
            self.commit_text()
            if self.tool == "text":
                if self.in_sel(x, y):
                    self.editing = Shape("text", self.color, self.width,
                                         p1=(x, y - self.text_size() * 0.6),
                                         size=self.text_size(), font=self.font)
            else:
                self.current = Shape(self.tool, self.color, self.width, p1=(x, y))
                self.drag_mode = "draw"
        else:
            self.commit_text()
            handle = self.hit_handle(x, y) if self.sel else None
            if handle:
                self.drag_mode, self.drag_handle, self.drag_orig = "resize", handle, self.sel
            elif self.in_sel(x, y):
                self.drag_mode, self.drag_orig = "move", self.sel
            else:
                self.drag_mode = "select"
                self.sel = None
        self.update_bars()
        self.area.queue_draw()

    def on_drag_update(self, _g, dx, dy):
        sx, sy = self.drag_start
        W, H = self.area.get_width(), self.area.get_height()
        x, y = sx + dx, sy + dy
        cx, cy = min(max(0, x), W), min(max(0, y), H)
        m = self.drag_mode
        if m == "select":
            self.sel = self.norm(sx, sy, cx, cy)
        elif m == "move":
            ox, oy, ow, oh = self.drag_orig
            self.sel = (min(max(0, ox + dx), W - ow), min(max(0, oy + dy), H - oh), ow, oh)
        elif m == "resize":
            ox, oy, ow, oh = self.drag_orig
            x1, y1, x2, y2 = ox, oy, ox + ow, oy + oh
            hd = self.drag_handle
            if "w" in hd:
                x1 = cx
            if "e" in hd:
                x2 = cx
            if "n" in hd:
                y1 = cy
            if "s" in hd:
                y2 = cy
            self.sel = self.norm(x1, y1, x2, y2)
        elif m == "draw" and self.current:
            if self.current.kind in ("pen", "marker"):
                lx, ly = self.current.points[-1]
                if math.hypot(x - lx, y - ly) >= 1:
                    self.current.points.append((x, y))
            else:
                self.current.p2 = (x, y)
        self.pointer = (x, y)
        self.area.queue_draw()

    def on_drag_end(self, _g, _dx, _dy):
        m = self.drag_mode
        if m in ("select", "move", "resize"):
            if self.sel and (self.sel[2] < 4 or self.sel[3] < 4):
                self.sel = None  # treat tiny selections as a click
        elif m == "draw" and self.current:
            if self.current.is_meaningful():
                self.shapes.append(self.current)
                self.redo_stack.clear()
            self.current = None
        self.drag_mode = None
        self.update_bars()
        self.update_cursor(*self.pointer)
        self.area.queue_draw()

    def on_scroll(self, _ctrl, _dx, dy):
        # Accumulate so smooth touchpad scrolling does not change the width too fast.
        self.scroll_acc += dy
        changed = False
        while abs(self.scroll_acc) >= 1:
            step = -1 if self.scroll_acc > 0 else 1
            self.scroll_acc += step
            self.width = max(1, min(40, self.width + step))
            changed = True
        if changed:
            if self.editing:
                self.editing.size = self.text_size()
            self.hint_until = time.monotonic() + 0.9
            GLib.timeout_add(950, lambda: (self.area.queue_draw(), False)[1])
            self.area.queue_draw()
        return True

    # ---------------- keyboard ----------------
    def run_action(self, action):
        if action == "copy":
            self.copy_and_quit()
        elif action == "save":
            self.save_and_quit()
        elif action == "undo":
            self.undo()
        elif action == "redo":
            self.redo()
        elif action in TOOLS:
            if self.sel:
                self.select_tool(action)

    def on_key(self, _ctrl, keyval, _code, state):
        mods = int(state & MOD_MASK)
        action = self.keymap.get((Gdk.keyval_to_lower(keyval), mods))
        strong = bool(mods & int(STRONG_MODS))

        # While typing text only Ctrl/Alt/Super shortcuts act as commands.
        if self.editing is not None and not strong:
            if keyval == Gdk.KEY_Escape:
                self.commit_text()
            elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                if mods & int(Gdk.ModifierType.SHIFT_MASK):
                    self.editing.text += "\n"
                else:
                    self.commit_text()
            elif keyval == Gdk.KEY_BackSpace:
                self.editing.text = self.editing.text[:-1]
            else:
                ch = Gdk.keyval_to_unicode(keyval)
                if ch >= 32:
                    self.editing.text += chr(ch)
                else:
                    return False
            self.area.queue_draw()
            return True

        if action:
            self.run_action(action)
            return True
        if keyval == Gdk.KEY_Escape and not mods:
            self.quit()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not mods:
            self.copy_and_quit()
            return True
        return False

    # ---------------- output ----------------
    def render(self):
        return render_region(self.img, self.sel, self.shapes, self.factor())

    def png_bytes(self):
        buf = io.BytesIO()
        self.render().write_to_png(buf)
        return buf.getvalue()

    def copy_and_quit(self):
        self.commit_text()
        if copy_png_to_clipboard(self.png_bytes()) and self.cfg.get("notify_copy"):
            notify("Copied to clipboard")
        self.quit()

    def save_and_quit(self):
        self.commit_text()
        save_dir = os.path.expanduser(self.cfg.get("save_dir") or DEFAULTS["save_dir"])
        try:
            os.makedirs(save_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            path = os.path.join(save_dir, f"Screenshot from {stamp}.png")
            n = 1
            while os.path.exists(path):
                path = os.path.join(save_dir, f"Screenshot from {stamp} ({n}).png")
                n += 1
            data = self.png_bytes()
            with open(path, "wb") as fh:
                fh.write(data)
            if self.cfg.get("save_also_copy"):
                copy_png_to_clipboard(data)
            notify(f"Saved: {path}")
        except OSError as e:
            notify(f"Could not save: {e}")
        self.quit()

    def open_settings(self):
        app = self.get_application()
        app.open_settings()  # open first so the application keeps running
        self.destroy()

    def quit(self):
        self.destroy()

    # ---------------- drawing ----------------
    def on_draw(self, _area, ctx, w, h):
        # Frozen screenshot
        ctx.save()
        ctx.scale(w / self.img_w, h / self.img_h)
        ctx.set_source_surface(self.img, 0, 0)
        ctx.paint()
        ctx.restore()

        # Dim everything except the selection
        ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        ctx.set_source_rgba(0, 0, 0, self.dim)
        ctx.rectangle(0, 0, w, h)
        if self.sel:
            ctx.rectangle(*self.sel)
        ctx.fill()
        ctx.set_fill_rule(cairo.FILL_RULE_WINDING)

        if self.sel:
            x, y, sw, sh = self.sel
            ctx.save()
            ctx.rectangle(x, y, sw, sh)
            ctx.clip()
            for s in self.shapes:
                draw_shape(ctx, s)
            if self.current:
                draw_shape(ctx, self.current)
            if self.editing:
                draw_shape(ctx, self.editing, editing=True)
            ctx.restore()

            ctx.set_line_width(1)
            ctx.set_source_rgba(1, 1, 1, 0.95)
            ctx.set_dash([5, 4])
            ctx.rectangle(x + 0.5, y + 0.5, max(0, sw - 1), max(0, sh - 1))
            ctx.stroke()
            ctx.set_dash([])

            if self.tool is None:
                mx, my = x + sw / 2, y + sh / 2
                for hx, hy in ((x, y), (mx, y), (x + sw, y), (x + sw, my),
                               (x + sw, y + sh), (mx, y + sh), (x, y + sh), (x, my)):
                    ctx.rectangle(hx - 3.5, hy - 3.5, 7, 7)
                    ctx.set_source_rgb(1, 1, 1)
                    ctx.fill_preserve()
                    ctx.set_source_rgb(0.2, 0.2, 0.2)
                    ctx.stroke()

            # Selection size in real image pixels
            f = self.factor()
            label = f"{round(sw * f)} × {round(sh * f)}"
            ly = y - 26 if y >= 26 else y + 4
            self.draw_label(ctx, label, x + (0 if y >= 26 else 4), ly, 12)
        else:
            copy_key = accel_label(self.cfg["keys"].get("copy", ""))
            msg = f"Drag to select an area   •   {copy_key}: copy full screen   •   Esc: quit"
            layout = PangoCairo.create_layout(ctx)
            layout.set_font_description(Pango.FontDescription.from_string("Sans 13"))
            layout.set_text(msg, -1)
            _ink, lg = layout.get_pixel_extents()
            self.draw_label(ctx, msg, (w - lg.width) / 2 - 12, h * 0.08, 13)

        # Short-lived indicator after changing the width with the mouse wheel
        if time.monotonic() < self.hint_until:
            px, py = self.pointer
            if self.tool == "text" or self.editing:
                txt = f"Text size: {self.text_size()} px"
            else:
                txt = f"Stroke width: {self.width} px"
            ctx.set_source_rgb(*self.color)
            ctx.arc(px, py, max(1.5, self.width / 2), 0, 2 * math.pi)
            ctx.fill()
            self.draw_label(ctx, txt, px + 16, py + 12, 11)

    @staticmethod
    def draw_label(ctx, text, x, y, size):
        """Draw text on a rounded dark background."""
        layout = PangoCairo.create_layout(ctx)
        layout.set_font_description(Pango.FontDescription.from_string(f"Sans {size}"))
        layout.set_text(text, -1)
        _ink, lg = layout.get_pixel_extents()
        pad = 6
        bw, bh = lg.width + pad * 2, lg.height + pad
        r = 5
        ctx.new_sub_path()
        ctx.arc(x + bw - r, y + r, r, -math.pi / 2, 0)
        ctx.arc(x + bw - r, y + bh - r, r, 0, math.pi / 2)
        ctx.arc(x + r, y + bh - r, r, math.pi / 2, math.pi)
        ctx.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        ctx.close_path()
        ctx.set_source_rgba(0.08, 0.08, 0.09, 0.85)
        ctx.fill()
        ctx.set_source_rgb(1, 1, 1)
        ctx.move_to(x + pad, y + pad / 2)
        PangoCairo.show_layout(ctx, layout)


# --------------------------------------------------------------------------
# Settings window
# --------------------------------------------------------------------------
class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="melihSS Settings")
        self.set_default_size(560, 760)
        self.set_icon_name(APP_ID)
        self.cfg = load_config()
        self.recording = None        # (button, kind, key) while capturing a shortcut
        self.global_old = None
        self.key_buttons = {}

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # --- General ---
        general = Adw.PreferencesGroup(title="General")
        page.add(general)

        self.global_btn = self.shortcut_button(get_global_shortcut(), "global", None)
        general.add(self.row("Screenshot shortcut",
                             "Freezes the screen and starts a selection", self.global_btn))

        self.dir_row = Adw.ActionRow(title="Save folder")
        self.dir_row.set_use_markup(False)
        self.dir_row.set_subtitle(display_path(self.cfg["save_dir"]))
        dir_btn = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        dir_btn.connect("clicked", self.on_choose_dir)
        self.dir_row.add_suffix(dir_btn)
        general.add(self.dir_row)

        notify_row = Adw.SwitchRow(title="Show a notification after copying")
        notify_row.set_active(bool(self.cfg["notify_copy"]))
        notify_row.connect("notify::active", lambda r, _p: self.set_cfg("notify_copy", r.get_active()))
        general.add(notify_row)

        also_row = Adw.SwitchRow(title="Also copy to clipboard when saving")
        also_row.set_active(bool(self.cfg["save_also_copy"]))
        also_row.connect("notify::active", lambda r, _p: self.set_cfg("save_also_copy", r.get_active()))
        general.add(also_row)

        # --- Drawing & appearance ---
        look = Adw.PreferencesGroup(title="Drawing and appearance")
        page.add(look)

        color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue = self.cfg["color"]
        rgba.alpha = 1.0
        color_btn.set_rgba(rgba)
        color_btn.connect("notify::rgba", lambda b, _p: self.set_cfg(
            "color", [b.get_rgba().red, b.get_rgba().green, b.get_rgba().blue]))
        look.add(self.row("Default color", None, color_btn))

        width_row = Adw.SpinRow.new_with_range(1, 40, 1)
        width_row.set_title("Default stroke width")
        width_row.set_subtitle("Can also be changed with the mouse wheel while editing")
        width_row.set_value(self.cfg["width"])
        width_row.connect("notify::value", lambda r, _p: self.set_cfg("width", int(r.get_value())))
        look.add(width_row)

        font_btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog())
        font_btn.set_level(Gtk.FontLevel.FAMILY)
        font_btn.set_font_desc(Pango.FontDescription.from_string(self.cfg["font"]))
        font_btn.connect("notify::font-desc", lambda b, _p: self.set_cfg(
            "font", b.get_font_desc().get_family() or "Sans"))
        look.add(self.row("Font", "Used by the text tool", font_btn))

        dim = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 90, 5)
        dim.set_size_request(180, -1)
        dim.set_value(round(self.cfg["dim"] * 100))
        dim.set_draw_value(True)
        dim.set_format_value_func(lambda _s, v: f"{int(v)}%")
        dim.connect("value-changed", lambda s: self.set_cfg("dim", s.get_value() / 100))
        look.add(self.row("Screen dimming", "Darkness of the area outside the selection", dim))

        # --- Editor shortcuts ---
        keys = Adw.PreferencesGroup(
            title="Editor shortcuts",
            description="Click a button, then press the new key combination. "
                        "Esc cancels, Backspace removes the shortcut.\n"
                        "Fixed keys: Esc closes, Enter copies, mouse wheel changes the width.")
        page.add(keys)
        for action in ["copy", "save", "undo", "redo"] + TOOLS:
            btn = self.shortcut_button(self.cfg["keys"].get(action, ""), "edit", action)
            self.key_buttons[action] = btn
            keys.add(self.row(ACTION_NAMES[action], None, btn))

        # --- Reset ---
        reset_group = Adw.PreferencesGroup()
        page.add(reset_group)
        reset = Gtk.Button(label="Restore defaults", halign=Gtk.Align.CENTER)
        reset.add_css_class("destructive-action")
        reset.add_css_class("pill")
        reset.connect("clicked", self.on_reset)
        reset_group.add(reset)

        ctrl = Gtk.EventControllerKey()
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl.connect("key-pressed", self.on_key)
        self.add_controller(ctrl)
        self.connect("close-request", self.on_close)

    # ---------------- helpers ----------------
    @staticmethod
    def row(title, subtitle, widget):
        r = Adw.ActionRow(title=title)
        r.set_use_markup(False)  # labels such as "Ctrl+<" must not be parsed as markup
        if subtitle:
            r.set_subtitle(subtitle)
        widget.set_valign(Gtk.Align.CENTER)
        r.add_suffix(widget)
        return r

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=3))

    def set_cfg(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    def shortcut_button(self, accel, kind, key):
        btn = Gtk.Button(label=accel_label(accel))
        btn.add_css_class("shortcut-btn")
        btn.connect("clicked", lambda b: self.start_record(b, kind, key))
        return btn

    # ---------------- shortcut recording ----------------
    def start_record(self, btn, kind, key):
        if self.recording:
            self.stop_record(cancel=True)
        self.recording = (btn, kind, key)
        btn.set_label("Press keys…")
        btn.add_css_class("suggested-action")
        if kind == "global":
            # Disable the current global shortcut while recording, otherwise
            # pressing it would launch a new capture instead of reaching us.
            self.global_old = get_global_shortcut()
            set_global_shortcut("")

    def stop_record(self, cancel):
        if not self.recording:
            return
        btn, kind, key = self.recording
        self.recording = None
        btn.remove_css_class("suggested-action")
        if kind == "global":
            if cancel and self.global_old is not None:
                set_global_shortcut(self.global_old)
            self.global_old = None
            btn.set_label(accel_label(get_global_shortcut()))
        else:
            btn.set_label(accel_label(self.cfg["keys"].get(key, "")))

    def finish_record(self, accel):
        btn, kind, key = self.recording
        if kind == "global":
            set_global_shortcut(accel)
            self.global_old = None
            self.toast(f"Screenshot shortcut: {accel_label(accel)}")
        else:
            new = parse_accel(accel)
            if new:
                # One key per action: clear any other action using the same key.
                for other, oacc in self.cfg["keys"].items():
                    if other != key and parse_accel(oacc) == new:
                        self.cfg["keys"][other] = ""
                        self.key_buttons[other].set_label("None")
                        self.toast(f"Removed the shortcut of “{ACTION_NAMES[other]}” (conflict)")
            self.cfg["keys"][key] = accel
            save_config(self.cfg)
        self.stop_record(cancel=False)

    def on_key(self, _ctrl, keyval, _code, state):
        if not self.recording:
            return False
        if keyval in MODIFIER_KEYVALS:
            return True  # wait for the actual key
        mods = state & MOD_MASK
        if keyval == Gdk.KEY_Escape and not mods:
            self.stop_record(cancel=True)
        elif keyval == Gdk.KEY_BackSpace and not mods:
            self.finish_record("")
        else:
            self.finish_record(Gtk.accelerator_name(Gdk.keyval_to_lower(keyval), mods))
        return True

    # ---------------- other ----------------
    def on_choose_dir(self, _btn):
        dlg = Gtk.FileDialog(title="Choose a save folder")
        current = os.path.expanduser(self.cfg["save_dir"])
        if os.path.isdir(current):
            dlg.set_initial_folder(Gio.File.new_for_path(current))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                path = f.get_path()
                home = os.path.expanduser("~")
                stored = "~" + path[len(home):] if path.startswith(home + os.sep) else path
                self.set_cfg("save_dir", stored)
                self.dir_row.set_subtitle(display_path(path))

        dlg.select_folder(self, None, done)

    def on_reset(self, _btn):
        self.stop_record(cancel=True)
        save_config(copy.deepcopy(DEFAULTS))
        set_global_shortcut(DEFAULT_GLOBAL_SHORTCUT)
        app = self.get_application()
        new = SettingsWindow(app)
        new.present()
        new.toast("Settings restored to defaults")
        self.destroy()

    def on_close(self, *_):
        self.stop_record(cancel=True)
        return False


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if "--version" in args:
            cmdline.print_literal(f"melihSS {__version__}\n")
            return 0
        if "--settings" in args or "-s" in args:
            self.open_settings()
        else:
            self.start_capture()
        return 0

    def do_activate(self):
        self.start_capture()

    def start_capture(self):
        # A second hotkey press while capturing just raises the existing window.
        for w in self.get_windows():
            if isinstance(w, ShotWindow):
                w.present()
                return
        try:
            image = capture_screen()
        except Exception as e:  # noqa: BLE001 - show any failure to the user
            notify(f"Could not take a screenshot: {e}")
            return
        ShotWindow(self, image, load_config()).present()

    def open_settings(self):
        for w in self.get_windows():
            if isinstance(w, SettingsWindow):
                w.present()
                return
        SettingsWindow(self).present()


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
