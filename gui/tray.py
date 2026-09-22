"""
StatusNotifierItem tray icon, spoken directly to D-Bus.

Why hand-roll this instead of using pystray or libayatana-appindicator:

* pystray and the appindicator bindings are extra packages that are not present
  on a stock install, and the appindicator path only works on GNOME with an
  extension enabled.
* ``org.kde.StatusNotifierItem`` plus ``com.canonical.dbusmenu`` is what KDE,
  GNOME (with the AppIndicator extension), Xfce and most status bars actually
  speak, and ``dbus-python`` is already a dependency here.

Only the subset a host really uses is implemented: the item properties, the
Activate/ContextMenu methods, the NewIcon/NewStatus signals, and enough of
dbusmenu for a host to draw and dispatch a menu.
"""

from __future__ import annotations

import logging
import os

import dbus
import dbus.service
from dbus.service import Object as DBusObject
from dbus.service import signal as dbus_signal

PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

SNI_IFACE = "org.kde.StatusNotifierItem"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
#: The spec documents a single object path, but implementations disagree in
#: practice: kded6 exports ``/StatusNotifierWatcher`` while gnome-shell's
#: AppIndicator extension exports ``/org/kde/StatusNotifierWatcher``.  Probe
#: the known candidates instead of assuming one.
WATCHER_PATHS = (
    "/StatusNotifierWatcher",
    "/org/kde/StatusNotifierWatcher",
)
WATCHER_IFACE = "org.kde.StatusNotifierWatcher"

MENU_IFACE = "com.canonical.dbusmenu"

_log = logging.getLogger("evo-x2.tray")

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

#: dbusmenu property names, and the default when a host asks for "all".
_MENU_PROPERTY_NAMES = ("label", "enabled", "visible", "type",
                        "toggle-type", "toggle-state", "icon-name",
                        "children-display")


class MenuItem:
    """One row of the tray menu.

    `kind` is either "standard" or "separator".  `toggle_type` may be "radio" or
    "checkmark", in which case `toggle_state` (0 or 1) marks the current one.
    """

    __slots__ = ("id", "label", "kind", "enabled", "visible",
                 "toggle_type", "toggle_state", "icon_name", "children")

    def __init__(self, id, label="", kind="standard", enabled=True, visible=True,
                 toggle_type=None, toggle_state=0, icon_name=None, children=None):
        self.id = int(id)
        self.label = label
        self.kind = kind
        self.enabled = enabled
        self.visible = visible
        self.toggle_type = toggle_type
        self.toggle_state = int(toggle_state)
        self.icon_name = icon_name
        self.children = list(children or [])

    def as_dict(self, requested=None) -> dict:
        properties = {
            "label": self.label,
            "enabled": self.enabled,
            "visible": self.visible,
            "type": self.kind,
        }
        if self.toggle_type:
            properties["toggle-type"] = self.toggle_type
            properties["toggle-state"] = self.toggle_state
        if self.icon_name:
            properties["icon-name"] = self.icon_name
        if requested:
            properties = {key: value for key, value in properties.items()
                          if key in requested}
        return properties


def _as_variant(value):
    """Wrap a plain Python value so dbus-python sends it as a D-Bus variant."""
    if isinstance(value, bool):
        return dbus.Boolean(value, variant_level=1)
    if isinstance(value, int):
        return dbus.Int32(value, variant_level=1)
    return dbus.String(str(value), variant_level=1)


def _layout_node(item, requested=None, as_variant=False):
    """dbusmenu layout node: (id, properties, [variant(child), ...]).

    dbus-python has no variant wrapper class -- it spells a variant as a value
    carrying variant_level >= 1 -- so child nodes are constructed with it set
    and the top-level node, which must not be a variant, without.
    """
    children = dbus.Array(
        [_layout_node(child, requested, True) for child in item.children],
        signature="v")
    return dbus.Struct(
        (dbus.Int32(item.id), dbus.Dictionary(item.as_dict(requested), "sv"),
         children),
        signature="ia{sv}av",
        variant_level=1 if as_variant else 0)


