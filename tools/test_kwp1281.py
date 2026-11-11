#!/usr/bin/env python3
"""Hardware-free tests for tools/kwp1281.py via a transaction-level
FakeECU that models the K-line exactly like the real bus:

  * everything the tester writes is echoed back to it (single wire)
  * the ECU sends ONE byte at a time and waits for the tester's
    complement ACK before the next byte (that IS the flow control)
  * complement bytes are verified, tester blocks are parsed and answered
  * the 0x03 block end gets no complement and hands the bus back
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ftdi_usb as F  # noqa: E402
import kwp1281 as K  # noqa: E402


class FakePort:
    """KKLPort duck-type wired to a FakeECU. No real timing."""

    def __init__(self, ecu):
        self.ecu = ecu
        self.rx = ecu.rx

    def write(self, data, timeout_ms=1000):
        for b in data:
            self.ecu.tester_wrote(b)
        return len(data)

    def read_exact(self, n, timeout_ms, sleep=None):
        if len(self.rx) < n:
            raise F.KLineTimeout(bytes(self.rx[:n]))
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def slow_init(self, addr=0x01, bit_ms=200, sync_timeout_ms=1500, n=3):
        self.ecu.on_init(addr)
        if len(self.rx) < n:
            raise F.KLineTimeout(bytes(self.rx))
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out


class FakeECU:
    """KW1281 ECU simulator, byte-interleaved like the real protocol."""

    def __init__(self, ident_strings, groups=None, header_variant=False):
        self.rx = bytearray()          # bytes the tester reads
        self.idents = list(ident_strings)
        self.groups = groups or {}
        self.header_variant = header_variant
        self._ctr = 0
        self._ident_idx = 0
        self.frame = None              # ECU block currently streaming
        self.frame_pos = 0
        self.expect_comp = None        # byte just sent, comp awaited
        self.rx_partial = bytearray()  # tester block under construction
        self.comps_checked = 0
        self.disconnected = False

    # -- ECU transmission (one byte per comp round-trip) --
    def _start_block(self, title, data=b""):
        self._ctr = (self._ctr + 1) & 0xFF
        payload = bytes([self._ctr, title]) + bytes(data) + b"\x03"
        self.frame = bytes([len(payload)]) + payload
        self.frame_pos = 0
        self._send_next()

    def _send_next(self):
        b = self.frame[self.frame_pos]
        self.frame_pos += 1
        self.rx.append(b)
        if self.frame_pos >= len(self.frame):
            # 0x03 end sent: no comp expected, bus hands back to tester
            self.frame = None
            self.rx_partial = bytearray()
        else:
            self.expect_comp = b  # wait for b ^ 0xFF

    def _ident_block(self, idx):
        s = self.idents[idx].ljust(12)[:12].encode()
        self._start_block(K.KWP_R_ASCII_DATA, s)

    # -- events --
    def on_init(self, addr):
        assert addr == 0x01, "fake only models the engine address"
        # sync + keyword arrive back-to-back, no comps between them
        self.rx.extend(b"\x55\x01\x8A")
        self.expect_comp = 0x8A  # the single 0x75 keyword complement

    def tester_wrote(self, b):
        self.rx.append(b)  # line echo
        if self.expect_comp is not None:
            want = self.expect_comp ^ 0xFF
            assert b == want, f"ECU: bad complement {b:02x} want {want:02x}"
            self.comps_checked += 1
            self.expect_comp = None
            if self.frame is not None:
                self._send_next()
            else:
                # keyword complement received -> ident phase begins
                self._ident_idx = 1
                self._ident_block(0)
            return
        # tester block assembly (bus is ours)
        self.rx_partial.append(b)
        if len(self.rx_partial) == 1:
            assert b >= 3, f"ECU: bad tester block length {b:02x}"
        elif len(self.rx_partial) == self.rx_partial[0] + 1:
            frame = bytes(self.rx_partial)
            self.rx_partial = bytearray()
            self._on_tester_block(frame)

    def _on_tester_block(self, frame):
        length, ctr, title = frame[0], frame[1], frame[2]
        data = frame[3:-1]
        assert frame[-1] == 0x03, "tester block missing end marker"
        if title == K.KWP_ACK:
            if self._ident_idx < len(self.idents):
                self._ident_block(self._ident_idx)
                self._ident_idx += 1
            else:
                self._start_block(K.KWP_ACK)  # ends the ident phase
            return
        if title == K.KWP_GROUP_READING:
            group = data[0]
            if self.header_variant and not getattr(self, "_hdr_sent", False):
                self._hdr_sent = True
                self._start_block(K.KWP_R_GROUP_READ_HEADER, bytes([group]))
            elif self.header_variant:
                self._start_block(K.KWP_R_GROUP_READ_DATA,
                                  bytes([group]) + self.groups[group])
            elif group in self.groups:
                self._start_block(K.KWP_R_GROUP_READING, self.groups[group])
            else:
                self._start_block(K.KWP_ACK)  # valid group, no data
            return
        if title == K.KWP_DISCONNECT:
            self.disconnected = True
            return
        raise AssertionError(f"ECU: unexpected tester title {title:02x}")


def build(groups=None, header_variant=False,
          idents=("045906013D  ", "R3 1.4.EDC  ", "G000SG 0477")):
    ecu = FakeECU(idents, groups, header_variant)
    port = FakePort(ecu)
    kwp = K.Kwp1281(port, sleep=lambda s: None)
    return ecu, port, kwp


class ConnectTest(unittest.TestCase):
    def test_full_handshake_and_idents(self):
        ecu, port, kwp = build()
        idents = kwp.connect(0x01)
        self.assertEqual(idents, ["045906013D", "R3 1.4.EDC", "G000SG 0477"])
        # comps: 1 (keyword 75) + 3 ident blocks * 15 + final ACK block * 3
        # (per block: length + counter + title + data each get a complement;
        # the 0x03 end gets none; ident wire = 16 bytes -> 15 comps)
        self.assertEqual(ecu.comps_checked, 1 + 3 * 15 + 3)
        self.assertEqual(kwp.counter, ecu._ctr)   # counter mirrors ECU
        self.assertEqual(len(port.rx), 0)          # all echoes consumed
        self.assertEqual(len(ecu.rx_partial), 0)  # no half blocks

    def test_bad_keyword_rejected(self):
        ecu = FakeECU(("x",))
        port = FakePort(ecu)

        def fake_init(addr=0x01, **kw):
            ecu.rx.extend(b"\x55\x01\x89")  # keyword != 0x018A
            return b"\x55\x01\x89"

        port.slow_init = fake_init
        kwp = K.Kwp1281(port, sleep=lambda s: None)
        with self.assertRaises(K.Kwp1281Error):
            kwp.connect(0x01)

    def test_no_sync_raises(self):
        ecu, port, kwp = build()
        port.slow_init = lambda **kw: b""
        with self.assertRaises(K.Kwp1281Error):
            kwp.connect(0x01)


class GroupTest(unittest.TestCase):
    TRIPLETS = bytes([0x01, 0xC8, 0x16,   # rpm: 200*22/5 = 880
                      0x05, 0x0F, 0x7C,   # unknown id -> raw
                      0x14, 0x64, 0x7E,   # %
                      0x04, 0x4B, 0x6D])

    def test_group_read_e7(self):
        ecu, port, kwp = build(groups={0x03: self.TRIPLETS})
        kwp.connect(0x01)
        fields = kwp.read_group(3)
        self.assertEqual([f[0] for f in fields], [0x01, 0x05, 0x14, 0x04])
        self.assertEqual(fields[0][1], 880.0)
        self.assertEqual(fields[0][2], "rpm")
        self.assertEqual(len(port.rx), 0)  # echo discipline held

    def test_group_header_variant(self):
        ecu, port, kwp = build(groups={0x45: bytes([0x01, 0x0A, 0x64])},
                               header_variant=True)
        kwp.connect(0x01)
        fields = kwp.read_group(0x45)
        self.assertEqual(fields[0][1], 0x0A * 0x64 / 5.0)

    def test_empty_group_returns_none(self):
        ecu, port, kwp = build(groups={})
        kwp.connect(0x01)
        self.assertIsNone(kwp.read_group(0x99))

    def test_bad_triplet_length(self):
        ecu, port, kwp = build(groups={0x02: b"\x01\x02"})  # not % 3
        kwp.connect(0x01)
        with self.assertRaises(K.Kwp1281Error):
            kwp.read_group(2)


class DecodeTest(unittest.TestCase):
    def test_table_anchors(self):
        self.assertEqual(K._decode(0x01, 200, 22), (880.0, "rpm"))
        self.assertEqual(K._decode(0x07, 100, 250), (250.0, "km/h"))
        self.assertEqual(K._decode(0x1A, 30, 120), (90, "°C"))
        self.assertEqual(K._decode(0x08, 0x01, 0x02), ((1 << 8) | 2, "binary"))
        self.assertEqual(K._decode(0x36, 1, 2), (258, "count"))
        self.assertEqual(K._decode(0x12, 50, 25), (50.0, "mbar"))
        self.assertEqual(K._decode(0x37, 100, 10), (5.0, "s"))
        self.assertEqual(K._decode(0x23, 100, 45), (45.0, "l/h"))


class DisconnectTest(unittest.TestCase):
    def test_disconnect_block(self):
        ecu, port, kwp = build()
        kwp.connect(0x01)
        frame = kwp.disconnect()
        self.assertEqual(frame, bytes([0x03, (ecu._ctr + 1) & 0xFF, 0x06, 0x03]))
        self.assertTrue(ecu.disconnected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
