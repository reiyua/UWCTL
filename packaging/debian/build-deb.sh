#!/bin/bash
# Build uwctl_<version>-1_all.deb with plain dpkg-deb (no debhelper needed).
set -euo pipefail
cd "$(dirname "$0")/../.."

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/uwctl/__init__.py)
REV=1
PKG=build/deb/uwctl_${VERSION}-${REV}_all
rm -rf build/deb
mkdir -p "$PKG/DEBIAN" dist

make --no-print-directory install DESTDIR="$PWD/$PKG" PREFIX=/usr >/dev/null
find "$PKG" -name __pycache__ -prune -exec rm -rf {} +

SIZE=$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)

cat > "$PKG/DEBIAN/control" <<EOF
Package: uwctl
Version: ${VERSION}-${REV}
Section: utils
Priority: optional
Architecture: all
Installed-Size: ${SIZE}
Depends: python3 (>= 3.9), systemd
Recommends: acpi-call-dkms, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
Suggests: linux-headers-amd64 | linux-headers-generic
Maintainer: uwctl maintainers <uwctl@localhost>
Description: control centre for Uniwill-based gaming laptops
 Linux replacement for the Windows "GamingCenterU / Control Center" app
 shipped with Uniwill and Tongfang laptops sold under many brands.
 .
 Controls keyboard backlight colour, brightness and effects (single-zone EC
 keyboards, ITE 829x per-key keyboards, and any keyboard exposed by a kernel
 LED driver), fan modes and custom fan curves written to the embedded
 controller, Office/Gaming/Turbo performance profiles, lightbar, battery
 charge limit and Fn/Win-key lock.
 .
 Provides the uwctld system service, the uwctl command line tool and the
 uwctl-gui GTK 4 application.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    if command -v systemd-sysusers >/dev/null 2>&1; then
        systemd-sysusers uwctl.conf >/dev/null 2>&1 || true
    fi
    getent group uwctl >/dev/null || addgroup --system uwctl >/dev/null || true
    added=""
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id "$SUDO_USER" >/dev/null 2>&1; then
        if usermod -aG uwctl "$SUDO_USER" 2>/dev/null; then added="$SUDO_USER"; fi
    fi
    modprobe acpi_call >/dev/null 2>&1 || true
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl enable uwctld.service >/dev/null 2>&1 || true
        systemctl restart uwctld.service >/dev/null 2>&1 || true
    fi
    echo "uwctl: service enabled. Check your laptop with:  uwctl doctor"
    if [ -n "$added" ]; then
        echo "uwctl: added $added to the 'uwctl' group (only needed for use over SSH)."
    fi
    echo "uwctl: whoever is logged in at the laptop can use uwctl and the app right away."
fi
exit 0
EOF

cat > "$PKG/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] && [ -d /run/systemd/system ]; then
    systemctl disable --now uwctld.service >/dev/null 2>&1 || true
fi
exit 0
EOF

cat > "$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "purge" ]; then
    rm -rf /etc/uwctl
fi
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
exit 0
EOF

chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "dist/uwctl_${VERSION}-${REV}_all.deb"
