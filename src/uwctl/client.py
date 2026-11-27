"""Talking to uwctld (or to the hardware directly when running as root)."""

from __future__ import annotations

import json
import os
import socket

from .daemon import SOCKET, dispatch


class ClientError(RuntimeError):
    pass


class DaemonUnavailable(ClientError):
    pass


class Client:
    def __init__(self, path: str = SOCKET, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self.mode = "daemon"

    def call(self, cmd: str, **args):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.path)
        except FileNotFoundError:
            raise DaemonUnavailable(
                f"uwctld is not running ({self.path} missing). Start it with "
                "'sudo systemctl enable --now uwctld'") from None
        except PermissionError:
            raise DaemonUnavailable(
                "permission denied on the uwctld socket. uwctld lets in whoever is logged in "
                "at the laptop itself within a few seconds of login; for remote (SSH) use, add "
                "yourself to the 'uwctl' group ('sudo usermod -aG uwctl $USER') and log in again") from None
        except OSError as exc:
            raise DaemonUnavailable(f"cannot reach uwctld: {exc}") from None
        with s:
            s.sendall(json.dumps({"cmd": cmd, "args": args}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        try:
            res = json.loads(buf)
        except ValueError:
            raise ClientError("bad reply from uwctld") from None
        if not res.get("ok"):
            raise ClientError(res.get("error", "unknown error"))
        return res["result"]


class DirectClient:
    """Same interface as :class:`Client`, but drives the hardware in-process.
    Needs root (or ``dry_run``).  Software effects only animate while the
    process runs, so this is for testing and for systems without the daemon."""

    def __init__(self, dry_run: bool = False, state_path: str | None = None) -> None:
        from .controller import Controller
        from .state import DEFAULT_PATH, StateStore

        ec = None
        if dry_run:
            from .ec import MockEC

            rt = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
            state_path = state_path or os.path.join(rt, "uwctl-demo-state.json")
            ec = MockEC(os.path.join(rt, "uwctl-demo-ec.json"))
        self.ctl = Controller(StateStore(state_path or DEFAULT_PATH), ec=ec, dry_run=dry_run)
        self.mode = "demo" if dry_run else "direct"

    def call(self, cmd: str, **args):
        try:
            return json.loads(json.dumps(dispatch(self.ctl, cmd, args)))
        except Exception as exc:
            raise ClientError(str(exc)) from exc


def connect(direct: bool = False, dry_run: bool = False):
    if dry_run:
        return DirectClient(dry_run=True)
    if direct:
        if os.geteuid() != 0:
            raise ClientError("--direct needs root")
        return DirectClient()
    c = Client()
    try:
        c.call("ping")
        return c
    except DaemonUnavailable:
        if os.geteuid() == 0:
            return DirectClient()
        raise
