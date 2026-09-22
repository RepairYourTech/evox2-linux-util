# How it works, and how it was figured out

Everything below was derived from the DSDT and the ACPI tables of a GMKtec
NucBox EVO-X2 (Ubuntu 26.04, kernel 7.0.0-31-generic), cross-checked against the
public reverse-engineering of the same reference board. It is written down so
that the next person with a rebadged IP3-Tech machine can follow the same trail
instead of starting from nothing.

## 1. The button is invisible to the usual tools

Pressing the front-panel power-profile button on Linux produces:

* nothing in `/dev/input/event*`
* nothing in `/dev/hidraw*`
* nothing in `journalctl -k`
* no change to `power-profiles-daemon`

The button is not a USB device and not a keyboard scancode. It is wired to the
embedded controller through the SuperIO chip, and its only software-visible
effect is an ACPI WMI event.

## 2. Find the WMI GUIDs

`/sys/bus/wmi/devices/` lists every GUID the firmware exposes. On this machine:

```
05901221-D566-11D1-B2F0-00A0C9062910    DEVTYPE=data    (BMOF metadata)
05901221-D566-11D1-B2F0-00A0C9062910-1  DEVTYPE=data    (BMOF metadata)
8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C    DEVTYPE=event   notify_id=BC
99D89064-8D50-42BB-BEA9-155B2E5D0FCD    DEVTYPE=method  object_id=AA
```

`DEVTYPE` is in each device's `uevent` file. The two vendor GUIDs split cleanly
into one event (the button) and one callable method.

Critically, **neither vendor GUID has a `driver` symlink**:

```bash
$ ls /sys/bus/wmi/devices/8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C/driver
ls: cannot access '.../driver': No such file or directory
```

That is the whole bug. The kernel receives the event, finds no driver bound to
that GUID, and drops it.

## 3. Decode the BMOF to name the event

