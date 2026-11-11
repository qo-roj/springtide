#!/usr/bin/env python3
"""KW1281 protocol layer on top of KKLPort (Springtide, KKL on phone).

Protocol behavior per mnaberez/vwradio kwp1281.c (reference implementation,
proven on real VAG modules) — ported, not copied:

  connect:  5-baud address -> ECU sync 0x55 -> keyword 01 8A (KW1281)
            -> tester sends 75 (complement of 0x8A, after ~30 ms)
            -> ECU sends ident blocks (title 0xF6, 12 ASCII bytes),
            each ACKed by the tester with an ACK block (title 0x09),
            until the ECU itself sends an ACK block.
  frames:   [len][counter][title][data...][0x03]
            len counts all bytes after itself (counter .. end).
            NO checksum — integrity is per byte: every received byte
            except the 0x03 end must be ACKed with its COMPLEMENT (b^0xFF),
            and every byte we send comes back as an ECHO on the single-wire
            bus and must be consumed and validated.
  group:    send title 0x29 + group -> response 0xE7 whose data is
            (id,a,b) triplets; 0x02-header/0xF4-data variant on some ECUs;
            0x3F = string field.
  timing:   1 ms interbyte, 10 ms interblock, 3 s recv timeout, 100 ms
            echo timeout (vwradio constants).
"""

import time

KWP_ACK = 0x09
KWP_NAK = 0x0A
KWP_DISCONNECT = 0x06
KWP_GROUP_READING = 0x29
KWP_R_GROUP_READ_HEADER = 0x02
KWP_R_GROUP_READING = 0xE7
KWP_R_GROUP_READ_DATA = 0xF4
KWP_R_ASCII_DATA = 0xF6

KW1281_KEYWORD = 0x018A

INTERBYTE_S = 0.001
INTERBLOCK_S = 0.010
POSTKEYWORD_S = 0.030
RECV_TIMEOUT_MS = 3000
ECHO_TIMEOUT_MS = 150

# jazdw vag-blocks decode table: field id -> (formula, units)
def _decode(id_, a, b):
    """Decode a (id, a, b) measurement triplet. Returns (value, units)."""
    if id_ == 0x01:
        return a * b / 5.0, "rpm"
    if id_ == 0x04:
        return abs(b - 127) * 0.01 * a, "° ATDC" if b > 127 else "° BTDC"
    if id_ == 0x07:
        return 0.01 * a * b, "km/h"
    if id_ == 0x11:
        return bytes([a, b]).decode("ascii", "replace"), "ASCII"
    if id_ == 0x12:
        return a * b / 25.0, "mbar"
    if id_ == 0x14:
        return a * b / 128.0 - 1, "%"
    if id_ == 0x15:
        return a * b / 1000.0, "V"
    if id_ == 0x16:
        return 0.001 * a * b, "ms"
    if id_ == 0x17:
        return b * a / 256.0, "%"
    if id_ == 0x1A:
        return b - a, "°C"
    if id_ == 0x21:
        return 100.0 * b / a if a else 100.0 * b, "%"
    if id_ == 0x22:
        return (b - 128) * 0.01 * a, "kW"
    if id_ == 0x23:
        return a * b / 100.0, "l/h"
    if id_ == 0x27:
        return a * b / 256.0, "mg/stk"
    if id_ == 0x31:
        return a * b / 40.0, "mg/stk"
    if id_ == 0x33:
        return (b - 128) / 255.0 * a, "mg/stk Δ"
    if id_ == 0x36:
        return a * 256 + b, "count"
    if id_ == 0x37:
        return a * b / 200.0, "s"
    if id_ == 0x3F:
        return None, "string"
    if id_ in (0x08, 0x10, 0x25):
        return (a << 8) | b, "binary"
    return (a << 8) | b, "raw"


class Kwp1281Error(Exception):
    pass


