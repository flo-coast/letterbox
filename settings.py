#!/usr/bin/env python3
# settings.py: settings app for letterbox mode. Live preview of the screen split with the real bar drawn
# from live data (same Renderer as the bar), on/off switch, game size and position, start mode, background
# (plain / ambient), theme, colours and fonts, clock, tile layout, event timer, combat behaviour, profiles.
# Every change is saved to ~/.config/letterbox/config.json at once; a running bar reloads it.
import glob, importlib.util, json, os, re, shutil, subprocess, sys

import cairo

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

DIR = os.path.dirname(os.path.abspath(__file__))
FLAG = os.path.expanduser("~/.config/letterbox/enabled")
PROFILES = os.path.expanduser("~/.config/letterbox/profiles")    # <name>.json, full copies of the config
SH = os.path.join(DIR, "letterbox")
POSITIONS = [("bottom", "Bottom (bar on top)"), ("center", "Center"), ("top", "Top (bar below)")]
MODE_NAMES = [("full", "Full"), ("compact", "Compact"), ("clock", "Clock only"), ("off", "Off (black)")]
CLOCK_STYLES = [("digital", "Digital"), ("analog", "Analog"), ("minimal", "Minimal"), ("off", "No clock")]
COMBAT = [("off", "Nothing changes"), ("dim", "Dim the tiles"), ("warnings", "Only warnings")]
GRAPH = [(30, "30 s"), (60, "60 s"), (120, "2 min"), (300, "5 min")]
GROUP_NAMES = {"fps": "FPS", "cpu": "CPU", "gpu": "GPU", "smc": "SMC", "game": "game", "fan": "fans", "mem": "memory",
               "net": "network"}
TEXT_COLORS = [("number", "Numbers"), ("dim", "Small text"), ("bg", "Background")]

_spec = importlib.util.spec_from_file_location("letterbox_bar", os.path.join(DIR, "bar.py"))
gb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gb)
OUT_W = OUT_H = 0                                    # the monitor GW2 goes on, in device pixels
PRESETS = []


def screen_setup():
    """Monitor size and the game size presets for it (again after the monitor was changed)."""
    global OUT_W, OUT_H, PRESETS
    scr = gb.screen() or dict(name="?", w=3840, h=2160)
    OUT_W, OUT_H = scr["w"], scr["h"]

    def preset(label, ratio):
        h = int(OUT_W / ratio) // 2 * 2
        return (f"{label}  ({OUT_W}×{h})", h)
    PRESETS = [p for p in (preset("2.13 : 1", 2.133), preset("21 : 9", 21 / 9), preset("2.4 : 1", 2.4),
                           preset("2 : 1", 2.0)) if OUT_H // 2 <= p[1] < OUT_H] + [("custom", None)]


screen_setup()


def list_profiles():
    try:
        return sorted(f[:-5] for f in os.listdir(PROFILES) if f.endswith(".json"))
    except OSError:
        return []


def gw2_running():
    return subprocess.run(["pgrep", "-i", "-x", "gw2-64.exe"], capture_output=True).returncode == 0


def rgba(hexstr):
    c = Gdk.RGBA()
    c.parse(hexstr)
    return c


def hexof(c):
    return "#{:02x}{:02x}{:02x}".format(*(round(x * 255) for x in (c.red, c.green, c.blue)))


def swatch(hexstr, on_change):
    """Small colour button (a swatch, not a full-width bar)."""
    btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), halign=Gtk.Align.START,
                                valign=Gtk.Align.CENTER, width_request=44)
    btn.set_rgba(rgba(hexstr))
    btn.connect("notify::rgba", on_change)
    return btn


def header(cols):
    """Column titles above a list (outside its scrolled window, so they stay visible). Same margins and
    spacing as the rows, plus the list row padding, so the titles sit above their checkboxes."""
    head = Gtk.Box(spacing=10, margin_start=6 + 2, margin_end=6, margin_top=4)
    for txt, wd in cols:
        head.append(Gtk.Label(label=f"<small><b>{txt}</b></small>", use_markup=True, width_request=wd, xalign=0))
    return head


def keep_scroll(sc, rebuild):
    """Rebuild a scrolled list without jumping back to the top: restore the scroll position once the new
    content has its size (the adjustment shrinks to 0 while the old children are removed)."""
    adj = sc.get_vadjustment()
    v = adj.get_value()
    rebuild()
    t_end = GLib.get_monotonic_time() + 400_000

    def restore(a):
        a.set_value(min(v, a.get_upper() - a.get_page_size()))
        if GLib.get_monotonic_time() > t_end or a.get_upper() - a.get_page_size() >= v:
            a.disconnect(hid)
    hid = adj.connect("changed", restore)
    adj.set_value(v)


def dropdown(items, current, on_change):
    dd = Gtk.DropDown.new_from_strings([n for _, n in items])
    dd.set_halign(Gtk.Align.START)             # as wide as its longest entry, not the whole column
    keys = [k for k, _ in items]
    dd.set_selected(keys.index(current) if current in keys else 0)
    dd.connect("notify::selected", lambda d, _p: on_change(keys[d.get_selected()]))
    return dd


