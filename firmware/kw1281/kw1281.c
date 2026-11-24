/*
 * kw1281.c — portable KW1281 client core (see kw1281.h).
 * C port of tools/kwp1281.py; behavior is intentionally identical.
 */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "kw1281.h"

/* ---- byte layer ------------------------------------------------ */

static int kw_send_byte(kw1281_t *k, uint8_t b)
{
    uint8_t echo;
    if (k->io->write(b) != 0)
        return KW_E_IO;
    if (k->io->read(&echo, KW_ECHO_TIMEOUT_MS) != 0 || echo != b)
        return KW_E_ECHO;
    return KW_OK;
}

/* read one ECU byte, answer its complement (echo consumed inside) */
static int kw_recv_byte_ack(kw1281_t *k, uint8_t *out, uint32_t timeout_ms)
{
    uint8_t c;
    int rc;
    if (k->io->read(&c, timeout_ms) != 0)
        return KW_E_IO;
    if ((rc = kw_send_byte(k, (uint8_t)(c ^ 0xFF))) != KW_OK)
        return rc;
    *out = c;
    return KW_OK;
}

/* ---- block layer ------------------------------------------------ */

static int kw_send_block(kw1281_t *k, uint8_t title, const uint8_t *data, int dlen)
{
    uint8_t frame[3 + 255];
    int flen = 0, i, rc;
    uint8_t payload_len = (uint8_t)(2 + dlen + 1); /* ctr, title, data, end */

    k->counter = (uint8_t)(k->counter + 1);
    frame[flen++] = payload_len;
    frame[flen++] = k->counter;
    frame[flen++] = title;
    if (dlen > 0) {
        memcpy(frame + flen, data, (size_t)dlen);
        flen += dlen;
    }
    frame[flen++] = 0x03;

    for (i = 0; i < flen; i++) {
        if (i)
            k->io->sleep_ms(KW_INTERBYTE_MS);
        if ((rc = kw_send_byte(k, frame[i])) != KW_OK)
            return rc;
    }
    k->io->sleep_ms(KW_INTERBLOCK_MS);
    return KW_OK;
}

/* Receive one block into buf (capacity KW_MAX_BLOCK+1), ACKing every
 * byte with its complement except the 0x03 end marker.
 * Returns block length incl. all bytes, or negative KW_E_*. */
static int kw_recv_block(kw1281_t *k, uint8_t *buf, uint32_t timeout_ms)
{
    uint8_t length, c;
    int i, rc;
    int blen = 0;

    if (k->io->read(&length, timeout_ms) != 0)
        return KW_E_IO;
    if (length < 3)
        return KW_E_BLOCK;
    if ((rc = kw_send_byte(k, (uint8_t)(length ^ 0xFF))) != KW_OK)
        return rc;
    buf[blen++] = length;

    if ((rc = kw_recv_byte_ack(k, &c, timeout_ms)) != KW_OK)
        return rc;
    k->counter = c; /* mirror the ECU's counter */
    buf[blen++] = c;

    for (i = 0; i < length - 1; i++) {
        int is_end = (i == length - 2);
        if (is_end) {
            if (k->io->read(&c, timeout_ms) != 0)
                return KW_E_IO;
            if (c != 0x03)
                return KW_E_BLOCK;
        } else {
            if ((rc = kw_recv_byte_ack(k, &c, timeout_ms)) != KW_OK)
                return rc;
        }
        buf[blen++] = c;
    }
    return blen; /* buf: [len][ctr][title][data...][0x03] */
}

/* ---- field decode (jazdw table, port of _decode) ---------------- */

