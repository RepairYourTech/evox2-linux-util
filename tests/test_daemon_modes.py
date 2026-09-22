#!/usr/bin/env python3
"""
Unit tests for the daemon's mode bookkeeping: what a sequence of EC readings
means, how the user's preferences are layered over the administrator's config,
and what restore-on-boot does.

Nothing here touches the EC, the session bus or root; the hardware functions
are stubbed.  The daemon is a script rather than a module, so it is loaded by
path, the same way test_notifier.py does it:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "lib"))

import evo_x2_hw as hw            # noqa: E402
import evo_x2_settings as settings  # noqa: E402


def _load_daemon():
    path = os.path.join(REPO_DIR, "daemon", "evo-x2-thermal-osd")
    loader = importlib.machinery.SourceFileLoader("evo_x2_thermal_osd_modes",
                                                  path)
    spec = importlib.util.spec_from_loader("evo_x2_thermal_osd_modes", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["evo_x2_thermal_osd_modes"] = module
    loader.exec_module(module)
    return module


osd = _load_daemon()
logging.getLogger("evo-x2-thermal-osd").addHandler(logging.NullHandler())
logging.getLogger("evo-x2-thermal-osd").propagate = False


class FakeUser:
    """Stands in for a pwd entry; only pw_dir is used for the settings file."""

    def __init__(self, home, name="birdman"):
        self.pw_dir = home
        self.pw_name = name


class TrackerCase(unittest.TestCase):
    """Timings are chosen to mirror the flare seen on this machine.

    Recorded in the daemon journal on 2026-09-21: the mode register walked
    Quiet -> Balanced -> Performance with the values arriving two and three
    seconds apart.
    """

    def setUp(self):
        self.tracker = osd.ModeTracker(2, now=0.0)   # Performance

    def events(self, value, now):
        return self.tracker.update(value, now)

    def test_a_steady_reading_reports_nothing(self):
        for now in (1.0, 2.0, 3.0, 4.0):
            self.assertEqual(self.events(2, now), [])

    def test_a_single_change_is_reported_immediately(self):
        """A real press has to feel instant, so it is not held back."""
        events = self.events(0, 1.0)
        self.assertEqual(events, [("changed", 0)])
        self.assertEqual(self.tracker.origin, 2)

    def test_a_reading_of_none_is_ignored(self):
        """A failed EC read must not look like the mode changing."""
        self.assertEqual(self.events(None, 1.0), [])
        self.assertEqual(self.tracker.mode, 2)
        self.assertFalse(self.tracker.bursting)

    def test_a_completed_change_settles_once(self):
        self.assertEqual(self.events(0, 1.0), [("changed", 0)])
        # The minimum burst age holds the decision off even though the reading
        # is already still.
        self.assertEqual(self.events(0, 3.0), [])
        # announce=False: this value was already reported when the burst
        # opened, so settling must not report it a second time.
        self.assertEqual(self.events(0, 5.5), [("settled", 0, 2, False)])
        # ... and only once.
        self.assertEqual(self.events(0, 8.0), [])

    def test_the_observed_flap_reports_once_and_settles_back(self):
        """The regression this whole mechanism exists for.

        Before it, this sequence produced three notifications, three power
        profile changes, and a tray icon that showed a mode the machine was
        not in.
        """
        collected = []
        for value, now in ((0, 1.0),      # Quiet
                           (0, 2.0),
                           (1, 3.0),      # Balanced
                           (1, 4.0),
                           (1, 5.0),
                           (2, 6.0),      # back to Performance
                           (2, 7.0),
                           (2, 8.0),
                           (2, 9.0),
                           (2, 10.0)):
            collected.extend(self.events(value, now))

        reported = [e for e in collected if e[0] == "changed"]
        settled = [e for e in collected if e[0] == "settled"]
        self.assertEqual(reported, [("changed", 0)],
                         "only the first step may be announced")
        # announce is True here -- the mode it settled on was never shown,
        # because the announced one was the first step of the flare.  The
        # daemon must nevertheless revert rather than announce it, which is
        # what test_settling_back_wins_over_a_pending_announce pins down.
        self.assertEqual(settled, [("settled", 2, 2, True)],
                         "and it must settle back where it began")

    def test_a_flap_that_settles_elsewhere_reports_the_final_mode(self):
        collected = []
        for value, now in ((0, 1.0), (1, 3.0), (3, 5.0),
                           (3, 7.0), (3, 9.0)):
            collected.extend(self.events(value, now))

        self.assertEqual([e for e in collected if e[0] == "changed"],
                         [("changed", 0)])
        # announce=True: the mode it landed on was never reported.
        self.assertEqual([e for e in collected if e[0] == "settled"],
                         [("settled", 3, 2, True)])

    def test_a_later_press_is_a_new_event(self):
        self.events(0, 1.0)
        self.events(0, 6.0)                 # settles
        self.assertEqual(self.events(3, 60.0), [("changed", 3)])
        self.assertEqual(self.tracker.origin, 0)

    def test_a_change_after_a_long_gap_is_a_new_event(self):
        """With a coarse poll the intermediate readings are never sampled, so
        a much later change has to start a fresh event rather than be folded
        into the old one."""
        self.assertEqual(self.events(0, 1.0), [("changed", 0)])
        late = osd.FLAP_WINDOW + 2.0
        self.assertEqual(self.events(1, late), [("changed", 1)])
        self.assertEqual(self.tracker.origin, 0)

    def test_a_change_inside_the_window_is_folded_in(self):
        self.assertEqual(self.events(0, 1.0), [("changed", 0)])
        inside = osd.FLAP_WINDOW - 0.5
        self.assertEqual(self.events(1, inside), [])
        self.assertEqual(self.tracker.mode, 1)

    def test_the_minimum_burst_age_delays_the_decision(self):
        tracker = osd.ModeTracker(2, now=0.0, settle=0.5, min_burst=10.0)
        self.assertEqual(tracker.update(0, 1.0), [("changed", 0)])
        self.assertEqual(tracker.update(0, 5.0), [])
        self.assertEqual(tracker.update(0, 12.0), [("settled", 0, 2, False)])

    def test_one_clean_change_is_announced_exactly_once(self):
        """Regression: settling used to repeat the report, so every single
        mode change produced two notifications."""
        announced = 0
        for value, now in ((0, 1.0), (0, 2.0), (0, 5.0), (0, 9.0), (0, 14.0)):
            for event in self.events(value, now):
                if event[0] == "changed":
                    announced += 1
                elif event[3]:
                    announced += 1
        self.assertEqual(announced, 1)


class OverlayCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.user = FakeUser(self.home)
        self.state = {"user": self.user, "settings": None, "forced": {}}
        self.cfg = {
            "user": "",
            "poll_interval": "1.0",
            "show_notifications": "yes",
            "sync_power_profiles": "yes",
            "restore_on_boot": "no",
        }

    def tearDown(self):
        self._tmp.cleanup()

    def prefs(self):
        return settings.Settings(home=self.home)

    def test_without_a_user_nothing_is_overridden(self):
        state = {"user": None, "settings": None, "forced": {}}
        self.assertEqual(osd.effective(self.cfg, state), self.cfg)

    def test_the_users_preferences_win_over_the_system_config(self):
        prefs = self.prefs()
        prefs.set("show_notifications", "no")
        prefs.set("poll_interval", 2.5)
        prefs.save()

        live = osd.effective(self.cfg, self.state)
        self.assertEqual(live["show_notifications"], "no")
        # Strings: this is a config overlay, not a typed view of it.
        self.assertEqual(live["poll_interval"], "2.5")
        # Untouched keys stay the administrator's.
        self.assertEqual(live["sync_power_profiles"], "yes")

    def test_keys_the_user_does_not_own_cannot_be_overridden(self):
        """A hand-written file must not reach the administrator's settings."""
        prefs = self.prefs()
        prefs.set("show_notifications", "no")
        prefs.save()
        with open(prefs.path, "a", encoding="utf-8") as handle:
            handle.write("sync_power_profiles = no\n"
                         "icon_quiet = /tmp/evil.svg\n")

        live = osd.effective(self.cfg, self.state)
        self.assertEqual(live["sync_power_profiles"], "yes")
        self.assertNotIn("icon_quiet", live)

    def test_a_command_line_flag_beats_the_users_preference(self):
        prefs = self.prefs()
        prefs.set("show_notifications", "yes")
        prefs.save()
        self.state["forced"] = {"show_notifications": "no"}

        self.assertEqual(
            osd.effective(self.cfg, self.state)["show_notifications"], "no")

    def test_an_edit_is_picked_up_between_calls(self):
        """The point of re-reading: a switch in the window applies to the next
        mode change rather than to the next restart."""
        self.assertEqual(
            osd.effective(self.cfg, self.state)["show_notifications"], "yes")

        prefs = self.prefs()
        prefs.set("show_notifications", "no")
        prefs.save()

        self.assertEqual(
            osd.effective(self.cfg, self.state)["show_notifications"], "no")

    def test_the_settings_follow_a_change_of_session_user(self):
        first = settings.Settings(home=self.home)
        first.set("poll_interval", 4.0)
        first.save()
        self.assertEqual(osd.effective(self.cfg, self.state)["poll_interval"],
                         "4.0")

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.state["user"] = FakeUser(other.name)
        self.assertEqual(osd.effective(self.cfg, self.state)["poll_interval"],
                         "1.0")

    def test_poll_interval_is_clamped_and_survives_junk(self):
        self.assertEqual(osd.poll_interval({"poll_interval": "2.5"}), 2.5)
        self.assertEqual(osd.poll_interval({"poll_interval": "0.01"}), 0.25)
        self.assertEqual(osd.poll_interval({"poll_interval": "nonsense"}), 1.0)
        self.assertEqual(osd.poll_interval({}), 1.0)


class RememberModeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.state = {"user": FakeUser(self.home), "settings": None,
                      "forced": {}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_remembering_a_mode_writes_its_key(self):
        osd.remember_mode(self.state, hw.MODES[1].index)
        self.assertEqual(settings.Settings(home=self.home).get("restore_mode"),
                         "balanced")

    def test_remembering_an_unreadable_index_changes_nothing(self):
        osd.remember_mode(self.state, 99)
        self.assertEqual(settings.Settings(home=self.home).get("restore_mode"),
                         "")

    def test_nothing_is_written_when_the_mode_is_unchanged(self):
        osd.remember_mode(self.state, 2)
        path = settings.settings_path(home=self.home)
        first = os.stat(path).st_mtime_ns
        osd.remember_mode(self.state, 2)
        self.assertEqual(os.stat(path).st_mtime_ns, first)


class RestoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.user = FakeUser(self.home)
        self.state = {"user": self.user, "settings": None, "forced": {},
                      "restore_pending": True, "dry_run": False}
        self.cfg = {"restore_on_boot": "no"}

        patcher = mock.patch.object(osd.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def prefs(self, enabled=True, mode="performance"):
        prefs = settings.Settings(home=self.home)
        prefs.set("restore_on_boot", enabled)
        prefs.set("restore_mode", mode)
        prefs.save()
        return prefs

    def test_nothing_happens_before_a_session_user_is_known(self):
        """A machine boots long before anyone logs in, and both the switch and
        the remembered mode belong to that user."""
        self.prefs()
        self.state["user"] = None
        with mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_not_called()
        # Still pending, so it is retried once a user appears.
        self.assertTrue(self.state["restore_pending"])

    def test_the_saved_mode_is_reapplied(self):
        self.prefs(mode="balanced")
        with mock.patch.object(osd.hw, "read_mode", return_value=0), \
                mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_called_once_with(hw.MODES_BY_KEY["balanced"])
        self.assertFalse(self.state["restore_pending"])

    def test_it_is_not_reapplied_when_the_ec_already_agrees(self):
        """Firmware that keeps the selection makes this a no-op rather than a
        pointless EC write at every boot."""
        self.prefs(mode="performance")
        with mock.patch.object(osd.hw, "read_mode", return_value=2), \
                mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_not_called()

    def test_it_does_nothing_when_the_switch_is_off(self):
        self.prefs(enabled=False)
        with mock.patch.object(osd.hw, "read_mode", return_value=0), \
                mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_not_called()

    def test_it_does_nothing_when_no_mode_was_recorded(self):
        self.prefs(mode="")
        with mock.patch.object(osd.hw, "read_mode", return_value=0), \
                mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_not_called()

    def test_an_unreadable_saved_mode_is_ignored(self):
        self.prefs(mode="teleport")
        with mock.patch.object(osd.hw, "read_mode", return_value=0), \
                mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_not_called()
        self.assertFalse(self.state["restore_pending"])

    def test_a_failed_write_is_reported_and_not_retried(self):
        self.prefs(mode="quiet")
        with mock.patch.object(osd.hw, "read_mode", return_value=2), \
                mock.patch.object(
                    osd.hw, "write_mode",
                    side_effect=hw.EcAccessError("write_support is off")):
            osd.restore_last_mode(self.cfg, self.state)   # must not raise
        self.assertFalse(self.state["restore_pending"])

    def test_a_failed_read_does_not_write_anything(self):
        self.prefs(mode="quiet")
        with mock.patch.object(
                osd.hw, "read_mode", side_effect=hw.EcAccessError("gone")), \
                mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_not_called()

    def test_a_dry_run_changes_nothing(self):
        self.prefs(mode="quiet")
        self.state["dry_run"] = True
        with mock.patch.object(osd.hw, "read_mode", return_value=2), \
                mock.patch.object(osd.hw, "write_mode") as write:
            osd.restore_last_mode(self.cfg, self.state)
        write.assert_not_called()

    def test_the_restored_mode_is_published_for_the_tray(self):
        """The tray cannot read the EC and goes by the daemon's state file, so
        a restore that did not publish would leave the icon showing the mode it
        just moved away from."""
        self.prefs(mode="balanced")
        with mock.patch.object(osd.hw, "read_mode", return_value=0), \
                mock.patch.object(osd.hw, "write_mode"), \
                mock.patch.object(osd.hw, "write_state_file") as published:
            osd.restore_last_mode(self.cfg, self.state)
        published.assert_called_once_with(hw.MODES_BY_KEY["balanced"].index)

    def test_it_reports_which_mode_it_restored(self):
        """The caller has to know, because the daemon moved the mode itself:
        without that the loop reads the new value back a pass later and cannot
        tell it from a button press (see AdoptCase)."""
        self.prefs(mode="balanced")
        with mock.patch.object(osd.hw, "read_mode", return_value=0), \
                mock.patch.object(osd.hw, "write_mode"):
            restored = osd.restore_last_mode(self.cfg, self.state)
        self.assertEqual(restored, hw.MODES_BY_KEY["balanced"].index)

    def test_the_paths_that_change_nothing_report_nothing(self):
        """Every early return has to be None, or the caller would adopt a mode
        the EC was never put into."""
        cases = [
            (dict(enabled=False, mode="quiet"), 2),   # switch off
            (dict(mode=""), 2),                       # nothing remembered
            (dict(mode="teleport"), 2),               # unreadable value
            (dict(mode="quiet"), 0),                  # already in that mode
        ]
        for prefs, current in cases:
            with self.subTest(prefs=prefs, current=current):
                self.prefs(**prefs)
                with mock.patch.object(osd.hw, "read_mode",
                                       return_value=current), \
                        mock.patch.object(osd.hw, "write_mode"):
                    self.assertIsNone(
                        osd.restore_last_mode(self.cfg, self.state))


class SettleCase(unittest.TestCase):
    """What the daemon does with a burst once it stops moving."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.state = {"user": FakeUser(self.home), "settings": None,
                      "forced": {}, "dry_run": False,
                      "notifier": mock.Mock()}
        self.cfg = {"show_notifications": "yes", "sync_power_profiles": "no"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_settling_back_undoes_the_change_without_a_notification(self):
        with mock.patch.object(osd.hw, "write_state_file") as state_write:
            osd.settle(2, 2, False, self.cfg, self.state)
        self.state["notifier"].send.assert_not_called()
        state_write.assert_called_once_with(2)

    def test_settling_elsewhere_announces_the_new_mode(self):
        with mock.patch.object(osd.hw, "write_state_file"):
            osd.settle(1, 2, True, self.cfg, self.state)
        self.state["notifier"].send.assert_called_once()

    def test_settling_on_the_announced_mode_stays_quiet(self):
        """The ordinary single change: already reported when it happened, so
        settling must not report it again."""
        with mock.patch.object(osd.hw, "write_state_file") as state_write:
            osd.settle(1, 2, False, self.cfg, self.state)
        self.state["notifier"].send.assert_not_called()
        state_write.assert_not_called()

    def test_settling_back_wins_over_a_pending_announce(self):
        """A flare ends on a value the user was never shown, so `announce` is
        true -- but it is also where the flap began, so it must be undone
        rather than announced."""
        with mock.patch.object(osd.hw, "write_state_file"):
            osd.settle(2, 2, True, self.cfg, self.state)
        self.state["notifier"].send.assert_not_called()


class AdoptCase(unittest.TestCase):
    """A restore moves the mode with nobody pressing the button.

    The loop samples the EC once a pass, so a mode the daemon wrote itself
    comes back about a second later and is, byte for byte, the same reading a
    press produces.  `adopt` is what stops a restore at boot from being
    announced as one, and from syncing the profile as if the user had chosen
    the mode just now.
    """

    def test_adopting_silences_the_mode_that_was_written(self):
        tracker = osd.ModeTracker(0, now=0.0)          # booted in Quiet
        tracker.adopt(2, now=1.0)                      # restored Performance
        self.assertEqual(tracker.update(2, 10.0), [])  # next pass reads it back

    def test_without_adopting_the_same_reading_is_a_press(self):
        """The bug this prevents, stated as a test: the reading is identical
        and only the baseline differs."""
        tracker = osd.ModeTracker(0, now=0.0)
        self.assertEqual([event[0] for event in tracker.update(2, 10.0)],
                         ["changed"])

    def test_a_real_press_after_a_restore_is_still_reported(self):
        tracker = osd.ModeTracker(0, now=0.0)
        tracker.adopt(2, now=1.0)
        self.assertEqual([event[0] for event in tracker.update(1, 10.0)],
                         ["changed"])

    def test_adopting_closes_an_open_burst(self):
        """Otherwise a restore would leave a burst open that swallows the next
        genuine change instead of reporting it."""
        tracker = osd.ModeTracker(0, now=0.0)
        tracker.update(1, 1.0)
        self.assertTrue(tracker.bursting)
        tracker.adopt(2, now=2.0)
        self.assertFalse(tracker.bursting)


class StartupSyncCase(unittest.TestCase):
    """ppd cannot be up when the daemon starts.

    power-profiles-daemon orders itself after multi-user.target, so it is
    always later than a service wanted by that target -- which is why the unit
    cannot order after it (that was the ordering cycle that stopped the daemon
    starting at boot).  The start-up profile sync therefore waits for ppd.
    """

    def test_it_keeps_trying_while_the_budget_lasts(self):
        for tries in (1, osd.PPD_STARTUP_RETRIES - 1):
            with self.subTest(tries=tries):
                self.assertIsNotNone(osd.startup_sync_retry(tries, 100.0))

    def test_it_stops_at_the_budget(self):
        self.assertIsNone(osd.startup_sync_retry(osd.PPD_STARTUP_RETRIES, 100.0))

    def test_retries_are_spaced_out(self):
        self.assertEqual(osd.startup_sync_retry(3, 100.0),
                         100.0 + osd.PPD_STARTUP_INTERVAL)

    def test_the_budget_outlasts_a_desktop_starting_up(self):
        self.assertGreaterEqual(
            osd.PPD_STARTUP_RETRIES * osd.PPD_STARTUP_INTERVAL, 20.0)


class SyncPpdQuietCase(unittest.TestCase):
    """A failing retry during boot is expected, not noteworthy.

    ppd is *supposed* to be missing for the first few seconds, so reporting
    each attempt as a warning would put one complaint per second in the
    journal.  Only giving up is worth a line.
    """

    LOGGER = "evo-x2-thermal-osd"

    def _sync(self, warn):
        failed = osd.subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"no daemon")
        with mock.patch.object(osd.shutil, "which",
                              return_value="/usr/bin/powerprofilesctl"), \
                mock.patch.object(osd.subprocess, "run", return_value=failed):
            with self.assertLogs(self.LOGGER, level="DEBUG") as captured:
                ok = osd.sync_ppd({}, hw.MODES_BY_KEY["performance"], warn=warn)
        return ok, captured.records

    def test_a_failure_is_reported_when_the_caller_asked_for_it(self):
        ok, records = self._sync(warn=True)
        self.assertFalse(ok)
        self.assertTrue([r for r in records if r.levelno >= logging.WARNING])

    def test_a_retry_failure_stays_at_debug(self):
        ok, records = self._sync(warn=False)
        self.assertFalse(ok)
        self.assertEqual([r for r in records if r.levelno >= logging.WARNING],
                         [])


if __name__ == "__main__":
    unittest.main()
