#!/usr/bin/env python3
"""Decode Ross-Tech VCDS compiled .clb label files to plaintext .lbl text.

Port of the format documented in pashokitsme/vagcan (crates/data/vag-data-labels):
TEA-32 in CBC mode, fixed key, per-record IV derived from a brute-forced file
constant w7 and the record index. Container: big-endian u16 plaintext length,
ceil(len/8)*8 bytes ciphertext, records separated by 00 0A markers (00 0A alone
= blank line).

Output is the same Latin-1 text format as plaintext .lbl files, one record per
line, joined with \n. Decoded output is for Springtide private use (the maintainer's own
licensed VCDS install, his own car) — never redistribute Ross-Tech data.

Usage:
  clb_decode.py --selftest
  clb_decode.py FILE.clb                 # decoded text to stdout
  clb_decode.py -o OUT.lbl FILE.clb      # Latin-1 bytes to file
  clb_decode.py --outdir DIR FILE...     # each NAME.clb -> DIR/NAME.lbl
"""

import sys
from pathlib import Path

M = 0xFFFFFFFF
DELTA = 0x9E3779B9
SUM0 = 0xC6EF3720
KEY_CLB = (0xFA7E14D0, 0x249B910E, 0x2FDD6FFC, 0x15834A78)

BLANK = object()  # marker for a blank-line record


def tea_dec_block(block, key):
    v0 = int.from_bytes(block[0:4], "little")
    v1 = int.from_bytes(block[4:8], "little")
    s = SUM0
    for _ in range(32):
        v1 = (v1 - ((((v0 << 4) & M) + key[2] & M) ^ ((v0 + s) & M)
                    ^ (((v0 >> 5) + key[3]) & M))) & M
        v0 = (v0 - ((((v1 << 4) & M) + key[0] & M) ^ ((v1 + s) & M)
                    ^ (((v1 >> 5) + key[1]) & M))) & M
        s = (s - DELTA) & M
    return v0.to_bytes(4, "little") + v1.to_bytes(4, "little")


def tea_cbc_decrypt(cipher, key, iv):
    out = bytearray()
    prev = iv
    for i in range(0, len(cipher) - len(cipher) % 8, 8):
        blk = cipher[i:i + 8]
        dec = tea_dec_block(blk, key)
        out += bytes(d ^ p for d, p in zip(dec, prev))
        prev = blk
    return bytes(out)


def clb_iv(w7, w15):
    w15_1 = (w15 + 1) & M
    w15_2 = (w15 + 2) & M
    w15_3 = (w15 + 3) & M
    w15_4 = (w15 + 4) & M
    w7_1 = (w7 + 1) & M
    w7_3 = (w7 + 3) & M

    a = (((w7 + 2) & M) * w15_1 & M) * w15_3 & M
    w24 = (w7_1 * w15_2 + a) & M
    r = w7 % w15_1
    w8 = r if r != 0 else w15 % w7_1
    w23 = (((w7_3 * w7_1 & M) * w15_2 & M) + w8) & M
    if w24 < 0xFFFF:
        w24 = ((w24 << 16) + (((w15_4 * w7_3 & M) * w7_1 & M) * w15_2 & M)) & M
    if w23 < 0xFFFF:
        w23 = ((w23 << 16) + (((w15_1 * w15_2 & M) * w15_3 & M))) & M
    return w24.to_bytes(4, "little") + w23.to_bytes(4, "little")


def parse_container(data):
    records = []
    pos = 0
    index = 0
    while pos + 2 <= len(data):
        if data[pos] == 0x00 and data[pos + 1] == 0x0A:
            records.append(BLANK)
            pos += 2
            continue
        length = (data[pos] << 8) | data[pos + 1]
        if length == 0:
            break
        clen = (length + 7) // 8 * 8
        if pos + 2 + clen > len(data):
            break  # truncated container
        cipher = data[pos + 2:pos + 2 + clen]
        pos += 2 + clen
        if pos + 2 <= len(data) and data[pos] == 0x00 and data[pos + 1] == 0x0A:
            pos += 2
        records.append((cipher, length, index))
        index += 1
    return records


