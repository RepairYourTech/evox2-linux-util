#!/usr/bin/env bash
#
# Installer for evo-x2-linux-osd -- a Linux on-screen display for the
# power-profile button on IP3-Tech Strix Halo mini-PCs (GMKtec EVO-X1/X2,
# Corsair AI Workstation 300, some Beelink GTR/SER AI, ...).
#
#   sudo ./install.sh
#
# See --help for the available flags.

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

# The GUI's data files go in the system-wide locations, not into PREFIX, so that
# every desktop finds them without PREFIX being on XDG_DATA_DIRS.
ICON_ROOT="/usr/share/icons/hicolor"
APPLICATIONS_DIR="/usr/share/applications"
AUTOSTART_DIR="/etc/xdg/autostart"
DESKTOP_ID="org.evox2.Control"

SESSION_USER=""
READ_ONLY=0
WITH_SUDOERS=1
START_SERVICE=1
GUI_WANTED=1
AUTOSTART=1
DRY_RUN=0
VERBOSE=0

PYTHON=""
VERSION="$(cat "${REPO_DIR}/VERSION" 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------------------

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
        [[ $VERBOSE -eq 1 ]] && printf '    %s+ %s%s\n' "$C_DIM" "$*" "$C_RESET"
        "$@"
    fi
}

write_file() {
    local path="$1" content="$2" mode="${3:-0644}"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    %s[dry-run]%s write %s (mode %s)\n' "$C_DIM" "$C_RESET" "$path" "$mode"
        return 0
    fi
    install -m "$mode" -o root -g root /dev/null "$path"
    # %b, not %s: the callers pass multi-line content with \n escapes, and
    # without it the whole file collapses into a single commented-out line.
    printf '%b\n' "$content" > "$path"
    chmod "$mode" "$path"
}

usage() {
    cat <<EOF
${C_BOLD}evo-x2-linux-osd installer ${VERSION}${C_RESET}

Usage: sudo ./install.sh [options]

Options:
  --user NAME        Send notifications to NAME (default: auto-detect the
                     logged-in graphical session)
  --prefix DIR       Install the program to DIR (default: ${PREFIX})
  --config PATH      Configuration file (default: ${CONFIG_PATH})
  --read-only        Install only the on-screen display.  Never enables EC
                     writes and does not install the mode switcher.  Use this
                     if you want the button's feedback and nothing else.
  --no-gui           Do not install the graphical front end.  It is skipped
                     automatically when GTK 4 and libadwaita are missing.
  --no-autostart     Install the GUI but do not put its tray icon in the login
                     autostart directory
  --no-sudoers       Do not let the mode switcher run without a password
  --no-start         Install everything but do not start the service
  --dry-run          Print what would happen, change nothing
  -v, --verbose      Show every command
  -h, --help         This text

Uninstall with ./uninstall.sh
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)       SESSION_USER="${2:?--user needs a value}"; shift 2 ;;
        --prefix)     PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
        --config)     CONFIG_PATH="${2:?--config needs a value}"; shift 2 ;;
        --read-only)  READ_ONLY=1; shift ;;
        --no-gui)     GUI_WANTED=0; shift ;;
        --no-autostart) AUTOSTART=0; shift ;;
        --no-sudoers) WITH_SUDOERS=0; shift ;;
        --no-start)   START_SERVICE=0; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            usage >&2; die "unknown option: $1" ;;
    esac
done

# ---------------------------------------------------------------------------

if [[ $DRY_RUN -eq 0 && "$(id -u)" -ne 0 ]]; then
    die "this installer needs root; run it with sudo"
fi

if [[ ! -f "${REPO_DIR}/lib/evo_x2_hw.py" ]]; then
    die "run this script from inside the repository (lib/evo_x2_hw.py not found)"
fi

# The install path is used in recursive deletes below; never trust it blindly.
case "$PREFIX" in
    ""|/|"/*") die "refusing to install into '${PREFIX}' -- pass a real --prefix" ;;
esac
[[ "${PREFIX}" == *".."* ]] && die "--prefix must not contain '..'"

command -v systemctl >/dev/null 2>&1 || die "systemd is required"

PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || die "python3 is required but was not found in PATH"
info "python3: ${PYTHON}"