The `data` GUID's `bmof` file is a binary description of the vendor WMI classes.
Feeding it to [`bmfdec`](https://github.com/pali/bmfdec) yields:

```
Class 2:
Name=IP3_WMIEvent
Superclassname=WmiEvent
Provider=WmiProv
guid={8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C}
Variable: InstanceName, Active, EventDetail (UInt8[8])
```

So `8FAFC061-…` is the button's event and carries an 8-byte payload.

## 4. Disassemble the DSDT

```bash
sudo apt install acpica-tools        # or your distro's equivalent
sudo cp /sys/firmware/acpi/tables/DSDT /tmp/DSDT
cd /tmp && iasl -d DSDT
```

Searching for the method GUID's packed form
(`64 90 D8 99 50 8D BB 42 BE A9 15 5B 2E 5D 0F CD`) lands in a WMI device whose
`_UID` gives the game away:

```asl
Device (WMIB)
{
    Name (_HID, "PNP0C14")            // standard WMI device
    Name (_UID, "IP3POWERSWITCH")     // <-- the smoking gun
    Name (_WDG, Buffer (0x28) { ... })
    Method (WMAA, 3, NotSerialized) { ... }
}
```

The method's ASL name is `WMAA` (object id `AA`), so its full path is
`\_SB.WMIB.WMAA`. Its body dispatches on `Arg1`:

| `Arg1` | Function |
|---|---|
| `0x01` | **Set mode** — `EC0.FCMI = 0x80 \| mode`, then fires the event |
| `0x02` | **Get mode** — returns `EC0.FCMO` (0..3) |
| `0x03` | Set fan PWM |
| `0x04` | Read fan RPM (dead on these boards) |
| `0x09` / `0x0A` | Keyboard backlight (laptop-firmware leftover) |
| `0x0B` | Read CPU/GPU temperatures |
| `0x0C` | "Smart fan" flags |
| `0x0D` | **Get OSD code** — the value Windows' `ImagesShow` renders as a toast |

The set branch is the interesting one:

```asl
If ((Local1 == One))                    // mode 1 = Balanced
{
    ^^AMW0.FEBC [Zero] = One
    ^^PCI0.SBRG.EC0.FCMI = 0x81
    ^^AMW0.FEBC [One] = 0x13
    Local2 = Zero
}
If ((Local1 == 0x02))                   // mode 2 = Performance
{
    ^^AMW0.FEBC [Zero] = One
    ^^PCI0.SBRG.EC0.FCMI = 0x82
    ^^AMW0.FEBC [One] = 0x11
    Local2 = Zero
}
```

A mode change is two EC writes: `FCMI = 0x80 | mode`, plus an event-code byte
into the payload buffer. Linux has nothing listening for that event, so on Linux
the whole operation collapses to the first write.

## 5. Locate the registers

The EC operation region gives FCMO and FCMI their offsets:

```asl
OperationRegion (ECMM, EmbeddedControl, ...)
{
    ...
    Offset (0x31), FCMO, 8,     // mode output  -- read this for the current state
    Offset (0x32), FCMI, 8,     // mode input   -- write this to request a change
    ...
}
```

| EC offset | DSDT name | Direction | Meaning |
|---|---|---|---|
| `0x31` | `FCMO` | read | Current mode: 0 Quiet, 1 Balanced, 2 Performance, 3 Sustained |
| `0x32` | `FCMI` | write | Mode request: `0x80 \| mode` |

This is how the daemon works: it never calls an ACPI method at all. It reads
`EC[0x31]` through the mainline `ec_sys` module, which is both simpler and more
robust than invoking `WMAA` — the only cost is the WMI event, which nothing on
Linux consumes anyway.

## 6. Reading and writing the EC from userspace

`modprobe ec_sys` exposes a 256-byte window onto EC address space at
`/sys/kernel/debug/ec/ec0/io`. With no module parameters the node is read-only
(`0400`). Loading it with `write_support=1` makes it writable (`0600`) and
enables the `.write` handler, whose file position *is* the EC address:

```bash
# read the current mode
sudo dd if=/sys/kernel/debug/ec/ec0/io bs=1 count=1 skip=49 2>/dev/null | xxd
# 00000000: 02                       -> Performance

# ask for Quiet
printf '\x80' | sudo dd of=/sys/kernel/debug/ec/ec0/io bs=1 count=1 seek=50 conv=notrunc
```

`skip=49` is `0x31`; `seek=50` is `0x32`. Reading a single byte is safe and
side-effect free. Writing one byte at `0x32` is what the BIOS's own ACPI method
does, so every downstream behaviour — fan curves, power limits, the OSD code —
propagates by itself.

## 7. Listening for the button

Since no driver binds to `8FAFC061-…`, you might expect the event to be
unobservable. It is not: the ACPI bus still delivers the notify through its
generic netlink family, which is the same feed `acpi_listen` reads. Talking to
it directly avoids depending on `acpid`:

```
family:  "acpi_event"                          (generic netlink id 32 here)
group:   "acpi_mc_group"                       (numeric id 12 here)
message: device_class="wmi", bus_id="PNP0C14:00", type=0xBC, data=0
```

`type=0xBC` is the `notify_id` from the WMI device's sysfs attributes, which
comes straight from the `_WDG` metadata in the DSDT. So a press *would* show up
as:

```
wmi PNP0C14:00 000000bc 00000000
```

### What actually happens on the EVO-X2

Subscribing to the group succeeds, but **nothing is ever delivered**. With the
daemon's log level at `debug` and a full button cycle performed, the journal
contained zero `ACPI event:` lines, and `acpi_listen` would show nothing either.
The WMI core does not republish a notify for a GUID that has no driver bound, so
the ACPI netlink feed simply stays silent.

That matches what the earlier IP3 investigation reported -- "no acpi_listen
event" -- and it has a practical consequence: **the poll is what makes this work,
not the event listener.** It is also why `poll_interval` defaults to one second.

So this whole section is, on this hardware, an explanation of a path that turns
out not to deliver anything. It is kept in the code on purpose: it costs
nothing, it is correct as written, and on firmware that *does* publish the event
it upgrades a one-second poll into an instant response. Measuring which path is
in use is just a `journalctl -u evo-x2-thermal-osd` at debug level.

### The nesting gotcha

`CTRL_CMD_GETFAMILY` nests each multicast group **one level deeper than the
obvious reading of the header** would suggest. `ctrl_fill_info()` in
`net/netlink/genetlink.c` does:

```c
nest = nla_nest_start(skb, CTRL_ATTR_MCAST_GROUPS);
for (i = 0; i < family->n_mcgrps; i++) {
        mgrp = nla_nest_start(skb, i + 1);          /* <- an extra nest, per group */
        nla_put_string(skb, CTRL_ATTR_MCAST_GRP_NAME, family->mcgrps[i].name);
        nla_put_u32(skb, CTRL_ATTR_MCAST_GRP_ID, family->mcgrps[i].id);
        nla_nest_end(skb, mgrp);
}
nla_nest_end(skb, nest);
```

So the shape is
`CTRL_ATTR_MCAST_GROUPS { <index>: { GRP_NAME, GRP_ID } }`.
Parsing only one level yields a group entry rather than its fields, the name
lookup misses, and subscription silently never happens. A byte-exact reply
captured from this machine is kept in `tests/test_evo_x2_hw.py` as a
regression fixture.

## 8. Prior art and what is different here

| Project | Approach |
|---|---|
| `MintyMods/ip3-power-switch` | Reads `EC[0x31]`, writes `EC[0x32]`, publishes to Home Assistant over MQTT. Correct register map, MQTT frontend. |
| `bla10/evo-x2-power-mode` | EVO-X2 daemon that reads the mode via `acpi_call`'s `_WED 0xBC` and sends a desktop notification. Needs an out-of-tree kernel module and the ACPI header for that module; also only reacts on its own polling. |

This project takes the register map from the first and the desktop-facing goal
of the second, and replaces `acpi_call` with a direct ACPI netlink subscription
plus `ec_sys` reads, so nothing outside mainline is required.

## 9. A trap worth knowing about: confined `notify-send`

Once mode detection worked, the last piece -- showing the toast -- failed in a
way that looked exactly like a permissions bug and was not:

```
notify-send exited 1: Failed to show notification: Could not connect: Permission denied
```

The session bus socket was `srw-rw-rw-`, the connecting user owned it, and a
bare `connect()` from Python to that very path succeeded. `strace` showed the
`connect()` returning `EACCES` anyway. `dmesg` gave it away:

```
apparmor="DENIED" operation="connect" class="file"
info="Failed name lookup - disconnected path" error=-13
profile="notify-send" name="run/user/1000/bus" fsuid=1000 ouid=1000
```

Three conditions had to line up:

1. `/usr/bin/notify-send` is confined by an AppArmor profile (this machine has
   `/etc/apparmor.d/notify-send`). Unconfined programs such as `python3` are not
   path-mediated the same way, which is why the bare `connect()` probe passed
   and misled for a while.
2. The daemon's unit used `ProtectSystem=` / `PrivateTmp=` /
   `ProtectControlGroups=`, which put the process in a private mount namespace.
3. AppArmor's file mediation has to resolve the socket path, and from a
   *disconnected* mount namespace it cannot -- so it denies instead of allows.

Once framed that way the bisect was unambiguous: dropping any one of those three
systemd properties fixed it and no other property mattered. Non-namespace
properties (`NoNewPrivileges`, `RestrictAddressFamilies`, `LockPersonality`, ...)
were harmless throughout. Note that a bare `socket()`+`connect()` probe is *not*
a valid test for this, because it misses the confinement entirely -- use the
real binary.

The fix here has two parts:

* the shipped unit omits the three mount-namespace properties, with a comment
  explaining why, and
* the notifier falls back to `busctl --user call ... Notify ...` (sd-bus), which
  is unconfined and therefore unaffected. The fallback was verified by
  deliberately re-adding the namespace that breaks `notify-send` and confirming
  the toast still arrives (`send -> True | transport: dbus`).

The general lesson for anyone writing something similar: **do not assume
`notify-send` is usable, and do not sandbox the process that calls it.** A
path-mediated LSM can turn an ordinary `connect()` into `EACCES` for reasons
that have nothing to do with file modes.

## 10. Verifying on a new machine

1. `ls /sys/bus/wmi/devices/` — look for `8FAFC061-…` and `99D89064-…`.
2. `cat /sys/bus/wmi/devices/8FAFC061-.../notify_id` — expect `BC`.
3. `grep -ac IP3POWERSWITCH /sys/firmware/acpi/tables/DSDT` (as root) — expect
   at least 1.
4. `sudo modprobe ec_sys && sudo dd if=/sys/kernel/debug/ec/ec0/io bs=1 count=1
   skip=49 | xxd` — expect a byte in `0x00..0x03`.
5. Press the button and repeat step 4 — the byte should move on to the next mode.

If those five pass, everything in this repository applies unchanged. If they do
not, the same method — DSDT plus differential EC snapshots — will get you there
for a different firmware, but the offsets will be different.

## 11. Desktop integration

Four things bit us here, all of them the kind that fail *plausibly* rather than
loudly. They are recorded because none was obvious from the specifications.

### Where the watcher actually lives

A tray icon is a `org.kde.StatusNotifierItem` object on the session bus that
registers itself with `org.kde.StatusNotifierWatcher`. The specification names
the watcher's object path as `/org/kde/StatusNotifierWatcher`. On this machine the
watcher is `kded6`, and it exports `/StatusNotifierWatcher` — no `/org/kde`.
Registering against the documented path fails with `UnknownObject`, which looks
exactly like "this desktop has no system tray".

gnome-shell's AppIndicator extension uses the documented path. So both have to
be probed, and each candidate has to be *queried*, not merely resolved, because
a path can exist without implementing the interface:

```python
WATCHER_PATHS = ("/StatusNotifierWatcher",
                 "/org/kde/StatusNotifierWatcher")
```

### Icons nobody ships

The natural choice for a power-mode icon is `power-profile-balanced-symbolic`
and friends. They exist — in **Yaru** and **Adwaita**. They do not exist in
**Breeze**, and they are not in **hicolor** either (and no shipped icon may end
in `-symbolic` for the separate reason in the next subsection):

```console
$ python3 -c "from gi.repository import Gtk; t = Gtk.IconTheme.new(); \
    t.set_theme_name('breeze-dark'); \
    print(t.has_icon('power-profile-balanced-symbolic'))"
False
```

On a stock KDE desktop that means a tray icon with no image, a menu entry with a
generic icon, and notifications with no icon — with no error anywhere, because a
missing icon name is not an error. This package therefore ships its own icons in
`data/icons` and installs them into hicolor, where every icon theme falls back to
them. `tests/test_tray.py` asserts that every icon name referenced in code has a
file behind it, and that none of them is borrowed from the theme.

### `currentColor` is a black hole on Qt

Having shipped its own icons, the package then wrote them the conventional
symbolic way: no colour of their own, `fill="currentColor"`, leaving the theme
to recolour them to match the panel. On Breeze Dark the entire icon came out
solid black — invisible.

Qt does not implement `currentColor` at all. It is not that the recolouring
step is skipped: the string does not appear anywhere in Qt's SVG plugin, so the
parser drops the paint and the shape falls back to the SVG default of opaque
black:

```console
$ strings /usr/lib64/qt6/plugins/imageformats/libqsvg.so | grep -c currentColor
0
$ ksvgtopng 16 16 evo-x2-balanced-symbolic.svg /tmp/out.png   # then read the pixels
# every opaque pixel is (0, 0, 0, a) -- pure black, whatever the theme
```

This matters because **plasmashell renders both the tray icon and the
notification icon with that same engine**, so a symbolic icon is a black blob on
dark panels and on light ones it is whatever the theme paints behind it. The
icons are now plain coloured files with no `currentColor` anywhere, one bar per
mode level, and mid-tone colours chosen to clear 3:1 contrast against a dark
panel (#1b1e23) and a light one (#ffffff) — a bright icon disappears on the
first, a dark one on the second. Three tests hold that down: no `currentColor`
in any shipped icon, an explicit colour on every shape, and the contrast check.

### The application icon is a different file, and needs a different rule

Fixing the tray icons did not fix the task manager, and it took a moment to see
why: they are different files. `evo-x2-<mode>.svg` is drawn by the tray and the
notifications and is transparent, so the shapes themselves have to carry the
contrast. `evo-x2-control.svg` is drawn by the menu entry, the window list and
the task manager, and is *opaque* — so what the panel samples is the badge, and
the bars inside it only have to be legible against the badge.

The application icon was still the first version: a dark slate gradient badge,
`#39435a` to `#1a1f27`. Measured against the two panels:

```
#39435a  vs dark  1.69:1  FAIL     vs light  9.88:1  ok
#1a1f27  vs dark  1.01:1  FAIL     vs light 16.55:1  ok
```

The bottom of that gradient is 1.01:1 against a dark panel — not low contrast,
literally invisible. On Breeze Dark the task manager drew a black square with a
few faint bars on it. The replacement badge is `#3d8ae0` (4.70:1 dark, 3.55:1
light) with a `#1f6fd0` outline (3.38:1 / 4.95:1), so one badge works on either
theme.

Why it survived review: the tests read paint out of the parsed SVG, and a
gradient is not a `#rrggbb` — every shape in the old file was painted with
`url(#badge)` or `url(#bars)`, so the contrast tests found no colours to check
and the app icon was excluded from them. It is now flat-coloured and checked:
the app icon must contain at least one colour that clears 3:1 against *both*
panels, and no colour in it may be invisible against everything around it.

The other half of the link, the one the asset change cannot fix, was checked
directly against the compositor rather than assumed:

```console
$ qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript window-list.js
js: EVOXWINDOW app_id=org.evox2.Control | desktopFileName=org.evox2.Control \
    | caption=Thermal Mode | skipTaskbar=false
```

`desktopFileName` equals the `.desktop` file name, which is what makes Plasma
look up `Icon=evo-x2-control` for the window. Had those diverged, the task
manager would have shown a *generic* icon — a different symptom, and a different
fix, from the black one.

### A double hyphen makes an SVG invalid, silently

XML forbids `--` inside a comment. An icon whose comment contained a dashed
clause was therefore not well-formed XML, and GTK quietly declined to render it.
The test suite now parses every shipped SVG with `xml.etree`, which turns that
class of mistake into a failing test. It caught this twice: once on the original
symbolic icon, and again on the rewrite, in the very comment explaining why
`currentColor` had to go.

### A tray icon does not keep a GtkApplication alive

`evo-x2-control --tray` initially registered nothing and exited immediately. The
cause: `Gio.Application` quits when its use count reaches zero, and a window
increments it while a D-Bus object does not. With no window to hold it, the
process exited before its own retry loop could run. The fix is an explicit
`self.hold()`, released again if the tray turns out to be unavailable so that a
window-less run does not linger forever with no icon.

### An ordering cycle deletes the job, and nothing tells you

The first reboot after a working install lost the daemon. Everything looked
fine: `systemctl is-enabled` said `enabled`, the unit validated, and
`systemctl start` worked when asked by hand — which is what `install.sh` does,
and why the bug survived every check until a real reboot.

What systemd said, once, at boot:

```
multi-user.target: Found ordering cycle: evo-x2-thermal-osd.service/start
  after power-profiles-daemon.service/start after multi-user.target/start
  - after evo-x2-thermal-osd.service
multi-user.target: Job evo-x2-thermal-osd.service/start deleted to break
  ordering cycle starting with multi-user.target/start
```

The cycle is real and unavoidable as written. A service that is `WantedBy=` a
target is ordered *before* it, so this unit sits before `multi-user.target`;
`power-profiles-daemon` ships with `After=multi-user.target`, so that it can
order before `graphical.target`; and the unit asked to start after ppd. Each
statement is reasonable on its own and together they cannot be satisfied.
systemd resolves it by deleting the job — the least alarming of the things it
could do, and the hardest to notice.

Not ordering after ppd is the fix. ppd is always later than a unit wanted by
`multi-user.target`, so the OS-profile mirror became a retry
(`PPD_STARTUP_RETRIES`) rather than a one-shot call at start-up.

Two consequences of a dead daemon are worth stating, because neither is
obvious. The tray reads the *daemon's* state file rather than the EC — it runs
unprivileged, and only the daemon may touch `ec_sys` — and that file lives in
`/run`, so a reboot wipes it. The symptom of the missing daemon is therefore
not an error message but a tray icon reading "unknown", and a window saying
`unknown -- is the daemon running?`.

`tests/test_systemd_unit.py` resolves the same ordering from the unit files on
disk and fails on this shape — the only way to catch it without rebooting.

### Why the tray backend is hand-written

`pystray` and the libayatana appindicator bindings are extra packages that are
not present on a stock install, and the appindicator path additionally needs a
GNOME extension. `org.kde.StatusNotifierItem` plus `com.canonical.dbusmenu` over
`dbus-python` is what KDE, GNOME (with the extension), Xfce and the common status
bars all speak, and `dbus-python` is already a dependency of the daemon. So
`gui/tray.py` implements the subset a host uses: the item properties, the
`Activate`/`ContextMenu` methods, the `NewIcon`/`NewTitle` signals, and enough of
dbusmenu to draw and dispatch a menu. Note that dbus-python 1.x has no property
decorator, so `org.freedesktop.DBus.Properties` is written out by hand.

## 12. The rest of the embedded controller

The same DSDT that gave up `FCMO` and `FCMI` also names about 145 EC registers.
They live in a single `Field()` list for region `ERAX`, at offset `0x086b3` of
this machine's DSDT. The decoder is about forty lines of Python: find the `5b 81`
FieldOp, decode the PkgLength, read the region NameSeg, skip the flags byte, then
walk the element list where each named element is a NameSeg followed by a
PkgLength giving its width in bits, and the widths accumulate.

**`FCMO` decoding to `0x31` is what makes the rest believable.** That offset was
established behaviourally, long before the DSDT was decoded, by writing a byte
and watching the fans change. Two independent methods agreeing on the same
number is the entire justification for trusting the other 144 names.

| EC byte | Name | What it is |
|---|---|---|
| `0x17` | `DLTM` | unknown, reads 0 |
| `0x1A` | `FNLK` | fan lock, reads 0 |
| `0x31` | `FCMO` | current thermal mode (read) |
| `0x32` | `FCMI` | mode request (write `0x80 \| mode`) |
| `0x33` | `FAN1` | reads 0 in every state tested |
| `0x34` | `FAN2` | reads 0 in every state tested |
| `0x35`/`0x36` | `FN1L`/`FN1H` | a 16-bit fan-1 quantity, see below |
| `0x37`/`0x38` | `FN2L`/`FN2H` | a 16-bit fan-2 quantity, see below |
| `0x3F` | `OSD0` | the OSD code register, `0xFF` when idle |
| `0x54` | `MODS` | `0x80` on this firmware |
| `0x60`-`0x6B` | unnamed | two identical 5-byte blocks, `28 32 64 1e 01` |
| `0x70` | `CPUT` | the EC's own CPU temperature |

The rest is battery, keyboard-backlight and timer registers that this
board does not populate.

### The fan registers do not give you fan speed

`FN1L`/`FN1H` look exactly like the two halves of a 16-bit value, and they are:
reading only those four bytes shows a number that drifts around 1900-2290 and
changes roughly once a second. It is tempting to call that RPM.

It is not. Across a 60-second full-package load the value did not move at all:
temperature went from 45 to 56 degrees and the number stayed inside a 6-unit
band, giving a correlation with temperature of `r = -0.17`. A fan that ignores a
60-second load is not a fan reading. What these bytes are is still unknown:
fan duty written by the EC's own controller, a PWM period, or something else.

There is also no fan speed to *fall back* to. On this machine there is no
`fan*_input` under `/sys/class/hwmon`, no `pwm*` attribute, and `sensors` reports
temperatures and package power only. The thermal cooling devices are `Processor`
(ACPI throttling) and `PCIe_Port_Link_Speed` — nothing fan-shaped. So fan RPM is
simply not exposed to Linux on this platform, and no amount of EC archaeology
will produce it if the EC does not publish it.

### The FAN-MODE button is a lighting control

GMKtec's own manual describes two front-panel buttons. `P-MODE` cycles the
thermal mode — the thing this project implements. `FAN-MODE` is labelled
"Lighting Effect Control": short press cycles 13 fan lighting effects, holding it
for two seconds turns the light off, and a further short press turns it back on.

So the fan *speed* differences you notice belong to `P-MODE`, and the lighting
belongs to `FAN-MODE`. Nothing on this platform exposes the latter:

* The DSDT contains no LED object of any kind — not a `_BCM`, not an
  `acpi_led`, nothing. A case-insensitive search for `led` finds only `FNLK`.
* There is no `/sys/class/leds` entry for it; the only LEDs there are the network
  interface and the keyboard lock indicators.
* It is not a HID device. The only USB device present is a 2.4 GHz keyboard and
  mouse receiver.
* It is not an EC query. The DSDT declares 17 `_Qxx` handlers and none of them is
  lighting- or fan-related; the documented ones are brightness, volume, AC
  status, the power button, and `_Q74` "dynamic DPTC, change thermal table".
* The `MGI0`-`MGIF` and `MGO0`-`MGOF` register blocks are all zero.

That leaves the raw EC register file, reached through `ec_sys` — the same route
that worked for the thermal mode. To find out whether the lighting state is even
readable there, `tools/ec-probe --capture 90` polls the whole window and reports
every register that moves while you press the button. If a register tracks the
13 effects, reading it is safe and writing it might follow; if nothing moves, the
lighting lives entirely inside the EC's own firmware and is out of reach.

## 13. The mode register is not always quiet

Watching the toast is how you find out that `EC[0x31]` can move on its own. The
daemon journal for a plain panel restart on 2026-09-21:

```
20:59:18  INFO thermal mode -> Quiet
20:59:18  INFO OS power profile -> power-saver
20:59:18  INFO notified birdman via notify-send: Quiet
20:59:20  INFO thermal mode -> Balanced
20:59:20  INFO OS power profile -> balanced
20:59:20  INFO notified birdman via notify-send: Balanced
20:59:23  INFO thermal mode -> Performance
20:59:23  INFO OS power profile -> performance
20:59:23  INFO notified birdman via notify-send: Performance
```

No button was pressed. The register walked `2 -> 0 -> 1 -> 2` in five seconds and
came back to where it started. Nothing in userspace did it: changing the OS power
profile was tested directly and does not move `EC[0x31]` at all, so it is not the
daemon's own `sync_ppd` causing a feedback loop. The trigger looks like whatever
happened to the display, but that was not pinned down, and it is worth being
honest that it was not.

### Why a confirmation timer cannot fix this

The obvious fix — wait for the reading to hold still before believing it — does
not work here, and the timings say why. The individual steps above held for two
and three seconds. Anything short enough to keep the OSD responsive is therefore
*also* short enough to be fooled by a single step, and anything long enough to
reject them would delay a genuine press by four seconds.

What does separate them is the **ending**: a press settles somewhere and stays
there, while this walked and returned. `ModeTracker` in the daemon uses that:

* A change is reported **at once**, because a real press has to feel instant.
* That opens a *burst*. Further changes within `FLAP_WINDOW` (6 s) are the same
event and are followed silently, so the steps in between produce no toasts.
* A burst closes once the reading has held for `SETTLE_SECONDS` (2.6 s) *and* it
  has been open for `MIN_BURST_SECONDS` (4 s). The second condition is what stops
a slow flare from being closed again between two of its own steps; it delays only
  the *decision*, never the first report.
* If it closed where it began, that was a flap, not a press: the OS profile and
  the published state are put back and nothing further is announced.

One notification is still unavoidable, because the first reading of a burst is
indistinguishable from a press at the moment it arrives. What the tracker removes
is the cascade — three toasts, three power-profile changes, and a tray icon
showing a mode the machine was not in.

The step that is easy to get wrong is the second report. Announcing again when a
burst settles looks harmless and is not: for an ordinary single change the
settled value is exactly the one already reported, so every press produced *two*
notifications. The tracker therefore remembers what it last told the caller to
announce, and only asks for another one when the burst landed somewhere the user
has not been shown. `tests/test_daemon_modes.py` pins both halves of that down.

## 14. Two layers of configuration

There is a system config and a per-user one, and the split is not decoration:

| | `/etc/evo-x2-linux-osd.conf` | `~/.config/evo-x2-linux-osd/settings.conf` |
|---|---|---|
| Written by | the installer and the administrator | the Thermal Mode window |
| Needs root | yes | no |
| Applies to | every user | that user |
| Can set | everything | four keys |

The daemon runs as root, so editing the system file from the window would mean a
password prompt on every toggle. Instead the daemon reads the *session user's*
file — found from their home directory, since it does not inherit their
environment — and overlays it on the administrator's. The overlay is filtered
through `evo_x2_settings.KEYS`, so a hand-written file still cannot change which
user is notified, which icons are used or whether the OS profile is mirrored.

The daemon re-reads that file on every pass rather than capturing it once, so a
switch applies to the next mode change instead of the next restart. Command line
flags such as `--no-notify` are applied after the overlay, so an administrator is
not overruled by someone's window.

### A restore is not a button press

The daemon samples the mode once a pass, so a mode it wrote itself comes back a
second later as a reading it cannot tell from the button. Two things followed
from that, both wrong: the restore was announced as a mode change — a toast
about the mode you picked yesterday, at every login — and its `handle_change`
published the state file as a side effect, so suppressing the toast alone would
have left the tray showing the mode the restore had just moved away from.

A restore now does deliberately what a press does as a side effect: mirror the
OS profile, write the state file, and hand the restored mode to the tracker as
the new baseline (`ModeTracker.adopt`) so the next pass sees no change. The
reading already in hand is discarded at the same time, because it was sampled
before the restore and is a reading of the mode just left.

### The login autostart is not in either file

"Start the tray icon at login" writes
`~/.config/autostart/org.evox2.Control.desktop` instead. A user autostart file
*replaces* the system one of the same name rather than merging with it, so the
override is authoritative whenever it exists and only `Hidden=true` means "do not
run". Turning the switch back on removes the override so the installed entry
applies again — and if there is no installed entry, a complete one is written,
because otherwise clearing the override would leave the tray with no way to
start. There is deliberately no settings key mirroring this state: two records of
one fact would eventually disagree.

