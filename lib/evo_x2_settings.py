"""Per-user preferences for the EVO-X2 thermal-mode tools.

There are deliberately two layers of configuration:

``/etc/evo-x2-linux-osd.conf``
    The administrator's config, installed by install.sh and read by the
    daemon.  Applies to every user on the machine.

``~/.config/evo-x2-linux-osd/settings.conf``
    The user's own preferences, written by the GUI.  The daemon -- which runs
    as root -- reads the *session* user's file and lets it override the system
    one, so a switch in the window takes effect without a password prompt and
    without restarting anything.

The second layer is what makes the switches usable at all.  Editing
/etc/evo-x2-linux-osd.conf needs root, and asking for a password on every
toggle would be a worse experience than having no toggles.

Only the keys in KEYS are understood.  Anything else in the file is preserved
on write, so a hand-added key is never silently dropped.
"""

from __future__ import annotations

import configparser
import os

__all__ = [
    "APP_DIR", "APP_ID", "AUTOSTART_NAME", "DEFAULTS", "KEYS", "SECTION",
    "SETTINGS_NAME", "SYSTEM_AUTOSTART_DIR", "Settings", "as_bool",
    "autostart_entry", "autostart_path", "autostart_enabled", "set_autostart",
    "settings_path",
]

#: Must match APP_ID in gui/evo-x2-control and the installed .desktop files:
#: that equality is what ties the window, the task manager icon and this
#: autostart override to one identity.
APP_ID = "org.evox2.Control"

APP_DIR = "evo-x2-linux-osd"
SETTINGS_NAME = "settings.conf"
SECTION = "osd"

AUTOSTART_NAME = APP_ID + ".desktop"
SYSTEM_AUTOSTART_DIR = "/etc/xdg/autostart"

TRUE = ("1", "yes", "true", "on", "y")

#: The keys the GUI edits.  poll_interval and show_notifications use the same
#: names as the system config so the daemon can overlay one onto the other
#: without a translation table; restore_mode is written by the daemon, not the
#: GUI, and records the last mode so it can be re-applied at boot.
#:
#: There is deliberately no key for the login autostart.  That state lives in
#: the autostart file itself, and duplicating it here would give two answers to
#: one question.
KEYS = (
    "show_notifications",
    "poll_interval",
    "restore_on_boot",
    "restore_mode",
)

DEFAULTS = {
    "show_notifications": "yes",
    "poll_interval": "1.0",
    "restore_on_boot": "no",
    "restore_mode": "",
}

HEADER = """\
# Preferences for the EVO-X2 thermal-mode tools.
#
# Written by the Thermal Mode window.  The daemon reads this file as the
# session user and lets it override /etc/evo-x2-linux-osd.conf, so changes
# here take effect immediately and need no privileges.
#
# Editing by hand is fine -- the window rewrites this file in place and keeps
# any key it does not recognise.
"""


def as_bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in TRUE


def settings_path(home=None) -> str:
    """Where the settings file lives.

    With ``home`` given -- which is the case for the root daemon reading the
    session user's file -- the XDG default of ~/.config is used, because the
    daemon does not inherit the user's XDG_CONFIG_HOME.  Without it, the
    environment is honoured, which is what the GUI and a login shell want.
    """
    if home is not None:
        return os.path.join(home, ".config", APP_DIR, SETTINGS_NAME)
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR, SETTINGS_NAME)


