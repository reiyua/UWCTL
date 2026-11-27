"""uwctl - command line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from . import colors as col
from .client import ClientError, connect
from .fans import CPU_T_MAX, PRESETS, curve_to_table
from .keyboard import DIRECTIONS, EFFECTS
from .power import PROFILES


def parse_curve(text: str) -> list[list[int]]:
    """'40:0,50:25,60:40' -> [[40,0],[50,25],[60,40]]"""
    pts = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        t, s = part.split(":")
        pts.append([int(t), int(s.rstrip("%"))])
    return pts


def fmt_curve(curve) -> str:
    return ", ".join(f"{t}°C→{s}%" for t, s in curve)


def _v(x, unit=""):
    return "-" if x is None else f"{x:g}{unit}" if isinstance(x, (int, float)) else str(x)


def print_status(st: dict, state: dict) -> None:
    s = st.get("sensors", {})
    print(f"CPU {_v(s.get('cpu_temp'), '°C'):>7}   GPU {_v(s.get('gpu_temp'), '°C'):>7}")
    for i in (1, 2):
        rpm, duty = s.get(f"fan{i}_rpm"), s.get(f"fan{i}_duty")
        if rpm is not None or duty is not None:
            print(f"Fan {i}  {_v(rpm, ' rpm'):>9}   duty {_v(duty, '%')}")
    print(f"Fan mode     {st.get('fan_mode') or state['fans']['mode']}"
          + (f" ({st['fan_control']} curve)" if st.get("fan_control") else "")
          + ("  (FAILSAFE: max)" if st.get("failsafe") else ""))
    print(f"Profile      {st.get('profile') or '-'}")
    print(f"Power        {'AC' if st.get('on_ac') else 'battery' if st.get('on_ac') is False else '?'}")
    k = state["keyboard"]
    print(f"Keyboard     {k['effect']} {' '.join(k['colors'])} @ {k['brightness']}%")
    if st.get("charge_limit") is not None:
        print(f"Charge limit {st['charge_limit']}%")
    for name, on in (st.get("toggles") or {}).items():
        print(f"{name:12} {'on' if on else 'off'}")
    for sec, err in (st.get("errors") or {}).items():
        print(f"! {sec}: {err}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="uwctl",
        description="Control keyboard lighting, fans and power modes on Uniwill-based laptops.")
    ap.add_argument("--version", action="version", version=f"uwctl {__version__}")
    ap.add_argument("--direct", action="store_true", help="drive the hardware without the daemon (root)")
    ap.add_argument("--dry-run", action="store_true", help="use a simulated laptop (no hardware access)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("status", help="temperatures, fans and current settings")
    p = sub.add_parser("monitor", help="live temperatures and fan speeds")
    p.add_argument("-i", "--interval", type=float, default=1.0)
    sub.add_parser("doctor", help="detect hardware support and print tips")
    sub.add_parser("apply", help="re-apply saved settings")
    sub.add_parser("caps", help="show what this laptop supports")

    k = sub.add_parser("kbd", help="keyboard backlight").add_subparsers(dest="kcmd", metavar="ACTION")
    p = k.add_parser("color", help="static colour(s): one colour, or one per zone")
    p.add_argument("colors", nargs="+", metavar="COLOR", help="#rrggbb, r,g,b or a name")
    p.add_argument("-b", "--brightness", type=int)
    p = k.add_parser("effect", help="lighting effect")
    p.add_argument("effect", choices=list(EFFECTS))
    p.add_argument("-c", "--color", action="append", dest="colors", metavar="COLOR")
    p.add_argument("-s", "--speed", type=int, help="1 (slow) - 10 (fast)")
    p.add_argument("-d", "--direction", choices=DIRECTIONS)
    p.add_argument("-r", "--random", action="store_true", help="use the built-in palette")
    p.add_argument("-b", "--brightness", type=int)
    p = k.add_parser("brightness", help="brightness in percent")
    p.add_argument("percent", type=int)
    p.add_argument("--battery", type=int, metavar="PERCENT", help="different brightness on battery (-1 = same)")
    k.add_parser("off", help="turn the backlight off")
    p = k.add_parser("backend", help="force a keyboard backend")
    p.add_argument("name", choices=["auto", "ite829x", "leds", "ec", "none"])

    f = sub.add_parser("fan", help="fan control").add_subparsers(dest="fcmd", metavar="ACTION")
    f.add_parser("auto", help="firmware automatic fan control")
    f.add_parser("max", help="both fans at full speed")
    p = f.add_parser("custom", help="custom fan curves (EC hardware tables)")
    p.add_argument("-p", "--preset", choices=list(PRESETS))
    p.add_argument("--cpu", metavar="CURVE", help="e.g. '40:0,50:25,60:40,70:60,80:80,88:100'")
    p.add_argument("--gpu", metavar="CURVE")
    p.add_argument("--separate", action="store_true",
                   help="software curves: drive each fan from its own temperature")
    p = f.add_parser("show", help="show saved curves and the EC table they become")

    p = sub.add_parser("profile", help="performance profile")
    p.add_argument("name", choices=list(PROFILES) + ["none"])
    p.add_argument("--battery", choices=list(PROFILES) + ["none"], help="profile to use on battery")

    p = sub.add_parser("battery", help="battery charge limit")
    p.add_argument("limit", help="percent (40-100) or 'off'")

    p = sub.add_parser("lightbar", help="lightbar (if fitted)")
    p.add_argument("state", choices=["on", "off", "rainbow"])
    p.add_argument("-c", "--color")

    for name, key in (("fnlock", "fn_lock"), ("winkey", "super_key"), ("touchpad-toggle", "touchpad_toggle")):
        p = sub.add_parser(name, help=f"{key.replace('_', ' ')} on/off (uniwill-laptop driver)")
        p.add_argument("state", choices=["on", "off"])
        p.set_defaults(toggle=key)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    try:
        c = connect(direct=a.direct, dry_run=a.dry_run)
        return run(a, c)
    except (ClientError, ValueError) as exc:
        print(f"uwctl: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _set(c, partial: dict, a) -> int:
    res = c.call("set", state=partial)
    errs = res.get("errors") or {}
    for sec, err in errs.items():
        print(f"uwctl: {sec}: {err}", file=sys.stderr)
    if a.json:
        print(json.dumps(res, indent=2))
    return 1 if errs else 0


def run(a, c) -> int:
    out = (lambda x: print(json.dumps(x, indent=2))) if a.json else None
    if getattr(c, "mode", "") == "demo":
        print("(dry run: simulated laptop, nothing is sent to hardware)", file=sys.stderr)

    if a.cmd == "status":
        st, state = c.call("status"), c.call("get_state")
        out({"status": st, "state": state}) if out else print_status(st, state)
        return 0
    if a.cmd == "monitor":
        while True:
            s = c.call("status")["sensors"]
            print(f"\rCPU {_v(s.get('cpu_temp'), '°C'):>6}  GPU {_v(s.get('gpu_temp'), '°C'):>6}  "
                  f"fan1 {_v(s.get('fan1_rpm'), 'rpm'):>8} ({_v(s.get('fan1_duty'), '%')})  "
                  f"fan2 {_v(s.get('fan2_rpm'), 'rpm'):>8} ({_v(s.get('fan2_duty'), '%')})   ",
                  end="", flush=True)
            time.sleep(a.interval)
    if a.cmd == "doctor":
        from .doctor import format_report
        r = c.call("doctor")
        out(r) if out else print(format_report(r))
        return 0
    if a.cmd == "caps":
        caps = c.call("capabilities")
        if out:
            out(caps)
        else:
            kb = caps.get("keyboard") or {}
            print(f"EC access     {caps.get('ec') or 'no (' + str(caps.get('ec_error')) + ')'}")
            print(f"Keyboard      {kb.get('backend', 'none')} {kb.get('kind', '')} "
                  f"zones={kb.get('zones', 0)} effects: {', '.join(kb.get('effects', []))}")
            print(f"Fan modes     {', '.join(caps['fans'].get('modes', []))}")
            print(f"Profiles      {', '.join(caps.get('profiles') or ['-'])}")
            print(f"Lightbar      {caps.get('lightbar') or '-'}")
            print(f"Charge limit  {'yes' if caps.get('charge_limit') else 'no'}")
            print(f"Toggles       {', '.join(caps.get('toggles') or ['-'])}")
        return 0
    if a.cmd == "apply":
        errs = c.call("apply")["errors"]
        for sec, err in errs.items():
            print(f"uwctl: {sec}: {err}", file=sys.stderr)
        return 1 if errs else 0

    if a.cmd == "kbd":
        kb: dict = {}
        if a.kcmd == "color":
            kb = {"effect": "static", "colors": [col.to_hex(col.parse(x)) for x in a.colors]}
            if a.brightness is not None:
                kb["brightness"] = a.brightness
        elif a.kcmd == "effect":
            kb = {"effect": a.effect, "random_colors": a.random}
            if a.colors:
                kb["colors"] = [col.to_hex(col.parse(x)) for x in a.colors]
            for key in ("speed", "direction", "brightness"):
                if getattr(a, key) is not None:
                    kb[key] = getattr(a, key)
        elif a.kcmd == "brightness":
            kb = {"brightness": a.percent}
            if a.battery is not None:
                kb["battery_brightness"] = None if a.battery < 0 else a.battery
            if c.call("get_state")["keyboard"]["effect"] == "off":
                kb["effect"] = "static"
        elif a.kcmd == "off":
            kb = {"effect": "off"}
        elif a.kcmd == "backend":
            kb = {"backend": a.name}
        else:
            print("uwctl kbd {color,effect,brightness,off,backend} - see 'uwctl kbd -h'", file=sys.stderr)
            return 2
        return _set(c, {"keyboard": kb}, a)

    if a.cmd == "fan":
        if a.fcmd in ("auto", "max"):
            return _set(c, {"fans": {"mode": a.fcmd}}, a)
        if a.fcmd == "custom":
            fans: dict = {"mode": "custom"}
            if a.preset:
                fans["cpu_curve"] = fans["gpu_curve"] = PRESETS[a.preset]
            if a.cpu:
                fans["cpu_curve"] = parse_curve(a.cpu)
                fans.setdefault("gpu_curve", fans["cpu_curve"])
            if a.gpu:
                fans["gpu_curve"] = parse_curve(a.gpu)
            fans["same_speed"] = not a.separate
            return _set(c, {"fans": fans}, a)
        if a.fcmd == "show":
            f = c.call("get_state")["fans"]
            if out:
                out(f)
                return 0
            print(f"mode: {f['mode']}")
            for which in ("cpu", "gpu"):
                print(f"{which.upper()} curve: {fmt_curve(f[which + '_curve'])}")
            kind = c.call("capabilities")["fans"].get("custom_kind")
            if kind == "software":
                print("\nCurves run in software: uwctld sets the fan speed every 2 s "
                      f"({'both fans together' if f.get('same_speed', True) else 'each fan separately'}).")
                return 0
            print("\nEC table for the CPU fan (start-end °C: duty %):")
            for start, end, duty in curve_to_table(f["cpu_curve"], CPU_T_MAX):
                if start >= CPU_T_MAX:
                    break
                print(f"  {start:3d}-{end:3d}: {round(duty / 2):3d}%")
            return 0
        print("uwctl fan {auto,max,custom,show}", file=sys.stderr)
        return 2

    if a.cmd == "profile":
        prof = {"ac": None if a.name == "none" else a.name}
        if a.battery:
            prof["battery"] = None if a.battery == "none" else a.battery
        return _set(c, {"profile": prof}, a)
    if a.cmd == "battery":
        lim = 100 if a.limit.lower() in ("off", "none") else int(a.limit)
        return _set(c, {"battery": {"charge_limit": lim}}, a)
    if a.cmd == "lightbar":
        lb = {"managed": True, "enabled": a.state != "off", "rainbow": a.state == "rainbow"}
        if a.color:
            lb["color"] = col.to_hex(col.parse(a.color))
        return _set(c, {"lightbar": lb}, a)
    if getattr(a, "toggle", None):
        return _set(c, {"toggles": {a.toggle: a.state == "on"}}, a)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