int kw_decode(uint8_t id, uint8_t a, uint8_t b,
              double *value, const char **units)
{
    switch (id) {
    case 0x01: *value = a * b / 5.0;             *units = "rpm";     return 0;
    case 0x04: *value = abs(b - 127) * 0.01 * a;
               *units = (b > 127) ? "deg ATDC" : "deg BTDC";        return 0;
    case 0x07: *value = 0.01 * a * b;            *units = "km/h";    return 0;
    case 0x11: *units = "ASCII";                return 0; /* text handled by caller */
    case 0x12: *value = a * b / 25.0;            *units = "mbar";    return 0;
    case 0x14: *value = a * b / 128.0 - 1;       *units = "%";       return 0;
    case 0x15: *value = a * b / 1000.0;          *units = "V";       return 0;
    case 0x16: *value = 0.001 * a * b;           *units = "ms";     return 0;
    case 0x17: *value = b * a / 256.0;           *units = "%";       return 0;
    case 0x1A: *value = (double)b - (double)a;   *units = "degC";   return 0;
    case 0x21: *value = a ? (100.0 * b / a) : (100.0 * b);
               *units = "%";                                       return 0;
    case 0x22: *value = (b - 128) * 0.01 * a;    *units = "kW";      return 0;
    case 0x23: *value = a * b / 100.0;           *units = "l/h";     return 0;
    case 0x27: *value = a * b / 256.0;           *units = "mg/stk";  return 0;
    case 0x31: *value = a * b / 40.0;            *units = "mg/stk";  return 0;
    case 0x33: *value = (b - 128) / 255.0 * a;    *units = "mg/stk"; return 0;
    case 0x36: *value = a * 256.0 + b;           *units = "count";   return 0;
    case 0x37: *value = a * b / 200.0;           *units = "s";       return 0;
    case 0x08: case 0x10: case 0x25:
    case 0x3F: *units = "binary"; return 0; /* 0x3F = string, caller handles */
    default:   *value = ((double)a * 256.0) + b; *units = "raw";     return 0;
    }
}

static int kw_parse_triplets(kw1281_t *k, const uint8_t *data, int dlen,
                             kw_field_t *fields, int max_fields)
{
    int i = 0, n = 0;
    (void)k;
    /* A 0x3F string field ends the group with an arbitrary-length tail:
     * only reject non-multiple-of-3 data when no string field is present
     * (deepseek 2026-11-24; mirrors the Python fix). */
    if (dlen % 3 && memchr(data, 0x3F, (size_t)dlen) == NULL)
        return KW_E_BLOCK;
    while (i + 2 < dlen || (i < dlen && data[i] == 0x3F)) {
        uint8_t fid = data[i], a, b;
        if (fid == 0x3F) {
            /* string field: rest of the block is ASCII, ends the group */
            int len = dlen - (i + 1), j;
            if (n >= max_fields)
                return KW_E_FIELDS;
            fields[n].id = 0x3F;
            if (len > (int)sizeof(fields[n].text) - 1)
                len = (int)sizeof(fields[n].text) - 1;
            for (j = 0; j < len; j++)
                fields[n].text[j] = (char)data[i + 1 + j];
            fields[n].text[len] = 0;
            fields[n].units = "string";
            fields[n].raw_a = 0; fields[n].raw_b = 0;
            fields[n].value = 0;
            return n + 1;
        }
        if (i + 3 > dlen)
            return KW_E_BLOCK;
        a = data[i + 1]; b = data[i + 2];
        if (n >= max_fields)
            return KW_E_FIELDS;
        fields[n].id = fid;
        fields[n].raw_a = a; fields[n].raw_b = b;
        fields[n].value = 0; fields[n].text[0] = 0;
        kw_decode(fid, a, b, &fields[n].value, &fields[n].units);
        n++;
        i += 3;
    }
    return n;
}

/* ---- session ---------------------------------------------------- */

