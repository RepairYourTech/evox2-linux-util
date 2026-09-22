"""
evo_x2_hw -- hardware access layer for the IP3-Tech "Strix Halo" mini-PC
front-panel power-profile button.

Machines built on the IP3-Tech Strix Halo reference board (GMKtec EVO-X1/X2,
Corsair AI Workstation 300, some Beelink GTR/SER AI units and other rebadges)
expose this interface:

  * a front-panel button that cycles Quiet -> Balanced -> Performance
  * a WMI *event*  device, GUID 8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C
    (ACPI notify id 0xBC), which fires on every press
  * a WMI *method* device, GUID 99D89064-8D50-42BB-BEA9-155B2E5D0FCD
    (object_id "AA"), implemented in the DSDT as \\_SB.WMIB.WMAA
  * two Embedded Controller bytes used by that method:

        EC[0x31]  DSDT name FCMO  -- current mode, readable, values 0..3
        EC[0x32]  DSDT name FCMI  -- mode request, write 0x80 | mode

Nothing in mainline Linux binds to either vendor GUID, so the button press is
silently dropped by the kernel and the Windows-only OSD application
(ImagesShow.exe) is the only thing that ever showed the result.

This module reads the state directly out of the EC through the mainline
`ec_sys` module, and can subscribe to the kernel's ACPI netlink feed to learn
about a press immediately instead of waiting for a poll.

No out-of-tree kernel modules are required.
"""

from __future__ import annotations

import glob
import os
import pwd
import socket
import struct
import time
from dataclasses import dataclass

__all__ = [
    "Mode", "MODES", "ModeError",
    "EcAccessError", "EC_DEBUGFS_PATH", "EC_REG_MODE_READ", "EC_REG_MODE_WRITE",
    "WMI_EVENT_GUID", "WMI_METHOD_GUID", "WMI_BUTTON_NOTIFY_ID",
    "STATE_DIR", "STATE_FILE",
    "mode_from_index", "mode_from_key", "mode_from_ppd",
    "ec_io_available", "ec_sys_loaded", "ec_write_support",
    "read_mode", "write_mode", "read_ec_byte",
    "wmi_device_present", "wmi_notify_id", "dsdt_contains",
    "open_acpi_event_listener", "read_acpi_events",
    "find_session_user", "read_state_file", "write_state_file",
    "parse_attributes", "iter_netlink_messages", "parse_acpi_event",
    "build_getfamily_request", "parse_getfamily_response",
]


# ---------------------------------------------------------------------------
# Hardware constants -- derived from the DSDT.  See docs/HOW-IT-WORKS.md.
# ---------------------------------------------------------------------------

EC_DEBUGFS_PATH = "/sys/kernel/debug/ec/ec0/io"

EC_REG_MODE_READ = 0x31    # FCMO -- current mode
EC_REG_MODE_WRITE = 0x32   # FCMI -- mode request
EC_MODE_WRITE_BASE = 0x80  # FCMI = 0x80 | mode

WMI_EVENT_GUID = "8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C"
WMI_METHOD_GUID = "99D89064-8D50-42BB-BEA9-155B2E5D0FCD"
WMI_BUTTON_NOTIFY_ID = 0xBC

#: Where the daemon publishes the current mode so unprivileged tools
#: (status bars, scripts) can read it without root.
STATE_DIR = "/run/evo-x2-linux-osd"
STATE_FILE = "mode"


# ---------------------------------------------------------------------------
# Mode table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Mode:
    index: int
    key: str           # name accepted on the command line
    name: str          # human readable
    ppd: str           # power-profiles-daemon profile to mirror
    icon: str          # default icon name (shipped by this package, see below)
    description: str
    documented: bool   # reachable by the physical button?


