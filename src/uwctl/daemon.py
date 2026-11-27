"""uwctld - the root service that owns the hardware.

* applies saved settings at boot, after resume and when AC power changes
* runs software keyboard effects
* re-applies custom fan tables if the EC drops them, and fails safe to max
  fans if temperatures run away
* serves a small JSON API on a UNIX socket (``/run/uwctl/uwctl.sock``) so
  the GUI and CLI can run as a normal user in the ``uwctl`` group
"""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import signal
import socket
import struct
import threading
import time

from . import __version__
from .access import SocketAccess
from .controller import FAN_TICK, Controller
from .state import DEFAULT_PATH, StateError, StateStore

log = logging.getLogger("uwctl")
SOCKET = os.environ.get("UWCTL_SOCKET", "/run/uwctl/uwctl.sock")
MAX_REQUEST = 256 * 1024


def dispatch(ctl: Controller, cmd: str, args: dict):
    if cmd == "ping":
        return {"version": __version__}
    if cmd == "capabilities":
        return ctl.capabilities()
    if cmd == "status":
        return ctl.status()
    if cmd == "get_state":
        return ctl.store.state
    if cmd == "set":
        partial = args.get("state")
        if not isinstance(partial, dict):
            raise StateError("'set' needs a 'state' object")
        errors = ctl.update(partial)
        return {"state": ctl.store.state, "errors": errors}
    if cmd == "apply":
        return {"errors": ctl.apply(args.get("sections") or None)}
    if cmd == "doctor":
        from .doctor import report
        return report(ctl)
    raise ValueError(f"unknown command {cmd!r}")


def _boot_offset() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    except (AttributeError, OSError):  # pragma: no cover
        return 0.0


class Daemon:
    def __init__(self, ctl: Controller, socket_path: str = SOCKET, group: str = "uwctl",
                 sessions_dir: str | None = None) -> None:
        self.ctl = ctl
        self.sessions_dir = sessions_dir
        self.socket_path = socket_path
        self.group = group
        self.stop = threading.Event()
        self.sock: socket.socket | None = None
        self.access: SocketAccess | None = None
        self.group_ok = False

    # ----------------------------------------------------------- socket
    def _bind(self) -> socket.socket:
        os.makedirs(os.path.dirname(self.socket_path) or ".", exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o117)
        try:
            s.bind(self.socket_path)
        finally:
            os.umask(old)
        try:
            gid = grp.getgrnam(self.group).gr_gid
            os.chown(self.socket_path, 0 if os.geteuid() == 0 else -1, gid)
            os.chmod(self.socket_path, 0o660)
            self.group_ok = True
        except KeyError:
            log.warning("group %r missing - only root can use the socket", self.group)
            os.chmod(self.socket_path, 0o600)
        except PermissionError:
            os.chmod(self.socket_path, 0o600)
        s.listen(8)
        s.settimeout(1.0)
        return s

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            try:
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", creds)
            except OSError:
                uid = -1
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST:
                    conn.sendall(b'{"ok": false, "error": "request too large"}\n')
                    return
            try:
                req = json.loads(buf or b"{}")
                cmd = req.get("cmd", "")
                if cmd not in ("ping", "status", "capabilities", "get_state"):
                    log.info("uid %s: %s", uid, cmd)
                res = {"ok": True, "result": dispatch(self.ctl, cmd, req.get("args") or {})}
            except Exception as exc:  # report every error back to the client
                res = {"ok": False, "error": str(exc)}
            conn.sendall(json.dumps(res).encode() + b"\n")

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                raise
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    # ------------------------------------------------------- background
    def _loop(self) -> None:
        next_watch = next_fan = 0.0
        offset = _boot_offset()
        while not self.stop.is_set():
            now = time.monotonic()
            new_offset = _boot_offset()
            if new_offset - offset > 2.0:
                log.info("resume from suspend detected - re-applying settings")
                self.stop.wait(3.0)
                self.ctl.apply()
                offset = _boot_offset()
                continue
            offset = new_offset
            if self.ctl.sw is not None and now >= next_fan:
                self.ctl.fan_tick()
                next_fan = now + FAN_TICK
            if now >= next_watch:
                self.ctl.watchdog()
                if self.access:
                    self.access.refresh()
                next_watch = now + 5.0
            kb = self.ctl.keyboard
            if self.ctl.effect and kb:
                try:
                    with self.ctl.lock:
                        self.ctl.render_effect()
                except Exception as exc:
                    log.error("effect stopped: %s", exc)
                    self.ctl.effect = None
                self.stop.wait(kb.frame_interval)
            else:
                self.stop.wait(0.5)

    def run(self) -> None:
        self.ctl.daemon_mode = True
        errors = self.ctl.apply()
        if errors:
            log.warning("some settings could not be applied: %s", errors)
        self.sock = self._bind()
        kw = {"sessions_dir": self.sessions_dir} if self.sessions_dir else {}
        self.access = SocketAccess(self.socket_path, self.group_ok, **kw)
        self.access.refresh()
        log.info("uwctld %s listening on %s (EC: %s, keyboard: %s)", __version__, self.socket_path,
                 self.ctl.ec.name if self.ctl.ec else "none",
                 self.ctl.keyboard.backend if self.ctl.keyboard else "none")
        worker = threading.Thread(target=self._loop, daemon=True)
        worker.start()
        try:
            self._serve()
        finally:
            self.stop.set()
            worker.join(timeout=3)
            self.ctl.release_fans()
            self.sock.close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass


def release_fans(state_path: str, dry_run: bool = False, ec=None) -> int:
    """If the saved mode is a *software* fan curve, the fans are sitting in
    full fan mode with whatever duty uwctld last wrote.  Without the daemon
    nothing would adjust them, so put them back on firmware Auto."""
    from .ec import ECError, ECUnavailable, open_ec
    from .fans import FanController

    try:
        mode = StateStore(state_path).state["fans"]["mode"]
        if mode != "custom":
            return 0
        fc = FanController(ec or open_ec(dry_run=dry_run))
        if fc.capabilities()["custom_kind"] == "software" and fc.boost_on():
            fc.set_auto()
            log.info("fans handed back to the firmware (Auto)")
    except (ECError, ECUnavailable, OSError) as exc:
        log.error("release-fans: %s", exc)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="uwctld", description="Uniwill laptop control daemon")
    ap.add_argument("--socket", default=SOCKET)
    ap.add_argument("--state", default=DEFAULT_PATH)
    ap.add_argument("--group", default="uwctl")
    ap.add_argument("--dry-run", action="store_true", help="use a simulated EC (no hardware access)")
    ap.add_argument("--release-fans", action="store_true",
                    help="hand the fans back to the firmware if a software fan curve was "
                         "running (used by systemd after the service stops or crashes)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if a.release_fans:
        return release_fans(a.state, a.dry_run)
    ctl = Controller(StateStore(a.state), dry_run=a.dry_run)
    d = Daemon(ctl, a.socket, a.group)

    def _term(*_):
        d.stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    d.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
