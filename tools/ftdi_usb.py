#!/usr/bin/env python3
"""FT232R-over-usbdevfs K-line transport for Springtide (KKL on phone OTG).

The OnePlus 5T kernel (LineageOS, msm8998) ships no ftdi_sio and has no
module tree at all, so /dev/ttyUSB0 never appears. This module is a
userland FT232R driver: it opens the raw usbdevfs node
(/dev/bus/usb/<bus>/<dev>) and speaks usbfs ioctls + FTDI vendor requests
directly. Stdlib only; Python 3.10+.

Ground truth (constants are bit-exact ports, not folklore):
  usbfs protocol    include/uapi/linux/usbdevice_fs.h (torvalds tree)
  FTDI requests     drivers/usb/serial/ftdi_sio.h
  baud divisor      ftdi_232bm_baud_base_to_divisor() in ftdi_sio.c, base
                    48 MHz. 10400 baud -> wValue 0x4120, wIndex 0
                    (divisor 288.5, 10398.6 baud effective, -0.013%).
  break             FTDI_SIO_SET_DATA bit 14 (0x4000) == TX held low.

K-line init (ISO 14230-2):
  slow init ("5 baud"): the FT232R cannot clock 5 baud (min ~184), so the
    pulse train is synthesized from BREAK (line low) and TX-idle (line
    high) half-bits at 200 ms each. Works for any address; 0x01 (engine)
    is the Springtide case.
  fast init (KWP2000): ~50 ms break, brief idle, request at 10400.

Permissions: /dev/bus/usb/* nodes are root-only on Android. Run the smoke
test under `su -c`, or grant per-attach with
`su -c 'chmod 666 /dev/bus/usb/001/002'` (node number changes on replug).
"""

import ctypes
import errno
import os
import struct
import time

# ---------------------------------------------------------------- usbdevfs

_IOC_WRITE, _IOC_READ = 1, 2


def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | ((t & 0xFF) << 8) | (nr & 0xFF)


class _CtrlTransfer(ctypes.Structure):
    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


