#!/usr/bin/env python3
"""
Unit tests for lib/evo_x2_hw.py.

These need no hardware and no root: the netlink protocol parsing is exercised
against synthetic buffers, and the embedded-controller access is exercised
against a plain file standing in for /sys/kernel/debug/ec/ec0/io.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                os.pardir, "lib"))

import evo_x2_hw as hw  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic netlink message builders
# ---------------------------------------------------------------------------

def nlattr(atype: int, payload: bytes) -> bytes:
    raw = struct.pack("=HH", 4 + len(payload), atype) + payload
    return raw + b"\x00" * ((4 - len(raw) % 4) % 4)


def nlmsg(mtype: int, payload: bytes, seq: int = 1) -> bytes:
    header = struct.pack("=IHHII", 16 + len(payload), mtype, 0, seq, 0)
    return header + payload


def genl_payload(cmd: int, attrs: bytes, version: int = 1) -> bytes:
    return struct.pack("=BBH", cmd, version, 0) + attrs


# Byte-for-byte CTRL_CMD_GETFAMILY reply for "acpi_event", captured from a
# GMKtec EVO-X2 running kernel 7.0.0-31-generic.  Kept verbatim as a
# regression fixture: the group attrs are nested one level deeper than the
# obvious reading of the protocol, and this is the case that broke once.
REAL_GETFAMILY_ACPI_EVENT = (
    "68000000100000000100000005120300010200000f000200616370695f65"
    "76656e740000060001002000000008000300010000000800040000000000"
    "08000500010000002400070020000100080002000c000000120001006163"
    "70695f6d635f67726f7570000000"
)


def getfamily_response(family_id: int = 0x1A, group: str = "acpi_mc_group",
                       group_id: int = 12) -> bytes:
    # Mirrors ctrl_fill_info() in net/netlink/genetlink.c: each multicast
    # group is itself a nested attribute, indexed from 1.
    entry = nlattr(1, group.encode() + b"\x00") + nlattr(2, struct.pack("=I", group_id))
    attrs = nlattr(1, struct.pack("=H", family_id)) + nlattr(7, nlattr(1, entry))
    return genl_payload(3, attrs)


def acpi_event_payload(device_class: str = "wmi", bus_id: str = "PNP0C14:00",
                       etype: int = 0xBC, data: int = 0) -> bytes:
    inner = (
        nlattr(1, device_class.encode() + b"\x00")
        + nlattr(2, bus_id.encode() + b"\x00")
        + nlattr(3, struct.pack("=I", etype))
        + nlattr(4, struct.pack("=I", data))
    )
    return genl_payload(1, nlattr(1, inner))


# ---------------------------------------------------------------------------

class TestModeTable(unittest.TestCase):
    def test_indices_are_contiguous_from_zero(self):
        self.assertEqual([m.index for m in hw.MODES], list(range(len(hw.MODES))))

    def test_fcmi_encoding(self):
        # The DSDT writes 0x80 | mode; this is the value the BIOS itself uses.
        self.assertEqual(hw.EC_MODE_WRITE_BASE | hw.mode_from_index(0).index, 0x80)
        self.assertEqual(hw.EC_MODE_WRITE_BASE | hw.mode_from_index(2).index, 0x82)

    def test_register_offsets_match_dsdt(self):
        self.assertEqual(hw.EC_REG_MODE_READ, 0x31)
        self.assertEqual(hw.EC_REG_MODE_WRITE, 0x32)

    def test_lookup_by_name_and_number(self):
        self.assertEqual(hw.mode_from_key("balanced").index, 1)
        self.assertEqual(hw.mode_from_key("1").index, 1)
        self.assertEqual(hw.mode_from_key("PERFORMANCE").index, 2)
        self.assertEqual(hw.mode_from_key("0x2").index, 2)

    def test_unknown_mode_raises(self):
        for bad in ("banana", "", None, "9"):
            with self.assertRaises(hw.ModeError, msg=repr(bad)):
                hw.mode_from_key(bad)

    def test_ppd_profiles_are_valid_names(self):
        for mode in hw.MODES:
            self.assertIn(mode.ppd, ("power-saver", "balanced", "performance"))

    def test_quoted_mode_is_not_advertised_as_a_button_mode(self):
        self.assertFalse(hw.mode_from_index(3).documented)
        self.assertTrue(all(m.documented for m in hw.MODES if m.index != 3))


class TestEcEncoding(unittest.TestCase):
    """The EC access functions, against a file standing in for the EC region."""

    def setUp(self):
        self.handle = tempfile.NamedTemporaryFile(delete=False)
        self.handle.write(bytes(256))
        self.handle.close()
        self.path = self.handle.name

    def tearDown(self):
        os.unlink(self.path)

    def _poke(self, offset, value):
        with open(self.path, "r+b") as fh:
            fh.seek(offset)
            fh.write(bytes([value]))

    def test_reads_the_mode_byte(self):
        self._poke(hw.EC_REG_MODE_READ, 2)
        self.assertEqual(hw.read_mode(self.path), 2)

    def test_reads_every_position(self):
        for offset in (0, 1, 0x30, 0x31, 0x32, 0xFF):
            self._poke(offset, 0x5A)
            self.assertEqual(hw.read_ec_byte(offset, self.path), 0x5A)
            self._poke(offset, 0)

    def test_missing_node_is_reported_clearly(self):
        with self.assertRaises(hw.EcAccessError) as ctx:
            hw.read_mode("/nonexistent/ec/io")
        self.assertIn("ec_sys", str(ctx.exception))

    def test_write_sets_fcmi_and_keeps_the_offset(self):
        hw.write_mode(2, self.path)
        with open(self.path, "rb") as fh:
            blob = fh.read()
        self.assertEqual(blob[hw.EC_REG_MODE_WRITE], 0x82)
        # Writing one byte must not disturb the mode read-back register.
        self.assertEqual(blob[hw.EC_REG_MODE_READ], 0x00)
        self.assertEqual(sum(blob), 0x82)

    def test_write_never_exceeds_one_byte(self):
        hw.write_mode(hw.mode_from_index(0), self.path)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read().count(0x80), 1)

    def test_write_accepts_a_mode_name(self):
        hw.write_mode("performance", self.path)
        with open(self.path, "rb") as fh:
            fh.seek(hw.EC_REG_MODE_WRITE)
            self.assertEqual(fh.read(1), b"\x82")


class TestNetlinkParsing(unittest.TestCase):
    def test_parse_attributes_round_trip(self):
        blob = nlattr(1, b"hello") + nlattr(2, b"world")
        attrs = hw.parse_attributes(blob)
        self.assertEqual(attrs[1], [b"hello"])
        self.assertEqual(attrs[2], [b"world"])

    def test_parse_attributes_ignores_truncated_input(self):
        blob = nlattr(1, b"hello") + struct.pack("=HH", 40, 9) + b"short"
        attrs = hw.parse_attributes(blob)
        self.assertEqual(attrs[1], [b"hello"])
        self.assertNotIn(9, attrs)

    def test_iter_messages_walks_a_multi_message_datagram(self):
        payload_a = genl_payload(1, nlattr(1, b"a"))
        payload_b = genl_payload(1, nlattr(1, b"b"))
        datagram = nlmsg(0x10, payload_a) + nlmsg(0x10, payload_b)
        found = list(hw.iter_netlink_messages(datagram))
        self.assertEqual(len(found), 2)
        self.assertEqual([mtype for mtype, _ in found], [0x10, 0x10])

    def test_iter_messages_stops_on_a_bogus_length(self):
        datagram = struct.pack("=IHHII", 4, 0x10, 0, 1, 0)  # length < header
        self.assertEqual(list(hw.iter_netlink_messages(datagram)), [])

    def test_getfamily_request_shape(self):
        request = hw.build_getfamily_request(hw.ACPI_EVENT_FAMILY, seq=7)
        length, mtype, flags, seq, pid = struct.unpack_from("=IHHII", request, 0)
        self.assertEqual(length, len(request))
        self.assertEqual(seq, 7)
        self.assertEqual(pid, 0)
        self.assertTrue(flags & 0x01)  # NLM_F_REQUEST
        self.assertIn(b"acpi_event", request)

    def test_getfamily_response_is_parsed(self):
        family_id, groups = hw.parse_getfamily_response(
            getfamily_response(0x1A, "acpi_mc_group", 12))
        self.assertEqual(family_id, 0x1A)
        self.assertEqual(groups, {"acpi_mc_group": 12})

    def test_real_kernel_reply_is_parsed(self):
        """The exact reply this machine's kernel sent, header stripped."""
        payload = bytes.fromhex(REAL_GETFAMILY_ACPI_EVENT)[16:]
        family_id, groups = hw.parse_getfamily_response(payload)
        self.assertEqual(family_id, 32)
        self.assertEqual(groups, {"acpi_mc_group": 12})

    def test_several_groups_are_each_parsed(self):
        entries = (nlattr(1, nlattr(1, b"first\x00") + nlattr(2, struct.pack("=I", 5)))
                   + nlattr(2, nlattr(1, b"second\x00") + nlattr(2, struct.pack("=I", 6))))
        attrs = nlattr(1, struct.pack("=H", 7)) + nlattr(7, entries)
        family_id, groups = hw.parse_getfamily_response(genl_payload(3, attrs))
        self.assertEqual(family_id, 7)
        self.assertEqual(groups, {"first": 5, "second": 6})

    def test_group_name_is_looked_up_by_alias(self):
        """We must match 'acpi_mc_group' even though we also accept 'acpi'."""
        self.assertIn("acpi_mc_group", hw.ACPI_EVENT_GROUP_NAMES)

    def test_getfamily_response_without_a_family_id(self):
        payload = genl_payload(3, nlattr(7, b""))
        self.assertEqual(hw.parse_getfamily_response(payload), (None, {}))

    def test_acpi_event_is_parsed(self):
        event = hw.parse_acpi_event(acpi_event_payload())
        self.assertEqual(event, ("wmi", "PNP0C14:00", 0xBC, 0))

    def test_acpi_event_carries_the_payload(self):
        event = hw.parse_acpi_event(
            acpi_event_payload("wmi", "PNP0C14:00", 0xBC, 0x1234))
        self.assertEqual(event[2], 0xBC)
        self.assertEqual(event[3], 0x1234)

    def test_non_event_message_is_ignored(self):
        self.assertIsNone(hw.parse_acpi_event(genl_payload(1, nlattr(9, b"x"))))


class TestButtonRecognition(unittest.TestCase):
    def test_the_button_event_is_recognised(self):
        event = hw.parse_acpi_event(acpi_event_payload())
        self.assertTrue(hw.is_button_event(event))

    def test_the_notify_id_comes_from_the_dsdt(self):
        self.assertEqual(hw.WMI_BUTTON_NOTIFY_ID, 0xBC)

    def test_other_acpi_events_are_ignored(self):
        others = [
            acpi_event_payload("wmi", "PNP0C14:00", 0x00),
            acpi_event_payload("button", "power", 0xBC),
            acpi_event_payload("video", "LCD0", 0x87),
        ]
        for payload in others:
            event = hw.parse_acpi_event(payload)
            self.assertFalse(hw.is_button_event(event), msg=str(event))


if __name__ == "__main__":
    unittest.main(verbosity=2)
