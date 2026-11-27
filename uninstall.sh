#!/usr/bin/env bash
# Remove a uwctl installed with install.sh.  Settings in /etc/uwctl are kept
# unless you pass --purge.  Fans are handed back to the firmware first.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi
cd "$(dirname "$(readlink -f "$0")")"

/usr/bin/uwctl fan auto >/dev/null 2>&1 || true
systemctl disable --now uwctld.service 2>/dev/null || true
make --no-print-directory uninstall PREFIX=/usr >/dev/null
systemctl daemon-reload
if [[ ${1:-} == --purge ]]; then
    rm -rf /etc/uwctl
fi
echo "uwctl removed. (The 'uwctl' group and acpi_call were left in place.)"