#: Mode table.  The icon names are shipped by this package (data/icons) rather
#: than borrowed from the icon theme: the freedesktop power-profile-*-symbolic
#: names that look like the obvious choice exist in Adwaita and Yaru but not in
#: Breeze or in hicolor, so on a stock KDE, Fedora or Arch desktop they resolve
#: to nothing and every notification and tray icon comes out blank.
#:
#: The names have no -symbolic suffix on purpose.  A symbolic icon is one the
#: theme recolours, and Qt -- which renders both tray and notification icons on
#: KDE -- cannot even parse the `currentColor` such icons are written with, so
#: it drew them opaque black instead.  These are plain coloured icons, one bar
#: per mode level (see data/icons/hicolor/scalable/apps).
MODES = (
    Mode(0, "quiet", "Quiet", "power-saver",
         "evo-x2-quiet",
         "Reduced power limits, fans kept slow for near-silent operation.",
         True),
    Mode(1, "balanced", "Balanced", "balanced",
         "evo-x2-balanced",
         "Default balance between performance, fan noise and power draw.",
         True),
    Mode(2, "performance", "Performance", "performance",
         "evo-x2-performance",
         "Maximum sustained performance; the fans will be audible.",
         True),
    Mode(3, "sustained", "Sustained", "performance",
         "evo-x2-sustained",
         "Undocumented 4th mode: cores held near boost with package power "
         "capped. Lower latency, lower throughput.",
         False),
)

MODES_BY_INDEX = {m.index: m for m in MODES}
MODES_BY_KEY = {m.key: m for m in MODES}


class ModeError(ValueError):
    """Raised for an unknown mode name or index."""


def mode_from_index(index: int) -> Mode:
    try:
        return MODES_BY_INDEX[index]
    except KeyError:
        raise ModeError(f"no such thermal mode: {index!r} "
                        f"(known: {sorted(MODES_BY_INDEX)})") from None


def mode_from_key(key: str) -> Mode:
    """Accept a name ('balanced'), an alias ('perf'), or a number ('1')."""
    if key is None:
        raise ModeError("no mode given")
    text = str(key).strip().lower()
    if text in MODES_BY_KEY:
        return MODES_BY_KEY[text]
    try:
        return mode_from_index(int(text, 0))
    except (TypeError, ValueError):
        pass
    raise ModeError(
        f"unknown mode {key!r}; expected one of "
        f"{', '.join(m.key for m in MODES)} or 0-3"
    )


def mode_from_ppd(profile: str):
    """Best-effort inverse mapping of a power-profiles-daemon profile."""
    for mode in MODES:
        if mode.ppd == profile:
            return mode
    return None


# ---------------------------------------------------------------------------
# Embedded controller access
# ---------------------------------------------------------------------------

class EcAccessError(RuntimeError):
    """The EC could not be read or written."""


def ec_io_available(path: str = EC_DEBUGFS_PATH) -> bool:
    """True when the ec_sys driver is loaded and exposing the EC region."""
    return os.path.exists(path)


def ec_sys_loaded() -> bool:
    return os.path.isdir("/sys/module/ec_sys")


def ec_write_support():
    """True/False when ec_sys is loaded, None when it is not."""
    params = "/sys/module/ec_sys/parameters/write_support"
    try:
        with open(params, "r", encoding="ascii", errors="replace") as fh:
            value = fh.read().strip().lower()
    except OSError:
        return None
    return value in ("y", "1", "yes")