class _StatusNotifierItem(DBusObject):
    def __init__(self, bus_name, path, title, icon_name, activate, context_menu):
        super().__init__(bus_name, path)
        self._title = title
        self._icon_name = icon_name
        self._activate = activate
        self._context_menu = context_menu

    # dbus-python 1.x exposes no property decorator, so the whole
    # org.freedesktop.DBus.Properties interface is written out by hand.  The
    # values are built with variant_level=1 because Get returns a variant and
    # GetAll returns a{sv}.
    def _all_properties(self) -> dict:
        return {
            "Category": dbus.String("ApplicationStatus", variant_level=1),
            "Id": dbus.String("evo-x2-control", variant_level=1),
            "Title": dbus.String(self._title, variant_level=1),
            "Status": dbus.String("Active", variant_level=1),
            "IconName": dbus.String(self._icon_name, variant_level=1),
            "IconThemePath": dbus.String("", variant_level=1),
            "Menu": dbus.ObjectPath(MENU_PATH, variant_level=1),
            "ItemIsMenu": dbus.Boolean(False, variant_level=1),
            "WindowId": dbus.UInt32(0, variant_level=1),
            "IconPixmap": dbus.Array([], signature="(iiay)", variant_level=1),
            "ToolTip": dbus.Struct(
                ("", dbus.Array([], signature="(iiay)"), self._title, ""),
                signature="sa(iiay)ss", variant_level=1),
        }

    @dbus.service.method(PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        return self._all_properties().get(str(name),
                                          dbus.String("", variant_level=1))

    @dbus.service.method(PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return dbus.Dictionary(self._all_properties(), signature="sv")

    @dbus.service.method(PROPERTIES_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface, name, value):
        raise dbus.exceptions.DBusException(
            f"{name} is read-only",
            name="org.freedesktop.DBus.Error.PropertyReadOnly")

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def Activate(self, x, y):
        self._activate()

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        self._activate()

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        self._context_menu()

    @dbus.service.method(SNI_IFACE, in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    @dbus_signal(SNI_IFACE, signature="")
    def NewIcon(self):
        pass

    @dbus_signal(SNI_IFACE, signature="")
    def NewTitle(self):
        pass

    @dbus_signal(SNI_IFACE, signature="s")
    def NewStatus(self, status):
        pass

    @dbus_signal(SNI_IFACE, signature="")
    def NewToolTip(self):
        pass

    def set_icon(self, icon_name) -> None:
        self._icon_name = icon_name
        self.NewIcon()

    def set_title(self, title) -> None:
        self._title = title
        self.NewTitle()


class _Menu(DBusObject):
    def __init__(self, bus_name, path, get_items, on_event):
        super().__init__(bus_name, path)
        self._get_items = get_items
        self._on_event = on_event
        self.revision = 1

    def _find(self, item_id, items=None):
        for item in (items if items is not None else self._get_items()):
            if item.id == item_id:
                return item
            found = self._find(item_id, item.children)
            if found is not None:
                return found
        return None

    def refresh(self) -> None:
        self.revision += 1
        self.LayoutUpdated(dbus.UInt32(self.revision), dbus.Int32(0))

    @dbus.service.method(MENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        requested = [str(name) for name in property_names] or None
        if parent_id == 0:
            root = MenuItem(0, children=self._get_items())
        else:
            root = self._find(int(parent_id)) or MenuItem(int(parent_id))
        return (dbus.UInt32(self.revision), _layout_node(root, requested))

    @dbus.service.method(MENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, property_names):
        requested = [str(name) for name in property_names] or None
        wanted = [int(item_id) for item_id in ids]
        result = []
        for item in self._get_items():
            if item.id in wanted or not wanted:
                result.append((dbus.Int32(item.id),
                               dbus.Dictionary(item.as_dict(requested), "sv")))
        return dbus.Array(result, signature="(ia{sv})")

    @dbus.service.method(MENU_IFACE, in_signature="is", out_signature="v")
    def GetProperty(self, item_id, name):
        item = self._find(int(item_id))
        values = item.as_dict() if item else {}
        return _as_variant(values.get(str(name), ""))

    @dbus.service.method(MENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, item_id):
        return False

    @dbus.service.method(MENU_IFACE, in_signature="ai", out_signature="ba(ia{sv})")
    def AboutToShowGroup(self, ids):
        return False, dbus.Array([], signature="(ia{sv})")

    @dbus.service.method(MENU_IFACE, in_signature="isvu", out_signature="")
    def Event(self, item_id, event_id, data, timestamp):
        if str(event_id) == "clicked":
            self._on_event(int(item_id))

    @dbus_signal(MENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass


    @dbus_signal(MENU_IFACE, signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated, removed):
        pass


class Tray:
    """A tray icon, or a clear explanation of why there isn't one.

    `get_items` and `on_event` are supplied by the application; `on_event`
    receives the clicked item's id.  `on_activate` is what a left click does.
    """

    def __init__(self, title, icon_name, on_activate, get_items, on_event,
                 bus=None):
        self.title = title
        self.icon_name = icon_name
        self.available = False
        self.reason = "not started"
        self._on_activate = on_activate
        self._get_items = get_items
        self._on_event = on_event
        self._bus = bus
        self._item = None
        self._menu = None
        self._bus_name = None

    def start(self) -> bool:
        """Own the item name, export the objects and register with the host."""
        try:
            if self._bus is None:
                self._bus = dbus.SessionBus()
        except dbus.exceptions.DBusException as exc:
            self.reason = f"no session bus: {exc.get_dbus_message()}"
            return False

        # A unique-ish name per process is the conventional SNI naming scheme.
        # It is not released on the failure paths below: dbus-python caches a
        # BusName per process and hands the same instance back, so re-owning it
        # on a later attempt succeeds rather than raising NameExistsException.
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        try:
            self._bus_name = dbus.service.BusName(name, self._bus,
                                                  do_not_queue=True)
        except dbus.exceptions.NameExistsException:
            self.reason = f"{name} is already owned"
            return False

        # These objects claim fixed paths on this connection, so every failure
        # from here on has to unexport them again -- see _release().
        self._item = _StatusNotifierItem(self._bus_name, SNI_PATH, self.title,
                                         self.icon_name, self._on_activate,
                                         self._on_activate)
        self._menu = _Menu(self._bus_name, MENU_PATH, self._get_items_safe,
                           self._on_event_safe)

        watcher, detail = self._find_watcher()
        if watcher is None:
            self.reason = ("no StatusNotifierWatcher on the session bus "
                           f"({detail}) -- the desktop has no system tray")
            self._release()
            return False

        try:
            properties = dbus.Interface(watcher, "org.freedesktop.DBus.Properties")
            host_present = bool(properties.Get(WATCHER_IFACE,
                                               "IsStatusNotifierHostRegistered"))
        except dbus.exceptions.DBusException as exc:
            self.reason = ("the StatusNotifierWatcher stopped answering "
                           f"({exc.get_dbus_name()})")
            self._release()
            return False

        if not host_present:
            self.reason = "a watcher exists but no tray host is registered"
            self._release()
            return False

        try:
            dbus.Interface(watcher, WATCHER_IFACE).RegisterStatusNotifierItem(name)
        except dbus.exceptions.DBusException as exc:
            self.reason = f"could not register with the watcher: {exc.get_dbus_message()}"
            self._release()
            return False

        self.available = True
        self.reason = "registered"
        return True

    def _release(self) -> None:
        """Unexport the objects and forget the bus name.

        Called on every failure path as well as by stop().  It has to be: the
        object paths are fixed, and dbus-python raises KeyError rather than
        replacing a handler, so a retry that skipped this would die with a
        traceback inside a GLib timeout instead of re-registering.
        """
        for obj in (self._menu, self._item):
            if obj is not None:
                try:
                    obj.remove_from_connection()
                except Exception:      # noqa: BLE001
                    pass
        self._menu = None
        self._item = None
        self._bus_name = None
        self.available = False

    def _find_watcher(self):
        """Locate the watcher object, or return ``(None, why)``.

        Each candidate is queried rather than merely resolved, so a path that
        exists but does not implement the interface is rejected too.
        """
        last_error = "no known object path answered"
        for path in WATCHER_PATHS:
            try:
                watcher = self._bus.get_object(WATCHER_NAME, path)
                dbus.Interface(watcher,
                               "org.freedesktop.DBus.Properties").Get(
                                   WATCHER_IFACE, "IsStatusNotifierHostRegistered")
            except dbus.exceptions.DBusException as exc:
                last_error = f"{path}: {exc.get_dbus_name()}"
                continue
            return watcher, path
        return None, last_error

    def _get_items_safe(self):
        # A broken menu must not take the icon down with it -- but it must not be
        # invisible either, or a host shows a plausible-looking placeholder menu
        # and the bug goes unnoticed.  Hence: fall back *and* log.
        try:
            return self._get_items()
        except Exception:      # noqa: BLE001
            _log.exception("building the tray menu failed; showing a placeholder")
            return [MenuItem(9999, label="Menu unavailable", enabled=False)]

    def _on_event_safe(self, item_id):
        try:
            self._on_event(item_id)
        except Exception:      # noqa: BLE001
            _log.exception("tray menu event %s failed", item_id)
        # Refreshed even after a failure: a partial mode change still moved the
        # tick marks, so the host needs to redraw.
        self.refresh()

    # -- updates -----------------------------------------------------------

    def set_icon(self, icon_name) -> None:
        self.icon_name = icon_name
        if self._item is not None:
            try:
                self._item.set_icon(icon_name)
            except dbus.exceptions.DBusException:
                pass

    def set_title(self, title) -> None:
        self.title = title
        if self._item is not None:
            try:
                self._item.set_title(title)
            except dbus.exceptions.DBusException:
                pass

    def refresh(self) -> None:
        """Tell the host the menu changed (tick marks moved, labels changed)."""
        if self._menu is not None:
            try:
                self._menu.refresh()
            except dbus.exceptions.DBusException:
                pass

    def stop(self) -> None:
        self._release()
