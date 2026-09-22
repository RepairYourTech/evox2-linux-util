#!/usr/bin/env python3
"""
Unit tests for ec-probe's capture mode.

The capture logic exists because a single before/after diff misses a button that
only pulses a register.  The bookkeeping that decides *what* changed is the part
worth testing, so it lives in TransitionLog rather than inline in the I/O loop.

The probe is a script rather than a module, so it is loaded by path.  The
end-to-end test runs it against a regular file standing in for the debugfs node,
which exercises the real read path without touching a controller.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import threading
import time
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "lib"))


def _load_probe():
    path = os.path.join(REPO_DIR, "tools", "ec-probe")
    loader = importlib.machinery.SourceFileLoader("evo_x2_ec_probe", path)
    spec = importlib.util.spec_from_loader("evo_x2_ec_probe", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["evo_x2_ec_probe"] = module
    loader.exec_module(module)
    return module


probe = _load_probe()

#: An offset nothing in the code knows about, so a change there is unambiguous.
UNKNOWN = 0xC0
PULSE = 0xC1


def region(values=None):
    """A 256-byte EC window, with {offset: value} applied."""
    blob = bytearray(256)
    for offset, value in (values or {}).items():
        blob[offset] = value
    return bytes(blob)


class TestTransitionLog(unittest.TestCase):
    def test_an_unchanged_sample_reports_nothing(self):
        log = probe.TransitionLog(region())
        self.assertEqual(log.observe(region(), 0.5), [])
        self.assertEqual(log.changed_offsets(), [])

    def test_a_change_is_reported_and_recorded(self):
        log = probe.TransitionLog(region())
        changed = log.observe(region({UNKNOWN: 0x07}), 1.25)
        self.assertEqual(changed, [(UNKNOWN, 0x00, 0x07)])
        self.assertEqual(log.changed_offsets(), [UNKNOWN])
        self.assertEqual(log.moments(UNKNOWN), [1.25])

    def test_a_value_that_returns_to_baseline_is_still_reported(self):
        """The whole reason capture polls: a brief pulse must not be missed."""
        log = probe.TransitionLog(region())
        log.observe(region({PULSE: 0x05}), 1.0)
        log.observe(region(), 2.0)          # back to where it started
        self.assertIn(PULSE, log.changed_offsets())
        self.assertEqual(log.transitions[PULSE],
                         [(1.0, 0x00, 0x05), (2.0, 0x05, 0x00)])
        self.assertEqual(log.values_seen(PULSE), [0x00, 0x05, 0x00])

    def test_repeated_changes_accumulate(self):
        log = probe.TransitionLog(region())
        for step, value in enumerate([1, 2, 3], start=1):
            log.observe(region({UNKNOWN: value}), float(step))
        self.assertEqual(len(log.transitions[UNKNOWN]), 3)
        self.assertEqual(log.values_seen(UNKNOWN), [0, 1, 2, 3])

    def test_values_seen_starts_from_the_baseline(self):
        log = probe.TransitionLog(region({UNKNOWN: 0x09}))
        log.observe(region({UNKNOWN: 0x0A}), 1.0)
        self.assertEqual(log.values_seen(UNKNOWN), [0x09, 0x0A])

    def test_changed_offsets_are_sorted(self):
        log = probe.TransitionLog(region())
        log.observe(region({0xC5: 1, 0xC0: 1, 0xC2: 1}), 1.0)
        self.assertEqual(log.changed_offsets(), [0xC0, 0xC2, 0xC5])

    def test_only_the_watched_registers_are_considered(self):
        log = probe.TransitionLog(region(), registers=[UNKNOWN])
        self.assertEqual(log.observe(region({UNKNOWN: 1, PULSE: 2}), 1.0),
                         [(UNKNOWN, 0x00, 0x01)])
        self.assertNotIn(PULSE, log.changed_offsets())

    def test_several_registers_changing_at_once_are_all_reported(self):
        log = probe.TransitionLog(region())
        changed = log.observe(region({0xC0: 1, 0xC1: 2}), 3.0)
        self.assertEqual(sorted(offset for offset, _, _ in changed),
                         [0xC0, 0xC1])
        self.assertEqual(log.moments(0xC0), log.moments(0xC1))


class TestRegisterLabels(unittest.TestCase):
    def test_the_mode_registers_are_named(self):
        import evo_x2_hw as hw

        self.assertIn("FCMO", probe.register_label(hw.EC_REG_MODE_READ))
        self.assertIn("FCMI", probe.register_label(hw.EC_REG_MODE_WRITE))

    def test_a_decoded_fan_register_is_named(self):
        self.assertEqual(probe.register_label(0x33), "FAN1")
        self.assertEqual(probe.register_label(0x38), "FN2H")

    def test_an_unnamed_register_has_no_label(self):
        self.assertEqual(probe.register_label(UNKNOWN), "")

    def test_the_labels_agree_with_the_offsets_the_project_relies_on(self):
        """FCMO must still name the register the daemon reads."""
        import evo_x2_hw as hw

        self.assertEqual(probe.REGISTER_NAMES[0x31], "FCMO")
        self.assertEqual(hw.EC_REG_MODE_READ, 0x31)
        self.assertEqual(hw.EC_REG_MODE_WRITE, 0x32)


class TestCaptureAgainstAFile(unittest.TestCase):
    """End-to-end, with a regular file standing in for the debugfs node."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "fake-ec")
        with open(self.path, "wb") as handle:
            handle.write(region())
        # Keep the run short: no countdown, and poll as fast as the loop allows.
        self._countdown = probe.CAPTURE_COUNTDOWN
        probe.CAPTURE_COUNTDOWN = 0

    def tearDown(self):
        probe.CAPTURE_COUNTDOWN = self._countdown
        self.directory.cleanup()

    def capture(self, seconds, interval=0.05):
        """Run a capture, keeping its running commentary out of the test log."""
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = probe.cmd_capture(self.path, seconds=seconds,
                                     interval=interval)
        return code, output.getvalue()

    def test_a_change_written_by_someone_else_is_caught(self):
        def mutate():
            time.sleep(0.15)
            with open(self.path, "r+b") as handle:
                handle.seek(UNKNOWN)
                handle.write(bytes([0x2A]))

        worker = threading.Thread(target=mutate, daemon=True)
        worker.start()
        try:
            code, output = self.capture(seconds=1.2)
        finally:
            worker.join(timeout=2)
        self.assertEqual(code, 0)
        self.assertIn(f"EC[{UNKNOWN:#04x}]", output)

    def test_nothing_changing_reports_failure(self):
        code, output = self.capture(seconds=0.4)
        self.assertEqual(code, 1)
        self.assertIn("Nothing in the window changed", output)

    def test_a_short_read_is_reported_not_crashed(self):
        with open(self.path, "wb") as handle:
            handle.write(b"\x00" * 10)      # not a 256-byte window
        code, _ = self.capture(seconds=0.4)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