class Kwp1281:
    """KW1281 client. `port` is a KKLPort (or duck-type: write, read_exact,
    slow_init) — injectable for tests."""

    def __init__(self, port, log=None, sleep=time.sleep):
        self.p = port
        self.counter = 0
        self.log = log or (lambda *a: None)
        self.sleep = sleep

    # ---- byte layer ------------------------------------------------

    def _send_byte(self, b):
        self.p.write(bytes([b]), timeout_ms=500)
        echo = self.p.read_exact(1, ECHO_TIMEOUT_MS)
        if echo != bytes([b]):
            raise Kwp1281Error(
                f"TX echo mismatch: sent {b:02x} got {echo.hex()}")

    def _recv_byte_ack(self, timeout_ms=RECV_TIMEOUT_MS):
        c = self.p.read_exact(1, timeout_ms)[0]
        self._send_byte(c ^ 0xFF)  # complement ACK (echo consumed inside)
        return c

    # ---- block layer -----------------------------------------------

    def send_block(self, title, data=b""):
        """Transmit [len][ctr][title][data][0x03]; returns the frame."""
        self.counter = (self.counter + 1) & 0xFF
        payload = bytes([self.counter, title]) + bytes(data) + b"\x03"
        frame = bytes([len(payload)]) + payload
        for i, b in enumerate(frame):
            if i:
                self.sleep(INTERBYTE_S)
            self._send_byte(b)
        self.sleep(INTERBLOCK_S)
        self.log("TX", frame)
        return frame

    def recv_block(self, timeout_ms=RECV_TIMEOUT_MS):
        """Receive one block, ACKing every byte with its complement
        (except the 0x03 end marker). Returns dict(length, counter,
        title, data)."""
        length = self.p.read_exact(1, timeout_ms)[0]
        if length < 3:
            raise Kwp1281Error(f"bad block length {length}")
        self._send_byte(length ^ 0xFF)
        counter = self._recv_byte_ack()
        self.counter = counter  # mirror the ECU's counter
        buf = bytearray([length, counter])
        for i in range(length - 1):  # title + data + end marker
            is_end = i == length - 2
            if is_end:
                c = self.p.read_exact(1, timeout_ms)[0]
                if c != 0x03:
                    raise Kwp1281Error(
                        f"block end expected, got {c:02x} (buf {buf.hex()})")
            else:
                c = self._recv_byte_ack()
            buf.append(c)
        self.log("RX", bytes(buf))
        return {"length": length, "counter": counter,
                "title": buf[2], "data": bytes(buf[3:-1])}

    # ---- session ---------------------------------------------------

    def connect(self, addr=0x01, sync_timeout_ms=1500):
        """5-baud init + handshake + ident blocks. Returns the ident
        strings (VAG number, component, coding)."""
        reply = self.p.slow_init(addr=addr, sync_timeout_ms=sync_timeout_ms)
        if not reply.startswith(b"\x55"):
            raise Kwp1281Error(
                "no ECU sync: " + (reply.hex() if reply else "line silent"))
        if len(reply) < 3:
            reply += self.p.read_exact(3 - len(reply), RECV_TIMEOUT_MS)
        keyword = (reply[1] << 8) | reply[2]
        if keyword != KW1281_KEYWORD:
            raise Kwp1281Error(f"keyword {keyword:04x} is not KW1281")
        self.sleep(POSTKEYWORD_S)
        self._send_byte(reply[2] ^ 0xFF)  # 0x75 for the BMS
        idents = []
        while True:
            blk = self.recv_block()
            if blk["title"] == KWP_ACK:
                break
            if blk["title"] != KWP_R_ASCII_DATA:
                raise Kwp1281Error(
                    f"unexpected ident title {blk['title']:02x}")
            idents.append(
                blk["data"].decode("ascii", "replace").rstrip())
            self.send_block(KWP_ACK)
        return idents

    def read_group(self, group, timeout_ms=RECV_TIMEOUT_MS):
        """Read measuring-value block. Returns list of
        (field_id, value, units, raw_ab) triplets, or None for an
        empty-but-valid group (ACK response)."""
        self.send_block(KWP_GROUP_READING, bytes([group]))
        blk = self.recv_block(timeout_ms)
        title = blk["title"]
        if title == KWP_ACK:
            return None
        if title == KWP_R_GROUP_READ_HEADER:
            self.send_block(KWP_GROUP_READING, bytes([group]))
            blk = self.recv_block(timeout_ms)
            title = blk["title"]
            if title != KWP_R_GROUP_READ_DATA:
                raise Kwp1281Error(
                    f"group {group}: expected 0xF4 after header, "
                    f"got {title:02x}")
        if title == KWP_R_GROUP_READ_DATA:
            data = blk["data"]
            # 0xF4 payload: group echo byte + triplets (jazdw blockData)
            if data and data[0] == group:
                data = data[1:]
            return self._parse_triplets(data)
        if title != KWP_R_GROUP_READING:
            raise Kwp1281Error(
                f"group {group}: unexpected title {title:02x}")
        data = blk["data"]
        if data and data[0] == 0x3F:
            return [(0x3F, data[1:].decode("ascii", "replace"), "string", ())]
        return self._parse_triplets(data)

    @staticmethod
    def _parse_triplets(data):
        if len(data) % 3:
            raise Kwp1281Error(f"triplet data not multiple of 3: {data.hex()}")
        out = []
        for i in range(0, len(data), 3):
            fid, a, b = data[i], data[i + 1], data[i + 2]
            if fid == 0x3F:
                out.append((fid, bytes(data[i + 1:]).decode("ascii", "replace"),
                            "string", (a, b)))
                break
            val, units = _decode(fid, a, b)
            out.append((fid, val, units, (a, b)))
        return out

    def disconnect(self):
        return self.send_block(KWP_DISCONNECT)
