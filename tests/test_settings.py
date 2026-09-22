#!/usr/bin/env python3
"""
Unit tests for the per-user settings layer.

Everything here runs against a temporary HOME, so nothing touches the real
config, the real autostart directory or the real daemon:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import configparser
import os
import sys
import tempfile
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "lib"))

import evo_x2_settings as settings  # noqa: E402


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class TestPaths(TempHomeCase):
    def test_path_is_under_the_users_config(self):
        path = settings.settings_path(home=self.home)
        self.assertEqual(
            path, os.path.join(self.home, ".config", "evo-x2-linux-osd",
                               "settings.conf"))

    def test_xdg_config_home_is_honoured_without_a_home(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.home, "xdg")
        try:
            self.assertTrue(
                settings.settings_path().startswith(
                    os.path.join(self.home, "xdg", "evo-x2-linux-osd")))
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old

    def test_autostart_override_is_the_app_id_desktop_file(self):
        self.assertTrue(
            settings.autostart_path(home=self.home).endswith(
                "/.config/autostart/org.evox2.Control.desktop"))


class TestSettingsFile(TempHomeCase):
    def test_defaults_when_there_is_no_file(self):
        prefs = settings.Settings(home=self.home)
        self.assertFalse(prefs.exists)
        self.assertEqual(prefs.get("show_notifications"), "yes")
        self.assertEqual(prefs.get("poll_interval"), "1.0")
        self.assertFalse(prefs.get_bool("restore_on_boot"))
        # Nothing explicit, so nothing would shadow the administrator.
        self.assertEqual(prefs.explicit(), {})

    def test_round_trip(self):
        prefs = settings.Settings(home=self.home)
        prefs.set("show_notifications", False)
        prefs.set("poll_interval", 2.5)
        prefs.set("restore_on_boot", True)
        prefs.set("restore_mode", "performance")
        self.assertTrue(prefs.save())

        again = settings.Settings(home=self.home)
        self.assertEqual(again.explicit(), {
            "show_notifications": "no",
            "poll_interval": "2.5",
            "restore_on_boot": "yes",
            "restore_mode": "performance",
        })
        self.assertFalse(again.get_bool("show_notifications"))
        self.assertEqual(again.get_float("poll_interval"), 2.5)
        self.assertTrue(again.get_bool("restore_on_boot"))

    def test_only_known_keys_are_overridden(self):
        """A key this module does not own must not reach the daemon's config.

        effective() filters through explicit(), so an administrator's
        show_notifications could be overridden but their icon choice cannot.
        """
        prefs = settings.Settings(home=self.home)
        prefs.set("show_notifications", "no")
        prefs.save()
        self.assertEqual(set(prefs.explicit()), {"show_notifications"})

    def test_setting_an_unknown_key_is_refused(self):
        prefs = settings.Settings(home=self.home)
        with self.assertRaises(KeyError):
            prefs.set("icon_quiet", "/tmp/whatever.svg")

    def test_foreign_keys_are_preserved(self):
        prefs = settings.Settings(home=self.home)
        prefs.set("poll_interval", 2.0)
        prefs.save()
        with open(prefs.path, "a", encoding="utf-8") as handle:
            handle.write("future_option = keep me\n")

        again = settings.Settings(home=self.home)
        again.set("poll_interval", 3.0)
        again.save()

        with open(again.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("keep me", text)
        self.assertIn("poll_interval = 3.0", text)
        # ... and it must not leak into the overlay the daemon uses.
        self.assertNotIn("future_option", again.explicit())

    def test_the_header_comment_survives_a_rewrite(self):
        prefs = settings.Settings(home=self.home)
        prefs.set("poll_interval", 2.0)
        prefs.save()
        with open(prefs.path, encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("# Preferences"))

    def test_a_corrupt_file_falls_back_to_defaults(self):
        prefs = settings.Settings(home=self.home)
        os.makedirs(os.path.dirname(prefs.path), exist_ok=True)
        with open(prefs.path, "w", encoding="utf-8") as handle:
            handle.write("this is not ini at all [[[\n")
        broken = settings.Settings(home=self.home)
        self.assertEqual(broken.explicit(), {})
        self.assertEqual(broken.get("poll_interval"), "1.0")

    def test_reload_if_changed_notices_an_edit(self):
        prefs = settings.Settings(home=self.home)
        self.assertFalse(prefs.reload_if_changed())

        # Exactly what the daemon writing restore_mode looks like.
        other = settings.Settings(home=self.home)
        other.set("restore_mode", "quiet")
        other.save()

        self.assertTrue(prefs.reload_if_changed())
        self.assertEqual(prefs.get("restore_mode"), "quiet")
        self.assertFalse(prefs.reload_if_changed())

    def test_a_nonsense_number_does_not_raise(self):
        prefs = settings.Settings(home=self.home)
        prefs.set("poll_interval", "not a number")
        self.assertEqual(prefs.get_float("poll_interval", fallback=1.0), 1.0)

    def test_as_bool(self):
        for value in ("yes", "YES", "true", "1", "on", "y"):
            self.assertTrue(settings.as_bool(value), value)
        for value in ("no", "false", "0", "off", "", None, "maybe"):
            self.assertFalse(settings.as_bool(value), value)
        self.assertTrue(settings.as_bool(None, default=True))


class TestAutostart(TempHomeCase):
    def setUp(self):
        super().setUp()
        self._system = tempfile.TemporaryDirectory()
        self._saved = settings.SYSTEM_AUTOSTART_DIR
        settings.SYSTEM_AUTOSTART_DIR = self._system.name
        self.system_entry = os.path.join(self._system.name,
                                         settings.AUTOSTART_NAME)

    def tearDown(self):
        settings.SYSTEM_AUTOSTART_DIR = self._saved
        self._system.cleanup()
        super().tearDown()

    def test_nothing_installed_and_no_override_means_off(self):
        self.assertFalse(settings.autostart_enabled(home=self.home))

    def test_a_system_entry_means_on(self):
        open(self.system_entry, "w").close()
        self.assertTrue(settings.autostart_enabled(home=self.home))

    def test_disabling_writes_a_hidden_override(self):
        open(self.system_entry, "w").close()
        self.assertTrue(settings.set_autostart(False, home=self.home))
        self.assertFalse(settings.autostart_enabled(home=self.home))

        # The override has to be a valid desktop entry, or a desktop that
        # validates its autostart files would complain about it.
        parser = configparser.ConfigParser()
        parser.read(settings.autostart_path(home=self.home))
        self.assertTrue(parser.has_section("Desktop Entry"))
        self.assertEqual(parser.get("Desktop Entry", "Type"), "Application")
        self.assertTrue(settings.as_bool(
            parser.get("Desktop Entry", "Hidden")))

    def test_enabling_removes_the_override_when_a_system_entry_exists(self):
        open(self.system_entry, "w").close()
        settings.set_autostart(False, home=self.home)
        self.assertFalse(settings.autostart_enabled(home=self.home))

        self.assertTrue(settings.set_autostart(True, home=self.home))
        self.assertFalse(os.path.exists(settings.autostart_path(self.home)))
        self.assertTrue(settings.autostart_enabled(home=self.home))

    def test_enabling_writes_an_entry_when_there_is_no_system_one(self):
        """Clearing the override must not leave the tray with nothing to start.

        A distribution that packaged the daemon without the autostart file
        would otherwise make the switch a one-way door.
        """
        self.assertTrue(settings.set_autostart(True, home=self.home))
        path = settings.autostart_path(home=self.home)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(settings.autostart_enabled(home=self.home))

        parser = configparser.ConfigParser()
        parser.read(path)
        self.assertEqual(parser.get("Desktop Entry", "Exec"),
                         "evo-x2-control --tray")
        self.assertFalse(settings.as_bool(
            parser.get("Desktop Entry", "Hidden", fallback="false")))

    def test_the_override_is_authoritative_over_the_system_entry(self):
        """Per the XDG spec a user entry replaces, rather than merges with,
        the system one -- so a user file that is not hidden must win."""
        open(self.system_entry, "w").close()
        path = settings.autostart_path(home=self.home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[Desktop Entry]\nType=Application\nName=x\n"
                         "Exec=true\n")
        self.assertTrue(settings.autostart_enabled(home=self.home))

    def test_an_unreadable_override_is_not_fatal(self):
        open(self.system_entry, "w").close()
        path = settings.autostart_path(home=self.home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not ini [[[\n")
        # Unparseable is treated as "no Hidden", so the tray still starts.
        self.assertTrue(settings.autostart_enabled(home=self.home))


if __name__ == "__main__":
    unittest.main()