if [[ -z "$SESSION_USER" ]]; then
    SESSION_USER="$("$PYTHON" - "$REPO_DIR" <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "lib"))
try:
    import evo_x2_hw
except Exception:
    raise SystemExit(0)
user = evo_x2_hw.find_session_user()
print(user.pw_name if user else "")
PY
)"
fi
if [[ -z "$SESSION_USER" && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    SESSION_USER="$SUDO_USER"
fi

printf '\n%s%s%s (%s)\n' "$C_BOLD" "evo-x2-linux-osd ${VERSION}" "$C_RESET" "install"
info "prefix:       ${PREFIX}"
info "config:       ${CONFIG_PATH}"
info "session user: ${SESSION_USER:-<auto-detect at runtime>}"
info "mode:         $([[ $READ_ONLY -eq 1 ]] && echo 'on-screen display only (read-only)' || echo 'OSD + mode switcher')"

# -- 1. sanity check the hardware -------------------------------------------

step "Checking that this machine has the interface we expect"
EVENT_GUID_DIR="/sys/bus/wmi/devices/8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C"
if [[ -d "$EVENT_GUID_DIR" ]]; then
    ok "found the IP3-Tech WMI event device"
else
    warn "the WMI event device 8FAFC061-... is not present on this machine."
    warn "This tool targets the IP3-Tech Strix Halo mainboard (GMKtec EVO-X1/X2,"
    warn "Corsair AI Workstation 300, some Beelink GTR/SER AI).  Installing"
    warn "anyway, but the service will be skipped by its ConditionPathExists."
fi

# -- 2. install the program tree --------------------------------------------

step "Installing the program to ${PREFIX}"
run install -d -o root -g root -m 0755 "$PREFIX"
run rm -rf "${PREFIX}/lib" "${PREFIX}/bin" "${PREFIX}/daemon" \
           "${PREFIX}/docs" "${PREFIX}/tools" "${PREFIX}/gui" "${PREFIX}/data"
for sub in lib bin daemon gui data; do
    run cp -a "${REPO_DIR}/${sub}" "${PREFIX}/${sub}"
done
for sub in docs tools; do
    if [[ -d "${REPO_DIR}/${sub}" ]]; then
        run cp -a "${REPO_DIR}/${sub}" "${PREFIX}/${sub}"
    fi
done
for file in README.md LICENSE VERSION; do
    if [[ -f "${REPO_DIR}/${file}" ]]; then
        run install -m 0644 "${REPO_DIR}/${file}" "${PREFIX}/${file}"
    fi
done
# The uninstaller is referenced in the closing message, so it has to travel
# with the installed tree.
if [[ -f "${REPO_DIR}/uninstall.sh" ]]; then
    run install -m 0755 "${REPO_DIR}/uninstall.sh" "${PREFIX}/uninstall.sh"
fi

# The repository is owned by whoever cloned it.  Nothing inside the installed
# tree may stay writable by a normal user, since root executes it.
if [[ $DRY_RUN -eq 0 ]]; then
    chown -R root:root "$PREFIX"
    chmod -R go-w "$PREFIX"
    chmod 0755 "${PREFIX}/daemon/evo-x2-thermal-osd" "${PREFIX}/bin/evo-x2-power"
    # if/then, not `[[ ]] && cmd`: a bare failing test is the last command of
    # the AND list and would trip `set -e` when the file happens to be absent.
    if [[ -f "${PREFIX}/gui/evo-x2-control" ]]; then
        chmod 0755 "${PREFIX}/gui/evo-x2-control"
    fi
    for tool in ec-probe mode-report; do
        if [[ -f "${PREFIX}/tools/${tool}" ]]; then
            chmod 0755 "${PREFIX}/tools/${tool}"
        fi
    done
    find "$PREFIX" -type d -exec chmod 0755 {} +
    # Byte-caches from the source tree are useless to the installed copy.
    find "$PREFIX" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
ok "installed $("$PYTHON" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo ""), tree is root-owned and not group/other writable"

# -- 3. make the embedded controller readable -------------------------------

step "Loading the ec_sys module at boot"
write_file "$MODULES_LOAD_PATH" "ec_sys" 0644
ok "ec_sys will be loaded on every boot"

if [[ $READ_ONLY -eq 1 ]]; then
    dim "read-only install: not enabling embedded-controller writes"
    if [[ -f "$MODPROBE_PATH" ]]; then
        run rm -f "$MODPROBE_PATH"
        dim "removed a previous write_support configuration"
    fi
else
    write_file "$MODPROBE_PATH" "# Allow evo-x2-power to request a thermal-mode change.\n# The only byte this tool ever writes is EC[0x32] (FCMI), which is exactly\n# what the BIOS's own ACPI method writes when you press the front button.\noptions ec_sys write_support=1" 0644
    ok "embedded-controller writes enabled (used only by evo-x2-power)"
fi

# If it is already loaded with the wrong flags, reload it now, before the
# daemon starts depending on it.  /sys/module is probed directly rather than
# shelling out to lsmod, which is not on every PATH under sudo.
want_write="N"
if [[ $READ_ONLY -eq 0 ]]; then want_write="Y"; fi

if [[ -d /sys/module/ec_sys ]]; then
    current_write="$(cat /sys/module/ec_sys/parameters/write_support 2>/dev/null || echo N)"
    if [[ "$current_write" != "$want_write" ]]; then
        run systemctl stop "$UNIT_NAME" 2>/dev/null || true
        if [[ $DRY_RUN -eq 0 ]]; then
            if modprobe -r ec_sys 2>/dev/null && modprobe ec_sys 2>/dev/null; then
                ok "reloaded ec_sys (write_support now ${want_write})"
            else
                warn "could not reload ec_sys; reboot to apply the new options"
            fi
        fi
    else
        dim "ec_sys already loaded with the right options"
    fi
elif [[ $DRY_RUN -eq 0 ]]; then
    if modprobe ec_sys 2>/dev/null; then
        ok "ec_sys loaded"
    else
        warn "could not load ec_sys now; it will load at next boot"
    fi
fi

# Confirm the flag really took effect.  A stray comment character or a
# conflicting entry elsewhere in /etc/modprobe.d is easy to miss and would
# disable mode switching silently.
if [[ $DRY_RUN -eq 0 && -r /sys/module/ec_sys/parameters/write_support ]]; then
    actual="$(cat /sys/module/ec_sys/parameters/write_support)"
    if [[ "$actual" != "$want_write" ]]; then
        warn "write_support is '${actual}' but should be '${want_write}'"
        warn "check for a conflicting ec_sys entry: grep -r ec_sys /etc/modprobe.d/"
        warn "a reboot will use whichever value modprobe reads last"
    fi
fi

# -- 4. configuration -------------------------------------------------------

step "Configuration"
if [[ -f "$CONFIG_PATH" ]]; then
    dim "keeping the existing ${CONFIG_PATH}"
else
    if [[ $DRY_RUN -eq 0 ]]; then
        install -m 0644 -o root -g root "${REPO_DIR}/config/evo-x2-linux-osd.conf" "$CONFIG_PATH"
    else
        printf '    %s[dry-run]%s install %s\n' "$C_DIM" "$C_RESET" "$CONFIG_PATH"
    fi
    ok "wrote ${CONFIG_PATH}"
fi

# -- 5. systemd unit --------------------------------------------------------

step "Installing the systemd service"
if [[ $DRY_RUN -eq 0 ]]; then
    sed -e "s|@PYTHON@|${PYTHON}|g" \
        -e "s|@PREFIX@|${PREFIX}|g" \
        -e "s|@CONFIG@|${CONFIG_PATH}|g" \
        "${REPO_DIR}/systemd/evo-x2-thermal-osd.service.in" > "$UNIT_PATH"
    chown root:root "$UNIT_PATH"
    chmod 0644 "$UNIT_PATH"
else
    printf '    %s[dry-run]%s render %s\n' "$C_DIM" "$C_RESET" "$UNIT_PATH"
fi
run systemctl daemon-reload

if [[ $START_SERVICE -eq 1 ]]; then
    run systemctl enable "$UNIT_NAME" || warn "could not enable the service"
    if [[ $DRY_RUN -eq 0 ]]; then
        # restart, not 'enable --now': on an upgrade the old process is still
        # running the previous code, and 'enable --now' leaves it in place.
        if systemctl restart "$UNIT_NAME"; then
            sleep 1
            if systemctl is-active --quiet "$UNIT_NAME"; then
                ok "service is running"
            else
                warn "the service did not stay up; check: journalctl -u ${UNIT_NAME} -n 40"
            fi
        else
            warn "could not start the service; check: journalctl -u ${UNIT_NAME} -n 40"
        fi
    fi
else
    dim "not starting the service (--no-start)"
fi

# -- 6. command line tool ---------------------------------------------------

if [[ $READ_ONLY -eq 1 ]]; then
    step "Skipping the mode switcher (--read-only)"
else
    step "Installing the mode switcher"
    run ln -sf "${PREFIX}/bin/evo-x2-power" "$BIN_LINK"
    ok "evo-x2-power is available in /usr/local/bin"

    if [[ $WITH_SUDOERS -eq 1 ]]; then
        if ! command -v visudo >/dev/null 2>&1; then
            warn "visudo not found; skipping the passwordless sudo rule"
        elif [[ -z "$SESSION_USER" ]]; then
            warn "no session user detected; skipping the passwordless sudo rule"
            warn "re-run with --user NAME to add it"
        else
            tmp="$(mktemp)"
            cat > "$tmp" <<EOF
# Installed by evo-x2-linux-osd ${VERSION}.
# Lets ordinary users switch the thermal mode of their own machine without a
# password.  The script only ever writes one byte, EC[0x32], and takes no
# arbitrary paths or commands, so the elevated surface is limited to picking
# a thermal profile.
${SESSION_USER} ALL=(root) NOPASSWD: ${PREFIX}/bin/evo-x2-power, ${BIN_LINK}
EOF
            if visudo -cf "$tmp" >/dev/null 2>&1; then
                # run, not a bare install: --dry-run is allowed without root, so
                # this is the one command in the script that could otherwise
                # rewrite /etc/sudoers.d during a run that promised to change
                # nothing.
                run install -m 0440 -o root -g root "$tmp" "$SUDOERS_PATH"
                rm -f "$tmp"
                ok "'sudo evo-x2-power <mode>' will not prompt for a password"
            else
                rm -f "$tmp"
                warn "generated sudoers file failed validation; skipping"
            fi
        fi
    else
        dim "not installing a sudo rule (--no-sudoers); use sudo evo-x2-power"
    fi
fi

# -- 7. graphical front end ------------------------------------------------

# The GUI has real dependencies the daemon does not, so probe for them the way
# the GUI itself will, and degrade to a command-line-only install rather than
# failing the whole run over a missing package.
check_gui_dependencies() {
    [[ $DRY_RUN -eq 1 ]] && return 0
    "$PYTHON" - <<'PY' >/dev/null 2>&1
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk      # noqa: F401
import dbus                             # noqa: F401
PY
}

# Package names differ per distribution, so print the one that works here.
gui_dependency_hint() {
    if command -v apt-get >/dev/null 2>&1; then
        printf 'sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-dbus'
    elif command -v dnf >/dev/null 2>&1; then
        printf 'sudo dnf install python3-gobject gtk4 libadwaita python3-dbus'
    elif command -v pacman >/dev/null 2>&1; then
        printf 'sudo pacman -S python-gobject gtk4 libadwaita python-dbus'
    elif command -v zypper >/dev/null 2>&1; then
        printf 'sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 python3-dbus-python'
    else
        printf 'install PyGObject with GTK 4 and libadwaita, plus dbus-python'
    fi
}

# The icons are installed unconditionally.  They are not a GUI asset: the
# daemon names them in every notification it sends, so --no-gui must not take
# them away or the toast loses its icon.
step "Installing icons"
icon_count=0
for icon in "${REPO_DIR}"/data/icons/hicolor/*/apps/*.svg; do
    if [[ ! -f "$icon" ]]; then
        continue
    fi
    # data/icons/hicolor/<category>/apps/<name>.svg -> the same layout under
    # /usr/share/icons/hicolor, so a new size or category needs no change here.
    category="$(basename "$(dirname "$(dirname "$icon")")")"
    run install -d -m 0755 "${ICON_ROOT}/${category}/apps"
    run install -m 0644 -o root -g root "$icon" \
        "${ICON_ROOT}/${category}/apps/$(basename "$icon")"
    icon_count=$((icon_count + 1))
done
if [[ $icon_count -eq 0 ]]; then
    die "no icons found in ${REPO_DIR}/data/icons -- the checkout is incomplete"
fi
ok "installed ${icon_count} icons into ${ICON_ROOT}"

# Icons a previous version installed under this prefix and this one no longer
# ships.  Without this an upgrade leaves the old names in the theme, where a
# stale config or an old copy of the code can still resolve to them -- and the
# older set was the monochrome one Qt drew solid black.  Only ever our own
# prefix, never the directory itself.
stale_count=0
for stale in "${ICON_ROOT}"/*/apps/evo-x2-*.svg; do
    if [[ ! -f "$stale" ]]; then
        continue
    fi
    if [[ ! -f "${REPO_DIR}/data/icons/hicolor/${stale#"${ICON_ROOT}/"}" ]]; then
        run rm -f "$stale"
        stale_count=$((stale_count + 1))
    fi
done
if [[ $stale_count -gt 0 ]]; then
    ok "removed ${stale_count} icon(s) this version no longer ships"
fi

if [[ $DRY_RUN -eq 0 ]]; then
    # KDE and GTK both fall back to scanning the icon tree, so a failure here is
    # cosmetic -- the icons are already in place.  Never fatal.
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1 \
            || warn "could not refresh the icon cache in ${ICON_ROOT}"
    fi
else
    printf '    %s[dry-run]%s refresh the icon cache\n' "$C_DIM" "$C_RESET"
fi

if [[ $GUI_WANTED -eq 1 ]] && ! check_gui_dependencies; then
    step "Graphical front end"
    warn "the GUI needs GTK 4, libadwaita and dbus-python, which are not present"
    warn "install them with:"
    warn "  $(gui_dependency_hint)"
    warn "skipping the GUI; re-run ./install.sh afterwards to add it"
    GUI_WANTED=0
fi

if [[ $GUI_WANTED -eq 1 ]]; then
    step "Installing the graphical front end"
    run ln -sf "${PREFIX}/gui/evo-x2-control" "$GUI_LINK"
    ok "evo-x2-control is available in /usr/local/bin"

    run install -m 0644 -o root -g root \
        "${REPO_DIR}/data/${DESKTOP_ID}.desktop" \
        "${APPLICATIONS_DIR}/${DESKTOP_ID}.desktop"
    ok "added 'Thermal Mode' to the application menu"

    if [[ $AUTOSTART -eq 1 ]]; then
        run install -m 0644 -o root -g root \
            "${REPO_DIR}/data/${DESKTOP_ID}.autostart.desktop" \
            "${AUTOSTART_DIR}/${DESKTOP_ID}.desktop"
        ok "the tray icon will start automatically at login"
    else
        dim "not adding an autostart entry (--no-autostart)"
    fi

    if [[ $DRY_RUN -eq 0 ]]; then
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
        fi
    else
        printf '    %s[dry-run]%s refresh the desktop database\n' "$C_DIM" "$C_RESET"
    fi
fi

# -- 8. report --------------------------------------------------------------

step "Verifying"
if [[ $DRY_RUN -eq 1 ]]; then
    dim "skipped in dry-run mode"
    printf '\n%sDry run complete -- nothing was changed.%s\n\n' "$C_BOLD" "$C_RESET"
    exit 0
fi

"${PREFIX}/bin/evo-x2-power" --check || true

step "Done"
cat <<EOF
    Try it:

      ${C_BOLD}evo-x2-power${C_RESET}                 show the current thermal mode
      ${C_BOLD}sudo evo-x2-power balanced${C_RESET}   switch modes
      ${C_BOLD}evo-x2-power --check${C_RESET}         run these diagnostics again

    Now press the physical front-panel button.  A notification should appear
    with the new mode.  If nothing happens:

      journalctl -u ${UNIT_NAME} -f

    To see what the hardware itself is doing:

      ${C_BOLD}sudo ${PREFIX}/tools/ec-probe --diff${C_RESET}
EOF

if [[ $GUI_WANTED -eq 1 ]]; then
    cat <<EOF

    Or open ${C_BOLD}Thermal Mode${C_RESET} from the application menu (Settings >
    Hardware), and look for its icon in the system tray.
EOF
fi

cat <<EOF

    Reboot after installing so ec_sys is loaded with the right options from
    boot, then everything is automatic.

    Uninstall with: sudo ${PREFIX}/uninstall.sh
EOF