class _BulkTransfer(ctypes.Structure):
    _fields_ = [
        ("ep", ctypes.c_uint32),
        ("len", ctypes.c_uint32),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


class _ConnectInfo(ctypes.Structure):
    _fields_ = [("devnum", ctypes.c_uint32), ("slow", ctypes.c_uint8)]


_U = 0x55  # 'U'
_CTRL_SZ = ctypes.sizeof(_CtrlTransfer)
_BULK_SZ = ctypes.sizeof(_BulkTransfer)

USBDEVFS_CONTROL = _IOC(_IOC_WRITE | _IOC_READ, _U, 0, _CTRL_SZ)
USBDEVFS_BULK = _IOC(_IOC_WRITE | _IOC_READ, _U, 2, _BULK_SZ)
USBDEVFS_RESETEP = _IOC(_IOC_READ, _U, 3, 4)
USBDEVFS_SETINTERFACE = _IOC(_IOC_READ, _U, 4, 8)
USBDEVFS_CLAIMINTERFACE = _IOC(_IOC_READ, _U, 15, 4)
USBDEVFS_RELEASEINTERFACE = _IOC(_IOC_READ, _U, 16, 4)
USBDEVFS_CONNECTINFO = _IOC(_IOC_WRITE, _U, 17, ctypes.sizeof(_ConnectInfo))
USBDEVFS_RESET = _IOC(0, _U, 20, 0)
USBDEVFS_CLEAR_HALT = _IOC(_IOC_READ, _U, 21, 4)
USBDEVFS_GET_CAPABILITIES = _IOC(_IOC_READ, _U, 26, 4)

USBDEVFS_CAP_NO_PACKET_SIZE_LIM = 0x04

_libc = ctypes.CDLL(None, use_errno=True)
_libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
_libc.ioctl.restype = ctypes.c_int


def _ioctl(fd, req, arg):
    rv = _libc.ioctl(fd, req, arg)
    if rv < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return rv


class UsbDevice:
    """Raw usbdevfs access to one USB device node."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get_capabilities(self):
        caps = ctypes.c_uint32(0)
        _ioctl(self.fd, USBDEVFS_GET_CAPABILITIES, ctypes.byref(caps))
        return caps.value

    def connect_info(self):
        ci = _ConnectInfo()
        _ioctl(self.fd, USBDEVFS_CONNECTINFO, ctypes.byref(ci))
        return {"devnum": ci.devnum, "slow": bool(ci.slow)}

    def claim_interface(self, iface=0):
        _ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, ctypes.byref(ctypes.c_uint32(iface)))

    def release_interface(self, iface=0):
        _ioctl(self.fd, USBDEVFS_RELEASEINTERFACE, ctypes.byref(ctypes.c_uint32(iface)))

    def clear_halt(self, ep):
        _ioctl(self.fd, USBDEVFS_CLEAR_HALT, ctypes.byref(ctypes.c_uint32(ep)))

    def control(self, rtype, request, value, index, data=None, timeout_ms=1000):
        """Control transfer. data: bytes (OUT) / None / int length (IN).
        Returns (n_transferred, data_in_bytes)."""
        length = 0
        buf = None
        if isinstance(data, int):
            length = data
            buf = ctypes.create_string_buffer(max(length, 1))
        elif data is not None:
            data = bytes(data)
            length = len(data)
            if length:
                buf = ctypes.create_string_buffer(data, length)
        st = _CtrlTransfer(
            rtype, request, value, index, length, timeout_ms,
            ctypes.addressof(buf) if buf is not None else None,
        )
        n = _ioctl(self.fd, USBDEVFS_CONTROL, ctypes.byref(st))
        if buf is None:
            return n, b""
        return n, buf.raw[: max(n, 0)]

    def bulk(self, ep, data, timeout_ms=1000):
        """Synchronous bulk transfer on endpoint address `ep`.
        `data` is a bytearray of the requested length; on IN the buffer is
        filled in place. Returns n_transferred."""
        assert isinstance(data, bytearray)
        buf = (ctypes.c_char * len(data)).from_buffer(data) if len(data) else None
        st = _BulkTransfer(
            ep & 0xFF, len(data), timeout_ms,
            ctypes.addressof(buf) if buf is not None else None,
        )
        return _ioctl(self.fd, USBDEVFS_BULK, ctypes.byref(st))

    # -- descriptors ------------------------------------------------

    def device_descriptor(self):
        n, d = self.control(0x80, 0x06, 0x0100, 0x0000, 18, 2000)
        if n < 18:
            raise OSError(errno.ENODATA, "short device descriptor")
        f = struct.unpack_from("<BBHBBBBHHHBBBB", d)
        return {
            "bcdUSB": f[2], "bDeviceClass": f[3], "bMaxPacketSize0": f[6],
            "idVendor": f[7], "idProduct": f[8], "bcdDevice": f[9],
            "iManufacturer": f[10], "iProduct": f[11], "iSerialNumber": f[12],
            "bNumConfigurations": f[13],
        }

    def config_descriptor(self):
        n, d = self.control(0x80, 0x06, 0x0200, 0x0000, 9, 2000)
        if n < 9:
            raise OSError(errno.ENODATA, "short config descriptor")
        total = struct.unpack_from("<H", d, 2)[0]
        n, d = self.control(0x80, 0x06, 0x0200, 0x0000, total, 2000)
        if n < total:
            raise OSError(errno.ENODATA, "short config descriptor")
        out = {"interfaces": []}
        i = 0
        cur = None
        while i + 2 <= len(d):
            dlen, dtype = d[i], d[i + 1]
            if dlen < 2:
                break
            if dtype == 4:  # interface
                (num, alt, neps, cls, sub, proto) = struct.unpack_from("<BBBBBB", d, i + 2)
                cur = {"num": num, "alt": alt, "class": cls, "endpoints": []}
                out["interfaces"].append(cur)
            elif dtype == 5 and cur is not None:  # endpoint
                addr, attrs, _psz, _ival = struct.unpack_from("<BBHB", d, i + 2)
                cur["endpoints"].append(
                    {"address": addr, "transfer": (attrs >> 0) & 0x03,
                     "max_packet_size": struct.unpack_from("<H", d, i + 4)[0]}
                )
            i += dlen
        return out

    def string_descriptor(self, index, langid=0x0409):
        if index == 0:
            return None
        n, d = self.control(0x80, 0x06, (0x03 << 8) | index, langid, 255, 2000)
        if n < 2:
            return None
        return d[2: n].decode("utf-16-le", errors="replace")


# ------------------------------------------------------------- FT232R layer

FTDI_VID = 0x0403
FT232R_PID = 0x6001

# ftdi_sio.h request codes
FTDI_SIO_RESET = 0            # wValue: 0 reset SIO, 1 purge RX, 2 purge TX
FTDI_SIO_MODEM_CTRL = 1
FTDI_SIO_SET_FLOW_CTRL = 2    # wValue 0 = disable
FTDI_SIO_SET_BAUD_RATE = 3
FTDI_SIO_SET_DATA = 4         # bits0-7 data bits, 8-10 parity, 11-13 stop, 14 break
FTDI_SIO_GET_MODEM_STATUS = 5
FTDI_SIO_SET_EVENT_CHAR = 6
FTDI_SIO_SET_ERROR_CHAR = 7
FTDI_SIO_SET_LATENCY_TIMER = 9
FTDI_SIO_GET_LATENCY_TIMER = 0x0A
FTDI_SIO_READ_EEPROM = 0x90

FTDI_SIO_RESET_SIO = 0
FTDI_SIO_RESET_PURGE_RX = 1
FTDI_SIO_RESET_PURGE_TX = 2
FTDI_SIO_SET_BREAK = 0x1 << 14  # ftdi_sio.h: bit 14 == break (TX low)

# fraction index -> hardware code (ftdi_sio.c ftdi_232bm_baud_base_to_divisor)
_DIVFRAC_CODE = (0, 3, 2, 4, 1, 5, 6, 7)
# hardware fraction code -> actual fraction (cross-checked against the
# ftdi_232am comments: code 3 = "+0.125", code 2 = "+0.25", code 1 = "+0.5")
_FRAC_VALUE = {0: 0.0, 1: 0.5, 2: 0.25, 3: 0.125, 4: 0.375, 5: 0.625, 6: 0.75, 7: 0.875}

FTDI_BASE_HZ = 48_000_000


def bm_divisor(baud, base=FTDI_BASE_HZ):
    """Bit-exact port of ftdi_232bm_baud_base_to_divisor(). 17-bit result:
    bits 0-13 integer, 14-16 fraction code (code spills into wIndex)."""
    divisor3 = (base + baud) // (2 * baud)  # DIV_ROUND_CLOSEST(base, 2*baud)
    if not 8 <= divisor3 <= 0x1FFFF:
        # int field (divisor3>>3) is 14 bits; < 8 means baud > 3,000,000
        raise ValueError(f"baud {baud} outside FT232R range (~184..3,000,000)")
    divisor = divisor3 >> 3
    divisor |= _DIVFRAC_CODE[divisor3 & 0x7] << 14
    if divisor == 1:        # 1.0 special case
        divisor = 0
    elif divisor == 0x4001:  # 1.5 special case
        divisor = 1
    return divisor



def bm_baud_request(baud):
    """Returns (wValue, wIndex) for FTDI_SIO_SET_BAUD_RATE."""
    d = bm_divisor(baud)
    return d & 0xFFFF, d >> 16


def bm_effective_baud(baud):
    """Baud the chip actually clocks for request(baud) — for logging/tests."""
    d = bm_divisor(baud)
    frac = _FRAC_VALUE[(d >> 14) & 0x7]
    return 3_000_000 / ((d & 0x3FFF) + frac) if d else 3_000_000.0


def set_data_value(bits=8, parity="none", stop=1, break_=False):
    """wValue for FTDI_SIO_SET_DATA (ftdi_sio.h bit layout)."""
    if bits < 5 or bits > 8:
        raise ValueError("data bits must be 5..8")
    par = {"none": 0, "odd": 1, "even": 2, "mark": 3, "space": 4}[parity]
    stopc = {1: 0, 1.5: 1, 2: 2}[stop]
    v = bits | (par << 8) | (stopc << 11)
    if break_:
        v |= FTDI_SIO_SET_BREAK
    return v


class KLineTimeout(Exception):
    """Timed out reading from the K-line; .data carries any partial bytes."""

    def __init__(self, data=b"", *a):
        super().__init__(*[f"K-line read timeout, partial={data.hex()}"] + list(a))
        self.data = data


class Ftdi232R:
    """FT232R UART control + bulk data endpoints. `dev` is a UsbDevice
    (or anything with .control/.bulk for tests)."""

    def __init__(self, dev, interface=0):
        self.dev = dev
        self.interface = interface
        self._data_value = set_data_value()  # 8N1, break off
        cfg = dev.config_descriptor()
        eps = [
            e
            for i in cfg["interfaces"]
            if i["num"] == interface
            for e in i["endpoints"]
            if e["transfer"] == 2
        ]
        self.ep_in = next((e["address"] for e in eps if e["address"] & 0x80), None)
        self.ep_out = next((e["address"] for e in eps if not e["address"] & 0x80), None)
        self.max_packet_size = min([e["max_packet_size"] for e in eps] or [64])
        if not self.ep_in or not self.ep_out:
            raise OSError(errno.ENODEV, "no bulk endpoint pair on interface")

    # -- FTDI vendor requests ---------------------------------------

    def _ctrl(self, request, value, index=0, data=None, timeout_ms=1000):
        return self.dev.control(0x40, request, value, index, data, timeout_ms)

    def sio_reset(self):
        self._ctrl(FTDI_SIO_RESET, FTDI_SIO_RESET_SIO)

    def purge_rx(self):
        self._ctrl(FTDI_SIO_RESET, FTDI_SIO_RESET_PURGE_RX)

    def purge_tx(self):
        self._ctrl(FTDI_SIO_RESET, FTDI_SIO_RESET_PURGE_TX)

    def purge(self):
        self.sio_reset()
        self.purge_rx()
        self.purge_tx()

    def set_dtr_rts(self, dtr=True, rts=True):
        v = (0x0101 if dtr else 0x0100) | (0x0202 if rts else 0x0200)
        self._ctrl(FTDI_SIO_MODEM_CTRL, v)

    def set_flow_none(self):
        self._ctrl(FTDI_SIO_SET_FLOW_CTRL, 0)

    def disable_event_chars(self):
        self._ctrl(FTDI_SIO_SET_EVENT_CHAR, 0)
        self._ctrl(FTDI_SIO_SET_ERROR_CHAR, 0)

    def set_baud(self, baud):
        value, index = bm_baud_request(baud)
        self._ctrl(FTDI_SIO_SET_BAUD_RATE, value, index)
        return value, index

    def set_line(self, bits=8, parity="none", stop=1):
        self._data_value = set_data_value(bits, parity, stop)
        self._ctrl(FTDI_SIO_SET_DATA, self._data_value)

    def set_break(self, on):
        v = self._data_value | (FTDI_SIO_SET_BREAK if on else 0)
        self._ctrl(FTDI_SIO_SET_DATA, v)

    def set_latency(self, ms):
        if not 1 <= ms <= 255:
            raise ValueError("latency timer must be 1..255 ms")
        self._ctrl(FTDI_SIO_SET_LATENCY_TIMER, ms)

    def get_latency(self):
        n, d = self.dev.control(0xC0, FTDI_SIO_GET_LATENCY_TIMER, 0, 0, 1, 1000)
        if n != 1:
            raise OSError(errno.ENODATA, f"latency read returned {n} bytes")
        return d[0]

    def read_eeprom_word(self, addr):
        n, d = self.dev.control(0xC0, FTDI_SIO_READ_EEPROM, 0, addr, 2, 1000)
        if n != 2:
            raise OSError(errno.ENODATA, f"eeprom word {addr} read returned {n} bytes")
        return struct.unpack("<H", d)[0]

    # -- data --------------------------------------------------------

    def write(self, data, timeout_ms=1000):
        data = bytes(data)
        sent = 0
        while sent < len(data):
            chunk = data[sent: sent + self.max_packet_size]
            sent += self.dev.bulk(self.ep_out, bytearray(chunk), timeout_ms)
        return sent

    def read(self, n, timeout_ms=1000):
        """One bulk read, returns 0..n bytes. Timeout raises KLineTimeout."""
        buf = bytearray(n)
        try:
            got = self.dev.bulk(self.ep_in, buf, timeout_ms)
        except OSError as e:
            # ETIMEDOUT = quiet line; EOVERFLOW/EILSEQ = babble/CRC errors the
            # host controller reports when the line carries analog garbage —
            # both mean "no usable data", not a fatal driver state.
            if e.errno in (errno.ETIMEDOUT, errno.EAGAIN, errno.EOVERFLOW,
                          errno.EILSEQ):
                return b""
            raise
        return bytes(buf[:got])

    def read_exact(self, n, timeout_ms, sleep=None):
        """Read exactly n bytes before the deadline or raise KLineTimeout."""
        sleep = sleep or time.sleep
        deadline = time.monotonic() + timeout_ms / 1000.0
        out = b""
        while len(out) < n:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            got = self.read(min(n - len(out), 4096), max(remaining_ms, 1))
            if got:
                out += got
            else:
                sleep(0.001)
        if len(out) < n:
            raise KLineTimeout(out)
        return out


# ------------------------------------------------------------ K-line layer

def five_baud_halfbits(addr):
    """5-baud init pulse train as (level, halfbit_count) edges.
    VAG/KW1281 format (mnaberez/vwradio kwp1281.c, proven on real VAG
    modules): start bit (low) + 7 data bits LSB-first + even-ish parity bit
    (parity = 1 XOR data bits) + stop bit (high), 200 ms each.
    level 1 = line high (TX idle), 0 = line low (BREAK)."""
    seq = [0]
    parity = 1
    for i in range(7):
        bit = (addr >> i) & 1
        parity ^= bit
        seq.append(bit)
    seq += [parity, 1]
    edges = []
    for lvl in seq:
        if edges and edges[-1][0] == lvl:
            edges[-1][1] += 1
        else:
            edges.append([lvl, 1])
    return [(lvl, cnt) for lvl, cnt in edges]


class KKLPort:
    """K-line serial port on an FT232R-based KKL cable.

    `ftdi` injectable for tests (Ftdi232R duck-type), `sleep` injectable.
    """

    def __init__(self, ftdi, latency_ms=2, sleep=time.sleep):
        self.f = ftdi
        self.sleep = sleep
        ftdi.purge()
        ftdi.set_dtr_rts()
        ftdi.set_flow_none()
        ftdi.disable_event_chars()
        ftdi.set_latency(latency_ms)

    def open_10400(self):
        """The KW1281/KWP2000-on-VAG-K-line line settings: 10400 baud 8N1."""
        return self.f.set_baud(10400), self.f.set_line(8, "none", 1)

    def slow_init(self, addr=0x01, bit_ms=200.0, sync_timeout_ms=1500, n=3):
        """ISO 14230-2 5-baud slow init, break-synthesized. Returns the
        ECU's first reply bytes (n bytes, typically sync 0x55 + KW1281
        keyword 01 8A) or b'' / partial if the line stayed silent."""
        self.f.purge()
        self.f.set_line()  # break off
        for lvl, count in five_baud_halfbits(addr):
            self.f.set_break(lvl == 0)
            self.sleep(count * bit_ms / 1000.0)
        # the stop-bit edge always ends with break off (line idle high)
        self.f.purge_rx()
        try:
            return self.f.read_exact(n, sync_timeout_ms, sleep=self.sleep)
        except KLineTimeout as e:
            return e.data

    def fast_init(self, break_ms=50.0, idle_ms=25.0):
        """KWP2000 fast init: break, idle, then caller sends the request."""
        self.f.purge()
        self.f.set_line()
        self.f.set_break(True)
        self.sleep(break_ms / 1000.0)
        self.f.set_break(False)
        self.sleep(idle_ms / 1000.0)
        self.f.purge_rx()

    def write(self, data, timeout_ms=1000):
        return self.f.write(data, timeout_ms)

    def read_exact(self, n, timeout_ms):
        return self.f.read_exact(n, timeout_ms, sleep=self.sleep)

    def read(self, n, timeout_ms=1000):
        return self.f.read(n, timeout_ms)


# ------------------------------------------------------------ discovery

def find_ft232r(vid=FTDI_VID, pid=FT232R_PID, bus_root="/dev/bus/usb"):
    """Scan /dev/bus/usb for the FT232R. Returns the node path.
    Raises FileNotFoundError with a permission hint if nodes exist but were
    unreadable (run under su or chmod 666 the node)."""
    denied = []
    for bus in sorted(os.listdir(bus_root)):
        bp = os.path.join(bus_root, bus)
        for node in sorted(os.listdir(bp)):
            path = os.path.join(bp, node)
            try:
                d = UsbDevice(path)
            except OSError as e:
                if e.errno in (errno.EACCES,):
                    denied.append(path)
                continue
            try:
                dd = d.device_descriptor()
            except OSError:
                dd = None
            finally:
                d.close()
            if dd and dd["idVendor"] == vid and dd["idProduct"] == pid:
                return path
    hint = ""
    if denied:
        hint = (
            f" (found but unreadable: {' '.join(denied)} — run `su -c "
            f"'chmod 666 {denied[0]}'` or run under su)"
        )
    raise FileNotFoundError(f"no {vid:04x}:{pid:04x} device under {bus_root}{hint}")
