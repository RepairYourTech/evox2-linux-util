#!/usr/bin/env python3
"""
Tests for the systemd unit's ordering.

A unit whose ordering cannot be satisfied is not an error at the point of
writing it: systemd finds the cycle when it builds the boot transaction, logs
one line, and *deletes the job*.  Nothing else complains.  The service is
enabled, the unit file validates, `systemctl start` works if you ask for it by
hand -- and the daemon simply never runs at boot, so no mode is ever published
and the tray has nothing to show.

That is exactly what happened on 2026-09-21.  The unit asked to start after
power-profiles-daemon, which on every distro that ships it orders itself after
multi-user.target (it has to, so it can order before graphical.target), while
this unit is wanted by that same target and is therefore ordered before it:

    evo-x2-thermal-osd -> before -> multi-user.target -> before -> ppd
    ppd -> before -> evo-x2-thermal-osd          (that 'After=')

and systemd resolved it by not starting the daemon at all.

These tests read the unit template and the machine's real unit files, so they
need no systemd and no root.  They are skipped when the other unit is not
installed, which is what happens on a distribution without ppd.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
UNIT = REPO_DIR / "systemd" / "evo-x2-thermal-osd.service.in"

#: Where systemd looks for units, most specific first.
UNIT_DIRS = (
    Path("/etc/systemd/system"),
    Path("/run/systemd/system"),
    Path("/usr/local/lib/systemd/system"),
    Path("/usr/lib/systemd/system"),
    Path("/lib/systemd/system"),
)

DIRECTIVES = ("After", "Before", "WantedBy", "Wants", "Requires")


def read_directives(path: Path) -> dict[str, set]:
    """-> {directive: {names}} for one unit file, comments and blanks skipped."""
    found = {name: set() for name in DIRECTIVES}
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        for name in DIRECTIVES:
            prefix = f"{name}="
            if line.startswith(prefix):
                found[name].update(line[len(prefix):].split())
    return found


def find_unit(name: str):
    for directory in UNIT_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


class UnitOrderingCase(unittest.TestCase):
    def setUp(self):
        self.assertTrue(UNIT.is_file(), f"missing {UNIT}")
        self.unit = read_directives(UNIT)

    def test_power_profiles_daemon_is_wanted_but_not_ordered_against(self):
        """The profile mirror still needs ppd, so it stays in Wants= -- but
        ordering after it is precisely what stopped the daemon starting."""
        self.assertIn("power-profiles-daemon.service", self.unit["Wants"])
        self.assertNotIn("power-profiles-daemon.service", self.unit["After"])

    def test_nothing_is_ordered_after_a_unit_that_orders_after_our_target(self):
        """The general shape of the trap, so the next dependency added here
        cannot reintroduce it.

        A service that is wanted by a target is ordered before that target, so
        asking to start after a unit that itself starts after that target can
        never be satisfied.  `Before=` is treated as the same constraint as
        `WantedBy=`, since systemd derives one from the other.
        """
        own_targets = self.unit["WantedBy"] | self.unit["Before"]
        if not own_targets:
            self.skipTest("the unit is not ordered against any target")

        checked = 0
        for name in sorted(self.unit["After"]):
            path = find_unit(name)
            if path is None:
                continue
            checked += 1
            conflict = read_directives(path)["After"] & own_targets
            self.assertEqual(
                conflict, set(),
                f"ordering after {name} is an unresolvable cycle: {name} "
                f"orders after {sorted(conflict)}, which this unit is ordered "
                f"before (via WantedBy="
                f"{' '.join(sorted(self.unit['WantedBy']))}).  systemd would "
                f"delete this job at boot and the daemon would never start.",
            )
        if not checked:
            self.skipTest("none of the units listed in After= are installed here")

    def test_it_still_waits_for_the_module_loader(self):
        """ec_sys has to exist before the daemon can read the EC at all, and the
        unit loads it itself as a fallback."""
        self.assertIn("systemd-modules-load.service", self.unit["After"])

    def test_it_is_still_wanted_by_a_boot_target(self):
        self.assertTrue(
            self.unit["WantedBy"] & {"multi-user.target", "graphical.target"},
            f"nothing would enable it: WantedBy={sorted(self.unit['WantedBy'])}")


if __name__ == "__main__":
    unittest.main()
