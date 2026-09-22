#!/usr/bin/env python3
"""
Unit tests for the daemon's notification transports.

The daemon is a script rather than a module, so it is loaded by path.  Nothing
here touches a real session bus, real hardware, or root; the subprocess layer is
stubbed out and only the argument construction and fallback logic are checked.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import collections
import importlib.machinery
import importlib.util
import logging
import os
import sys
import unittest
from unittest import mock

REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "lib"))

import evo_x2_hw as hw  # noqa: E402


def _load_daemon():
    path = os.path.join(REPO_DIR, "daemon", "evo-x2-thermal-osd")
    loader = importlib.machinery.SourceFileLoader("evo_x2_thermal_osd", path)
    spec = importlib.util.spec_from_loader("evo_x2_thermal_osd", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["evo_x2_thermal_osd"] = module
    loader.exec_module(module)
    return module


osd = _load_daemon()

# The fallback paths log warnings on purpose; keep them out of the test output.
logging.getLogger("evo-x2-thermal-osd").addHandler(logging.NullHandler())
logging.getLogger("evo-x2-thermal-osd").propagate = False

FakeUser = collections.namedtuple(
    "FakeUser", "pw_uid pw_gid pw_name pw_dir pw_shell")
USER = FakeUser(1000, 1000, "tester", "/home/tester", "/bin/bash")

BALANCED = hw.mode_from_index(1)
PERFORMANCE = hw.mode_from_index(2)


class TestTimeout(unittest.TestCase):
    def test_default(self):
        self.assertEqual(osd.Notifier({}).timeout_ms(), 3000)

    def test_configured(self):
        self.assertEqual(osd.Notifier({"notify_timeout_ms": "1500"}).timeout_ms(), 1500)

    def test_nonsense_falls_back_to_default(self):
        for bad in ("", "soon", None, "abc"):
            self.assertEqual(osd.Notifier({"notify_timeout_ms": bad}).timeout_ms(), 3000,
                             msg=repr(bad))

    def test_negative_is_clamped(self):
        self.assertEqual(osd.Notifier({"notify_timeout_ms": "-5"}).timeout_ms(), 0)


class TestLibnotifyCommand(unittest.TestCase):
    def setUp(self):
        self.notifier = osd.Notifier({"notify_timeout_ms": "2500"})
        self.command = self.notifier.libnotify_command(BALANCED, "/usr/bin/notify-send")

    def test_binary_is_first(self):
        self.assertEqual(self.command[0], "/usr/bin/notify-send")

    def test_carries_summary_and_body_last(self):
        self.assertEqual(self.command[-2], "Thermal mode: Balanced")
        self.assertEqual(self.command[-1], BALANCED.description)

    def test_icon_and_timeout_come_from_config(self):
        self.assertIn("--icon=" + BALANCED.icon, self.command)
        self.assertIn("--expire-time=2500", self.command)

    def test_requests_replacement_not_stacking(self):
        self.assertIn(
            "--hint=string:x-canonical-private-synchronous:evo-x2-thermal-mode",
            self.command)

    def test_icon_override_is_honoured(self):
        notifier = osd.Notifier({"icon_balanced": "my-icon"})
        self.assertIn("--icon=my-icon", notifier.libnotify_command(BALANCED))


class TestDbusCommand(unittest.TestCase):
    def setUp(self):
        self.notifier = osd.Notifier({"notify_timeout_ms": "2500"})
        self.command = self.notifier.dbus_command(PERFORMANCE, "/usr/bin/busctl")

    def test_targets_the_freedesktop_notifications_service(self):
        self.assertEqual(self.command[0], "/usr/bin/busctl")
        self.assertEqual(self.command[1], "--user")
        self.assertEqual(self.command[2], "call")
        self.assertEqual(self.command[3], "org.freedesktop.Notifications")
        self.assertEqual(self.command[4], "/org/freedesktop/Notifications")
        self.assertEqual(self.command[5], "org.freedesktop.Notifications")
        self.assertEqual(self.command[6], "Notify")

    def test_signature_matches_the_argument_list(self):
        signature = self.command[7]
        # Notify(s app_name, u replaces_id, s app_icon, s summary,
        #        s body, as actions, a{sv} hints, i expire_timeout) -> u
        self.assertEqual(signature, "susssasa{sv}i")

        arguments = self.command[8:]
        self.assertEqual(len(arguments), 8, "one argument per signature field")

        app_name, replaces, icon, summary, body, actions, hints, timeout = arguments
        self.assertEqual(app_name, "Thermal mode")
        self.assertEqual(replaces, "0")
        self.assertEqual(icon, PERFORMANCE.icon)
        self.assertEqual(summary, "Thermal mode: Performance")
        self.assertEqual(body, PERFORMANCE.description)
        self.assertEqual(actions, "0", "empty 'as' is spelled as a bare 0")
        self.assertEqual(hints, "0", "empty 'a{sv}' is spelled as a bare 0")
        self.assertEqual(timeout, "2500")

    def test_replaces_id_is_reused_for_the_osd_effect(self):
        self.notifier.replaces_id = 42
        self.assertEqual(self.notifier.dbus_command(BALANCED)[9], "42")


class TestTransportFallback(unittest.TestCase):
    def test_send_without_a_user_does_nothing(self):
        notifier = osd.Notifier({})
        with mock.patch.object(notifier, "_via_libnotify") as libnotify:
            self.assertFalse(notifier.send(None, BALANCED))
        libnotify.assert_not_called()

    def test_libnotify_is_tried_first(self):
        notifier = osd.Notifier({})
        order = []

        def fake_libnotify(_user, _mode):
            order.append("libnotify")
            return True

        def fake_dbus(_user, _mode):
            order.append("dbus")
            return True

        with mock.patch.object(notifier, "_via_libnotify", fake_libnotify), \
             mock.patch.object(notifier, "_via_dbus", fake_dbus):
            self.assertTrue(notifier.send(USER, BALANCED))
        self.assertEqual(order, ["libnotify"], "must not try the fallback after success")

    def test_falls_back_when_libnotify_fails(self):
        notifier = osd.Notifier({})
        order = []

        def fake_libnotify(_user, _mode):
            order.append("libnotify")
            return False

        def fake_dbus(_user, _mode):
            order.append("dbus")
            return True

        with mock.patch.object(notifier, "_via_libnotify", fake_libnotify), \
             mock.patch.object(notifier, "_via_dbus", fake_dbus):
            self.assertTrue(notifier.send(USER, BALANCED))
        self.assertEqual(order, ["libnotify", "dbus"])

    def test_a_working_fallback_becomes_the_preferred_transport(self):
        notifier = osd.Notifier({})
        notifier.transport = "dbus"
        order = []

        def fake_libnotify(_user, _mode):
            order.append("libnotify")
            return True

        def fake_dbus(_user, _mode):
            order.append("dbus")
            return True

        with mock.patch.object(notifier, "_via_libnotify", fake_libnotify), \
             mock.patch.object(notifier, "_via_dbus", fake_dbus):
            self.assertTrue(notifier.send(USER, BALANCED))
        self.assertEqual(order, ["dbus"], "should stop calling the refused transport first")

    def test_both_transports_failing_is_reported_not_raised(self):
        notifier = osd.Notifier({})
        with mock.patch.object(notifier, "_via_libnotify", lambda *_: False), \
             mock.patch.object(notifier, "_via_dbus", lambda *_: False):
            self.assertFalse(notifier.send(USER, BALANCED))


class TestDbusTransport(unittest.TestCase):
    def test_success_records_the_returned_notification_id(self):
        notifier = osd.Notifier({})
        with mock.patch.object(notifier, "_run", return_value=(0, "u 42\n", "")), \
             mock.patch.object(osd.shutil, "which", return_value="/usr/bin/busctl"):
            self.assertTrue(notifier._via_dbus(USER, BALANCED))
        self.assertEqual(notifier.replaces_id, 42)
        self.assertEqual(notifier.transport, "dbus")

    def test_unparseable_output_still_counts_as_success(self):
        notifier = osd.Notifier({})
        with mock.patch.object(notifier, "_run", return_value=(0, "", "")), \
             mock.patch.object(osd.shutil, "which", return_value="/usr/bin/busctl"):
            self.assertTrue(notifier._via_dbus(USER, BALANCED))
        self.assertEqual(notifier.replaces_id, 0)

    def test_failure_is_returned_not_raised(self):
        notifier = osd.Notifier({})
        with mock.patch.object(notifier, "_run",
                               return_value=(1, "", "Call failed: no such service")), \
             mock.patch.object(osd.shutil, "which", return_value="/usr/bin/busctl"):
            self.assertFalse(notifier._via_dbus(USER, BALANCED))
        self.assertEqual(notifier.transport, None)

    def test_missing_busctl_is_reported_once(self):
        notifier = osd.Notifier({})
        with mock.patch.object(osd.shutil, "which", return_value=None):
            self.assertFalse(notifier._via_dbus(USER, BALANCED))
            self.assertFalse(notifier._via_dbus(USER, BALANCED))
        self.assertIn("no-busctl", notifier._warned)


class TestBooleanConfig(unittest.TestCase):
    def test_truthy_values(self):
        for value in ("yes", "YES", "true", "On", "1", "y"):
            self.assertTrue(osd._as_bool(value), msg=value)

    def test_falsy_values(self):
        for value in ("no", "false", "off", "0", "", None):
            self.assertFalse(osd._as_bool(value), msg=repr(value))

    def test_default_when_missing(self):
        self.assertTrue(osd._as_bool(None, True))
        self.assertFalse(osd._as_bool(None, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
