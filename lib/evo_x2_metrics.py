"""
evo_x2_metrics -- live platform readings that make a thermal-mode change visible.

The embedded controller tells you *which* mode is selected.  These readings tell
you whether the machine is actually behaving differently: CPU package
temperature, average clock, and whatever the platform exposes as package power.

Everything here is read-only and needs no privileges.  Paths are discovered once
and re-checked lazily, because hwmon numbering is not stable across boots and
the sensors available differ between machines (and between kernel versions).
"""

from __future__ import annotations

import glob
import os
import shutil
import statistics
import subprocess

import evo_x2_hw as hw

__all__ = ["Metrics", "read_text", "read_int", "hwmon_path"]


def read_text(path: str):
    """Contents of a sysfs attribute, or None if it cannot be read."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def read_int(path: str):
    value = read_text(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def hwmon_path(name: str, attribute: str):
    """Find /sys/class/hwmon/<a hwmon called `name`>/<attribute>."""
    for directory in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read_text(os.path.join(directory, "name")) == name:
            candidate = os.path.join(directory, attribute)
            if os.path.exists(candidate):
                return candidate
    return None


def _policy_dirs():
    return sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*"),
                  key=lambda path: int(os.path.basename(path)[6:]))


class Metrics:
    """The platform readings worth watching when the mode changes."""

    #: hwmon names that expose a package-power figure, most preferred first.
    #: On Strix Halo the amdgpu driver labels its reading "PPT", which is the
    #: closest thing this platform offers to a socket power number.
    POWER_SOURCES = (("amdgpu", "power1_average"),
                     ("amdgpu", "power1_input"),
                     ("k10temp", "power1_average"))

    def __init__(self):
        self.refresh_paths()

    def refresh_paths(self) -> None:
        self.cpu_temp = hwmon_path("k10temp", "temp1_input")
        self.package_power = None
        self.power_source = None
        for hwmon_name, attribute in self.POWER_SOURCES:
            found = hwmon_path(hwmon_name, attribute)
            if found:
                self.package_power = found
                self.power_source = f"{hwmon_name} {attribute.replace('power1_', '')}"
                break
        self.policies = _policy_dirs()

    # -- the EC's own view -------------------------------------------------

    def mode_index(self):
        try:
            return hw.read_mode()
        except hw.EcAccessError:
            return None

    def mode(self):
        index = self.mode_index()
        if index is None:
            return None
        try:
            return hw.mode_from_index(index)
        except hw.ModeError:
            return None

    def published_mode_index(self):
        """What the daemon last published; works without root."""
        return hw.read_state_file()

    # -- CPU governance ----------------------------------------------------

    def governor(self):
        for policy in self.policies:
            value = read_text(os.path.join(policy, "scaling_governor"))
            if value:
                return value
        return None

    def epp(self):
        """energy_performance_preference, i.e. what the OS asked the CPU for."""
        for policy in self.policies:
            value = read_text(os.path.join(policy, "energy_performance_preference"))
            if value:
                return value
        return None

    def clock_mhz(self):
        """Mean current clock across every CPU policy, in MHz."""
        frequencies = []
        for policy in self.policies:
            value = read_int(os.path.join(policy, "cpuinfo_avg_freq"))
            if value is None:
                value = read_int(os.path.join(policy, "scaling_cur_freq"))
            if value:
                frequencies.append(value / 1000.0)
        return statistics.fmean(frequencies) if frequencies else None

    # -- thermal / power ---------------------------------------------------

    def temperature(self):
        value = read_int(self.cpu_temp) if self.cpu_temp else None
        return value / 1000.0 if value is not None else None

    def power(self):
        value = read_int(self.package_power) if self.package_power else None
        return value / 1_000_000.0 if value is not None else None

    # -- power-profiles-daemon --------------------------------------------

    def ppd_profile(self):
        """power-profiles-daemon's own idea of the active profile."""
        binary = shutil.which("powerprofilesctl")
        if binary is None:
            return None
        try:
            result = subprocess.run([binary, "get"], capture_output=True,
                                    text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    # -- everything at once ------------------------------------------------

    def snapshot(self) -> dict:
        """One consistent-ish reading of the whole picture.

        Unprivileged callers cannot read the EC, so when that fails we fall back
        to the value the daemon publishes in /run.  `mode_source` says which was
        used so the caller can be honest about it.
        """
        index = self.mode_index()
        source = "ec"
        if index is None:
            index = self.published_mode_index()
            source = "daemon"
        return {
            "mode": index,
            "mode_name": _mode_name(index),
            "mode_source": source,
            "ppd_profile": self.ppd_profile(),
            "governor": self.governor(),
            "epp": self.epp(),
            "clock_mhz": self.clock_mhz(),
            "temperature": self.temperature(),
            "power": self.power(),
            "power_source": self.power_source,
        }


def _mode_name(index):
    if index is None:
        return None
    try:
        return hw.mode_from_index(index).name
    except hw.ModeError:
        return f"unknown ({index})"
