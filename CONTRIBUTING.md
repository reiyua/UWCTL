# Contributing

Thanks for helping! The most useful contribution of all is a **hardware
report**. Run `uwctl doctor` on your laptop and
[open one](../../issues/new/choose), whether things work or not.

## Development setup

Nothing needs building. You need Python 3.10+ and, for the GUI, PyGObject with
GTK 4 and libadwaita 1.2+.

```sh
make test                                   # unit tests (simulated EC, fake /sys)
PYTHONPATH=src python3 -m uwctl.cli --dry-run status
PYTHONPATH=src python3 -m uwctl.gui.app --demo
PYTHONPATH=src python3 -m uwctl.daemon --dry-run --socket /tmp/uwctl.sock --state /tmp/state.json
```

`--dry-run` and `--demo` use `MockEC`, an in-memory EC, so you can work on
everything without the hardware.

## Layout

| Path | What |
|---|---|
| `src/uwctl/constants.py` | EC register map and the **write allowlist** |
| `src/uwctl/ec.py` | EC access: `INOU0000`, WMI and mock |
| `src/uwctl/keyboard.py` | keyboard backends (EC, ITE 829x, LED class) |
| `src/uwctl/fans.py` | curve maths, EC fan tables, direct fan control |
| `src/uwctl/controller.py` | settings → hardware, software fan curve, failsafe |
| `src/uwctl/daemon.py` | `uwctld`: socket API, background loop |
| `src/uwctl/access.py` | seat-based socket access (ACL) |
| `src/uwctl/cli.py`, `gui/` | front ends |
| `packaging/` | Debian and Arch packaging |

See [docs/how-it-works.md](docs/how-it-works.md) for protocols and registers.

## Rules for hardware code

- **Never write an EC register you can't explain.** New writable registers go
  into `constants.WRITABLE`, with a comment and a source (kernel driver,
  tuxedo-drivers, or careful testing on real hardware).
- Every hardware path gets a test against `MockEC` or a fake `/sys`
  (see `tests/conftest.py`).
- Anything that can leave fans slower than the firmware would must have a
  failsafe.

## Pull requests

- Run `make test` and `pyflakes src tests` before pushing; CI runs both.
- Keep pull requests focused, and mention which laptop you tested on.
