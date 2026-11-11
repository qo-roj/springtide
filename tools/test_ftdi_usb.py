#!/usr/bin/env python3
"""Hardware-free unit tests for tools/ftdi_usb.py.

Run: python3 tools/test_ftdi_usb.py  (from the springtide repo root, or
     anywhere — the test adds its own dir to sys.path).

Anchors:
  * ioctl numbers against hand-computed _IOC literals from
    include/uapi/linux/usbdevice_fs.h (guards struct-size bugs)
  * baud divisor against known-good values (9600 = 0x4138/0 is the
    classic KKL constant) and effective-baud tolerance
  * 5-baud init pulse-train synthesis (edge merge, total timing)
  * slow_init handshake against a fake FT232R (no device needed)
"""

import ctypes
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ftdi_usb as F  # noqa: E402


class DescriptorTest(unittest.TestCase):
    def test_device_descriptor_parse(self):
        # synthetic FT232R device descriptor (18 bytes, correct layout)
        raw = bytes([
            0x12, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00, 0x40,
            0x03, 0x04, 0x01, 0x60, 0x00, 0x06, 0x01, 0x02,
            0x03, 0x01,
        ])
        dev = F.UsbDevice.__new__(F.UsbDevice)
        dev.control = lambda *a, **k: (18, raw)
        dd = dev.device_descriptor()
        self.assertEqual(dd["idVendor"], 0x0403)
        self.assertEqual(dd["idProduct"], 0x6001)
        self.assertEqual(dd["bMaxPacketSize0"], 0x40)
        self.assertEqual(dd["iSerialNumber"], 3)
        self.assertEqual(dd["bNumConfigurations"], 1)

    def test_config_descriptor_parse(self):
        # config(9) + interface(9) + ep in(7) + ep out(7), all bulk
        cfg = bytes([
            0x09, 0x02, 0x20, 0x00, 0x01, 0x01, 0x00, 0x80, 0xFA,  # 32 bytes total
            0x09, 0x04, 0x00, 0x00, 0x02, 0xFF, 0xFF, 0xFF, 0x00,  # iface 0, 2 eps
            0x07, 0x05, 0x81, 0x02, 0x40, 0x00, 0x00,             # bulk IN  0x81
            0x07, 0x05, 0x02, 0x02, 0x40, 0x00, 0x00,             # bulk OUT 0x02
        ])
        dev = F.UsbDevice.__new__(F.UsbDevice)
        dev.control = lambda *a, **k: (len(cfg), cfg)
        c = dev.config_descriptor()
        self.assertEqual(len(c["interfaces"]), 1)
        eps = c["interfaces"][0]["endpoints"]
        self.assertEqual(sorted(e["address"] for e in eps), [0x02, 0x81])
        self.assertTrue(all(e["transfer"] == 2 for e in eps))
        self.assertTrue(all(e["max_packet_size"] == 64 for e in eps))


class IoctlTest(unittest.TestCase):
    def test_numbers(self):
        # dir<<30 | size<<16 | 'U'<<8 | nr   (from usbdevice_fs.h)
        self.assertEqual(F.USBDEVFS_CONTROL, 0xC0185500)
        self.assertEqual(F.USBDEVFS_BULK, 0xC0185502)
        self.assertEqual(F.USBDEVFS_RESETEP, 0x80045503)
        self.assertEqual(F.USBDEVFS_SETINTERFACE, 0x80085504)
        self.assertEqual(F.USBDEVFS_CLAIMINTERFACE, 0x8004550F)
        self.assertEqual(F.USBDEVFS_RELEASEINTERFACE, 0x80045510)
        self.assertEqual(F.USBDEVFS_CONNECTINFO, 0x40085511)
        self.assertEqual(F.USBDEVFS_RESET, 0x00005514)
        self.assertEqual(F.USBDEVFS_CLEAR_HALT, 0x80045515)
        self.assertEqual(F.USBDEVFS_GET_CAPABILITIES, 0x8004551A)

    def test_struct_layout(self):
        self.assertEqual(ctypes.sizeof(F._CtrlTransfer), 24)
        self.assertEqual(ctypes.sizeof(F._BulkTransfer), 24)
        off = dict((name, getattr(F._CtrlTransfer, name).offset)
                   for name, _t in F._CtrlTransfer._fields_)
        self.assertEqual(off["wLength"], 6)
        self.assertEqual(off["timeout"], 8)
        self.assertEqual(off["data"], 16)  # ptr at 16 => padding is right