def _score_printable(bs):
    return sum(1 for b in bs if b in (9, 10, 13) or 32 <= b <= 126)


def recover_w7(records):
    # First-block TEA decrypt is w7-independent; w7 only enters via the IV XOR.
    firsts = []
    for rec in records:
        if rec is BLANK:
            continue
        cipher, length, index = rec
        if len(cipher) < 8:
            continue
        firsts.append((index, length, tea_dec_block(cipher[0:8], KEY_CLB)))
    best_w7, best_score = 0, -1
    for w7 in range(256):
        score = 0
        for index, length, dec in firsts:
            iv = clb_iv(w7, index)
            plain = bytes(d ^ p for d, p in zip(dec, iv))
            score += _score_printable(plain[:min(length, 8)])
        if score > best_score:
            best_score, best_w7 = score, w7
    return best_w7


def decrypt_clb(data):
    records = parse_container(data)
    w7 = recover_w7(records)
    out = bytearray()
    for i, rec in enumerate(records):
        if i > 0:
            out += b"\n"
        if rec is BLANK:
            continue
        cipher, length, index = rec
        plain = tea_cbc_decrypt(cipher, KEY_CLB, clb_iv(w7, index))
        out += plain[:min(length, len(plain))]
    return bytes(out), w7, len(records)


FIXTURE_HEX = ("002738e02cf98f11742ee0b6f41102c2e55c4890aa526e2753a9263c7947f8b"
               "656f3467dc8f892f6c03a000a00202d7dc10402a81d837c41c4b66f69b6b50"
               "479e421595f5f5c20f4d6edd2d07b99000a")


def selftest():
    iv = clb_iv(7, 0)
    assert iv == bytes([0x80, 0x02, 0x2B, 0x00, 0x06, 0x00, 0xA0, 0x00]), \
        f"IV vector mismatch: {iv.hex()}"
    data = bytes.fromhex(FIXTURE_HEX)
    text, w7, nrec = decrypt_clb(data)
    lines = text.decode("latin-1").split("\n")
    assert lines == ["001,1,Engine Speed,,Range: 0..8000 /min",
                     "001,2,Coolant,,Range: -48..143 C"], f"fixture mismatch: {lines!r}"
    assert w7 == 7 and nrec == 2, (w7, nrec)
    print("selftest OK: TEA-CBC, container, w7 recovery and fixture all match "
          "the reference vectors")


def main(argv):
    args = list(argv[1:])
    if "--selftest" in args:
        selftest()
        return 0
    outfile = outdir = None
    if "-o" in args:
        i = args.index("-o")
        outfile = args[i + 1]
        del args[i:i + 2]
    if "--outdir" in args:
        i = args.index("--outdir")
        outdir = Path(args[i + 1])
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 2
    rc = 0
    for name in args:
        data = Path(name).read_bytes()
        try:
            text, w7, nrec = decrypt_clb(data)
        except Exception as e:  # noqa: BLE001
            print(f"{name}: DECODE FAILED: {e}", file=sys.stderr)
            rc = 1
            continue
        printable = _score_printable(text) / max(len(text), 1)
        dest = outfile or (str(outdir / (Path(name).stem + ".lbl")) if outdir else None)
        if dest:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(text)
            print(f"{name}: w7={w7} records={nrec} printable={printable:.1%} -> {dest}")
        else:
            sys.stdout.buffer.write(text)
            sys.stdout.buffer.flush()
            print(f"; [decoded {name}: w7={w7} records={nrec} printable={printable:.1%}]",
                  file=sys.stderr)
        if printable < 0.9:
            print(f"{name}: WARNING printability {printable:.1%} < 90% — "
                  "decode likely wrong", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