class Settings(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="GW2 letterbox bar")
        self.set_default_size(1150, 1000)
        self.c = gb.load_config()
        self.data = gb.Data(self.c.get("graph_seconds", 60), self.c)
        self.renderer = gb.Renderer()
        self.color_buttons = {}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=14, margin_bottom=14,
                        margin_start=16, margin_end=16)
        self.set_child(outer)
        top = Gtk.Box(spacing=12)
        top.append(Gtk.Label(label="<b>Letterbox mode</b>", use_markup=True, xalign=0, hexpand=True))
        self.switch = Gtk.Switch(active=os.path.exists(FLAG), valign=Gtk.Align.CENTER)
        self.switch.connect("state-set", self.on_switch)
        top.append(self.switch)
        outer.append(top)
        self.status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        outer.append(self.status)
        self.area = Gtk.DrawingArea(content_height=330, hexpand=True)
        self.area.set_draw_func(self.draw_preview)
        outer.append(self.area)

        outer.append(self.profile_row())
        nb = self.nb = Gtk.Notebook(vexpand=True)
        nb.add_css_class("letterbox-tabs")
        css = Gtk.CssProvider()                   # divider lines between the tab headers
        css.load_from_string(".letterbox-tabs > header > tabs > tab { padding: 6px 18px; "
                             "border-right: 1px solid alpha(currentColor, 0.3); }"
                             ".letterbox-tabs > header > tabs > tab:last-child { border-right: none; }")
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        outer.append(nb)
        nb.append_page(self.page_layout(), Gtk.Label(label="Layout & look"))
        nb.append_page(self.page_tiles(), Gtk.Label(label="Tiles"))
        nb.append_page(self.page_sensors(), Gtk.Label(label="Sensor options"))
        nb.append_page(self.page_clock(), Gtk.Label(label="Clock"))
        nb.append_page(self.page_events(), Gtk.Label(label="Events"))
        nb.append_page(self.page_behaviour(), Gtk.Label(label="Behaviour & hotkeys"))
        nb.append_page(self.page_extras(), Gtk.Label(label="Extras"))
        nb.append_page(self.page_system(), Gtk.Label(label="System"))

        bb = Gtk.Box(spacing=10, halign=Gtk.Align.END)
        self.test = Gtk.ToggleButton(label="Show bar on screen")
        self.test.connect("toggled", self.on_test)
        bb.append(self.test)
        self.connect("close-request", self.on_close)
        outer.append(bb)
        self.update_status()
        self.data.update()
        GLib.timeout_add(200, self.data.sample_gpu)
        GLib.timeout_add_seconds(1, self.tick)

    # ---------- profiles ----------
    def profile_row(self):
        box = Gtk.Box(spacing=8)
        box.append(Gtk.Label(label="<b>Profile</b>", use_markup=True))
        self.prof_dd = Gtk.DropDown.new_from_strings(list_profiles() or ["(none saved)"])
        self.prof_dd.set_halign(Gtk.Align.START)
        box.append(self.prof_dd)
        box.append(Gtk.Box(hexpand=True))          # buttons stay on the right
        for label, cb in (("Load", self.on_load), ("Delete", self.on_delete)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            box.append(b)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.prof_name = Gtk.Entry(placeholder_text="name, e.g. WvW", width_chars=16)
        self.prof_name.connect("activate", self.on_save)
        box.append(self.prof_name)
        b = Gtk.Button(label="Save as new")
        b.connect("clicked", self.on_save)
        box.append(b)
        self.prof_over = Gtk.Button(css_classes=["suggested-action"])
        self.prof_over.connect("clicked", self.on_save_current)
        box.append(self.prof_over)
        self.sync_prof_button()
        return box

    def sync_prof_button(self):
        """'Save to <profile>' for the profile last loaded or saved (kept in the config as 'profile')."""
        name = self.c.get("profile")
        ok = bool(name) and name in list_profiles()
        self.prof_over.set_label(f"Save to '{name}'" if ok else "Save to profile")
        self.prof_over.set_sensitive(ok)
        self.prof_over.set_tooltip_text(f"Overwrite profile '{name}' with the current settings" if ok else
                                        "Load or save a profile first")

    def write_profile(self, name):
        self.c["profile"] = name
        self.changed()
        os.makedirs(PROFILES, exist_ok=True)
        with open(os.path.join(PROFILES, name + ".json"), "w") as fh:
            json.dump(self.c, fh, indent=1)
        self.refresh_profiles(name)
        self.sync_prof_button()
        self.update_status(f"Saved profile '{name}'.")

    def on_save_current(self, _b):
        if self.c.get("profile") in list_profiles():
            self.write_profile(self.c["profile"])

    def refresh_profiles(self, select=None):
        names = list_profiles()
        self.prof_dd.set_model(Gtk.StringList.new(names or ["(none saved)"]))
        if select in names:
            self.prof_dd.set_selected(names.index(select))

    def selected_profile(self):
        names = list_profiles()
        i = self.prof_dd.get_selected()
        return names[i] if names and i < len(names) else None

    def on_save(self, _w):
        name = re.sub(r"[^\w .-]", "", self.prof_name.get_text()).strip()
        if not name:
            self.update_status("Type a name for the profile first.")
            return
        self.prof_name.set_text("")
        self.write_profile(name)

    def on_load(self, _b):
        name = self.selected_profile()
        if not name:
            return
        with open(os.path.join(PROFILES, name + ".json")) as src:
            prof = json.load(src)
        prof["profile"] = name                     # older profiles were saved without their own name
        with open(gb.CONF, "w") as dst:
            json.dump(prof, dst, indent=1)
        if self.test.get_active():
            self.stop_test()
        self.rebuild(f"Loaded profile '{name}'. Game size and position apply at the next GW2 start.", name)

    def rebuild(self, status, profile=None, page=None):
        """New window from the saved config (after loading a profile or changing the monitor)."""
        app = self.get_application()
        win = Settings(app)
        win.refresh_profiles(profile or self.c.get("profile"))
        win.update_status(status)
        if page is not None:
            win.nb.set_current_page(page)
        win.present()
        Gio.bus_get_sync(Gio.BusType.SESSION).call("letterbox.bar", "/letterbox/bar", "letterbox.Bar", "Reload", None,
                                                    None, Gio.DBusCallFlags.NONE, 500, None, None)
        self.destroy()

    def on_delete(self, _b):
        name = self.selected_profile()
        if not name:
            return
        os.remove(os.path.join(PROFILES, name + ".json"))
        self.refresh_profiles()
        self.sync_prof_button()
        self.update_status(f"Deleted profile '{name}'.")

    # ---------- pages ----------
    def grid(self):
        g = Gtk.Grid(column_spacing=14, row_spacing=10, margin_top=12, margin_start=12, margin_end=12,
                     margin_bottom=12)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(g)
        return sc, g

    def row(self, g, r, label, widget, extra=None):
        g.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
        g.attach(widget, 1, r, 1, 1)
        if extra:
            g.attach(extra, 2, r, 1, 1)

    def page_layout(self):
        sc, g = self.grid()
        self.preset = Gtk.DropDown.new_from_strings([p[0] for p in PRESETS])
        self.height = Gtk.SpinButton.new_with_range(OUT_H // 2, OUT_H - 10, 10)
        self.height.set_value(self.c["height"])
        self.saving = Gtk.Label(xalign=0)
        self.sync_preset()
        self.preset.connect("notify::selected", self.on_preset)
        self.height.connect("value-changed", self.on_height)
        hb = Gtk.Box(spacing=8)
        hb.append(self.preset)
        hb.append(Gtk.Label(label="height px"))
        hb.append(self.height)
        self.row(g, 0, "Game size", hb, self.saving)
        self.row(g, 1, "Position", dropdown(POSITIONS, self.c["position"], lambda v: self.set("position", v)))
        self.row(g, 2, "Mode at game start", dropdown(MODE_NAMES, self.c.get("mode", "full"),
                                                      lambda v: self.set("mode", v)))
        self.row(g, 3, "Graph period", dropdown(GRAPH, self.c.get("graph_seconds", 60),
                                                lambda v: self.set("graph_seconds", v)))
        gap = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 3.0, 0.1)
        gap.set_value(self.c.get("tile_gap", 1.0))
        gap.set_hexpand(True)
        gap.add_mark(1.0, Gtk.PositionType.BOTTOM, "default")
        gap.connect("value-changed", lambda w: self.set("tile_gap", round(w.get_value(), 2)))
        self.row(g, 4, "Space between tiles", gap)
        g.attach(Gtk.Separator(margin_top=6, margin_bottom=6), 0, 5, 3, 1)
        self.row(g, 6, "Theme", dropdown([(k, k) for k in gb.THEMES], self.c.get("theme", "classic"), self.on_theme))
        tc = Gtk.Box(spacing=16)
        for key, name in TEXT_COLORS:
            b = Gtk.Box(spacing=6)
            btn = swatch(self.current_hex(key), lambda w, _p, k=key: self.on_color(w, _p, k))
            self.color_buttons[key] = btn
            b.append(btn)
            b.append(Gtk.Label(label=name))
            tc.append(b)
        self.row(g, 7, "Text colours", tc)
        fb = Gtk.Box(spacing=16)
        for key, name, default in (("font_text", "Text", "sans"), ("font_number", "Numbers", "monospace")):
            b = Gtk.Box(spacing=6)
            btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), level=Gtk.FontLevel.FAMILY, use_font=True)
            desc = Pango.FontDescription.from_string(self.c.get(key) or default)
            btn.set_font_desc(desc)
            btn.connect("notify::font-desc", lambda w, _p, key=key: self.set(key, w.get_font_desc().get_family()))
            b.append(btn)
            b.append(Gtk.Label(label=name))
            fb.append(b)
        reset_f = Gtk.Button(label="Default fonts", css_classes=["flat"])
        reset_f.connect("clicked", lambda _b: self.reset_fonts())
        fb.append(reset_f)
        self.font_box = fb
        self.row(g, 13, "Fonts", fb)
        self.row(g, 8, "Background", dropdown([("black", "Plain (background colour)"),
                                                ("ambient", "Ambient: colours of the game image"),
                                                ("image", "Own picture")],
                                               self.c.get("background", "black"), self.on_background))
        self.row(g, 12, "Picture", self.picture_editor())
        dim = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.1, 0.8, 0.05)
        dim.set_value(self.c.get("ambient_dim", 0.35))
        dim.set_hexpand(True)
        dim.connect("value-changed", lambda w: self.set("ambient_dim", round(w.get_value(), 2)))
        dim.set_tooltip_text("Only with background 'Ambient'. How strongly the bar takes on the colours of the "
                             "game image right next to it: low = dark bar with a faint tint, high = bright, "
                             "clearly coloured bar (the text stays readable).")
        self.row(g, 9, "Ambient strength", dim)
        rate = Gtk.SpinButton.new_with_range(1, 8, 1)
        rate.set_value(self.c.get("ambient_rate", 4))
        rate.set_halign(Gtk.Align.START)
        rate.connect("value-changed", lambda w: self.set("ambient_rate", int(w.get_value())))
        rate.set_tooltip_text("Only with background 'Ambient'. How often per second the game image is sampled for "
                              "the colours: more = the bar follows fast camera turns more closely, fewer = "
                              "calmer and a little less load (each sample costs ~2 ms of KWin time).")
        self.row(g, 10, "Ambient samples per second", rate,
                 Gtk.Label(label="each costs ~2 ms of KWin time", css_classes=["dim-label"]))
        bri = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.3, 1.0, 0.05)
        bri.set_value(self.c.get("brightness", 1.0))
        bri.connect("value-changed", lambda w: self.set("brightness", round(w.get_value(), 2)))
        self.row(g, 11, "Brightness of the bar", bri)
        reset = Gtk.Button(label="Reset all colours to the theme", halign=Gtk.Align.START)
        reset.connect("clicked", self.on_reset_colors)
        self.row(g, 14, "", reset)
        self.on_height_label()
        return sc

    # ---------- own background picture ----------
    def on_background(self, v):
        self.set("background", v)
        if v == "image" and not self.c.get("bg_image"):
            self.pick_picture()

    def bar_size(self):
        """The bar's size in device pixels (for the picture's frame)."""
        free = OUT_H - self.c["height"]
        return OUT_W, max(1, free // 2 if self.c.get("position") == "center" else free)

    def picture_editor(self):
        """Choose a picture, then move (drag) and zoom (wheel or slider) the frame that shows what the bar
        gets; darken and blur for readable text."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        top = Gtk.Box(spacing=8)
        choose = Gtk.Button(label="Choose picture…")
        choose.connect("clicked", lambda _b: self.pick_picture())
        top.append(choose)
        reset = Gtk.Button(label="Reset position", css_classes=["flat"])
        reset.connect("clicked", lambda _b: self.picture_set(bg_zoom=1.0, bg_x=0.5, bg_y=0.5, save=True))
        top.append(reset)
        self.pic_label = Gtk.Label(xalign=0, css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.MIDDLE,
                                   hexpand=True)
        top.append(self.pic_label)
        box.append(top)
        self.pic_area = Gtk.DrawingArea(content_height=220, hexpand=True)
        self.pic_area.set_draw_func(self.draw_picture_editor)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda _g, _x, _y: setattr(self, "_drag0", (self.c.get("bg_x", 0.5),
                                                                                self.c.get("bg_y", 0.5))))
        drag.connect("drag-update", self.on_picture_drag)
        drag.connect("drag-end", lambda *_: self.changed())
        self.pic_area.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda _c, _dx, dy: (self.picture_set(
            bg_zoom=min(8.0, max(1.0, self.c.get("bg_zoom", 1.0) * (0.9 if dy > 0 else 1.1))), save=True), True)[1])
        self.pic_area.add_controller(scroll)
        box.append(self.pic_area)
        self.pic_sliders = {}
        for key, label, lo, hi, step in (("bg_zoom", "Zoom", 1.0, 8.0, 0.05), ("bg_dim", "Darken", 0.0, 0.9, 0.05),
                                          ("bg_blur", "Blur", 0, 40, 1)):
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Label(label=label, width_chars=7, xalign=0))
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            sc.set_hexpand(True)
            sc.set_value(self.c.get(key, gb.DEFAULTS[key]))
            sc.connect("value-changed", lambda w, k=key: None if getattr(self, "_pic_sync", False) else
                       self.picture_set(**{k: int(w.get_value()) if k == "bg_blur" else round(w.get_value(), 2)},
                                        save=True))
            row.append(sc)
            self.pic_sliders[key] = sc
            box.append(row)
        box.append(Gtk.Label(label="Drag the frame to move it, scroll to zoom. The frame has the bar's shape.",
                             xalign=0, css_classes=["dim-label"]))
        self.picture_label()
        return box

    def picture_label(self):
        p = self.c.get("bg_image", "")
        self.pic_label.set_text(os.path.basename(self.c.get("bg_image_name", "") or p) if p else "no picture yet")

    def pick_picture(self):
        dlg = Gtk.FileDialog(title="Background picture", modal=True)
        flt = Gtk.FileFilter(name="Pictures")
        flt.add_mime_type("image/*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)
        pics = os.path.expanduser("~/Pictures")
        if os.path.isdir(pics):
            dlg.set_initial_folder(Gio.File.new_for_path(pics))

        def done(d, res):
            try:
                src = d.open_finish(res).get_path()
            except GLib.Error:
                return
            ext = os.path.splitext(src)[1].lower() or ".img"
            dst = os.path.join(os.path.dirname(gb.CONF), "wallpaper" + ext)   # a copy: the original may move
            for old in glob.glob(os.path.join(os.path.dirname(gb.CONF), "wallpaper.*")):
                os.remove(old)
            shutil.copyfile(src, dst)
            if gb.load_picture(dst) is None:
                self.update_status("That file isn't a picture letterbox can read.")
                return
            self.c.update(bg_image=dst, bg_image_name=os.path.basename(src), background="image")
            self.picture_set(bg_zoom=1.0, bg_x=0.5, bg_y=0.5, save=True)
            self.picture_label()
        dlg.open(self, None, done)

    def picture_set(self, save=False, **kw):
        self.c.update(kw)
        self._pic_sync = True
        for k, sc in self.pic_sliders.items():
            if k in kw:
                sc.set_value(kw[k])
        self._pic_sync = False
        self.pic_area.queue_draw()
        if save:
            self.changed()
        else:
            self.area.queue_draw()

    def editor_geometry(self, aw, ah):
        """Picture placement in the editor: (surface, scale, x0, y0, crop rect in picture px)."""
        surf = gb.load_picture(self.c.get("bg_image"))
        if surf is None:
            return None
        iw, ih = surf.get_width(), surf.get_height()
        k = min(aw / iw, ah / ih)
        bw, bh = self.bar_size()
        crop = gb.crop_rect(iw, ih, bw / bh, self.c.get("bg_zoom", 1.0), self.c.get("bg_x", 0.5), self.c.get("bg_y", 0.5))
        return surf, k, (aw - iw * k) / 2, (ah - ih * k) / 2, crop

    def draw_picture_editor(self, _a, cr, w, h):
        geo = self.editor_geometry(w, h)
        if geo is None:
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.paint()
            return
        surf, k, x0, y0, (cx, cy, cw, ch) = geo
        cr.save()
        cr.translate(x0, y0)
        cr.scale(k, k)
        cr.set_source_surface(surf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.6)                   # dim everything outside the frame
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(x0, y0, surf.get_width() * k, surf.get_height() * k)
        cr.rectangle(x0 + cx * k, y0 + cy * k, cw * k, ch * k)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(2)
        cr.rectangle(x0 + cx * k, y0 + cy * k, cw * k, ch * k)
        cr.stroke()

    def on_picture_drag(self, _g, dx, dy):
        geo = self.editor_geometry(self.pic_area.get_width(), self.pic_area.get_height())
        if geo is None:
            return
        surf, k, _x0, _y0, (_cx, _cy, cw, ch) = geo
        free_x, free_y = (surf.get_width() - cw) * k, (surf.get_height() - ch) * k
        x0, y0 = self._drag0
        self.picture_set(bg_x=min(1, max(0, x0 + dx / free_x)) if free_x > 1 else 0.5,
                         bg_y=min(1, max(0, y0 + dy / free_y)) if free_y > 1 else 0.5)

    def page_tiles(self):
        """Layout editor: blocks left to right, each with a width and rows of tiles."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_start=10,
                      margin_end=10)
        sw = Gtk.Box(spacing=12)
        sw.append(Gtk.Label(label="<b>Sensors in the bar</b>  (off = plain strip with only the clock)",
                            use_markup=True, xalign=0, hexpand=True))
        s = Gtk.Switch(active=self.c.get("sensors", True), valign=Gtk.Align.CENTER)
        s.connect("notify::active", lambda w, _p: self.set("sensors", w.get_active()))
        sw.append(s)
        box.append(sw)
        tb = Gtk.Box(spacing=8)
        tb.append(Gtk.Label(label="Edit layout"))
        tb.append(dropdown([("full", "Full"), ("compact", "Compact")], "full", self.on_edit_mode))
        add = Gtk.Button(label="+ Block")
        add.connect("clicked", lambda _b: self.lay_op("add_block"))
        tb.append(add)
        flat = Gtk.Button(label="One block per tile")
        flat.connect("clicked", lambda _b: self.lay_op("flatten"))
        tb.append(flat)
        box.append(tb)
        box.append(Gtk.Label(label="The bar is made of blocks from left to right. Width: share of the bar "
                                   "(a block with width 4 is four times as wide as one with 1). A block's rows "
                                   "are stacked; a row's tiles sit side by side. Meta+Shift+B switches between "
                                   "Full and Compact in game.", xalign=0, wrap=True, css_classes=["dim-label"]))
        self.lay = {m: [dict(w=b["w"], rows=[list(r) for r in b["rows"]]) for b in gb.layout(self.c, m)]
                    for m in ("full", "compact")}
        self.edit_mode = "full"
        self.lay_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.lay_sc = Gtk.ScrolledWindow(vexpand=True)
        self.lay_sc.set_child(self.lay_box)
        box.append(self.lay_sc)
        self.render_layout()
        return box

    def page_sensors(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_start=10,
                      margin_end=10)
        box.append(Gtk.Label(label="Number / graph per sensor (text tiles have neither) and its colour.",
                             xalign=0, css_classes=["dim-label"]))
        box.append(header((("Number", 64), ("Graph", 52), ("Colour", 44), ("Sensor", 0))))
        self.tile_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, show_separators=True)
        self.tile_sc = Gtk.ScrolledWindow(vexpand=True)
        self.tile_sc.set_child(self.tile_list)
        box.append(self.tile_sc)
        self.fill_tiles()
        return box

    # ---------- layout editor ----------
    def on_edit_mode(self, mode):
        self.edit_mode = mode
        self.render_layout()

    def tile_dropdown(self, key, on_pick):
        keys = list(gb.TILES)
        dd = Gtk.DropDown.new_from_strings([gb.TILES[k][0] for k in keys])
        dd.set_selected(keys.index(key) if key in keys else 0)
        dd.connect("notify::selected", lambda d, _p: on_pick(keys[d.get_selected()]))
        return dd

    def render_layout(self):
        while (c := self.lay_box.get_first_child()) is not None:
            self.lay_box.remove(c)
        blocks = self.lay[self.edit_mode]
        if not blocks:
            self.lay_box.append(Gtk.Label(label="Empty: add a block.", xalign=0, css_classes=["dim-label"]))
        for bi, b in enumerate(blocks):
            frame = Gtk.Frame()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6,
                            margin_start=8, margin_end=8)
            frame.set_child(inner)
            hb = Gtk.Box(spacing=8)
            hb.append(Gtk.Label(label=f"<b>Block {bi + 1}</b>", use_markup=True))
            hb.append(Gtk.Label(label="width"))
            wsp = Gtk.SpinButton.new_with_range(0.5, 10, 0.5)
            wsp.set_value(b["w"])
            wsp.connect("value-changed", lambda w, bi=bi: self.lay_op("width", bi, val=w.get_value()))
            hb.append(wsp)
            hb.append(Gtk.Box(hexpand=True))
            for icon, op, ok in (("go-previous-symbolic", "left", bi > 0),
                                 ("go-next-symbolic", "right", bi < len(blocks) - 1),
                                 ("list-add-symbolic", "add_row", True), ("edit-delete-symbolic", "del_block", True)):
                btn = Gtk.Button(icon_name=icon, sensitive=ok, css_classes=["flat"],
                                 tooltip_text={"left": "move left", "right": "move right", "add_row": "add a row",
                                               "del_block": "remove block"}[op])
                btn.connect("clicked", lambda _b, op=op, bi=bi: self.lay_op(op, bi))
                hb.append(btn)
            inner.append(hb)
            for ri, row in enumerate(b["rows"]):
                rb = Gtk.Box(spacing=6, margin_start=12)
                rb.append(Gtk.Label(label=f"row {ri + 1}", width_chars=6, xalign=0, css_classes=["dim-label"]))
                for ti, key in enumerate(row):
                    rb.append(self.tile_dropdown(key, lambda k, bi=bi, ri=ri, ti=ti: self.lay_op("pick", bi, ri, ti, k)))
                    x = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"], tooltip_text="remove tile")
                    x.connect("clicked", lambda _b, bi=bi, ri=ri, ti=ti: self.lay_op("del_tile", bi, ri, ti))
                    rb.append(x)
                add = Gtk.Button(label="+ tile")
                add.connect("clicked", lambda _b, bi=bi, ri=ri: self.lay_op("add_tile", bi, ri))
                rb.append(add)
                if len(b["rows"]) > 1:
                    dr = Gtk.Button(label="remove row", css_classes=["flat"])
                    dr.connect("clicked", lambda _b, bi=bi, ri=ri: self.lay_op("del_row", bi, ri))
                    rb.append(dr)
                inner.append(rb)
            self.lay_box.append(frame)

    def unused_tile(self):
        used = {k for b in self.lay[self.edit_mode] for r in b["rows"] for k in r}
        return next((k for k in gb.TILES if k not in used), "fps")

    def lay_op(self, op, bi=None, ri=None, ti=None, val=None):
        blocks = self.lay[self.edit_mode]
        structural = True
        if op == "add_block":
            blocks.append(dict(w=1.0, rows=[[self.unused_tile()]]))
        elif op == "flatten":
            keys = [k for b in blocks for r in b["rows"] for k in r]
            blocks[:] = [dict(w=float(gb.TILE_WEIGHT.get(k, 1)), rows=[[k]]) for k in keys]
        elif op == "width":
            blocks[bi]["w"] = val
            structural = False
        elif op in ("left", "right"):
            j = bi - 1 if op == "left" else bi + 1
            blocks[bi], blocks[j] = blocks[j], blocks[bi]
        elif op == "del_block":
            del blocks[bi]
        elif op == "add_row":
            blocks[bi]["rows"].append([self.unused_tile()])
        elif op == "del_row":
            del blocks[bi]["rows"][ri]
        elif op == "add_tile":
            blocks[bi]["rows"][ri].append(self.unused_tile())
        elif op == "del_tile":
            del blocks[bi]["rows"][ri][ti]
            if not blocks[bi]["rows"][ri]:
                del blocks[bi]["rows"][ri]
            if not blocks[bi]["rows"]:
                del blocks[bi]
        elif op == "pick":
            blocks[bi]["rows"][ri][ti] = val
            structural = False
        self.c["layout"], self.c["compact_layout"] = self.lay["full"], self.lay["compact"]
        flat = lambda m: [k for b in self.lay[m] for r in b["rows"] for k in r]   # old flat lists, kept in sync
        self.c["tiles"], self.c["compact_tiles"] = flat("full"), flat("compact")
        self.changed()
        if structural:
            keep_scroll(self.lay_sc, self.render_layout)

    def page_clock(self):
        sc, g = self.grid()
        ck = self.c["clock"]

        def setc(k, v):
            self.c["clock"][k] = v
            self.changed()
        self.row(g, 0, "Style", dropdown(CLOCK_STYLES, ck.get("style", "digital"), lambda v: setc("style", v)))
        for r, (k, name) in enumerate((("h24", "24-hour format"), ("seconds", "Seconds"), ("date", "Date"),
                                        ("tyria", "Tyria day/night"), ("resets", "Daily / weekly reset"),
                                        ("birthdays", "Character birthdays today or tomorrow (API key)")), 1):
            cb = Gtk.CheckButton(label=name, active=ck.get(k, k in ("h24", "tyria", "resets", "birthdays")))
            cb.connect("toggled", lambda w, k=k: setc(k, w.get_active()))
            g.attach(cb, 1, r, 1, 1)
        size = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.6, 1.6, 0.05)
        size.set_value(ck.get("size", 1.0))
        size.set_hexpand(True)
        size.connect("value-changed", lambda w: setc("size", round(w.get_value(), 2)))
        self.row(g, 7, "Size", size)
        cbtn = swatch(self.current_hex("clock"), lambda w, _p: self.on_color(w, _p, "clock"))
        self.color_buttons["clock"] = cbtn
        self.row(g, 8, "Colour", cbtn)
        g.attach(Gtk.Label(label="Tyria time: 2 h cycle, day from every even UTC hour + 30 min.", xalign=0,
                           wrap=True, css_classes=["dim-label"]), 0, 9, 3, 1)
        return sc

    def page_events(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_start=10,
                      margin_end=10)
        top = Gtk.Box(spacing=10)
        auto = Gtk.CheckButton(label="Also the meta of the map I'm on (timeline tile)",
                               active=self.c.get("event_auto_map", True), hexpand=True)
        auto.connect("toggled", lambda w: self.set("event_auto_map", w.get_active()))
        top.append(auto)
        nxt = Gtk.CheckButton(label="'next event' line under the clock", active=self.c["clock"].get("events", True))
        nxt.connect("toggled", lambda w: (self.c["clock"].__setitem__("events", w.get_active()), self.changed()))
        top.append(nxt)
        upd = Gtk.Button(label="Update event data")
        upd.connect("clicked", self.on_update_events)
        top.append(upd)
        box.append(top)
        box.append(Gtk.Label(label="Timeline: rows of the 'Event timer' tile (add the tile under 'Tiles'). "
                                   "Clock: which events the 'next' line under the clock considers. Data: the "
                                   "GW2 wiki's event timer (via github.com/giovazz89/gw2-api-event-timers).",
                             xalign=0, wrap=True, css_classes=["dim-label"]))
        al = self.c.setdefault("alert", dict(gb.DEFAULTS["alert"]))
        warn = Gtk.Box(spacing=10)
        warn.append(Gtk.Label(label="<b>Start warning</b> under the clock", use_markup=True))
        sw = Gtk.Switch(active=al.get("on", True), valign=Gtk.Align.CENTER)
        sw.connect("notify::active", lambda w, _p: self.set_alert("on", w.get_active()))
        warn.append(sw)
        warn.append(Gtk.Label(label="for"))
        warn.append(dropdown([("clock", "the 'next' line's events"), ("timeline", "the timeline's events"),
                              ("all", "the whole game"), ("custom", "column 'Warning'")],
                             al.get("source", "clock"), lambda v: self.set_alert("source", v)))
        lead = Gtk.SpinButton.new_with_range(1, 30, 1)
        lead.set_value(al.get("lead", 5))
        lead.connect("value-changed", lambda w: self.set_alert("lead", int(w.get_value())))
        warn.append(lead)
        warn.append(Gtk.Label(label="min before"))
        warn.append(swatch(al.get("color", "#ff8a3d"), lambda w, _p: self.set_alert("color", hexof(w.get_rgba()))))
        blink = Gtk.CheckButton(label="blink", active=al.get("blink", True),
                                tooltip_text="Pulse during the last minute before the start")
        blink.connect("toggled", lambda w: self.set_alert("blink", w.get_active()))
        warn.append(blink)
        snd = Gtk.CheckButton(label="sound", active=al.get("sound", False))
        snd.connect("toggled", lambda w: self.set_alert("sound", w.get_active()))
        warn.append(snd)
        box.append(warn)
        tl = Gtk.Box(spacing=10)
        tl.append(Gtk.Label(label="<b>Timeline</b>  shows", use_markup=True))
        ahead = Gtk.SpinButton.new_with_range(30, 360, 10)
        ahead.set_value(self.c.get("event_ahead", 110))
        ahead.set_tooltip_text("How far into the future the timeline tile reaches (plus 10 min of the past)")
        ahead.connect("value-changed", lambda w: self.set("event_ahead", int(w.get_value())))
        tl.append(ahead)
        tl.append(Gtk.Label(label="min ahead · bar height max"))
        rmax = Gtk.SpinButton.new_with_range(0, 200, 2)
        rmax.set_value(self.c.get("event_row_max", 0))
        rmax.set_tooltip_text("0 = the bars share the whole tile height. With only a few rows a limit keeps "
                              "them slim instead of filling the tile.")
        rmax.connect("value-changed", lambda w: self.set("event_row_max", int(w.get_value())))
        tl.append(rmax)
        tl.append(Gtk.Label(label="px (0 = fill) · space between bars"))
        rgap = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 0.6, 0.02)
        rgap.set_value(self.c.get("event_row_gap", 0.16))
        rgap.set_size_request(160, -1)
        rgap.set_tooltip_text("Vertical gap between the bars, as a share of a row")
        rgap.connect("value-changed", lambda w: self.set("event_row_gap", round(w.get_value(), 2)))
        tl.append(rgap)
        tl.append(Gtk.Label(label="· between events"))
        cgap = Gtk.SpinButton.new_with_range(0, 20, 1)
        cgap.set_value(self.c.get("event_col_gap", 2))
        cgap.set_tooltip_text("Horizontal space between events that follow each other directly. Larger gaps "
                              "in the timeline are real time without an event.")
        cgap.connect("value-changed", lambda w: self.set("event_col_gap", int(w.get_value())))
        tl.append(cgap)
        tl.append(Gtk.Label(label="px"))
        box.append(tl)
        box.append(Gtk.Separator())
        quick = Gtk.Box(spacing=8)
        quick.append(Gtk.Label(label="Quick choice (timeline and clock):"))
        for label, cats in (("Whole game", None), ("Core only", {"Core Tyria"}), ("None", set())):
            b = Gtk.Button(label=label)
            b.connect("clicked", self.on_quick_events, cats)
            quick.append(b)
        box.append(quick)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, show_separators=True)
        box.append(header((("Timeline", 70), ("Clock", 50), ("Warning", 64), ("Height", 96), ("Event", 0))))
        self.height_spins = {}                      # name -> height factor of its timeline row
        self.event_checks = {}                      # (cfgkey, name) -> checkbox
        self.cat_checks = {}                        # (cfgkey, category) -> header checkbox
        cats = []
        for sct in gb.EVENTS:
            if sct["category"] not in cats:
                cats.append(sct["category"])
        for cat in cats:                            # one group per expansion / season / festivals
            hrow = Gtk.Box(spacing=10, margin_top=6, margin_bottom=2, margin_start=6, margin_end=6)
            for cfgkey, wd in (("event_sections", 70), ("clock_events", 50), ("alert_events", 64)):
                cb = Gtk.CheckButton(width_request=wd)
                cb.connect("toggled", self.on_cat_toggle, cfgkey, cat)
                self.cat_checks[(cfgkey, cat)] = cb
                hrow.append(cb)
            hrow.append(Gtk.Box(width_request=96))
            hrow.append(Gtk.Label(label=f"<b>{GLib.markup_escape_text(cat)}</b>", use_markup=True, xalign=0,
                                  hexpand=True))
            lb.append(hrow)
            for sct in (x for x in gb.EVENTS if x["category"] == cat):
                row = Gtk.Box(spacing=10, margin_top=1, margin_bottom=1, margin_start=6, margin_end=6)
                for cfgkey, wd in (("event_sections", 70), ("clock_events", 50), ("alert_events", 64)):
                    cb = Gtk.CheckButton(active=sct["name"] in self.c.get(cfgkey, []), width_request=wd)
                    cb.connect("toggled", self.on_event_toggle, cfgkey, sct["name"])
                    self.event_checks[(cfgkey, sct["name"])] = cb
                    row.append(cb)
                hs = Gtk.SpinButton.new_with_range(0.25, 4, 0.25)
                hs.set_digits(2)
                hs.set_width_chars(4)
                hs.set_size_request(96, -1)
                hs.set_value(self.c.get("event_row_heights", {}).get(sct["name"], 1.0))
                hs.set_tooltip_text("Height of this row in the timeline: 1 = normal, 0.5 = half, 2 = double")
                hs.connect("value-changed", self.on_row_height, sct["name"])
                self.height_spins[sct["name"]] = hs
                row.append(hs)
                row.append(Gtk.Label(label="    " + GLib.markup_escape_text(sct["name"]) +
                                     ("" if sct.get("active", True) else "  <small>(inactive in the data)</small>"),
                                     use_markup=True, xalign=0, hexpand=True))
                lb.append(row)
        self.sync_cat_checks()
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(lb)
        box.append(sc)
        return box

    def set_alert(self, key, value):
        self.c.setdefault("alert", dict(gb.DEFAULTS["alert"]))[key] = value
        self.changed()

    def sync_cat_checks(self):
        """Group checkbox: ticked if all of its series are, 'mixed' if some are."""
        self._cat_sync = True
        for (cfgkey, cat), cb in self.cat_checks.items():
            names = [x["name"] for x in gb.EVENTS if x["category"] == cat]
            on = [n for n in names if n in self.c.get(cfgkey, [])]
            cb.set_inconsistent(0 < len(on) < len(names))
            cb.set_active(len(on) == len(names))
        self._cat_sync = False
        for name, hs in self.height_spins.items():          # height only matters for timeline rows
            hs.set_sensitive(name in self.c.get("event_sections", []))

    def on_row_height(self, spin, name):
        hts = self.c.setdefault("event_row_heights", {})
        v = round(spin.get_value(), 2)
        if v == 1.0:
            hts.pop(name, None)
        else:
            hts[name] = v
        self.changed()

    def set_events(self, cfgkey, names):
        self._bulk = True
        for (k, n), cb in self.event_checks.items():
            if k == cfgkey:
                cb.set_active(n in names)
        self._bulk = False
        self.set(cfgkey, [x["name"] for x in gb.EVENTS if x["name"] in names])
        self.sync_cat_checks()

    def on_cat_toggle(self, cb, cfgkey, cat):
        if getattr(self, "_cat_sync", False):
            return
        cb.set_inconsistent(False)
        names = set(self.c.get(cfgkey, []))
        group = {x["name"] for x in gb.EVENTS if x["category"] == cat}
        self.set_events(cfgkey, names | group if cb.get_active() else names - group)

    def on_quick_events(self, _b, cats):
        names = {x["name"] for x in gb.EVENTS if cats is None or x["category"] in cats}
        for cfgkey in ("event_sections", "clock_events"):
            self.set_events(cfgkey, names)

    def on_event_toggle(self, cb, cfgkey, name):
        if getattr(self, "_bulk", False):
            return
        lst = [n for n in self.c.get(cfgkey, []) if n != name]
        if cb.get_active():
            lst.append(name)
        self.set(cfgkey, [sct["name"] for sct in gb.EVENTS if sct["name"] in lst])   # keep data order
        self.sync_cat_checks()

    def on_update_events(self, _b):
        r = subprocess.run([sys.executable, os.path.join(DIR, "gw2events.py"), "--update"], capture_output=True,
                           text=True)
        self.update_status("Event data updated; applies at the next bar start." if r.returncode == 0 else
                           "Update failed: " + (r.stderr.strip().splitlines() or ["?"])[-1])

    def page_behaviour(self):
        sc, g = self.grid()
        self.row(g, 0, "In combat", dropdown(COMBAT, self.c.get("combat", "dim"), lambda v: self.set("combat", v)))
        wo = Gtk.Switch(active=self.c.get("warnings_only", False), halign=Gtk.Align.START)
        wo.connect("notify::active", lambda w, _p: self.set("warnings_only", w.get_active()))
        self.row(g, 1, "Warnings only (always)", wo)
        g.attach(Gtk.Label(label=(
            "Warnings: GPU hotspot ≥ 95 °C · GW2 main thread ≥ 90 % · GPU ≥ 97 % "
            "busy · ping ≥ 150 ms · 1 % low below 20 FPS.\n\n"
            "Hotkeys (KDE, work in fullscreen too):\n"
            "  Meta+Shift+B   bar layout: full → compact → clock → off\n"
            "  Meta+Shift+T   run timer (tile 'Run timer'): start / stop\n"
            "  Meta+Shift+W   window tile: set the window free (to set it up) / put it back in\n"
            "  Meta+Shift+M   lag marker: notes the time (sound + red flash) in\n"
            "                 ~/.local/share/letterbox/markers.log, to find laggy moments in your logs later\n"
            "Change them in System Settings → Shortcuts (search 'letterbox')."), xalign=0, wrap=True), 0, 2, 3, 1)
        return sc

    def page_extras(self):
        """Settings of the extra tiles, built from what the features in extras.py ask for."""
        sc, g = self.grid()
        r = 0
        g.attach(Gtk.Label(label="Add the tiles themselves under 'Tiles'.", xalign=0, css_classes=["dim-label"]),
                 0, r, 3, 1)
        r += 1
        for f in gb.extras.SETTINGS:
            if f["kind"] == "heading":
                g.attach(Gtk.Separator(margin_top=6, margin_bottom=2), 0, r, 3, 1)
                g.attach(Gtk.Label(label=f"<b>{GLib.markup_escape_text(f['label'])}</b>", use_markup=True, xalign=0),
                         0, r + 1, 3, 1)
                r += 2
                continue
            self.row(g, r, f["label"], self.extra_widget(f))
            r += 1
            if f.get("hint"):
                g.attach(Gtk.Label(label=f["hint"], xalign=0, wrap=True, css_classes=["dim-label"]), 1, r, 2, 1)
                r += 1
        return sc

    def get_path(self, key, default=None):
        """Config value; "a.b" reaches into a dict."""
        cur = self.c
        for part in key.split("."):
            cur = cur.get(part, None) if isinstance(cur, dict) else None
        return default if cur is None else cur

    def set_path(self, key, value):
        parts = key.split(".")
        cur = self.c
        for part in parts[:-1]:
            if not isinstance(cur.get(part), dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value
        self.changed()

    def apply_on_enter(self, entry, save):
        """Fields whose value is used for network requests or acts on things: save on Enter or when leaving the
        field, not after every letter (a half-typed value must never be used)."""
        def apply(*_a):
            save(entry.get_text().strip())
        entry.connect("activate", apply)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", apply)
        entry.add_controller(focus)

    def extra_widget(self, f):
        key, kind = f["key"], f["kind"]
        val = self.get_path(key, f.get("default"))
        if kind in ("entry", "secret"):
            w = (Gtk.PasswordEntry(show_peek_icon=True) if kind == "secret" else
                 Gtk.Entry(placeholder_text=f.get("placeholder", "")))
            w.set_hexpand(True)
            w.set_text(str(val or ""))
            if f.get("apply"):                             # only on Enter or leaving the field, not per letter
                self.apply_on_enter(w, lambda v: self.set_path(key, v) if v != (self.get_path(key, "") or "")
                                    else None)
            else:
                w.connect("changed", lambda e: self.set_path(key, e.get_text().strip()))
        elif kind == "spin":
            w = Gtk.SpinButton.new_with_range(f.get("min", 0), f.get("max", 999), f.get("step", 1))
            w.set_halign(Gtk.Align.START)
            w.set_value(val or 0)
            w.connect("value-changed", lambda e: self.set_path(key, int(e.get_value())))
        elif kind == "check":
            w = Gtk.Switch(active=bool(val), halign=Gtk.Align.START)
            w.connect("notify::active", lambda e, _p: self.set_path(key, e.get_active()))
        elif kind == "choice":
            w = dropdown(f["items"], val, lambda v: self.set_path(key, v))
        else:                                              # "custom": the feature builds its own widget
            w = f["build"](self, f)
        return w

    def page_system(self):
        """Things the installer finds on its own, to check or change: monitor, GPU, paths, API key."""
        sc, g = self.grid()
        outs = gb.outputs()
        prim = outs[0]["name"] if outs else "?"
        items = [("", f"Primary monitor ({prim})")] + [(o["name"], f"{o['name']}  {o['w']}×{o['h']}"
                                                         + ("  (primary)" if o["primary"] else "")) for o in outs]
        self.row(g, 0, "Monitor for GW2", dropdown(items, self.c.get("output", ""), self.on_output))
        gpus = gb.sensors.gpu_list()
        auto = gb.sensors.gpu_device("")
        auto_name = next((n for _p, d, n in gpus if d == auto), "none found")
        items = [("", f"Automatic ({auto_name})")] + [(pci, f"{name}  ({pci})") for pci, _d, name in gpus]
        self.row(g, 1, "GPU", dropdown(items, self.c.get("gpu", ""), self.on_gpu),
                 None if gpus else Gtk.Label(label="no AMD GPU: GPU tiles are hidden", css_classes=["dim-label"]))
        g.attach(Gtk.Separator(margin_top=6, margin_bottom=6), 0, 2, 3, 1)
        self.path_row(g, 3, "GW2 graphics settings", "gfx_settings", self.auto_gfx, folder=False,
                      hint="GFXSettings.Gw2-64.exe.xml, letterbox switches GW2 between windowed and fullscreen in it")
        self.path_row(g, 5, "GW2 game folder", "game_dir", self.auto_game, folder=True,
                      hint="the folder with Gw2-64.exe, for the Nexus bridge (squad tile)")
        self.path_row(g, 7, "MangoHud log folder", "mangohud_dir", None, folder=True,
                      hint="MangoHud's output_folder, for the FPS tiles")
        g.attach(Gtk.Separator(margin_top=6, margin_bottom=6), 0, 9, 3, 1)
        key = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True, text=self.c.get("api_key", ""))
        self.apply_on_enter(key, lambda v: self.set("api_key", v) if v != self.c.get("api_key", "") else None)
        self.row(g, 10, "GW2 API key", key)
        g.attach(Gtk.Label(label=(
            "Needed for the WvW tiles, the game doesn't tell outside programs which matchup you're in. Make one "
            "at account.arena.net/applications, the 'account' permission is enough. It stays in "
            "~/.config/letterbox/config.json, readable only by you."),
            xalign=0, wrap=True, css_classes=["dim-label"]), 1, 11, 2, 1)
        g.attach(Gtk.Separator(margin_top=6, margin_bottom=6), 0, 12, 3, 1)
        check = Gtk.Button(label="Check setup", halign=Gtk.Align.START)
        self.doctor_out = Gtk.Label(xalign=0, selectable=True, css_classes=["monospace"], wrap=True)
        check.connect("clicked", self.on_check)
        self.row(g, 13, "", check)
        g.attach(self.doctor_out, 0, 14, 3, 1)
        return sc

    def path_row(self, g, r, label, key, auto_fn, folder, hint):
        """Path with what's in use, 'Choose…' and (with auto_fn) 'Automatic'."""
        cur = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE, selectable=True)

        def show():
            val = os.path.expanduser(self.c.get(key) or "")
            if val:
                cur.set_text(val + ("" if os.path.exists(val) else "   (doesn't exist)"))
            else:
                a = auto_fn() if auto_fn else None
                cur.set_text(f"automatic: {a}" if a else "automatic: not found")
            cur.set_tooltip_text(cur.get_text())

        def chosen(dlg, res):
            try:
                f = dlg.select_folder_finish(res) if folder else dlg.open_finish(res)
            except GLib.Error:
                return                                     # cancelled
            self.set(key, f.get_path())
            if key in ("mangohud_dir", "game_dir"):        # the preview reads them
                self.data = gb.Data(self.c.get("graph_seconds", 60), self.c)
            show()

        def choose(_b):
            dlg = Gtk.FileDialog(title=label, modal=True)
            start = os.path.expanduser(self.c.get(key) or (auto_fn() if auto_fn else "") or "~")
            if os.path.exists(start):
                gf = Gio.File.new_for_path(start if folder or os.path.isdir(start) else os.path.dirname(start))
                dlg.set_initial_folder(gf)
            (dlg.select_folder if folder else dlg.open)(self, None, chosen)

        def automatic(_b):
            self.set(key, "")
            if key == "game_dir":
                self.data = gb.Data(self.c.get("graph_seconds", 60), self.c)
            show()
        box = Gtk.Box(spacing=6)
        b = Gtk.Button(label="Choose…")
        b.connect("clicked", choose)
        box.append(b)
        if auto_fn:
            a = Gtk.Button(label="Automatic", css_classes=["flat"])
            a.connect("clicked", automatic)
            box.append(a)
        self.row(g, r, label, cur, box)
        g.attach(Gtk.Label(label=hint, xalign=0, css_classes=["dim-label"]), 1, r + 1, 2, 1)
        show()

    def auto_gfx(self):
        try:
            env = dict(os.environ, LETTERBOX_IGNORE_GFX_CONF="1")
            return subprocess.run([SH, "gfx-path"], capture_output=True, text=True, timeout=5, env=env).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    def auto_game(self):
        p = gb.bridge_path(dict(self.c, bridge_file="", game_dir=""))
        return os.path.dirname(os.path.dirname(os.path.dirname(p))) if p else None

    def on_output(self, name):
        self.set("output", name)
        sc = gb.screen(self.c)
        if sc and not (sc["h"] // 2 <= self.c.get("height", 0) < sc["h"]):
            self.set("height", gb.default_height(sc["w"], sc["h"]))   # the old game size doesn't fit
        screen_setup()
        self.rebuild(f"GW2 goes on {sc['name'] if sc else name} from the next start.", page=self.nb.get_current_page())

    def on_gpu(self, pci):
        self.set("gpu", pci)
        self.data = gb.Data(self.c.get("graph_seconds", 60), self.c)
        self.update_status("GPU changed. The GW2 logger picks it up at its next start (or reboot).")

    def on_check(self, _b):
        try:
            out = subprocess.run([sys.executable, os.path.join(DIR, "setup.py"), "doctor"], capture_output=True,
                                 text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError) as ex:
            out = str(ex)
        self.doctor_out.set_text(out.strip())

    # ---------- tiles ----------
    def fill_tiles(self):
        while (r := self.tile_list.get_row_at_index(0)) is not None:
            self.tile_list.remove(r)
        for key in gb.TILES:
            title, unit, _m, group, _kind = gb.TILES[key]
            row = Gtk.Box(spacing=10, margin_top=2, margin_bottom=2, margin_start=6, margin_end=6)
            opts = self.c.setdefault("tile_opts", {}).get(key, {})
            for opt, wd in (("number", 64), ("graph", 52)):
                cb = Gtk.CheckButton(active=opts.get(opt, True), width_request=wd, sensitive=_kind == "num")
                cb.connect("toggled", self.on_tile_opt, key, opt)
                row.append(cb)
            row.append(swatch(self.current_hex(key, group), lambda w, _p, k=key: self.on_color(w, _p, k)))
            row.append(Gtk.Label(label=f"{title}  <small>{unit}</small>", use_markup=True, xalign=0, hexpand=True))
            if key == "fps":
                avg = Gtk.CheckButton(label="with 60 s average", active=opts.get("avg", False))
                avg.connect("toggled", self.on_tile_opt, key, "avg")
                row.append(avg)
            row.append(Gtk.Label(label=f"<small>{GROUP_NAMES.get(group, group)}</small>", use_markup=True,
                                 css_classes=["dim-label"]))
            self.tile_list.append(row)

    def on_tile_opt(self, cb, key, opt):
        self.c.setdefault("tile_opts", {}).setdefault(key, {})[opt] = cb.get_active()
        self.changed()

    # ---------- colours ----------
    def current_hex(self, key, group=None):
        c = gb.color(self.c, key) or (gb.color(self.c, group) if group else None) or (0.8, 0.8, 0.8)
        return "#{:02x}{:02x}{:02x}".format(*(round(x * 255) for x in c))

    def on_color(self, btn, _p, key):
        if getattr(self, "_syncing", False):
            return
        self.c.setdefault("colors", {})[key] = hexof(btn.get_rgba())
        self.changed()

    def reset_fonts(self):
        self.c["font_text"], self.c["font_number"] = "sans", "monospace"
        b = self.font_box.get_first_child()
        for fam in ("sans", "monospace"):
            b.get_first_child().set_font_desc(Pango.FontDescription.from_string(fam))
            b = b.get_next_sibling()
        self.changed()

    def on_theme(self, name):
        self.c["theme"] = name
        self.c["colors"] = {}
        self.sync_colors()
        self.changed()

    def on_reset_colors(self, _b):
        self.c["colors"] = {}
        self.sync_colors()
        self.changed()

    def sync_colors(self):
        self._syncing = True
        for key, btn in self.color_buttons.items():
            btn.set_rgba(rgba(self.current_hex(key)))
        self._syncing = False
        keep_scroll(self.tile_sc, self.fill_tiles)

    # ---------- size ----------
    def sync_preset(self):
        h = int(self.height.get_value())
        idx = next((i for i, p in enumerate(PRESETS) if p[1] == h), len(PRESETS) - 1)
        self._sync = True
        self.preset.set_selected(idx)
        self._sync = False

    def on_preset(self, dd, _p):
        if not getattr(self, "_sync", False) and PRESETS[dd.get_selected()][1]:
            self.height.set_value(PRESETS[dd.get_selected()][1])

    def on_height(self, _s):
        self.sync_preset()
        self.set("height", int(self.height.get_value()))
        self.on_height_label()

    def on_height_label(self):
        self.saving.set_text(f"{(1 - self.c['height'] / OUT_H) * 100:.0f} % fewer pixels for the GPU")

    # ---------- state ----------
    def set(self, key, value):
        self.c[key] = value
        self.changed()

    def changed(self):
        self.data.graph_seconds = self.c.get("graph_seconds", 60)
        os.makedirs(os.path.dirname(gb.CONF), exist_ok=True)
        with open(os.open(gb.CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:   # may hold the API key
            json.dump(self.c, fh, indent=1)
        os.chmod(gb.CONF, 0o600)
        Gio.bus_get_sync(Gio.BusType.SESSION).call("letterbox.bar", "/letterbox/bar", "letterbox.Bar", "Reload", None,
                                                    None, Gio.DBusCallFlags.NONE, 500, None, None)
        self.area.queue_draw()

    def tick(self):
        self.data.update()
        self.area.queue_draw()
        return True

    def update_status(self, extra=""):
        on = os.path.exists(FLAG)
        txt = "On: GW2 starts windowed in the letterbox." if on else "Off: GW2 starts in normal fullscreen."
        if gw2_running():
            txt += "  GW2 is running: on/off only while it is closed; other changes apply live."
        self.status.set_text(f"{txt}  {extra}".strip())

    def on_switch(self, sw, state):
        if gw2_running():
            self.update_status()
            return True
        self.changed()
        r = subprocess.run([SH, "on" if state else "off"], capture_output=True, text=True)
        if r.returncode != 0:
            self.update_status(r.stdout.strip() or r.stderr.strip())
            return True
        sw.set_state(state)
        self.update_status()
        return True

    def on_test(self, btn):
        """Toggle: show the bar on the screen without the game (until toggled off or the window closes)."""
        if btn.get_active():
            self.changed()
            made = not os.path.exists(FLAG)
            if made:                              # "letterbox start" only runs with the mode on
                open(FLAG, "w").close()
            subprocess.run([SH, "start"], env=dict(os.environ, FORCE="1"), capture_output=True)
            if made:
                os.remove(FLAG)
            self.update_status("Bar shown on the screen (toggle off to hide).")
        else:
            self.stop_test()

    def stop_test(self):
        if not gw2_running():                     # with the game running the bar belongs to it
            subprocess.run([SH, "stop"], capture_output=True)
        self.update_status()

    def on_close(self, _w):
        if self.test.get_active():
            self.stop_test()
        return False

    # ---------- preview: monitor with game area and the real bar ----------
    def draw_preview(self, _a, cr, w, h):
        c = self.c
        sc = min((w - 20) / OUT_W, (h - 20) / OUT_H)
        mw, mh = OUT_W * sc, OUT_H * sc
        x0, y0 = (w - mw) / 2, (h - mh) / 2
        gh_dev = c["height"]
        gy_dev = {"bottom": OUT_H - gh_dev, "center": (OUT_H - gh_dev) // 2, "top": 0}[c["position"]]
        bar_y_dev = gh_dev if c["position"] == "top" else 0
        bar_h_dev = OUT_H - gh_dev if c["position"] == "top" else gy_dev
        cr.set_source_rgb(0, 0, 0)
        cr.rectangle(x0, y0, mw, mh)
        cr.fill()
        cr.set_source_rgb(0.2, 0.33, 0.5)
        cr.rectangle(x0, y0 + gy_dev * sc, mw, gh_dev * sc)
        cr.fill()
        cr.set_source_rgb(0.85, 0.88, 0.92)
        cr.set_font_size(max(11, mh * 0.06))
        label = f"GW2  {OUT_W}×{gh_dev}"
        ext = cr.text_extents(label)
        cr.move_to(x0 + (mw - ext.width) / 2, y0 + (gy_dev + gh_dev / 2) * sc + ext.height / 2)
        cr.show_text(label)
        if bar_h_dev > 4:                          # the real bar at device resolution, scaled down
            cr.save()
            cr.translate(x0, y0 + bar_y_dev * sc)
            cr.scale(sc, sc)
            cr.rectangle(0, 0, OUT_W, bar_h_dev)
            cr.clip()
            self.renderer.draw(cr, OUT_W, bar_h_dev, c, self.data, c.get("mode", "full"))
            cr.restore()
        cr.set_source_rgb(0.4, 0.42, 0.45)
        cr.set_line_width(2)
        cr.rectangle(x0, y0, mw, mh)
        cr.stroke()


def main():
    app = Gtk.Application(application_id="letterbox.settings")
    app.connect("activate", lambda a: Settings(a).present())
    GLib.set_prgname("letterbox-settings")
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