class DivisorTest(unittest.TestCase):
    def test_known_values(self):
        # (baud, wValue, wIndex) — kernel ftdi_232bm encoding
        cases = [
            (9600, 0x4138, 0),   # the classic KKL constant, D = 312.5
            (10400, 0x4120, 0),  # D = 288.5 -> 10398.6 baud (-0.013%)
            (19200, 0x809C, 0),
            (38400, 0xC04E, 0),
            (4800, 0x0271, 0),
            (1200, 0x09C4, 0),
            (115200, 0x001A, 0),
        ]
        for baud, value, index in cases:
            got = F.bm_baud_request(baud)
            self.assertEqual(got, (value, index), f"baud {baud}")

    def test_special_cases(self):
        self.assertEqual(F.bm_baud_request(3_000_000), (0x0000, 0))
        self.assertEqual(F.bm_baud_request(2_000_000), (0x0001, 0))

    def test_effective_baud_tolerance(self):
        for baud in (1200, 4800, 9600, 10400, 19200, 38400, 57600, 115200):
            eff = F.bm_effective_baud(baud)
            self.assertLessEqual(
                abs(eff - baud) / baud, 0.005,
                f"baud {baud}: effective {eff:.1f}",
            )

    def test_effective_10400(self):
        # D = 288.5 -> 10398.6 baud (-0.013%), well inside ISO 14230 +-2%
        self.assertAlmostEqual(F.bm_effective_baud(10400), 10398.61, places=1)

    def test_out_of_range(self):
        for bad in (100, 183, 4_000_000):
            with self.assertRaises(ValueError):
                F.bm_baud_request(bad)


class SetDataTest(unittest.TestCase):
    def test_8n1(self):
        self.assertEqual(F.set_data_value(), 0x0008)
        self.assertEqual(F.set_data_value(break_=True), 0x4008)

    def test_other_formats(self):
        self.assertEqual(F.set_data_value(7, "even", 2), 0x1207)
        self.assertEqual(F.set_data_value(8, "odd", 1.5), 0x0908)


class FiveBaudTest(unittest.TestCase):
    def test_engine_address_0x01(self):
        # start low, bit0 high, bits1-7 low, stop high — 4 edges
        self.assertEqual(
            F.five_baud_halfbits(0x01),
            [(0, 1), (1, 1), (0, 7), (1, 1)],
        )

    def test_all_addresses_shape(self):
        for addr in range(256):
            edges = F.five_baud_halfbits(addr)
            total = sum(c for _l, c in edges)
            self.assertEqual(total, 10, f"addr 0x{addr:02x} total halfbits")
            self.assertEqual(edges[0][0], 0)   # start bit low
            self.assertEqual(edges[-1][0], 1)  # stop bit high
            for i in range(len(edges) - 1):     # properly merged
                self.assertNotEqual(edges[i][0], edges[i + 1][0])

    def test_specific_addresses(self):
        # 0x00: all data bits 0, parity = 1 -> low 1600 ms, then high 400 ms
        self.assertEqual(F.five_baud_halfbits(0x00), [(0, 8), (1, 2)])
        # 0x17: bits b0..b6 = 1,1,1,0,1,0,0; parity = 1^0 = 1
        self.assertEqual(
            F.five_baud_halfbits(0x17),
            [(0, 1), (1, 3), (0, 1), (1, 1), (0, 2), (1, 2)],
        )
        # 0xFF: all 7 data bits 1, parity = 1^1 = 0
        self.assertEqual(F.five_baud_halfbits(0xFF), [(0, 1), (1, 7), (0, 1), (1, 1)])


