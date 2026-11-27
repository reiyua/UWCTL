# Changelog

## 0.2.0: first public release

- **Software fan curves** for ECs without built-in fan tables (for example
  the 2020 Tongfang GK5): the service sets the fan speed every 2 s through the
  direct duty registers. Speeds rise fast and fall gently, both fans can run
  together, and the fans return to Auto whenever the service stops or crashes
  (`ExecStopPost=uwctld --release-fans`).
- **No re-login needed:** the logged-in local user gets access to the service
  automatically through a POSIX ACL on the socket, based on logind sessions.
- GUI fan page explains which kind of curve is running and adds a
  "Run both fans together" switch.
- `uwctl fan custom --separate`; `uwctl status` shows hardware or software curve.

## 0.1.1

- **WMI support** for firmware without the `INOU0000` device: EC access
  through the vendor's `GetSetULong` WMI method, via acpi_call.
- `uwctl doctor` reports the WMI route and lists USB devices (no `lsusb` needed).
- `install.sh` restarts the service when upgrading.

## 0.1.0

- `uwctld` service, `uwctl` CLI and GTK 4 / libadwaita app.
- Keyboard backends: EC single-zone, ITE 829x per-key (hidraw) and kernel LED
  class, with hardware and software effects.
- Fan modes Auto / Max / custom hardware curve (EC fan tables), failsafe.
- Office / Gaming / Turbo profiles, lightbar, battery charge limit, Fn/Win lock.
- Debian package, Arch PKGBUILD, `install.sh`.