def read_ec_byte(offset: int, path: str = EC_DEBUGFS_PATH) -> int:
    """Read one byte of EC address space.

    Prefers a seek+read of a single byte; falls back to reading the whole
    256-byte region and slicing it, which some kernel/throttle combinations
    handle more reliably.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        raise EcAccessError(
            f"{path} does not exist -- the ec_sys module is not loaded "
            f"(try: modprobe ec_sys)"
        ) from None
    except PermissionError:
        raise EcAccessError(
            f"{path} is not readable by uid {os.geteuid()} -- run as root"
        ) from None
    try:
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            data = os.read(fd, 1)
            if len(data) == 1:
                return data[0]
        except OSError:
            pass

        os.lseek(fd, 0, os.SEEK_SET)
        blob = bytearray()
        while len(blob) < 256:
            chunk = os.read(fd, 256 - len(blob))
            if not chunk:
                break
            blob.extend(chunk)
        if len(blob) <= offset:
            raise EcAccessError(
                f"short read from {path}: got {len(blob)} bytes, "
                f"needed offset {offset:#x}"
            )
        return blob[offset]
    finally:
        os.close(fd)


def read_mode(path: str = EC_DEBUGFS_PATH):
    """Current thermal mode index, or None if it could not be read."""
    return read_ec_byte(EC_REG_MODE_READ, path)


def write_mode(mode, path: str = EC_DEBUGFS_PATH) -> None:
    """Ask the EC to switch thermal mode.

    This writes the single byte the BIOS's own ACPI method writes when the
    front-panel button is pressed (EC[0x32] = 0x80 | mode).  All downstream
    hardware behaviour -- fan curves, power limits, the Windows OSD code --
    follows from the EC, so no ACPI method call is needed.
    """
    mode = mode_from_key(mode) if not isinstance(mode, Mode) else mode
    value = EC_MODE_WRITE_BASE | mode.index

    try:
        fd = os.open(path, os.O_WRONLY)
    except FileNotFoundError:
        raise EcAccessError(
            f"{path} does not exist -- the ec_sys module is not loaded "
            f"(try: modprobe ec_sys)"
        ) from None
    except PermissionError as exc:
        raise EcAccessError(
            f"cannot open {path} for writing: {exc.strerror}. If ec_sys is "
            f"loaded without write_support=1 the node is read-only; see the "
            f"README section 'Enabling the mode switcher'."
        ) from None
    try:
        os.lseek(fd, EC_REG_MODE_WRITE, os.SEEK_SET)
        written = os.write(fd, bytes([value]))
    except OSError as exc:
        raise EcAccessError(
            f"writing EC[{EC_REG_MODE_WRITE:#04x}] failed: {exc.strerror}. "
            f"That usually means ec_sys was loaded without write_support=1."
        ) from None
    finally:
        os.close(fd)

    if written != 1:
        raise EcAccessError(f"short write to {path}: {written} byte(s)")


# ---------------------------------------------------------------------------
# Hardware / firmware detection (used by `evo-x2-power --check`)
# ---------------------------------------------------------------------------

def wmi_device_present(guid: str) -> bool:
    return os.path.exists(f"/sys/bus/wmi/devices/{guid}")


def wmi_notify_id(guid: str):
    try:
        with open(f"/sys/bus/wmi/devices/{guid}/notify_id", "r") as fh:
            return int(fh.read().strip(), 16)
    except (OSError, ValueError):
        return None


def dsdt_contains(marker: str, path: str = "/sys/firmware/acpi/tables/DSDT"):
    """True/False, or None when the DSDT cannot be read (needs root)."""
    try:
        with open(path, "rb") as fh:
            return marker.encode() in fh.read()
    except OSError:
        return None


def dsdt_has_power_switch() -> bool:
    """Heuristic: does this firmware expose the IP3 power-switch interface?"""
    return wmi_device_present(WMI_EVENT_GUID) and wmi_device_present(WMI_METHOD_GUID)


# ---------------------------------------------------------------------------
# ACPI netlink event feed
#
# This is the same interface acpi_listen(8)/acpid use, but talking to it
# directly means neither package is required.
# ---------------------------------------------------------------------------

_NETLINK_GENERIC = getattr(socket, "NETLINK_GENERIC", 16)
_SOL_NETLINK = getattr(socket, "SOL_NETLINK", 270)
_NETLINK_ADD_MEMBERSHIP = getattr(socket, "NETLINK_ADD_MEMBERSHIP", 1)

_GENL_ID_CTRL = 0x10
_CTRL_CMD_GETFAMILY = 3
_NLM_F_REQUEST = 0x01
_NLMSG_ERROR = 0x2
_NLMSG_DONE = 0x3

_CTRL_ATTR_FAMILY_ID = 1
_CTRL_ATTR_FAMILY_NAME = 2
_CTRL_ATTR_MCAST_GROUPS = 7
_CTRL_ATTR_MCAST_GRP_NAME = 1
_CTRL_ATTR_MCAST_GRP_ID = 2

_ACPI_GENL_ATTR_EVENT = 1
_ACPI_GENL_ATTR_EVENT_DEVICE_CLASS = 1
_ACPI_GENL_ATTR_EVENT_BUS_ID = 2
_ACPI_GENL_ATTR_EVENT_TYPE = 3
_ACPI_GENL_ATTR_EVENT_DATA = 4

#: The generic netlink family the ACPI bus publishes events on.
ACPI_EVENT_FAMILY = "acpi_event"

#: Its multicast group is called "acpi_mc_group" (seen on this kernel).
#: Older or lighter builds have advertised other names, so fall back to
#: whatever the family actually offers rather than insisting on one name.
ACPI_EVENT_GROUP_NAMES = ("acpi_mc_group", "acpi")

_NLMSG_HDR_LEN = 16
_GENL_HDR_LEN = 4


def _align4(value: int) -> int:
    return (value + 3) & ~3


def parse_attributes(buf: bytes) -> dict:
    """Parse a run of netlink attributes into {type: [payload, ...]}."""
    attrs: dict = {}
    offset = 0
    end = len(buf)
    while offset + 4 <= end:
        length, atype = struct.unpack_from("=HH", buf, offset)
        if length < 4 or offset + length > end:
            break
        attrs.setdefault(atype, []).append(buf[offset + 4:offset + length])
        offset += _align4(length)
    return attrs


def iter_netlink_messages(data: bytes):
    """Yield (msg_type, payload) for every nlmsghdr in a netlink datagram."""
    offset = 0
    end = len(data)
    while offset + _NLMSG_HDR_LEN <= end:
        length, mtype, _flags, _seq, _pid = struct.unpack_from("=IHHII", data, offset)
        if length < _NLMSG_HDR_LEN or offset + length > end:
            break
        yield mtype, data[offset + _NLMSG_HDR_LEN:offset + length]
        offset += _align4(length)


def _decode_cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def build_getfamily_request(name: str, seq: int = 1) -> bytes:
    """Build a NETLINK_GENERIC CTRL_CMD_GETFAMILY request."""
    payload = struct.pack("=BBH", _CTRL_CMD_GETFAMILY, 1, 0)
    raw_name = name.encode() + b"\x00"
    attr_len = 4 + len(raw_name)
    payload += struct.pack("=HH", attr_len, _CTRL_ATTR_FAMILY_NAME) + raw_name
    payload += b"\x00" * (_align4(attr_len) - attr_len)
    header = struct.pack("=IHHII", _NLMSG_HDR_LEN + len(payload),
                         _GENL_ID_CTRL, _NLM_F_REQUEST, seq, 0)
    return header + payload


def parse_getfamily_response(payload: bytes):
    """-> (family_id, {group_name: group_id}); (None, {}) when not found."""
    attrs = parse_attributes(payload[_GENL_HDR_LEN:])
    family = attrs.get(_CTRL_ATTR_FAMILY_ID)
    if not family:
        return None, {}
    family_id = struct.unpack_from("=H", family[0], 0)[0]

    groups: dict = {}
    for nested in attrs.get(_CTRL_ATTR_MCAST_GROUPS, []):
        # The kernel nests each group one level deeper than you would expect:
        #   CTRL_ATTR_MCAST_GROUPS { <group index>: { GRP_NAME, GRP_ID } }
        # see ctrl_fill_info() in net/netlink/genetlink.c (nla_nest_start(skb, i + 1)).
        for entries in parse_attributes(nested).values():
            for entry in entries:
                group = parse_attributes(entry)
                gid = group.get(_CTRL_ATTR_MCAST_GRP_ID)
                if not gid:
                    continue
                name = _decode_cstr(group.get(_CTRL_ATTR_MCAST_GRP_NAME, [b""])[0])
                groups[name] = struct.unpack_from("=I", gid[0], 0)[0]
    return family_id, groups


def parse_acpi_event(payload: bytes):
    """-> (device_class, bus_id, type, data) or None."""
    attrs = parse_attributes(payload[_GENL_HDR_LEN:])
    nested = attrs.get(_ACPI_GENL_ATTR_EVENT)
    if not nested:
        return None
    event = parse_attributes(nested[0])
    etype = event.get(_ACPI_GENL_ATTR_EVENT_TYPE)
    edata = event.get(_ACPI_GENL_ATTR_EVENT_DATA)
    return (
        _decode_cstr(event.get(_ACPI_GENL_ATTR_EVENT_DEVICE_CLASS, [b""])[0]),
        _decode_cstr(event.get(_ACPI_GENL_ATTR_EVENT_BUS_ID, [b""])[0]),
        struct.unpack_from("=I", etype[0], 0)[0] if etype else None,
        struct.unpack_from("=I", edata[0], 0)[0] if edata else None,
    )


def open_acpi_event_listener(timeout: float = 2.0):
    """Subscribe to the ACPI netlink multicast group.

    Returns a non-blocking socket, or None when the kernel does not provide
    the acpi_event generic netlink family (or we are not allowed to use it).
    Callers must treat None as "fall back to polling", not as fatal.
    """
    try:
        control = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_GENERIC)
    except OSError:
        return None

    family_id = None
    groups: dict = {}
    try:
        control.bind((0, 0))
        control.settimeout(timeout)
        control.send(build_getfamily_request(ACPI_EVENT_FAMILY))

        deadline = time.monotonic() + timeout
        while family_id is None and time.monotonic() < deadline:
            try:
                data = control.recv(65536)
            except (socket.timeout, OSError):
                break
            for mtype, payload in iter_netlink_messages(data):
                if mtype == _NLMSG_ERROR:
                    return None
                found, found_groups = parse_getfamily_response(payload)
                if found:
                    family_id, groups = found, found_groups
                    break
    finally:
        control.close()

    if family_id is None:
        return None

    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_GENERIC)
        sock.bind((0, 0))
    except OSError:
        return None

    group_ids: list = []
    for name in ACPI_EVENT_GROUP_NAMES:
        if name in groups:
            group_ids = [groups[name]]
            break
    if not group_ids:
        # Nothing matched by name; the family normally advertises exactly one
        # group, so subscribing to all of them is the safe fallback.
        group_ids = list(groups.values())

    joined = 0
    for gid in group_ids:
        try:
            sock.setsockopt(_SOL_NETLINK, _NETLINK_ADD_MEMBERSHIP, gid)
            joined += 1
        except OSError:
            continue

    if not joined:
        sock.close()
        return None

    sock.setblocking(False)
    return sock


def read_acpi_events(sock) -> list:
    """Drain every pending ACPI event -> list of (class, bus, type, data)."""
    events: list = []
    while True:
        try:
            data = sock.recv(65536)
        except BlockingIOError:
            break
        except OSError:
            break
        if not data:
            break
        for mtype, payload in iter_netlink_messages(data):
            if mtype == _NLMSG_DONE:
                return events
            if mtype == _NLMSG_ERROR:
                continue
            event = parse_acpi_event(payload)
            if event and event[0]:
                events.append(event)
    return events


def is_button_event(event) -> bool:
    """True when an ACPI event is the front-panel thermal-mode button.

    The kernel does not bind a driver to the vendor WMI GUID, but the ACPI
    bus still delivers the notify as a generic WMI event, so the type field
    carries the notify id (0xBC) from the DSDT.
    """
    device_class, _bus_id, etype, _data = event
    return device_class == "wmi" and etype == WMI_BUTTON_NOTIFY_ID


# ---------------------------------------------------------------------------
# Session / state helpers
# ---------------------------------------------------------------------------

def find_session_user(preferred: str = ""):
    """Locate the logged-in graphical user's pwd entry, or None."""
    if preferred:
        try:
            return pwd.getpwnam(preferred)
        except KeyError:
            pass

    best = None
    for entry in sorted(glob.glob("/run/user/*")):
        name = os.path.basename(entry)
        if not name.isdigit():
            continue
        uid = int(name)
        if uid == 0:
            continue
        if not os.path.exists(os.path.join(entry, "bus")):
            continue
        graphical = (bool(glob.glob(os.path.join(entry, "wayland-*")))
                     or os.path.exists(os.path.join(entry, "X11")))
        if not graphical:
            continue
        try:
            user = pwd.getpwuid(uid)
        except KeyError:
            continue
        if best is None:
            best = user
    return best


def read_state_file(state_dir: str = STATE_DIR):
    """Mode index published by the daemon, or None. World readable."""
    try:
        with open(os.path.join(state_dir, STATE_FILE), "r", encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def write_state_file(index: int, state_dir: str = STATE_DIR) -> bool:
    try:
        os.makedirs(state_dir, exist_ok=True)
        os.chmod(state_dir, 0o755)
        tmp = os.path.join(state_dir, STATE_FILE + ".tmp")
        with open(tmp, "w", encoding="ascii") as fh:
            fh.write(f"{index}\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(state_dir, STATE_FILE))
        return True
    except OSError:
        return False
