#!/usr/bin/env python3
"""
Unit tests for the StatusNotifierItem tray backend and the GUI's tray logic.

Neither the tray nor the GUI is an importable module (one is a plain script with
a hyphen in its name), so both are loaded by path.  Nothing here opens a real
session bus: only the pure parts are exercised -- the dbusmenu payloads, the
callback plumbing, and the icon assets.

The callback-storage test exists because that bug actually shipped: the
constructor accepted `get_items` and `on_event` and never stored them, so every
menu request raised AttributeError inside a broad except, and a tray host
displayed a plausible-looking "Menu unavailable" placeholder instead of failing.
The icon-asset tests exist for the mirror-image reason: the tray first used
power-profile-*-symbolic, which Breeze does not ship, so the icon was invisible
on KDE; and the replacement icons were written with `currentColor`, which Qt's
SVG parser does not implement, so plasmashell drew them solid black.  Both
failure modes are silent, hence the checks on the shipped files themselves:
an explicit colour per shape, no `currentColor` anywhere, and enough contrast
against a dark panel and a light one.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import glob
import importlib.machinery
import importlib.util
import logging
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ElementTree
from unittest import mock

REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "lib"))
sys.path.insert(0, os.path.join(REPO_DIR, "gui"))

import dbus  # noqa: E402

import evo_x2_hw as hw  # noqa: E402


def _load(name, *parts):
    path = os.path.join(REPO_DIR, *parts)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


tray_mod = _load("evo_x2_tray", "gui", "tray.py")
gui = _load("evo_x2_control", "gui", "evo-x2-control")

# The placeholder path logs on purpose.  Silence it except where a test asserts
# on the log record itself.
logging.getLogger("evo-x2.tray").addHandler(logging.NullHandler())
logging.getLogger("evo-x2.tray").propagate = False

Tray = tray_mod.Tray
MenuItem = tray_mod.MenuItem

BALANCED = hw.mode_from_index(1)


def _relative_luminance(colour):
    """WCAG relative luminance of a #rrggbb string."""
    channels = []
    for offset in (1, 3, 5):
        value = int(colour[offset:offset + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.04045
                        else ((value + 0.055) / 1.055) ** 2.4)
    return (0.2126 * channels[0] + 0.7152 * channels[1]
            + 0.0722 * channels[2])


def _contrast(first, second):
    """WCAG contrast ratio between two #rrggbb strings."""
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")
_SHAPES = ("rect", "circle", "ellipse", "path", "polygon", "line")


def _paints(element):
    """Every explicit colour an element draws itself with.

    Both attributes are read, because an unfilled outline (a ring) carries its
    colour in `stroke` and nothing in `fill`.
    """
    return {value.lower()
            for attribute in ("fill", "stroke")
            for value in [element.get(attribute) or ""]
            if _HEX.match(value)}


def make_tray(get_items=None, on_event=None, on_activate=None):
    return Tray(
        title="Thermal mode: Balanced",
        icon_name="evo-x2-balanced",
        on_activate=on_activate or (lambda: None),
        get_items=get_items or (lambda: []),
        on_event=on_event or (lambda item_id: None),
    )


class TestCallbacksAreStored(unittest.TestCase):
    """Regression: the constructor used to drop both callbacks on the floor."""

    def test_get_items_is_kept(self):
        items = [MenuItem(1, label="Open")]
        tray = make_tray(get_items=lambda: items)
        self.assertIs(tray._get_items(), items)

    def test_on_event_is_kept(self):
        seen = []
        tray = make_tray(on_event=seen.append)
        tray._on_event(7)
        self.assertEqual(seen, [7])

    def test_on_activate_is_kept(self):
        calls = []
        tray = make_tray(on_activate=lambda: calls.append(1))
        self.assertEqual(tray._on_activate(), calls.append(1))

    def test_menu_receives_a_working_callback(self):
        """The path the host actually takes: _Menu -> _get_items_safe."""
        items = [MenuItem(1, label="Open")]
        tray = make_tray(get_items=lambda: items)
        menu = tray_mod._Menu.__new__(tray_mod._Menu)
        menu._get_items = tray._get_items_safe
        self.assertEqual(menu._get_items(), items)


class TestMenuFallback(unittest.TestCase):
    def test_items_pass_through(self):
        items = [MenuItem(1, label="Open")]
        tray = make_tray(get_items=lambda: items)
        self.assertEqual(tray._get_items_safe(), items)

    def test_a_broken_menu_yields_a_placeholder_instead_of_raising(self):
        def explode():
            raise AttributeError("'Tray' object has no attribute '_get_items'")

        tray = make_tray(get_items=explode)
        with self.assertLogs("evo-x2.tray", level="ERROR"):
            placeholder = tray._get_items_safe()
        self.assertEqual(len(placeholder), 1)
        self.assertEqual(placeholder[0].label, "Menu unavailable")
        self.assertFalse(placeholder[0].enabled)

    def test_the_placeholder_id_cannot_collide_with_a_real_item(self):
        app = gui.ControlApplication()
        app._current_mode = 1
        real_ids = {item.id for item in app._menu_items()}
        tray = make_tray(get_items=lambda: (_ for _ in ()).throw(RuntimeError()))
        with self.assertLogs("evo-x2.tray", level="ERROR"):
            placeholder = tray._get_items_safe()
        self.assertNotIn(placeholder[0].id, real_ids)

    def test_events_forward_to_the_application(self):
        seen = []
        tray = make_tray(on_event=seen.append)
        tray._menu = mock.Mock()
        tray._on_event_safe(11)
        self.assertEqual(seen, [11])
        tray._menu.refresh.assert_called_once()

    def test_a_failing_handler_is_reported_and_the_menu_still_refreshes(self):
        def explode(item_id):
            raise RuntimeError(f"mode {item_id} refused")

        tray = make_tray(on_event=explode)
        tray._menu = mock.Mock()
        with self.assertLogs("evo-x2.tray", level="ERROR"):
            tray._on_event_safe(11)
        # A partially applied mode change still moved the tick marks.
        tray._menu.refresh.assert_called_once()

    def test_a_refresh_failure_does_not_escape(self):
        tray = make_tray()
        tray._menu = mock.Mock()
        tray._menu.refresh.side_effect = dbus.exceptions.DBusException(
            "bus went away")
        tray._on_event_safe(1)      # must not raise


class TestMenuItem(unittest.TestCase):
    def test_separator_has_no_toggle_properties(self):
        props = MenuItem(2, kind="separator").as_dict()
        self.assertEqual(props["type"], "separator")
        self.assertNotIn("toggle-type", props)
        self.assertNotIn("toggle-state", props)

    def test_radio_carries_both_toggle_properties(self):
        props = MenuItem(11, label="Balanced", toggle_type="radio",
                         toggle_state=1).as_dict()
        self.assertEqual(props["toggle-type"], "radio")
        self.assertEqual(props["toggle-state"], 1)

    def test_checkmark_off_is_state_zero(self):
        props = MenuItem(11, label="Balanced", toggle_type="radio",
                         toggle_state=0).as_dict()
        self.assertEqual(props["toggle-state"], 0)

    def test_requested_properties_are_filtered(self):
        item = MenuItem(11, label="Balanced", toggle_type="radio", toggle_state=1)
        self.assertEqual(item.as_dict(["label"]), {"label": "Balanced"})

    def test_unrequested_icon_is_omitted(self):
        self.assertNotIn("icon-name", MenuItem(1, label="Open").as_dict())

    def test_requested_icon_is_included(self):
        item = MenuItem(1, label="Open", icon_name="evo-x2-control")
        self.assertEqual(item.as_dict(["icon-name"]),
                         {"icon-name": "evo-x2-control"})


class TestLayout(unittest.TestCase):
    def test_root_is_a_struct_with_int_id_and_children(self):
        node = tray_mod._layout_node(MenuItem(0, children=[MenuItem(1, label="A")]))
        self.assertEqual(node.signature, "ia{sv}av")
        self.assertEqual(node[0], 0)
        self.assertEqual(len(node[2]), 1)

    def test_children_are_variants(self):
        node = tray_mod._layout_node(MenuItem(0, children=[MenuItem(1, label="A")]),
                                     None)
        child = node[2][0]
        self.assertIsInstance(child, dbus.Struct)
        self.assertEqual(child.variant_level, 1)
        self.assertEqual(child.signature, "ia{sv}av")

    def test_a_leaf_has_no_children(self):
        node = tray_mod._layout_node(MenuItem(5, label="leaf"))
        self.assertEqual(len(node[2]), 0)

    def test_property_filter_reaches_children(self):
        node = tray_mod._layout_node(
            MenuItem(0, children=[MenuItem(1, label="A", toggle_type="radio",
                                           toggle_state=1)]),
            ["label"])
        self.assertEqual(dict(node[2][0][1]), {"label": "A"})


class TestIconAssets(unittest.TestCase):
    """Every icon name the code uses must exist in the installed icon theme.

    This is the check that would have caught the original bug: the tray
    referenced power-profile-*-symbolic, which neither hicolor nor Breeze ships,
    so plasmashell had nothing to draw.
    """

    def shipped_icons(self, name):
        return glob.glob(os.path.join(REPO_DIR, "data", "icons", "hicolor", "*",
                                      "apps", f"{name}.svg"))

    def test_every_mode_has_an_icon(self):
        for mode in hw.MODES:
            self.assertIn(mode.index, gui.MODE_ICONS,
                          f"mode {mode.index} ({mode.name}) has no tray icon")

    def test_no_mode_borrows_a_theme_icon_name(self):
        for name in gui.MODE_ICONS.values():
            self.assertTrue(name.startswith("evo-x2-"),
                            f"{name} must be shipped by this package, not by the "
                            f"icon theme")

    def test_every_referenced_icon_is_shipped(self):
        names = set(gui.MODE_ICONS.values())
        names.add(gui.ICON_UNKNOWN)
        names.add(gui.APP_ICON)
        for name in sorted(names):
            self.assertTrue(self.shipped_icons(name),
                            f"{name}.svg is referenced but not in data/icons/")

    def test_shipped_icons_are_valid_svg(self):
        root = os.path.join(REPO_DIR, "data", "icons", "hicolor")
        files = glob.glob(os.path.join(root, "*", "apps", "*.svg"))
        self.assertGreaterEqual(len(files), len(gui.MODE_ICONS) + 2)
        for path in files:
            with self.subTest(icon=os.path.basename(path)):
                tree = ElementTree.parse(path)
                self.assertEqual(tree.getroot().tag,
                                 "{http://www.w3.org/2000/svg}svg")

    def mode_icon_paths(self):
        """The icons the tray and the notifications draw.

        These are transparent, so the panel shows through and the shapes alone
        have to carry the contrast.  So every colour in them must clear 3:1
        against *both* panel colours.
        """
        names = sorted(set(gui.MODE_ICONS.values()) | {gui.ICON_UNKNOWN})
        return [self.shipped_icons(name)[0] for name in names]

    def app_icon_path(self):
        """The application icon: menu entry, window list and task manager.

        A different asset with different rules from the mode icons above.  It
        is opaque, so what the panel samples is the badge; the bars drawn on
        the badge only have to be legible against the badge.
        """
        paths = self.shipped_icons(gui.APP_ICON)
        self.assertTrue(paths, f"{gui.APP_ICON}.svg is not shipped")
        return paths[0]

    def test_no_shipped_icon_uses_current_colour(self):
        """Qt cannot parse `currentColor` and falls back to black.

        That is the whole bug: plasmashell renders tray and notification icons
        with libqsvg, which has no `currentColor` support at all, so a symbolic
        icon is drawn as an opaque black blob whatever the panel is doing.

        Read from the parsed tree, not the raw file: the comments deliberately
        name `currentColor` while explaining why it must not be used.
        """
        root = os.path.join(REPO_DIR, "data", "icons", "hicolor")
        files = glob.glob(os.path.join(root, "*", "apps", "*.svg"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(icon=os.path.basename(path)):
                for element in ElementTree.parse(path).iter():
                    for value in (element.get("fill"), element.get("stroke"),
                                  element.get("style")):
                        self.assertNotIn("currentColor", value or "")

    def test_every_shape_states_its_own_colour(self):
        """No shape may inherit a colour, and none may be left unpainted."""
        for path in self.mode_icon_paths() + [self.app_icon_path()]:
            with self.subTest(icon=os.path.basename(path)):
                shapes = [element for element in ElementTree.parse(path).iter()
                          if element.tag.rsplit("}", 1)[-1] in _SHAPES]
                self.assertTrue(shapes, "icon draws nothing")
                for shape in shapes:
                    tag = shape.tag.rsplit("}", 1)[-1]
                    with self.subTest(shape=tag, fill=shape.get("fill")):
                        self.assertTrue(_paints(shape),
                                        f"<{tag}> has no explicit colour")
                        if (shape.get("fill") or "") == "none":
                            self.assertTrue(
                                _HEX.match(shape.get("stroke") or ""),
                                f"<{tag}> is unfilled and so needs a stroked "
                                f"colour, or it is invisible")

    def test_mode_icons_are_visible_on_dark_and_light_panels(self):
        """A mid-tone is the only way to survive both themes.

        A bright icon vanishes on a light panel, a dark one on a dark panel, and
        neither failure produces an error -- just a hole in the panel.  3:1 is
        the WCAG minimum for a graphical object.
        """
        panels = {"dark": "#1b1e23", "light": "#ffffff"}
        for path in self.mode_icon_paths():
            colours = {colour for element in ElementTree.parse(path).iter()
                       for colour in _paints(element)}
            self.assertTrue(colours, "icon has no explicit colour to judge")
            for colour in colours:
                for panel, background in panels.items():
                    with self.subTest(icon=os.path.basename(path),
                                      colour=colour, panel=panel):
                        ratio = _contrast(colour, background)
                        self.assertGreaterEqual(
                            ratio, 3.0,
                            f"{colour} on a {panel} panel is {ratio:.2f}:1")

    def test_the_application_icon_has_a_mid_tone_anchor(self):
        """The badge is what the panel sees, so it must survive both themes.

        This is the regression test for the black square in the task manager.
        The first application icon was a dark slate gradient (#39435a to
        #1a1f27): against a dark panel that is 1.5:1, and against a white one
        10.7:1, so it was legible on exactly one theme and drew a black box on
        the other -- silently, because a low-contrast icon is not an error.
        """
        panels = {"dark": "#1b1e23", "light": "#ffffff"}
        tree = ElementTree.parse(self.app_icon_path())
        colours = {colour for element in tree.iter()
                   for colour in _paints(element)}
        anchors = [colour for colour in colours
                   if all(_contrast(colour, background) >= 3.0
                          for background in panels.values())]
        self.assertTrue(
            anchors,
            f"no colour in {gui.APP_ICON}.svg clears 3:1 against both a dark "
            f"and a light panel, so it will be invisible on one of them; "
            f"colours are {sorted(colours)}")

    def test_every_colour_in_the_application_icon_is_legible_against_something(self):
        """Nothing may be drawn in a colour that is invisible where it sits.

        A bar on the badge is judged against the badge, the badge itself
        against the panels.  A colour that fails all of those is a shape the
        user cannot see at all.
        """
        panels = {"#1b1e23": "dark panel", "#ffffff": "light panel"}
        tree = ElementTree.parse(self.app_icon_path())
        colours = sorted({colour for element in tree.iter()
                          for colour in _paints(element)})
        self.assertTrue(colours, "the application icon has no explicit colour")
        for colour in colours:
            backgrounds = dict(panels)
            backgrounds.update({other: "the badge" for other in colours
                                if other != colour})
            with self.subTest(colour=colour):
                self.assertTrue(
                    any(_contrast(colour, background) >= 3.0
                        for background in backgrounds),
                    f"{colour} is not legible against the panels or against "
                    f"any other colour in {gui.APP_ICON}.svg")

    def test_more_bars_means_a_higher_mode(self):
        """The bar count is the mode, so it has to be ordered and distinct."""
        counts = []
        for index in sorted(gui.MODE_ICONS):
            path = self.shipped_icons(gui.MODE_ICONS[index])[0]
            tree = ElementTree.parse(path)
            bars = [el for el in tree.iter()
                    if el.tag.rsplit("}", 1)[-1] == "rect"]
            counts.append(len(bars))
        self.assertEqual(counts, list(range(1, len(counts) + 1)),
                         f"bar counts per mode are {counts}: they must increase "
                         f"with the mode, one bar per level")


class TestMenuModel(unittest.TestCase):
    """What the tray menu actually offers, built by the real application."""

    def setUp(self):
        self.app = gui.ControlApplication()

    def menu_for(self, index):
        self.app._current_mode = index
        return self.app._menu_items()

    def test_only_documented_modes_are_offered(self):
        offered = {item.id - gui.MODE_ITEM_BASE
                   for item in self.menu_for(1)
                   if item.toggle_type == "radio"}
        self.assertEqual(offered,
                         {mode.index for mode in hw.MODES if mode.documented})

    def test_exactly_the_current_mode_is_ticked(self):
        for index in sorted({m.index for m in hw.MODES if m.documented}):
            with self.subTest(mode=index):
                ticked = [item.id - gui.MODE_ITEM_BASE
                          for item in self.menu_for(index)
                          if item.toggle_type == "radio"
                          and item.toggle_state == 1]
                self.assertEqual(ticked, [index])

    def test_an_unknown_mode_ticks_nothing(self):
        ticked = [item for item in self.menu_for(None)
                  if item.toggle_type == "radio" and item.toggle_state == 1]
        self.assertEqual(ticked, [])

    def test_ids_are_unique(self):
        ids = [item.id for item in self.menu_for(1)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_quit_and_open_are_present(self):
        ids = {item.id for item in self.menu_for(1)}
        self.assertIn(gui.MENU_OPEN, ids)
        self.assertIn(gui.MENU_QUIT, ids)


class TestModeIcons(unittest.TestCase):
    def test_every_documented_mode_has_its_own_icon(self):
        icons = [gui.MODE_ICONS[mode.index]
                 for mode in hw.MODES if mode.documented]
        self.assertEqual(len(icons), len(set(icons)),
                         "modes must be distinguishable by icon alone")

    def test_an_unreadable_mode_still_has_an_icon(self):
        self.assertNotIn(None, gui.MODE_ICONS)
        self.assertTrue(gui.ICON_UNKNOWN)

    def test_the_unknown_icon_differs_from_every_mode_icon(self):
        self.assertNotIn(gui.ICON_UNKNOWN, gui.MODE_ICONS.values())


class TestDesktopEntries(unittest.TestCase):
    """The link that puts the icon in the task manager and the system menu.

    On Wayland a compositor derives the window's app_id from the application id
    and looks for a .desktop file of that name to find the icon.  If the two
    ever drift apart the window shows a generic icon, with no error anywhere.
    """

    def read_entry(self, filename):
        import configparser

        path = os.path.join(REPO_DIR, "data", filename)
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        # Desktop entry keys are case-sensitive; configparser lowercases them
        # unless told not to, which would make every lookup below a KeyError.
        parser.optionxform = str
        parser.read(path)
        return parser["Desktop Entry"], path

    def test_the_menu_entry_is_named_after_the_application_id(self):
        entry, path = self.read_entry(f"{gui.APP_ID}.desktop")
        self.assertEqual(os.path.basename(path), f"{gui.APP_ID}.desktop")
        self.assertEqual(entry["StartupWMClass"], gui.APP_ID)

    def test_the_menu_entry_points_at_the_control_program(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.desktop")
        self.assertEqual(entry["Exec"], "evo-x2-control")

    def test_the_menu_entry_uses_the_shipped_icon(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.desktop")
        self.assertEqual(entry["Icon"], gui.APP_ICON)

    def test_the_menu_entry_is_in_settings(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.desktop")
        categories = entry["Categories"].split(";")
        self.assertIn("Settings", categories)
        # Listing two main categories makes some menus show the entry twice.
        self.assertNotIn("System", categories)

    def test_the_menu_entry_is_not_hidden(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.desktop")
        self.assertNotEqual(entry.get("NoDisplay", "false").lower(), "true")
        self.assertEqual(entry["Type"], "Application")

    def test_the_menu_entry_does_not_open_a_terminal(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.desktop")
        self.assertEqual(entry["Terminal"], "false")

    def test_the_autostart_entry_starts_the_tray_only(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.autostart.desktop")
        self.assertIn("--tray", entry["Exec"])

    def test_the_autostart_entry_is_hidden_from_the_menu(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.autostart.desktop")
        self.assertEqual(entry["NoDisplay"], "true")

    def test_the_autostart_entry_is_enabled(self):
        entry, _ = self.read_entry(f"{gui.APP_ID}.autostart.desktop")
        self.assertNotEqual(entry.get("Hidden", "false").lower(), "true")


class TestTrayRetryPolicy(unittest.TestCase):
    def test_a_missing_watcher_is_worth_retrying(self):
        reason = ("no StatusNotifierWatcher on the session bus "
                  "(org.freedesktop.DBus.Error.UnknownObject) -- the desktop has "
                  "no system tray")
        self.assertTrue(gui.tray_might_appear_later(reason))

    def test_a_watcher_without_a_host_is_worth_retrying(self):
        self.assertTrue(gui.tray_might_appear_later(
            "a watcher exists but no tray host is registered"))

    def test_a_registration_failure_is_worth_retrying(self):
        self.assertTrue(gui.tray_might_appear_later(
            "could not register with the watcher: something"))

    def test_no_session_bus_is_not_worth_retrying(self):
        self.assertFalse(gui.tray_might_appear_later(
            "no session bus: Could not connect: Permission denied"))


class TestWatcherPaths(unittest.TestCase):
    def test_both_known_object_paths_are_probed(self):
        # kded6 exports the first, gnome-shell's AppIndicator extension the
        # second.  Probing only one of them fails on half the desktops.
        self.assertIn("/StatusNotifierWatcher", tray_mod.WATCHER_PATHS)
        self.assertIn("/org/kde/StatusNotifierWatcher", tray_mod.WATCHER_PATHS)

    def test_the_spec_path_is_tried_first(self):
        self.assertEqual(tray_mod.WATCHER_PATHS[0], "/StatusNotifierWatcher")


if __name__ == "__main__":
    unittest.main()
