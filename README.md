# evo-x2-linux-osd

[![CI](https://github.com/RepairYourTech/evox2-linux-util/actions/workflows/ci.yml/badge.svg)](https://github.com/RepairYourTech/evox2-linux-util/actions/workflows/ci.yml)

**The on-screen display your mini PC was supposed to come with.**

The front-panel power-profile button on IP3-Tech *Strix Halo* mini PCs works on
Linux — the embedded controller dutifully switches between Quiet, Balanced and
Performance modes. What is missing is any feedback, because the toast that
confirms the change is drawn by GMKtec's Windows-only `ImagesShow.exe`, which
has no Linux build.

This is a small daemon that fills that gap. Press the button, get a
notification naming the new mode. Optionally, your OS power profile follows
along too. There is a desktop app as well: a window showing the live numbers
that prove the mode is doing something, and a tray icon that always tells you
which mode you are in.

No out-of-tree kernel modules, no proprietary tools, no build step: pure Python,
with GTK 4 only for the optional graphical front end.

```
$ evo-x2-power
Thermal mode: Performance (2)
  Maximum sustained performance; the fans will be audible.
  OS power profile: performance
```

---

## The problem

On Windows the chain is:

1. You press the button → the **EC switches mode**.
2. The BIOS's ACPI method fires a **WMI event** describing the change.
3. `ImagesShow.exe` receives it, reads an OSD code from the EC, draws the toast.

On Linux step 1 happens exactly the same way, and that is precisely the trap:
the machine really does change mode, so the button *looks* like it works. But
step 2 lands on deaf ears — no mainline Linux driver binds to the vendor's WMI
GUID, so the kernel discards the event — and step 3 was never ported. The result
is a button that quietly does something you cannot see, plus an OS power profile
that never follows it.

Once you know which two bytes to read, all three layers are reproducible in
userspace. That is what this project does. The full derivation, including the
DSDT excerpts, is in [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

## Supported hardware

Any machine built on the IP3-Tech Strix Halo reference board, which exposes a
WMI event GUID `8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C` (ACPI notify id `0xBC`) and
a WMI method GUID `99D89064-8D50-42BB-BEA9-155B2E5D0FCD`:

| Machine | Status |
|---|---|
| GMKtec NucBox EVO-X2 | **Verified** — Ubuntu 26.04, kernel 7.0.0-31 |
| GMKtec EVO-X1 | Expected to work (same reference design) |
| Corsair AI Workstation 300 | Expected to work |
| Beelink GTR / SER AI (IP3-based) | Expected to work |
| Other IP3-Tech rebadges | Expected to work |

Check before installing:

```bash
sudo install.sh --dry-run      # or simply:
ls /sys/bus/wmi/devices/8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C
```

If that path does not exist, this tool is not for your machine and the installer
will tell you so.

**Requirements:** Linux with systemd, Python 3.7+, and kernel 5.4 or newer —
just for `CONFIG_ACPI_EC_DEBUGFS` (`ec_sys`), which is mainline and has been
forever. `power-profiles-daemon` and `libnotify` are optional: without them you
get the mode detection but no notification or profile switch. Notifications are
delivered by `notify-send`, falling back to `busctl` (part of systemd) if
`notify-send` is missing or confined — see Troubleshooting.

The graphical front end needs PyGObject with GTK 4, libadwaita, and dbus-python.
Everything else works without them; the installer detects this, skips the GUI,
and prints the command for your distribution:

| Distribution | Install the GUI dependencies with |
|---|---|
| Debian / Ubuntu | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-dbus` |
| Fedora | `sudo dnf install python3-gobject gtk4 libadwaita python3-dbus` |
| Arch | `sudo pacman -S python-gobject gtk4 libadwaita python-dbus` |
| openSUSE | `sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 python3-dbus-python` |

Run the installer again afterwards and the GUI is added.

## Install

```bash
git clone https://github.com/RepairYourTech/evox2-linux-util
cd evox2-linux-util
sudo ./install.sh
```

The installer:

* copies the program to `/opt/evo-x2-linux-osd` (root-owned, not writable by
  anyone else, since root runs it),
* loads `ec_sys` at every boot via `/etc/modules-load.d`,
* enables EC writes via `/etc/modprobe.d/ec_sys.conf` so the mode switcher
  works, and reloads the module if it was already loaded,
* installs and starts a systemd service,
* adds `evo-x2-power` to your `PATH` and a passwordless sudo rule for it,
* installs the icons into `/usr/share/icons/hicolor` and refreshes the icon
  cache,
* adds **Thermal Mode** to the application menu
  (`/usr/share/applications/org.evox2.Control.desktop`),
* starts the tray icon at login
  (`/etc/xdg/autostart/org.evox2.Control.desktop`),
* runs the diagnostics and shows you the result.

Flags worth knowing:

| Flag | Effect |
|---|---|
| `--read-only` | Install only the OSD. Never enables EC writes, no mode switcher. |
| `--user NAME` | Send notifications to `NAME` instead of auto-detecting. |
| `--no-gui` | Skip the graphical front end. Automatic if its dependencies are missing. |
| `--no-autostart` | Install the GUI but do not start its tray icon at login. |
| `--no-sudoers` | Skip the passwordless sudo rule for `evo-x2-power`. |
| `--no-start` | Install everything but leave the service stopped. |
| `--dry-run` | Show what would happen, change nothing. |

All of it is idempotent — re-run it after any change, including `git pull`. The
icons are installed even with `--no-gui`, because the daemon names them in the
notifications it sends.

Press the button when it finishes. You should get a notification.

## Usage

```bash
evo-x2-power                  # what mode am I in?
evo-x2-power --list           # all four modes, with descriptions
evo-x2-power --json           # for scripts and status bars
evo-x2-power --value          # just the index: 0, 1, 2 or 3

evo-x2-power quiet            # switch modes (alias: eco, silent)
evo-x2-power balanced         #                (alias: normal, auto, default)
evo-x2-power performance      #                (alias: perf, boost, turbo)
evo-x2-power sustained --yes  # the undocumented 4th mode

evo-x2-power --check          # diagnostics (paste this into bug reports)
```

To see what the modes actually *do* to the machine, rather than just which one is
selected:

```bash
sudo tools/ec-probe --diff                    # what the button does to the EC
sudo tools/mode-report                        # live power / clock / temperature
sudo tools/mode-report --compare --load 24    # compare every mode under load
```

Status reads are unprivileged: they use the value the daemon publishes in
`/run/evo-x2-linux-osd/mode`. Switching modes writes to the EC and needs root.

## The desktop app and the tray icon

`evo-x2-control` is the graphical front end: the current mode, the same four
presets, and the live readings (mean clock, package temperature, package power)
that show whether the mode is actually doing anything.

```bash
evo-x2-control           # open the window, and the tray icon
evo-x2-control --tray    # tray icon only; what the autostart entry runs
```

It is in the application menu as **Thermal Mode** (Settings > Hardware), and its
tray icon starts at login. The icon is a rising-bar gauge, one bar per level, and
the tooltip names the mode — so both track the physical button even with no
window open:

| Mode | Icon |
|---|---|
| Quiet | one green bar |
| Balanced | two blue bars |
| Performance | three amber bars |
| Sustained | four purple bars |
| not readable | dashed grey ring |

The application icon — the one the menu entry, the window list and the task
manager draw — is the same three-bar mark on a mid-tone blue badge, so it stays
visible on a dark panel and a light one.

Closing the window hides it and leaves the tray icon running. **Quit** in the
tray menu exits the program.

The window also has a **Preferences** group for the settings you might want to
change without root — the tray icon at login, the mode-change notification, the
poll interval and restoring the last mode at boot. See
[Preferences](#preferences).

The GUI runs entirely unprivileged: it changes modes by calling `evo-x2-power`
through `sudo -n` (the passwordless rule from the installer), falling back to
`pkexec`. **Run diagnostics** in the window shows the same output as
`evo-x2-power --check`.

The icons are shipped in `data/icons` and installed into hicolor rather than
borrowed from the icon theme, because the obvious theme names do not exist
everywhere. They are plain coloured files, not `-symbolic` ones: the tray and
notification icons are drawn by Qt, which cannot parse the `currentColor` a
symbolic icon relies on and painted them solid black. Bar count is the mode —
one bar for Quiet, four for Sustained — and the colours are mid-tone so they
stay visible on a light panel and a dark one alike.

There are two sets, with different rules:

| icon | drawn by | rule |
|---|---|---|
| `evo-x2-<mode>` | the tray and the notifications | transparent, so every colour in it must clear 3:1 against *both* the dark and the light panel |
| `evo-x2-control` | the menu entry, the window list and the task manager | opaque, so the badge itself must clear 3:1 against both panels |

Both sets are mid-tone for the same reason: a bright icon disappears on a light
panel, a dark one on a dark panel, and neither failure is an error — the icon is
simply not there. See
[docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md#11-desktop-integration).

## What the modes do

| Mode | EC | Button? | OS profile | Notes |
|---|---|---|---|---|
| Quiet | 0 | yes | `power-saver` | Reduced power limits, fans kept slow. |
| Balanced | 1 | yes | `balanced` | Default behaviour. |
| Performance | 2 | yes | `performance` | Maximum sustained performance, audible fans. |
| Sustained | 3 | no | `performance` | Undocumented. Cores held near boost with a lower package power cap. |

**Sustained** is a real 4th mode the firmware accepts but the button cannot
reach. Its signature is the opposite of a "performance plus" mode: idle
temperature and power go up because idle states are inhibited, while peak power
under sustained load is *capped lower* than Performance. It is useful when
latency matters more than throughput; it is a poor choice for a machine that
idles.

### What these modes actually measure on an EVO-X2

`tools/mode-report` samples the platform per mode. On a NucBox EVO-X2 (Ubuntu
26.04, kernel 7.0.0-31), idle:

| mode | EPP | mean clock | Tctl | package power |
|---|---|---|---|---|
| Quiet | `power` | 1795 MHz | 51.8 °C | 25.2 W |
| Balanced | `balance_performance` | 2600 MHz | 49.3 °C | 21.8 W |
| Performance | `performance` | 3360 MHz | 51.5 °C | 30.7 W |

The modes really do change platform behaviour, and the OS profile follows.

Under a fixed 24-worker load, with each mode cooled to the same starting
temperature and reproduced in both orders, the ranking was **not** what the names
suggest:

| mode | mean clock | Tctl | package power |
|---|---|---|---|
| Quiet | ~2900 MHz | ~80 °C | ~84 W |
| Balanced | ~4090 MHz | ~96 °C | ~123 W |
| Performance | ~2600 MHz | ~60 °C | ~54 W |

Balanced sustains the most. Performance holds a remarkably flat ~60 °C and ~54 W.
Whether that is deliberate firmware behaviour, or whether writing `EC[0x32]`
directly is an *incomplete* mode change -- the BIOS's own ACPI method also writes
an event code into `AMW0.FEBC`, which is out of reach from userspace without the
out-of-tree `acpi_call` -- is not settled. If you own one of these machines,
comparing a **button-driven** Performance against a written one would answer it.

Reproduce with:

```bash
sudo tools/mode-report --compare --load 24 --sample 8 --reverse
```

## Preferences

The **Preferences** group in the window writes
`~/.config/evo-x2-linux-osd/settings.conf`:

```ini
[osd]
show_notifications = yes   # the on-screen display
poll_interval = 1.0        # seconds; this is the button's response time
restore_on_boot = no       # re-apply the last mode at boot
restore_mode = performance # remembered automatically, not edited by hand
```

The daemon runs as root and reads the *session user's* file on top of
`/etc/evo-x2-linux-osd.conf`, so a change takes effect on the next mode change —
no restart and no password prompt. Only those four keys can be set this way:
which user is notified, which icons are used and whether the OS profile is
mirrored stay the administrator's business, so a hand-written file cannot reach
them.

**Start the tray icon at login** writes an XDG autostart entry for your user
(`~/.config/autostart/org.evox2.Control.desktop`) instead of touching the
system-wide one in `/etc/xdg/autostart`. Turning it back on removes the override
so the installed entry applies again. It needs no privileges, which is the point
of doing it that way.

`restore_on_boot` only helps if this firmware forgets the mode across a power
cycle; if it keeps it, the EC already reads back the same mode and nothing is
written. It is applied once a graphical session exists, because both the switch
and the remembered mode live in that user's file. A restore is not treated as a
button press: you get no notification about the mode you chose yesterday, and
the tray icon is told the restored mode straight away so it matches the machine
from the moment the desktop appears.

## Configuration

`/etc/evo-x2-linux-osd.conf` — the administrator's settings, for every user:

```ini
[osd]
user =                  # empty = auto-detect the graphical session
poll_interval = 1.0     # how often the EC is read, in seconds
use_acpi_events = yes   # react to the button instantly
sync_power_profiles = yes
sync_on_start = no      # reconcile the OS profile with hardware at boot
show_notifications = yes
notify_timeout_ms = 3000
restore_on_boot = no    # usually set per user instead; see Preferences
log_level = info
```

```bash
sudo systemctl restart evo-x2-thermal-osd
```

`poll_interval` and `show_notifications` here are defaults that a user's own
preferences override.

## Troubleshooting

Start with `evo-x2-power --check`. It tests every link in the chain and prints a
list of things to fix.

**The button does nothing and I get no notification.** Read the daemon log:

```bash
journalctl -u evo-x2-thermal-osd -f
```

Press the button again. If nothing is logged, the ACPI event is not reaching
userspace on your firmware; the daemon still detects the change on its next poll
(within `poll_interval` seconds), so you should still get the notification
shortly afterwards. If you see `ACPI event: (...)` lines, the event path is
working and the problem is further down.

**After a reboot the tray icon says "unknown" and the window says the daemon is
not running.** The tray cannot read the EC itself — only the daemon may — so
with no daemon there is no mode to show. Check whether it started:

```bash
systemctl status evo-x2-thermal-osd
journalctl -b -u evo-x2-thermal-osd | head -5
```

If the journal contains `Found ordering cycle` and `Job ... deleted to break
ordering cycle`, the service is enabled but systemd refused to start it. This is
the failure an early build shipped: the unit ordered itself after
`power-profiles-daemon`, which starts after `multi-user.target`, while a service
wanted by that target is ordered *before* it. Nothing else reports it — the unit
validates, the service is `enabled`, and starting it by hand works. The unit now
pulls ppd in without ordering against it, and the daemon retries the OS-profile
sync at start-up instead.

**No notification at all, but the mode is detected.** The daemon runs as root
and drops to your session user before delivering the toast. If you are not
logged into a graphical session, or you have more than one, set `user =` in the
config explicitly.

**`notify-send` is refused with `Could not connect: Permission denied`.** This
is almost always confinement, not a bug here. Many Ubuntu derivatives ship an
AppArmor profile for `notify-send`, and AppArmor's path-based mediation of the
unix-socket `connect()` fails when the caller is in a private mount namespace:

```
apparmor="DENIED" operation="connect" class="file"
info="Failed name lookup - disconnected path" error=-13
profile="notify-send" name="run/user/1000/bus"
```

That is exactly why the shipped unit does **not** set `ProtectSystem=`,
`PrivateTmp=` or `ProtectControlGroups=` (see the comments in the unit file),
and why the daemon has a second transport: it falls back to calling
`org.freedesktop.Notifications` over D-Bus with `busctl`, which ships with
systemd and is not confined. If the fallback is doing the work you will see
`notified ... via D-Bus` in the log. You can confirm which one is being blocked
with:

```bash
sudo dmesg | grep -i apparmor | tail
```

**`evo-x2-power` says the write was rejected.** `ec_sys` is loaded without
`write_support=1`. Reload it:

```bash
sudo modprobe -r ec_sys && sudo modprobe ec_sys
```

**The mode changed but the OS power profile did not.** That needs
`power-profiles-daemon`. Systems using TLP or tuned instead will not have
`powerprofilesctl`, and the daemon silently skips the sync.

**The notification icon is blank.** The icons shipped by this package are not
in your icon theme path. Check that they landed and that the cache was rebuilt:

```bash
ls /usr/share/icons/hicolor/scalable/apps/evo-x2-*.svg
sudo gtk-update-icon-cache -f -t /usr/share/icons/hicolor
```

Override the names in the `[icons]` section of the config if you prefer your own.

**There is no tray icon.** Check the app's own explanation first — it prints the
reason rather than failing quietly:

```bash
evo-x2-control --tray
```

A `no StatusNotifierWatcher` or `no tray host` message means your desktop has no
StatusNotifierItem support: on GNOME that means the *App Indicator* extension is
not enabled and there is no system tray at all. The window still works normally.
The app retries registration for a minute before giving up, which is long enough
to cover the tray host starting after login; if it has never worked at all, that
retry will not help.

**The tray icon is there but empty, or the window shows a generic icon.** The
icon files or the `.desktop` entry are missing. Re-run the installer, which
installs both, and confirm the names resolve:

```bash
ls /usr/share/applications/org.evox2.Control.desktop
ls /usr/share/icons/hicolor/scalable/apps/evo-x2-balanced.svg
```

If the tray icon is there but drawn in **solid black**, an older
`evo-x2-*-symbolic.svg` is still installed. Such icons carry no colour of their
own (`fill="currentColor"`) and Qt, which draws the tray, cannot parse that, so
it falls back to opaque black. Re-run the installer: it removes icons this
version no longer ships, and ships plain coloured ones instead.

If the *task manager*, the window list or the menu entry shows a **black
square**, that is the application icon, which is a different file from the tray
icons. Early versions drew it as a dark slate badge (down to `#1a1f27`, which is
1.01:1 against a dark panel — invisible). It is now a mid-tone blue badge that
clears 3:1 against both themes:

```bash
ksvgtopng 32 32 /usr/share/icons/hicolor/scalable/apps/evo-x2-control.svg /tmp/i.png
# the badge should read as a blue square with three pale bars, not a black one
```

If the *task manager* instead shows a **generic** icon while the app menu icon is
fine, the window's app id has drifted from the `.desktop` file name. Report it —
`tests/test_tray.py` asserts those two stay in step. They are checked against
KWin directly with:

```bash
# app_id and desktopFileName must both be org.evox2.Control
qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript window-list.js
```

## Uninstall

```bash
sudo /opt/evo-x2-linux-osd/uninstall.sh            # keep the config
sudo /opt/evo-x2-linux-osd/uninstall.sh --purge    # remove it too
```

This also stops a running tray icon and removes the menu entry, the autostart
entry, and this package's own icons (`evo-x2-*.svg`) from hicolor. It never
removes the hicolor directories themselves, which are shared with every other
application on the system.

The front-panel button keeps working exactly as it did before; you just lose the
on-screen confirmation again.

## Security

* The daemon runs as root because the EC debugfs node is root-only, but it only
  ever *reads* `EC[0x31]`. It never writes to the EC.
* Writing (`evo-x2-power`) is always an explicit, separate action, and touches
  exactly one byte, `EC[0x32]`, which is the same byte the BIOS's own ACPI method
  writes when you press the front-panel button.
* The installer adds a passwordless sudo rule for `evo-x2-power` so mode
  switching works from a normal session. The script accepts only a mode name and
  has no path or command arguments, so the elevated surface is limited to picking
  a thermal profile. Skip it with `--no-sudoers`.
* The installed tree is `root:root` and not writable by group or other, because
  root executes it.
* Poking arbitrary EC bytes is a good way to brick a machine. This is not that:
  the offsets and the `0x80 | mode` encoding are taken directly from the DSDT.

## Development

The test suite needs no hardware, no root and no session bus: the
embedded-controller access is exercised against a plain file standing in for
the debugfs node, and the daemon's notification transports are stubbed.

```bash
python3 -m unittest discover -s tests -v
```

The GUI and D-Bus bindings do have to be installed for the tray and icon tests
to be collected. Without them that test module fails to import, and its tests
are reported as a single loader error rather than as failures — which is why CI
checks that stack before running anything.

CI runs the suite, `shellcheck` on the installer, `desktop-file-validate` on
both `.desktop` entries, and a syntax check on every script — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Credits

This stands on the reverse-engineering of the IP3-Tech platform that other
people did first:

* [MintyMods/ip3-power-switch](https://github.com/MintyMods/ip3-power-switch) —
  identified the WMI GUIDs and the `FCMO`/`FCMI` EC registers, and established
  that writing `EC[0x32]` is equivalent to invoking the BIOS's ACPI method.
* [bla10/evo-x2-power-mode](https://github.com/bla10/evo-x2-power-mode) —
  an EVO-X2-specific daemon built on `acpi_call`.
* `strixhalo.wiki` — the community documentation for the platform.
* [pali/bmfdec](https://github.com/pali/bmfdec), used to decode the WMI BMOF.

What this project adds is the missing frontend: a desktop OSD, `power-profiles-daemon`
integration, a mode switcher, a tray icon and appliance-style window, and a
direct ACPI-netlink listener so neither `acpid`/`acpi_listen` nor the
out-of-tree `acpi_call` module is needed.

## License

MIT — see [LICENSE](LICENSE).
