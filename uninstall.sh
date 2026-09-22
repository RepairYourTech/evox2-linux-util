#!/usr/bin/env bash
#
# Uninstaller for evo-x2-linux-osd.
#
#   sudo ./uninstall.sh              # remove the program and the service
#   sudo ./uninstall.sh --purge      # also remove the configuration file
#
# Reinstalling later is cheap, so this is safe to run and re-run.

set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

PREFIX="/opt/evo-x2-linux-osd"
CONFIG_PATH="/etc/evo-x2-linux-osd.conf"
UNIT_PATH="/etc/systemd/system/evo-x2-thermal-osd.service"
UNIT_NAME="evo-x2-thermal-osd.service"
MODULES_LOAD_PATH="/etc/modules-load.d/evo-x2-linux-osd.conf"
MODPROBE_PATH="/etc/modprobe.d/evo-x2-linux-osd.conf"
SUDOERS_PATH="/etc/sudoers.d/evo-x2-power"
BIN_LINK="/usr/local/bin/evo-x2-power"
GUI_LINK="/usr/local/bin/evo-x2-control"
ICON_ROOT="/usr/share/icons/hicolor"
APPLICATIONS_DIR="/usr/share/applications"
AUTOSTART_DIR="/etc/xdg/autostart"
DESKTOP_ID="org.evox2.Control"

PURGE=0
UNLOAD_MODULE=0
DRY_RUN=0

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""; C_OK=""; C_WARN=""; C_ERR=""
fi

step() { printf '\n%s==>%s %s%s%s\n' "$C_BOLD" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
dim()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
ok()   { printf '    %s%s%s\n' "$C_OK" "$*" "$C_RESET"; }
warn() { printf '    %swarning:%s %s\n' "$C_WARN" "$C_RESET" "$*" >&2; }
die()  { printf '\n%serror:%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; exit 1; }

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    %s[dry-run]%s %s\n' "$C_DIM" "$C_RESET" "$*"
    else
        "$@"
    fi
}

usage() {
    cat <<EOF
${C_BOLD}evo-x2-linux-osd uninstaller${C_RESET}

Usage: sudo ./uninstall.sh [options]

Options:
  --purge          Also delete ${CONFIG_PATH}
  --unload-module  Also unload ec_sys (only do this if nothing else needs it)
  --prefix DIR     Program location (default: ${PREFIX})
  --dry-run        Print what would happen, change nothing
  -h, --help       This text
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge)         PURGE=1; shift ;;
        --unload-module) UNLOAD_MODULE=1; shift ;;
        --prefix)        PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
        --dry-run)       DRY_RUN=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               usage >&2; die "unknown option: $1" ;;
    esac
done

if [[ $DRY_RUN -eq 0 && "$(id -u)" -ne 0 ]]; then
    die "this uninstaller needs root; run it with sudo"
fi

step "Stopping the service"
if systemctl list-unit-files "$UNIT_NAME" >/dev/null 2>&1; then
    run systemctl disable --now "$UNIT_NAME" || warn "the service was not running"
else
    dim "the service is not installed"
fi

step "Stopping the tray icon"
if [[ $DRY_RUN -eq 0 ]]; then
    # The bracketed letter keeps the pattern from matching this script's own
    # command line if it happens to be invoked through a shell that contains
    # the path.
    if pkill -f "${PREFIX}/gui/evo-x2-contro[l]" 2>/dev/null; then
        info "stopped a running tray icon"
    else
        dim "no tray icon was running"
    fi
else
    printf '    %s[dry-run]%s stop any running tray icon\n' "$C_DIM" "$C_RESET"
fi

step "Removing files"
run rm -f "$UNIT_PATH"
run rm -f "$MODULES_LOAD_PATH"
run rm -f "$MODPROBE_PATH"
run rm -f "$SUDOERS_PATH"
run rm -f "$BIN_LINK"
run rm -f "$GUI_LINK"
run rm -f "${APPLICATIONS_DIR}/${DESKTOP_ID}.desktop"
run rm -f "${AUTOSTART_DIR}/${DESKTOP_ID}.desktop"
# Only this package's own icons.  Never the directory: it is shared with every
# other application on the system.
for icon in "${ICON_ROOT}"/*/apps/evo-x2-*.svg; do
    if [[ -f "$icon" ]]; then
        run rm -f "$icon"
    fi
done
run rm -rf "$PREFIX"
run systemctl daemon-reload
run systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true

if [[ $DRY_RUN -eq 0 ]]; then
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1 || true
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
    fi
    ok "refreshed the icon cache and desktop database"
fi

if [[ $PURGE -eq 1 ]]; then
    step "Removing configuration"
    run rm -f "$CONFIG_PATH"
else
    dim "kept ${CONFIG_PATH} (use --purge to remove it)"
fi

if [[ $UNLOAD_MODULE -eq 1 ]]; then
    step "Unloading ec_sys"
    if [[ $DRY_RUN -eq 0 ]]; then
        if modprobe -r ec_sys 2>/dev/null; then
            info "ec_sys unloaded"
        else
            warn "could not unload ec_sys (it may still be in use)"
        fi
    fi
else
    dim "ec_sys is still loaded and will load again at boot if you reinstall"
fi

step "Done"
if [[ $DRY_RUN -eq 0 ]]; then
    info "The thermal mode is now left wherever it was last set.  The"
    info "front-panel button still works exactly as before -- you just will"
    info "not get an on-screen confirmation for it any more."
fi

# The repository itself is intentionally left alone.
dim "repository kept at ${REPO_DIR}"