class FakeFtdi:
    """Records FTDI ops; read_exact pulls from a scripted byte stream."""

    def __init__(self, stream=b"", read_chunk=None):
        self.stream = bytearray(stream)
        self.breaks = []      # (level, monotonic-ish clock ms)
        self.controlled = []
        self._clock = 0.0
        self._chunk = read_chunk

    def _tick(self, ms):
        self._clock += ms
        self.breaks[-1] = (self.breaks[-1][0], self._clock)

    # -- Ftdi232R duck type --
    def purge(self):
        self.controlled.append(("purge",))

    def purge_rx(self):
        self.controlled.append(("purge_rx",))

    def purge_tx(self):
        pass

    def set_dtr_rts(self):
        pass

    def set_flow_none(self):
        pass

    def disable_event_chars(self):
        pass

    def set_latency(self, ms):
        pass

    def set_baud(self, baud):
        return F.bm_baud_request(baud)

    def set_line(self, *a):
        self.controlled.append(("set_line", a))

    def set_break(self, on):
        self.breaks.append((1 if on else 0, self._clock))

    def write(self, data, timeout_ms=1000):
        return len(data)

    def read_exact(self, n, timeout_ms, sleep=None):
        got = bytes(self.stream[:n])
        del self.stream[:n]
        if len(got) < n:
            raise F.KLineTimeout(got)
        return got


class SlowInitTest(unittest.TestCase):
    def _run(self, addr, stream=b""):
        fake = FakeFtdi(stream)
        port = F.KKLPort(fake, sleep=lambda s: fake._tick(s * 1000))
        result = port.slow_init(addr=addr, bit_ms=200)
        return fake, result

    def test_engine_handshake(self):
        keybytes = b"\x55\x01\x8a"  # KW1281 sync + keyword (0x018A)
        fake, result = self._run(0x01, keybytes)
        self.assertEqual(result, keybytes)
        # FakeFtdi records (break_on, end_clock_ms); level 1 = break = line LOW.
        # 0x01 -> start low 200, bit0 high 200, bits1-7 low 1400, stop high 200.
        seq = fake.breaks
        durs = []
        for i, (lvl, t) in enumerate(seq):
            prev = seq[i - 1][1] if i else 0.0
            durs.append((lvl, t - prev))
        self.assertEqual(
            durs, [(1, 200.0), (0, 200.0), (1, 1400.0), (0, 200.0)]
        )

    def test_silent_line_returns_empty(self):
        fake, result = self._run(0x01)
        self.assertEqual(result, b"")

    def test_total_duration_is_2s(self):
        fake, _ = self._run(0x01)
        self.assertAlmostEqual(fake.breaks[-1][1], 2000.0, places=6)


class ReadExactTest(unittest.TestCase):
    class PartialDev:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def bulk(self, ep, buf, timeout_ms):
            if not self.chunks:
                raise OSError(110, "ETIMEDOUT")
            data = self.chunks.pop(0)
            buf[: len(data)] = data
            return len(data)

    def test_partial_then_timeout(self):
        ftdi = F.Ftdi232R.__new__(F.Ftdi232R)
        ftdi.dev = self.PartialDev([b"\x55"])
        ftdi.ep_in, ftdi.ep_out = 0x81, 0x02
        with self.assertRaises(F.KLineTimeout) as cm:
            ftdi.read_exact(4, 10)
        self.assertEqual(cm.exception.data, b"\x55")

    def test_exact_accumulates(self):
        ftdi = F.Ftdi232R.__new__(F.Ftdi232R)
        ftdi.dev = self.PartialDev([b"\x55", b"\x08\x08\x01"])
        ftdi.ep_in, ftdi.ep_out = 0x81, 0x02
        out = ftdi.read_exact(4, 1000)
        self.assertEqual(out, b"\x55\x08\x08\x01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
