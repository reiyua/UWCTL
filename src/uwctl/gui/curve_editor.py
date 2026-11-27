"""A draggable fan-curve editor widget (GTK 4)."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..fans import SAFETY_TEMP, curve_to_table, normalize_curve  # noqa: E402

T_MIN, T_MAX = 20, 100
ML, MR, MT, MB = 44, 14, 14, 30  # margins
HIT = 14


class CurveEditor(Gtk.DrawingArea):
    """Drag points to edit; double-click empty space to add a point;
    right-click a point to delete it."""

    def __init__(self, on_changed=None) -> None:
        super().__init__()
        self.points: list[list[int]] = [[40, 0], [60, 40], [80, 80], [90, 100]]
        self.on_changed = on_changed
        self.live_temp: float | None = None
        self.live_duty: float | None = None
        #: draw the EC staircase (hardware curves only)
        self.show_table = True
        self._drag: int | None = None
        self._drag_origin = (0.0, 0.0)
        self._hover: int | None = None
        self.set_content_width(520)
        self.set_content_height(300)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._pressed)
        self.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)

    # ---------------------------------------------------------- geometry
    def _plot(self):
        w, h = self.get_width(), self.get_height()
        return ML, MT, max(1, w - ML - MR), max(1, h - MT - MB)

    def to_px(self, t: float, s: float):
        x0, y0, pw, ph = self._plot()
        return x0 + (t - T_MIN) / (T_MAX - T_MIN) * pw, y0 + (1 - s / 100) * ph

    def from_px(self, x: float, y: float):
        x0, y0, pw, ph = self._plot()
        t = T_MIN + (x - x0) / pw * (T_MAX - T_MIN)
        s = (1 - (y - y0) / ph) * 100
        return t, s

    def _nearest(self, x: float, y: float) -> int | None:
        best, bd = None, HIT
        for i, (t, s) in enumerate(self.points):
            px, py = self.to_px(t, s)
            d = math.hypot(px - x, py - y)
            if d <= bd:
                best, bd = i, d
        return best

    # ---------------------------------------------------------- data
    def set_points(self, pts) -> None:
        self.points = [[int(t), int(s)] for t, s in pts]
        self.queue_draw()

    def get_points(self) -> list[list[int]]:
        return [list(p) for p in self.points]

    def set_live(self, temp, duty) -> None:
        self.live_temp, self.live_duty = temp, duty
        self.queue_draw()

    def _changed(self) -> None:
        self.queue_draw()
        if self.on_changed:
            self.on_changed(self.get_points())

    # ---------------------------------------------------------- input
    def _drag_begin(self, g, x, y) -> None:
        self._drag = self._nearest(x, y)
        self._drag_origin = (x, y)

    def _drag_update(self, g, dx, dy) -> None:
        if self._drag is None:
            return
        i = self._drag
        t, s = self.from_px(self._drag_origin[0] + dx, self._drag_origin[1] + dy)
        lo = self.points[i - 1][0] + 1 if i > 0 else T_MIN
        hi = self.points[i + 1][0] - 1 if i + 1 < len(self.points) else T_MAX
        t = int(round(min(max(t, lo), hi)))
        s = int(round(min(max(s, 0), 100)))
        # keep the curve non-decreasing
        if i > 0:
            s = max(s, self.points[i - 1][1])
        if i + 1 < len(self.points):
            s = min(s, self.points[i + 1][1])
        self.points[i] = [t, s]
        self.queue_draw()

    def _drag_end(self, g, dx, dy) -> None:
        if self._drag is not None and (abs(dx) > 1 or abs(dy) > 1):
            self._changed()
        self._drag = None

    def _pressed(self, g, n_press, x, y) -> None:
        button = g.get_current_button()
        idx = self._nearest(x, y)
        if button == 3 and idx is not None and len(self.points) > 2:
            del self.points[idx]
            self._changed()
        elif button == 1 and n_press == 2 and idx is None and len(self.points) < 11:
            t, s = self.from_px(x, y)
            t = int(round(min(max(t, T_MIN), T_MAX)))
            if any(p[0] == t for p in self.points):
                return
            self.points.append([t, int(round(min(max(s, 0), 100)))])
            self.points.sort()
            # re-impose monotonic speeds
            run = 0
            for p in self.points:
                run = max(run, p[1])
                p[1] = run
            self._changed()

    def _motion(self, c, x, y) -> None:
        h = self._nearest(x, y)
        if h != self._hover:
            self._hover = h
            self.set_cursor_from_name("grab" if h is not None else None)
            self.queue_draw()

    # ---------------------------------------------------------- drawing
    def _fg(self):
        try:
            c = self.get_color()
            return c.red, c.green, c.blue
        except AttributeError:  # GTK < 4.10
            c = self.get_style_context().get_color()
            return c.red, c.green, c.blue

    def _draw(self, area, cr, w, h) -> None:
        fr, fg, fb = self._fg()
        x0, y0, pw, ph = self._plot()
        accent = (0.21, 0.52, 0.89)

        # safety zone
        sx, _ = self.to_px(SAFETY_TEMP, 0)
        cr.set_source_rgba(0.88, 0.11, 0.14, 0.10)
        cr.rectangle(sx, y0, x0 + pw - sx, ph)
        cr.fill()

        # grid
        cr.set_line_width(1)
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for t in range(T_MIN, T_MAX + 1, 10):
            px, _ = self.to_px(t, 0)
            cr.set_source_rgba(fr, fg, fb, 0.12)
            cr.move_to(px + 0.5, y0)
            cr.line_to(px + 0.5, y0 + ph)
            cr.stroke()
            cr.set_source_rgba(fr, fg, fb, 0.6)
            label = f"{t}°"
            ext = cr.text_extents(label)
            cr.move_to(px - ext.width / 2, y0 + ph + 16)
            cr.show_text(label)
        for s in range(0, 101, 20):
            _, py = self.to_px(T_MIN, s)
            cr.set_source_rgba(fr, fg, fb, 0.12)
            cr.move_to(x0, py + 0.5)
            cr.line_to(x0 + pw, py + 0.5)
            cr.stroke()
            cr.set_source_rgba(fr, fg, fb, 0.6)
            label = f"{s}%"
            ext = cr.text_extents(label)
            cr.move_to(x0 - ext.width - 6, py + ext.height / 2)
            cr.show_text(label)

        try:
            effective = normalize_curve(self.points)
        except Exception:
            effective = self.points
        cr.set_source_rgba(*accent, 0.25)
        if self.show_table:
            # staircase actually written to the EC
            try:
                table = curve_to_table(self.points)
                cr.move_to(*self.to_px(T_MIN, 0))
                for start, end, duty in table:
                    if start >= T_MAX:
                        break
                    pct = duty * 100 / 200 if duty > 1 else 0
                    cr.line_to(*self.to_px(max(start, T_MIN), pct))
                    cr.line_to(*self.to_px(min(end, T_MAX), pct))
                cr.line_to(*self.to_px(T_MAX, 0))
                cr.close_path()
                cr.fill()
            except Exception:
                cr.new_path()
        else:
            # software curve: shade the area under the curve itself
            cr.move_to(*self.to_px(T_MIN, 0))
            cr.line_to(*self.to_px(T_MIN, effective[0][1]))
            for t, s in effective:
                cr.line_to(*self.to_px(max(t, T_MIN), s))
            cr.line_to(*self.to_px(T_MAX, effective[-1][1]))
            cr.line_to(*self.to_px(T_MAX, 0))
            cr.close_path()
            cr.fill()

        # curve (including the forced safety point)
        cr.set_source_rgb(*accent)
        cr.set_line_width(2.5)
        start = self.to_px(T_MIN, effective[0][1])
        cr.move_to(*start)
        for t, s in effective:
            cr.line_to(*self.to_px(t, s))
        cr.line_to(*self.to_px(T_MAX, effective[-1][1]))
        cr.stroke()

        # points
        for i, (t, s) in enumerate(self.points):
            px, py = self.to_px(t, s)
            r = 7 if i in (self._hover, self._drag) else 5.5
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.set_source_rgb(*accent)
            cr.fill_preserve()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(1.5)
            cr.stroke()
            if i in (self._hover, self._drag):
                cr.set_source_rgba(fr, fg, fb, 0.9)
                cr.set_font_size(11)
                cr.move_to(px + 9, py - 9)
                cr.show_text(f"{t}°C  {s}%")

        # live temperature marker
        if self.live_temp is not None and T_MIN <= self.live_temp <= T_MAX:
            lx, _ = self.to_px(self.live_temp, 0)
            cr.set_source_rgba(0.9, 0.38, 0.0, 0.9)
            cr.set_line_width(1.5)
            cr.set_dash([4, 3])
            cr.move_to(lx, y0)
            cr.line_to(lx, y0 + ph)
            cr.stroke()
            cr.set_dash([])
            cr.set_font_size(10)
            txt = f"now {self.live_temp:.0f}°C"
            if self.live_duty is not None:
                txt += f" · fan {self.live_duty:.0f}%"
            cr.move_to(min(lx + 4, x0 + pw - 90), y0 + 12)
            cr.show_text(txt)