class Settings:
    """The user's preferences, with the defaults filled in.

    ``explicit()`` returns only the keys actually present in the file, which
    is what the daemon overlays onto the system config: a key the user has
    never touched must not shadow the administrator's value.
    """

    def __init__(self, home=None, path=None):
        self.path = path or settings_path(home)
        self._values = {}
        self._mtime = None
        self.load()

    # -- loading ----------------------------------------------------------

    def load(self) -> None:
        self._values = {}
        parser = configparser.ConfigParser()
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, UnicodeDecodeError):
            pass
        except configparser.Error:
            # A corrupt file must not stop the daemon from starting; the
            # defaults below are always valid.
            pass
        else:
            if parser.has_section(SECTION):
                for key, value in parser.items(SECTION):
                    self._values[key.replace("-", "_")] = value.strip()
        self._mtime = self._stat()

    def _stat(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Re-read if the file changed on disk.  -> True if it did."""
        if self._stat() == self._mtime:
            return False
        self.load()
        return True

    # -- reading ----------------------------------------------------------

    def explicit(self) -> dict:
        """Only the keys the user has actually set."""
        return {key: value for key, value in self._values.items()
                if key in KEYS}

    def get(self, key, default=None):
        if key in self._values:
            return self._values[key]
        if default is not None:
            return default
        return DEFAULTS.get(key)

    def get_bool(self, key) -> bool:
        return as_bool(self.get(key), as_bool(DEFAULTS.get(key)))

    def get_float(self, key, fallback=1.0) -> float:
        try:
            return float(self.get(key))
        except (TypeError, ValueError):
            return fallback

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    # -- writing ----------------------------------------------------------

    def set(self, key, value) -> None:
        if key not in KEYS:
            raise KeyError(f"{key!r} is not a setting")
        if isinstance(value, bool):
            value = "yes" if value else "no"
        self._values[key] = str(value)

    def save(self) -> bool:
        """Write the file back, keeping the comments this module owns.

        Written to a temporary file and renamed, so the daemon can never read
        a half-written file.
        """
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(HEADER)
                handle.write(f"\n[{SECTION}]\n")
                for key in KEYS:
                    if key in self._values:
                        handle.write(f"{key} = {self._values[key]}\n")
                for key, value in self._values.items():
                    if key not in KEYS:          # preserve foreign keys
                        handle.write(f"{key} = {value}\n")
            os.replace(tmp, self.path)
        except OSError:
            return False
        self._mtime = self._stat()
        return True


# ---------------------------------------------------------------------------
# Starting the tray icon at login
# ---------------------------------------------------------------------------

def autostart_path(home=None) -> str:
    """The user's autostart override, not the system one."""
    if home is not None:
        base = os.path.join(home, ".config")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "autostart", AUTOSTART_NAME)


def _is_hidden(path) -> bool:
    parser = configparser.ConfigParser()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeDecodeError, configparser.Error):
        return False
    return as_bool(parser.get("Desktop Entry", "Hidden", fallback="false"))


def autostart_enabled(home=None) -> bool:
    """Whether the tray icon will appear at the next login.

    An XDG autostart file in the user's directory *replaces* a system one with
    the same name rather than merging with it, so the user's file is
    authoritative whenever it exists; only ``Hidden=true`` means "do not run".
    With no user file the installed system-wide entry decides it.
    """
    override = autostart_path(home)
    if os.path.exists(override):
        return not _is_hidden(override)
    return os.path.exists(os.path.join(SYSTEM_AUTOSTART_DIR, AUTOSTART_NAME))


def _write_entry(path: str, body: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def autostart_entry() -> str:
    """A complete entry, for the case where the system one is missing.

    Only used when there is nothing in SYSTEM_AUTOSTART_DIR to fall back on,
    so that clearing the override cannot leave the tray with no way to start.
    """
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_ID}\n"
        "# Written by the Thermal Mode window.\n"
        "Exec=evo-x2-control --tray\n"
        "Icon=evo-x2-control\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def set_autostart(enabled: bool, home=None) -> bool:
    """Turn the login autostart entry on or off for this user.

    Off writes an override carrying ``Hidden=true``.  On removes the override
    so the installed system-wide entry applies again -- unless there is no
    system entry, in which case a complete one is written instead, because
    otherwise clearing the override would leave the tray with nothing to
    start from.  Nothing here needs root, which is the point of doing it this
    way rather than editing a file under /etc.
    """
    override = autostart_path(home)
    system = os.path.join(SYSTEM_AUTOSTART_DIR, AUTOSTART_NAME)

    if enabled:
        if os.path.exists(system):
            try:
                if os.path.exists(override):
                    os.remove(override)
            except OSError:
                return False
            return True
        return _write_entry(override, autostart_entry())

    return _write_entry(override, (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_ID}\n"
        "# Written by the Thermal Mode window: this user does not want the\n"
        "# tray icon at login.  Delete this file to fall back to the\n"
        "# system-wide entry in " + SYSTEM_AUTOSTART_DIR + ".\n"
        "Hidden=true\n"
    ))
