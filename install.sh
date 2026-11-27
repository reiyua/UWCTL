#!/usr/bin/env bash
# One-step installer for Debian/Ubuntu/Mint and Arch/Manjaro/EndeavourOS.
#   ./install.sh            install dependencies + uwctl, start the service
#   ./install.sh --no-deps  skip the package manager step
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo --preserve-env=PATH "$0" "$@"
fi
cd "$(dirname "$(readlink -f "$0")")"

NODEPS=0
[[ ${1:-} == --no-deps ]] && NODEPS=1
USER_NAME=${SUDO_USER:-}
KERNEL=$(uname -r)

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==> warning:\033[0m %s\n' "$*" >&2; }

# shellcheck disable=SC1091
. /etc/os-release
FAMILY="$ID ${ID_LIKE:-}"

install_deps_debian() {
    say "Installing dependencies with apt"
    apt-get update -qq
    apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 make dkms
    if ! apt-get install -y "linux-headers-${KERNEL}"; then
        warn "no linux-headers-${KERNEL} package; trying the generic headers meta-package"
        apt-get install -y linux-headers-amd64 2>/dev/null || apt-get install -y linux-headers-generic || \
            warn "install the kernel headers for ${KERNEL} yourself, then: sudo apt install acpi-call-dkms"
    fi
    apt-get install -y acpi-call-dkms || warn "acpi-call-dkms failed to build - EC features will be unavailable"
}

install_deps_arch() {
    say "Installing dependencies with pacman"
    local pkgs=(python python-gobject gtk4 libadwaita make)
    local kpkg=""
    for k in linux-zen linux-hardened linux-lts linux-rt linux; do
        if pacman -Qq "$k" &>/dev/null && [[ -d /usr/lib/modules/${KERNEL} ]] && \
           pacman -Qqo "/usr/lib/modules/${KERNEL}" 2>/dev/null | grep -qx "$k"; then
            kpkg=$k; break
        fi
    done
    case "$kpkg" in
        linux)     pkgs+=(acpi_call) ;;
        linux-lts) pkgs+=(acpi_call-lts) ;;
        "")        warn "couldn't tell which kernel package owns ${KERNEL}; using acpi_call-dkms"
                   pkgs+=(dkms acpi_call-dkms) ;;
        *)         pkgs+=(dkms acpi_call-dkms "${kpkg}-headers") ;;
    esac
    pacman -S --needed --noconfirm "${pkgs[@]}"
}

if [[ $NODEPS -eq 0 ]]; then
    case "$FAMILY" in
        *arch*)            install_deps_arch ;;
        *debian*|*ubuntu*) install_deps_debian ;;
        *) warn "unsupported distribution '$ID' - install python3, PyGObject, GTK 4, libadwaita and acpi_call yourself" ;;
    esac
fi

say "Installing uwctl"
command -v make >/dev/null || { warn "'make' is missing"; exit 1; }
make --no-print-directory install PREFIX=/usr >/dev/null

if command -v systemd-sysusers >/dev/null; then
    systemd-sysusers uwctl.conf
else
    getent group uwctl >/dev/null || groupadd --system uwctl
fi
if [[ -n "$USER_NAME" && "$USER_NAME" != root ]]; then
    usermod -aG uwctl "$USER_NAME"
    say "Added $USER_NAME to the 'uwctl' group (only needed for use over SSH)"
fi

modprobe acpi_call 2>/dev/null || warn "acpi_call module not loaded - fan/EC features unavailable until it is"

if systemctl is-active --quiet tccd.service 2>/dev/null; then
    warn "TUXEDO Control Center (tccd) is running. uwctld will stop it - don't run both."
fi

systemctl daemon-reload
systemctl enable uwctld.service
systemctl restart uwctld.service
sleep 1
say "Done. Hardware report:"
echo
/usr/bin/uwctl doctor || true
echo
say "Open 'Uniwill Control Center' from your app menu, or use 'uwctl --help'."