int kw_connect(kw1281_t *k, uint8_t addr)
{
    uint8_t reply[8];
    int rlen = 0, rc, i;
    unsigned keyword;
    uint8_t buf[KW_MAX_BLOCK + 1];

    k->counter = 0;
    k->n_ident = 0;

    rc = k->io->wake(addr);
    if (rc != 0)
        return KW_E_SYNC;

    /* ECU: 55 <addr> 8A */
    while (rlen < 3) {
        if (k->io->read(&reply[rlen], KW_SYNC_TIMEOUT_MS) != 0)
            break;
        rlen++;
    }
    if (rlen == 0 || reply[0] != 0x55)
        return KW_E_SYNC;
    if (rlen < 3)
        return KW_E_SYNC;
    keyword = ((unsigned)reply[1] << 8) | reply[2];
    if (keyword != KW1281_KEYWORD)
        return KW_E_KEYWORD;

    k->io->sleep_ms(KW_POSTKEYWORD_MS);
    if ((rc = kw_send_byte(k, (uint8_t)(reply[2] ^ 0xFF))) != KW_OK)
        return rc;

    /* ident blocks until the ECU's own ACK */
    for (i = 0; i < 8; i++) {
        int blen = kw_recv_block(k, buf, KW_RECV_TIMEOUT_MS);
        uint8_t title;
        if (blen < 0)
            return blen;
        title = buf[2];
        if (title == KW_ACK)
            return KW_OK;
        if (title != KW_R_ASCII_DATA)
            return KW_E_TITLE;
        if (k->n_ident < 4) {
            int dlen = blen - 4; /* minus len, ctr, title, end */
            if (dlen > 12) dlen = 12;
            memcpy(k->ident[k->n_ident], buf + 3, (size_t)dlen);
            k->ident[k->n_ident][dlen] = 0;
            k->n_ident++;
        }
        if ((rc = kw_send_block(k, KW_ACK, NULL, 0)) != KW_OK)
            return rc;
    }
    return KW_E_TITLE; /* ident storm: never saw the ECU ACK */
}

int kw_read_group(kw1281_t *k, uint8_t group,
                  kw_field_t *fields, int max_fields)
{
    uint8_t buf[KW_MAX_BLOCK + 1];
    uint8_t gbyte = group;
    int blen, rc;
    uint8_t title;
    const uint8_t *data;
    int dlen;

    if ((rc = kw_send_block(k, KW_GROUP_READING, &gbyte, 1)) != KW_OK)
        return rc;

    blen = kw_recv_block(k, buf, KW_RECV_TIMEOUT_MS);
    if (blen < 0)
        return blen;
    title = buf[2];

    if (title == KW_ACK)
        return 0; /* empty-but-valid group */

    if (title == KW_R_GROUP_HEADER) {
        /* header 0x02 then data 0xF4: resend the request */
        if ((rc = kw_send_block(k, KW_GROUP_READING, &gbyte, 1)) != KW_OK)
            return rc;
        blen = kw_recv_block(k, buf, KW_RECV_TIMEOUT_MS);
        if (blen < 0)
            return blen;
        title = buf[2];
        if (title != KW_R_GROUP_DATA)
            return KW_E_TITLE;
    }

    if (title == KW_R_GROUP_DATA) {
        data = buf + 3;
        dlen = blen - 4;
        if (dlen > 0 && data[0] == group) { /* group echo byte */
            data++;
            dlen--;
        }
        return kw_parse_triplets(k, data, dlen, fields, max_fields);
    }

    if (title != KW_R_GROUP_READING)
        return KW_E_TITLE;

    data = buf + 3;
    dlen = blen - 4;
    if (dlen > 0 && data[0] == 0x3F) {
        /* 0xE7 string response */
        if (max_fields < 1)
            return KW_E_FIELDS;
        fields[0].id = 0x3F;
        {
            int len = dlen - 1, j;
            if (len > (int)sizeof(fields[0].text) - 1)
                len = (int)sizeof(fields[0].text) - 1;
            for (j = 0; j < len; j++)
                fields[0].text[j] = (char)data[1 + j];
            fields[0].text[len] = 0;
        }
        fields[0].units = "string";
        fields[0].raw_a = 0; fields[0].raw_b = 0;
        return 1;
    }
    return kw_parse_triplets(k, data, dlen, fields, max_fields);
}

int kw_disconnect(kw1281_t *k)
{
    return kw_send_block(k, KW_DISCONNECT, NULL, 0);
}

const char *kw_strerror(int rc)
{
    switch (rc) {
    case KW_OK:         return "ok";
    case KW_E_IO:       return "io timeout";
    case KW_E_ECHO:     return "tx echo mismatch";
    case KW_E_SYNC:     return "no ecu sync";
    case KW_E_KEYWORD:  return "keyword not kw1281";
    case KW_E_BLOCK:    return "malformed block";
    case KW_E_TITLE:    return "unexpected title";
    case KW_E_FIELDS:   return "field buffer too small";
    default:            return "unknown";
    }
}
