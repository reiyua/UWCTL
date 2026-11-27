"""uwctl-gui - GTK 4 / libadwaita front end (needs libadwaita >= 1.2)."""

from __future__ import annotations

import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, __version__  # noqa: E402
from .. import colors as col  # noqa: E402
from ..client import ClientError, DaemonUnavailable, connect  # noqa: E402
from ..doctor import format_report  # noqa: E402
from ..keyboard import DIRECTIONS, EFFECTS  # noqa: E402
from ..power import PROFILES  # noqa: E402
from .curve_editor import CurveEditor  # noqa: E402

DIRECTION_LABELS = {
    "left": "Left", "right": "Right", "up": "Up", "down": "Down",
    "up_left": "Up-left", "up_right": "Up-right", "down_left": "Down-left", "down_right": "Down-right",
}
DIRECTIONAL = {"wave", "rainbow_wave", "snake"}
MULTI_COLOUR = {"static", "breathing", "flash", "wave", "scan"}


def rgba(hexstr: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(hexstr)
    return c


def rgba_hex(c: Gdk.RGBA) -> str:
    return col.to_hex((int(round(c.red * 255)), int(round(c.green * 255)), int(round(c.blue * 255))))


class ColourButton(Gtk.Box):
    """Gtk.ColorDialogButton on GTK >= 4.10, Gtk.ColorButton before."""

    def __init__(self, hexstr: str, on_change) -> None:
        super().__init__()
        self.on_change = on_change
        if hasattr(Gtk, "ColorDialogButton"):
            dlg = Gtk.ColorDialog()
            dlg.set_with_alpha(False)
            self.btn = Gtk.ColorDialogButton(dialog=dlg)
            self.btn.set_rgba(rgba(hexstr))
            self.btn.connect("notify::rgba", lambda *_: self.on_change())
        else:  # pragma: no cover - old GTK
            self.btn = Gtk.ColorButton()
            self.btn.set_rgba(rgba(hexstr))
            self.btn.connect("color-set", lambda *_: self.on_change())
        self.btn.set_valign(Gtk.Align.CENTER)
        self.append(self.btn)

    def get(self) -> str:
        return rgba_hex(self.btn.get_rgba())

    def set(self, hexstr: str) -> None:
        self.btn.set_rgba(rgba(hexstr))


def combo_row(title: str, items: list[str], subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_model(Gtk.StringList.new(items))
    return row


def switch_row(title: str, subtitle: str = "") -> tuple[Adw.ActionRow, Gtk.Switch]:
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    sw = Gtk.Switch(valign=Gtk.Align.CENTER)
    row.add_suffix(sw)
    row.set_activatable_widget(sw)
    return row, sw


def scale_row(title: str, lo: float, hi: float, step: float, subtitle: str = "") -> tuple[Adw.ActionRow, Gtk.Scale]:
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
    sc.set_hexpand(True)
    sc.set_size_request(220, -1)
    sc.set_draw_value(True)
    sc.set_value_pos(Gtk.PositionType.RIGHT)
    row.add_suffix(sc)
    return row, sc


class Window(Adw.ApplicationWindow):
    def __init__(self, app, client) -> None:
        super().__init__(application=app, title="Uniwill Control Center")
        self.client = client
        self.set_default_size(880, 720)
        self._busy = False
        self._loading = False
        self._debounce: dict[str, int] = {}
        self.caps: dict = {}
        self.state: dict = {}

        self.toasts = Adw.ToastOverlay()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toasts.set_child(outer)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        menu = Gio.Menu()
        menu.append("Diagnostics", "win.doctor")
        menu.append("Re-apply settings", "win.reapply")
        menu.append("About", "win.about")
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(mb)
        outer.append(header)

        self.mode_label = Gtk.Label(xalign=0)
        self.mode_label.add_css_class("dim-label")
        self.mode_label.set_margin_start(16)
        self.mode_label.set_margin_top(4)
        if getattr(client, "mode", "") == "demo":
            self.mode_label.set_markup("<b>Demo mode</b> – simulated laptop, nothing is sent to hardware")
            outer.append(self.mode_label)

        self.stack.set_vexpand(True)
        outer.append(self.stack)

        for name, cb in (("doctor", self.show_doctor), ("reapply", self.reapply), ("about", self.show_about)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_a, f=cb: f())
            self.add_action(act)

        self.reload()

    # -------------------------------------------------------- helpers
    def toast(self, msg: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(msg))

    def call_async(self, cmd: str, done=None, **args) -> None:
        def work():
            try:
                res, err = self.client.call(cmd, **args), None
            except ClientError as exc:
                res, err = None, str(exc)
            GLib.idle_add(finish, res, err)

        def finish(res, err):
            if err:
                self.toast(err)
            elif done:
                done(res)
            return False

        threading.Thread(target=work, daemon=True).start()

    def push(self, partial: dict, key: str | None = None, delay: int = 250) -> None:
        """Send a settings change, debounced per section."""
        if self._loading:
            return
        key = key or next(iter(partial))

        def fire():
            self._debounce.pop(key, None)
            self.call_async("set", self._after_set, state=partial)
            return False

        old = self._debounce.pop(key, None)
        if old:
            GLib.source_remove(old)
        self._debounce[key] = GLib.timeout_add(delay, fire)

    def _after_set(self, res) -> None:
        self.state = res["state"]
        for sec, err in (res.get("errors") or {}).items():
            self.toast(f"{sec}: {err}")

    # -------------------------------------------------------- build
    def reload(self) -> None:
        try:
            self.caps = self.client.call("capabilities")
            self.state = self.client.call("get_state")
        except ClientError as exc:
            self.toast(str(exc))
            return
        self._loading = True
        self.build_dashboard()
        self.build_lighting()
        self.build_fans()
        self.build_power()
        self._loading = False
        self.refresh_status()
        if not getattr(self, "_timer", None):
            self._timer = GLib.timeout_add(1500, self.refresh_status)

    def add_page(self, page: Adw.PreferencesPage, name: str, title: str, icon: str) -> None:
        sp = self.stack.add_titled(page, name, title)
        sp.set_icon_name(icon)

    # ---- dashboard -------------------------------------------------
    def build_dashboard(self) -> None:
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Sensors")
        self.sensor_rows = {}
        for key, title in (("cpu_temp", "CPU temperature"), ("gpu_temp", "GPU temperature"),
                           ("fan1", "Fan 1 (CPU)"), ("fan2", "Fan 2 (GPU)")):
            row = Adw.ActionRow(title=title)
            bar = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER)
            bar.set_size_request(180, -1)
            lab = Gtk.Label(label="–", width_chars=14, xalign=1)
            row.add_suffix(bar)
            row.add_suffix(lab)
            g.add(row)
            self.sensor_rows[key] = (row, bar, lab)
        page.add(g)

        if self.caps.get("profiles"):
            g2 = Adw.PreferencesGroup(title="Mode", description="Same as the Office / Gaming / Turbo modes in the Windows app")
            box = Gtk.Box(spacing=0, homogeneous=True, css_classes=["linked"])
            self.quick_profile = {}
            group_btn = None
            for name, icon in (("office", "power-profile-power-saver-symbolic"),
                               ("gaming", "power-profile-balanced-symbolic"),
                               ("turbo", "power-profile-performance-symbolic")):
                b = Gtk.ToggleButton()
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=10)
                inner.append(Gtk.Image(icon_name=icon, pixel_size=24))
                inner.append(Gtk.Label(label=name.capitalize()))
                b.set_child(inner)
                if group_btn:
                    b.set_group(group_btn)
                group_btn = group_btn or b
                b.connect("toggled", self._quick_profile, name)
                box.append(b)
                self.quick_profile[name] = b
            cur = self.state["profile"]["ac"]
            if cur in self.quick_profile:
                self.quick_profile[cur].set_active(True)
            g2.add(box)
            page.add(g2)

        g3 = Adw.PreferencesGroup(title="Fans")
        box = Gtk.Box(homogeneous=True, css_classes=["linked"])
        self.quick_fan = {}
        first = None
        for mode in ("auto", "max", "custom"):
            b = Gtk.ToggleButton(label={"auto": "Auto", "max": "Max", "custom": "Custom curve"}[mode])
            b.set_sensitive(mode in self.caps["fans"].get("modes", []))
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._quick_fan, mode)
            box.append(b)
            self.quick_fan[mode] = b
        self.quick_fan[self.state["fans"]["mode"]].set_active(True)
        g3.add(box)
        page.add(g3)

        g4 = Adw.PreferencesGroup(title="This laptop")
        kb = self.caps.get("keyboard") or {}
        for title, val in (
            ("EC access", self.caps.get("ec") or f"unavailable – {self.caps.get('ec_error')}"),
            ("Keyboard", f"{kb.get('backend', 'none')} · {kb.get('kind', '-')} · {kb.get('zones', 0)} zone(s)"),
            ("Fan modes", ", ".join(self.caps["fans"].get("modes", []))),
        ):
            r = Adw.ActionRow(title=title, subtitle=str(val))
            if hasattr(r, "set_subtitle_selectable"):  # libadwaita >= 1.3
                r.set_subtitle_selectable(True)
            g4.add(r)
        page.add(g4)
        self.add_page(page, "dash", "Dashboard", "computer-symbolic")

    def _quick_profile(self, btn, name) -> None:
        if btn.get_active():
            self.push({"profile": {"ac": name}}, "profile", 0)
            if hasattr(self, "prof_ac"):
                self._loading = True
                self.prof_ac.set_selected(list(PROFILES).index(name) + 1)
                self._loading = False

    def _quick_fan(self, btn, mode) -> None:
        if btn.get_active():
            self.push({"fans": {"mode": mode}}, "fans", 0)
            if hasattr(self, "fan_mode_row"):
                self._loading = True
                self.fan_mode_row.set_selected(["auto", "max", "custom"].index(mode))
                self._loading = False
                self._fan_editor_sensitive()

    def refresh_status(self) -> bool:
        def done(st):
            s = st.get("sensors", {})
            for key in ("cpu_temp", "gpu_temp"):
                row, bar, lab = self.sensor_rows[key]
                v = s.get(key)
                row.set_visible(v is not None or key == "cpu_temp")
                bar.set_value(min(100, v or 0))
                lab.set_label("–" if v is None else f"{v:.0f} °C")
            for i in (1, 2):
                row, bar, lab = self.sensor_rows[f"fan{i}"]
                rpm, duty = s.get(f"fan{i}_rpm"), s.get(f"fan{i}_duty")
                row.set_visible(rpm is not None or duty is not None)
                bar.set_value(duty or 0)
                lab.set_label(f"{rpm or 0} rpm · {duty if duty is not None else '–'}%")
            if hasattr(self, "curve"):
                which = self.curve_which
                t = s.get("cpu_temp") if which == "cpu" else s.get("gpu_temp")
                d = s.get("fan1_duty") if which == "cpu" else s.get("fan2_duty")
                self.curve.set_live(t, d)
            if st.get("failsafe"):
                self.toast("Temperature too high – fans forced to maximum")
        if not self._busy:
            self.call_async("status", done)
        return True

    # ---- lighting --------------------------------------------------
    def build_lighting(self) -> None:
        page = Adw.PreferencesPage()
        kb = self.caps.get("keyboard")
        k = self.state["keyboard"]
        g = Adw.PreferencesGroup(title="Keyboard backlight")
        if not kb:
            g.set_description("No controllable keyboard backlight found. Run Diagnostics from the menu for details.")
            page.add(g)
            self.add_page(page, "light", "Lighting", "keyboard-brightness-symbolic")
            return
        self.effects = kb["effects"]
        self.eff_row = combo_row("Effect", [EFFECTS[e] for e in self.effects])
        hw = set(kb.get("hw_effects", []))
        self.eff_row.set_subtitle("Effects marked ✦ run on the keyboard controller" if hw else
                                  "Animated effects are drawn by the uwctl service")
        if hw:
            self.eff_row.set_model(Gtk.StringList.new(
                [EFFECTS[e] + (" ✦" if e in hw else "") for e in self.effects]))
        if k["effect"] in self.effects:
            self.eff_row.set_selected(self.effects.index(k["effect"]))
        self.eff_row.connect("notify::selected", lambda *_: self._kbd_changed())
        g.add(self.eff_row)

        # colours
        self.colour_row = Adw.ActionRow(title="Colours")
        self.colour_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.colour_buttons: list[ColourButton] = []
        for hx in k["colors"]:
            self._add_colour(hx)
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Add a colour / zone")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: (self._add_colour("#ffffff"), self._kbd_changed()))
        rem = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove last colour")
        rem.add_css_class("flat")
        rem.connect("clicked", lambda *_: self._remove_colour())
        self.colour_row.add_suffix(self.colour_box)
        self.colour_row.add_suffix(rem)
        self.colour_row.add_suffix(add)
        self.colour_row.set_subtitle("Several colours = zones left to right (static) or a palette (effects)")
        g.add(self.colour_row)

        pal = Adw.ActionRow(title="Quick colours")
        pbox = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        for rgb in col.DEFAULT_PALETTE + [(255, 255, 255)]:
            hx = col.to_hex(rgb)
            b = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text=hx)
            da = Gtk.DrawingArea(content_width=18, content_height=18)
            da.set_draw_func(lambda _a, cr, w, h, c=rgb: (cr.set_source_rgb(*(x / 255 for x in c)),
                                                          cr.rectangle(0, 0, w, h), cr.fill()))
            b.set_child(da)
            b.connect("clicked", self._quick_colour, hx)
            pbox.append(b)
        pal.add_suffix(pbox)
        g.add(pal)

        self.random_row, self.random_sw = switch_row("Use built-in colours", "Ignore the colours above for animated effects")
        self.random_sw.set_active(k["random_colors"])
        self.random_sw.connect("notify::active", lambda *_: self._kbd_changed())
        g.add(self.random_row)

        r, self.bri = scale_row("Brightness", 0, 100, 5)
        self.bri.set_value(k["brightness"])
        self.bri.connect("value-changed", lambda *_: self._kbd_changed())
        g.add(r)
        r, self.speed = scale_row("Speed", 1, 10, 1)
        self.speed.set_value(k["speed"])
        self.speed.connect("value-changed", lambda *_: self._kbd_changed())
        self.speed_row = r
        g.add(r)
        self.dir_row = combo_row("Direction", [DIRECTION_LABELS[d] for d in DIRECTIONS])
        self.dir_row.set_selected(DIRECTIONS.index(k["direction"]))
        self.dir_row.connect("notify::selected", lambda *_: self._kbd_changed())
        g.add(self.dir_row)
        page.add(g)

        g2 = Adw.PreferencesGroup(title="On battery")
        self.batbri_row, self.batbri_sw = switch_row("Different brightness on battery",
                                                     "Like the AC/DC light levels in the Windows app")
        self.batbri_sw.set_active(k["battery_brightness"] is not None)
        self.batbri_sw.connect("notify::active", lambda *_: self._kbd_changed())
        g2.add(self.batbri_row)
        r, self.batbri = scale_row("Battery brightness", 0, 100, 5)
        self.batbri.set_value(k["battery_brightness"] if k["battery_brightness"] is not None else 30)
        self.batbri.connect("value-changed", lambda *_: self._kbd_changed())
        self.batbri_scale_row = r
        g2.add(r)
        page.add(g2)

        if self.caps.get("lightbar"):
            lb = self.state["lightbar"]
            g3 = Adw.PreferencesGroup(title="Lightbar")
            r, self.lb_on = switch_row("Lightbar on")
            self.lb_on.set_active(lb["enabled"])
            self.lb_on.connect("notify::active", lambda *_: self._lb_changed())
            g3.add(r)
            r, self.lb_rainbow = switch_row("Rainbow animation")
            self.lb_rainbow.set_active(lb["rainbow"])
            self.lb_rainbow.connect("notify::active", lambda *_: self._lb_changed())
            g3.add(r)
            r = Adw.ActionRow(title="Colour")
            self.lb_colour = ColourButton(lb["color"], self._lb_changed)
            r.add_suffix(self.lb_colour)
            g3.add(r)
            page.add(g3)

        self._update_light_visibility()
        self.add_page(page, "light", "Lighting", "keyboard-brightness-symbolic")

    def _add_colour(self, hx: str) -> None:
        if len(self.colour_buttons) >= 8:
            return
        b = ColourButton(hx, self._kbd_changed)
        self.colour_buttons.append(b)
        self.colour_box.append(b)

    def _remove_colour(self) -> None:
        if len(self.colour_buttons) > 1:
            b = self.colour_buttons.pop()
            self.colour_box.remove(b)
            self._kbd_changed()

    def _quick_colour(self, _btn, hx: str) -> None:
        self.colour_buttons[0].set(hx)
        self._kbd_changed()

    def _update_light_visibility(self) -> None:
        eff = self.effects[self.eff_row.get_selected()]
        animated = eff not in ("static", "off")
        self.speed_row.set_visible(animated)
        self.dir_row.set_visible(eff in DIRECTIONAL)
        self.random_row.set_visible(animated and eff not in ("rainbow", "rainbow_wave"))
        self.colour_row.set_visible(eff not in ("off", "rainbow", "rainbow_wave"))
        self.batbri_scale_row.set_visible(self.batbri_sw.get_active())

    def _kbd_changed(self) -> None:
        if self._loading:
            return
        self._update_light_visibility()
        eff = self.effects[self.eff_row.get_selected()]
        kb = {
            "effect": eff,
            "colors": [b.get() for b in self.colour_buttons],
            "brightness": int(self.bri.get_value()),
            "speed": int(self.speed.get_value()),
            "direction": DIRECTIONS[self.dir_row.get_selected()],
            "random_colors": self.random_sw.get_active(),
            "battery_brightness": int(self.batbri.get_value()) if self.batbri_sw.get_active() else None,
        }
        self.push({"keyboard": kb})

    def _lb_changed(self) -> None:
        self.push({"lightbar": {"managed": True, "enabled": self.lb_on.get_active(),
                                "rainbow": self.lb_rainbow.get_active(), "color": self.lb_colour.get()}})

    # ---- fans ------------------------------------------------------
    def build_fans(self) -> None:
        page = Adw.PreferencesPage()
        fcaps = self.caps["fans"]
        f = self.state["fans"]
        g = Adw.PreferencesGroup(title="Fan mode")
        modes = ["auto", "max", "custom"]
        self.fan_mode_row = combo_row("Mode", ["Auto (firmware)", "Maximum", "Custom curve"])
        self.fan_mode_row.set_selected(modes.index(f["mode"]))
        self.fan_mode_row.connect("notify::selected", self._fan_mode_changed)
        kind = fcaps.get("custom_kind")
        if "custom" not in fcaps.get("modes", []):
            self.fan_mode_row.set_subtitle("Custom curves aren't supported on this laptop")
        elif kind == "software":
            self.fan_mode_row.set_subtitle("Custom curves run in the uwctl service, which sets the "
                                           "fan speed every 2 s")
        g.add(self.fan_mode_row)
        if kind == "software":
            r, self.same_sw = switch_row("Run both fans together",
                                         "Both fans follow whichever of CPU or GPU needs more cooling "
                                         "(they share heat pipes)")
            self.same_sw.set_active(f.get("same_speed", True))
            self.same_sw.connect("notify::active", lambda s, _p: self.push(
                {"fans": {"same_speed": s.get_active()}}, "fans_same", 0))
            g.add(r)
        page.add(g)

        desc = ("Drag points · double-click to add · right-click to remove. "
                "Above 90 °C the fans always run at 100 %. ")
        if kind == "software":
            desc += ("Speeds rise straight away and fall gently, so the fans don't hunt. If the "
                     "service stops, the fans go back to Auto.")
        else:
            desc += "The shaded steps show what is written to the embedded controller."
        g2 = Adw.PreferencesGroup(title="Fan curve", description=desc)
        bar = Gtk.Box(spacing=8, margin_bottom=8)
        self.curve_which = "cpu"
        tog = Gtk.Box(css_classes=["linked"])
        self.which_btns = {}
        first = None
        for which, label in (("cpu", "CPU fan"), ("gpu", "GPU fan")):
            b = Gtk.ToggleButton(label=label)
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._which_changed, which)
            tog.append(b)
            self.which_btns[which] = b
        bar.append(tog)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        for preset in self.caps.get("presets", {}):
            b = Gtk.Button(label=preset.capitalize())
            b.connect("clicked", self._preset, preset)
            bar.append(b)
        g2.add(bar)
        self.curves = {"cpu": f["cpu_curve"], "gpu": f["gpu_curve"]}
        frame = Gtk.Frame()
        self.curve = CurveEditor(self._curve_edited)
        self.curve.show_table = kind != "software"
        self.curve.set_points(self.curves["cpu"])
        self.curve.set_size_request(-1, 320)
        frame.set_child(self.curve)
        g2.add(frame)

        btns = Gtk.Box(spacing=8, margin_top=10, halign=Gtk.Align.END)
        copy = Gtk.Button(label="Copy to other fan")
        copy.connect("clicked", self._copy_curve)
        btns.append(copy)
        self.apply_curve_btn = Gtk.Button(label="Apply curve", css_classes=["suggested-action"])
        self.apply_curve_btn.connect("clicked", self._apply_curve)
        btns.append(self.apply_curve_btn)
        g2.add(btns)
        self.which_btns["cpu"].set_active(True)
        page.add(g2)
        self.curve_group = g2
        self._fan_editor_sensitive()
        self.add_page(page, "fans", "Fans", "weather-windy-symbolic")

    def _fan_editor_sensitive(self) -> None:
        self.curve_group.set_sensitive("custom" in self.caps["fans"].get("modes", []))

    def _fan_mode_changed(self, row, _p) -> None:
        mode = ["auto", "max", "custom"][row.get_selected()]
        if self._loading:
            return
        if mode == "custom" and "custom" not in self.caps["fans"].get("modes", []):
            self.toast("Custom fan curves are not supported by this EC")
            self._loading = True
            row.set_selected(0)
            self._loading = False
            return
        partial = {"mode": mode}
        if mode == "custom":
            partial.update(cpu_curve=self.curves["cpu"], gpu_curve=self.curves["gpu"])
        self.push({"fans": partial}, "fans", 0)
        if hasattr(self, "quick_fan"):
            self._loading = True
            self.quick_fan[mode].set_active(True)
            self._loading = False

    def _which_changed(self, btn, which) -> None:
        if btn.get_active():
            self.curves[self.curve_which] = self.curve.get_points()
            self.curve_which = which
            self.curve.set_points(self.curves[which])

    def _curve_edited(self, pts) -> None:
        self.curves[self.curve_which] = pts
        self.apply_curve_btn.set_label("Apply curve •")

    def _preset(self, _b, name) -> None:
        pts = self.caps["presets"][name]
        self.curve.set_points(pts)
        self._curve_edited(self.curve.get_points())

    def _copy_curve(self, _b) -> None:
        other = "gpu" if self.curve_which == "cpu" else "cpu"
        self.curves[other] = self.curve.get_points()
        self.toast(f"Copied to the {other.upper()} fan")
        self._curve_edited(self.curve.get_points())

    def _apply_curve(self, _b) -> None:
        self.curves[self.curve_which] = self.curve.get_points()
        self.apply_curve_btn.set_sensitive(False)

        def done(res):
            self.apply_curve_btn.set_sensitive(True)
            self.apply_curve_btn.set_label("Apply curve")
            self._after_set(res)
            if not res.get("errors"):
                self.toast("Fan curve written to the embedded controller")
            self._loading = True
            self.fan_mode_row.set_selected(2)
            self.quick_fan["custom"].set_active(True)
            self._loading = False

        self.call_async("set", done, state={"fans": {"mode": "custom", "cpu_curve": self.curves["cpu"],
                                                     "gpu_curve": self.curves["gpu"]}})

    # ---- power -----------------------------------------------------
    def build_power(self) -> None:
        page = Adw.PreferencesPage()
        groups = 0
        if self.caps.get("profiles"):
            groups += 1
            g = Adw.PreferencesGroup(title="Performance profile")
            items = ["Don't change"] + [PROFILES[p] for p in PROFILES]
            names = [None] + list(PROFILES)
            self.prof_ac = combo_row("On AC power", items)
            self.prof_bat = combo_row("On battery", items, "Leave at “Don't change” to use the AC profile")
            self.prof_ac.set_selected(names.index(self.state["profile"]["ac"]))
            self.prof_bat.set_selected(names.index(self.state["profile"]["battery"]))
            self.prof_ac.connect("notify::selected", lambda r, _p: self.push(
                {"profile": {"ac": names[r.get_selected()]}}, "profile", 0))
            self.prof_bat.connect("notify::selected", lambda r, _p: self.push(
                {"profile": {"battery": names[r.get_selected()]}}, "profile_bat", 0))
            g.add(self.prof_ac)
            g.add(self.prof_bat)
            page.add(g)

        if self.caps.get("charge_limit"):
            groups += 1
            g = Adw.PreferencesGroup(title="Battery")
            lim = self.state["battery"]["charge_limit"]
            r, self.cl_sw = switch_row("Limit charging", "Keeps the battery healthier when mostly plugged in")
            self.cl_sw.set_active(lim is not None and lim < 100)
            g.add(r)
            r2, self.cl = scale_row("Stop charging at", 40, 100, 5)
            self.cl.set_value(lim if lim and lim < 100 else 80)
            g.add(r2)

            def changed(*_):
                r2.set_visible(self.cl_sw.get_active())
                v = int(self.cl.get_value()) if self.cl_sw.get_active() else 100
                self.push({"battery": {"charge_limit": v}})
            self.cl_sw.connect("notify::active", changed)
            self.cl.connect("value-changed", changed)
            r2.set_visible(self.cl_sw.get_active())
            page.add(g)

        toggles = self.caps.get("toggles") or []
        if toggles:
            groups += 1
            g = Adw.PreferencesGroup(title="Keyboard")
            st = self.state["toggles"]
            labels = {"fn_lock": ("Fn lock", "F1–F12 without holding Fn"),
                      "super_key": ("Windows key", "Turn off to stop accidental presses in games"),
                      "touchpad_toggle": ("Touchpad toggle key", "")}
            for t in toggles:
                title, sub = labels.get(t, (t, ""))
                r, sw = switch_row(title, sub)
                sw.set_active(st.get(t, t != "fn_lock"))
                sw.connect("notify::active", lambda s, _p, key=t: self.push({"toggles": {key: s.get_active()}}, key, 0))
                g.add(r)
            page.add(g)

        if not groups:
            g = Adw.PreferencesGroup(title="Power",
                                     description="No power features are available. Run Diagnostics from the menu.")
            page.add(g)
        self.add_page(page, "power", "Power", "battery-full-symbolic")

    # ---- dialogs ---------------------------------------------------
    def show_doctor(self) -> None:
        def done(r):
            win = Adw.Window(transient_for=self, modal=True, title="Diagnostics")
            win.set_default_size(720, 560)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.append(Adw.HeaderBar())
            tv = Gtk.TextView(editable=False, monospace=True, top_margin=12, left_margin=12, right_margin=12)
            tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            tv.get_buffer().set_text(format_report(r))
            sw = Gtk.ScrolledWindow(vexpand=True)
            sw.set_child(tv)
            box.append(sw)
            win.set_content(box)
            win.present()
        self.call_async("doctor", done)

    def reapply(self) -> None:
        self.call_async("apply", lambda r: self.toast(
            "Settings re-applied" if not r["errors"] else "; ".join(f"{k}: {v}" for k, v in r["errors"].items())))

    def show_about(self) -> None:
        about = Adw.AboutWindow(transient_for=self, application_name="Uniwill Control Center",
                                application_icon=APP_ID, version=__version__,
                                comments="Keyboard lighting, fan curves and power modes for "
                                         "Uniwill-based gaming laptops on Linux.",
                                license_type=Gtk.License.GPL_2_0)
        about.present()


class ErrorWindow(Adw.ApplicationWindow):
    def __init__(self, app, message: str) -> None:
        super().__init__(application=app, title="Uniwill Control Center")
        self.set_default_size(560, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Adw.HeaderBar())
        sp = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Can't reach the uwctl service",
                            description=message, vexpand=True)
        demo = Gtk.Button(label="Try demo mode", halign=Gtk.Align.CENTER, css_classes=["pill"])
        demo.connect("clicked", lambda *_: app.start(demo=True, replace=self))
        sp.set_child(demo)
        box.append(sp)
        self.set_content(box)


class App(Adw.Application):
    def __init__(self, demo: bool = False) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.demo = demo
        self.connect("activate", lambda *_: self.start(self.demo))

    def start(self, demo: bool = False, replace=None) -> None:
        try:
            client = connect(dry_run=demo)
            win = Window(self, client)
        except (DaemonUnavailable, ClientError) as exc:
            win = ErrorWindow(self, str(exc))
        win.present()
        if replace is not None:
            replace.destroy()


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    demo = "--demo" in argv
    argv = [a for a in argv if a != "--demo"]
    return App(demo).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
