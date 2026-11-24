/*
 * kw1281.h — portable KW1281 client core for the Springtide K-line node.
 *
 * Direct C port of the FakeECU-tested python layer (tools/kwp1281.py):
 * same frame codec, same complement-ACK flow control, same jazdw
 * decode table. No Arduino/hardware dependencies — all I/O goes
 * through the kw_io_t vtable so the same core runs on the ESP32
 * (UART-backed) and in host tests (fake-ECU-backed).
 *
 * Protocol notes (see references/kwp-kline.md):
 *  - single-wire bus: our own TX echoes back and must be consumed+verified
 *  - flow control: after every ECU byte the tester answers b^0xFF,
 *    except the 0x03 block-end marker (no complement)
 *  - 5-baud wake (addr 0x01) -> ECU: 55 01 8A -> wait 30 ms -> send 8A^FF=75
 *  - ident blocks (title 0xF6) until the ECU's own ACK (title 0x09)
 */
#ifndef KW1281_H
#define KW1281_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- tunables (same values as the python layer) ---- */
#define KW_ECHO_TIMEOUT_MS   150u
#define KW_RECV_TIMEOUT_MS   3000u
#define KW_INTERBYTE_MS      1u
#define KW_INTERBLOCK_MS     10u
#define KW_POSTKEYWORD_MS    30u
#define KW_SYNC_TIMEOUT_MS   1500u

#define KW1281_KEYWORD       0x018Au
#define KW_MAX_BLOCK         255u
#define KW_MAX_FIELDS        10u

/* block titles */
#define KW_DISCONNECT        0x06u
#define KW_ACK               0x09u
#define KW_GROUP_READING     0x29u
#define KW_R_GROUP_HEADER    0x02u
#define KW_R_GROUP_READING   0xE7u
#define KW_R_GROUP_DATA      0xF4u
#define KW_R_ASCII_DATA      0xF6u

/* error codes */
#define KW_OK              0
#define KW_E_IO           -1   /* vtable write/read failed or timed out */
#define KW_E_ECHO         -2   /* TX echo mismatch (bus contention/wiring) */
#define KW_E_SYNC         -3   /* no 55 01 8A after wake */
#define KW_E_KEYWORD      -4   /* sync received, keyword is not KW1281 */
#define KW_E_BLOCK        -5   /* malformed block (length/end marker) */
#define KW_E_TITLE        -6   /* unexpected block title in session flow */
#define KW_E_FIELDS       -7   /* field buffer too small / triplet overflow */

typedef struct kw_io {
    /* write one byte; 0 ok, -1 fail */
    int (*write)(uint8_t b);
    /* read one byte within timeout_ms; 0 ok (*out set), -1 timeout/fail */
    int (*read)(uint8_t *out, uint32_t timeout_ms);
    /* 5-baud address wake-up; 0 ok, -1 fail */
    int (*wake)(uint8_t addr);
    /* sleep milliseconds */
    void (*sleep_ms)(uint32_t ms);
} kw_io_t;

typedef struct {
    const kw_io_t *io;
    uint8_t counter;   /* block counter, mirrored with the ECU */
    char ident[4][13]; /* up to 4 ident strings (045906013D / R3 1.4.EDC / ...) */
    int n_ident;
} kw1281_t;

/* one decoded measuring-value field */
typedef struct {
    uint8_t id;
    double  value;
    char    text[KW_MAX_BLOCK]; /* used when id == 0x3F (string field) */
    const char *units;
    uint8_t raw_a, raw_b;
} kw_field_t;

/* ---- session ---- */
int kw_connect(kw1281_t *k, uint8_t addr);
int kw_disconnect(kw1281_t *k);

/* Read one measuring-value block. fields/max_fields receive decoded
 * triplets; returns the field count, 0 for an empty-but-valid group
 * (ECU ACK), or a negative KW_E_* code. */
int kw_read_group(kw1281_t *k, uint8_t group,
                  kw_field_t *fields, int max_fields);

/* jazdw decode table: (id, a, b) -> (value, units). Returns 0 ok. */
int kw_decode(uint8_t id, uint8_t a, uint8_t b,
              double *value, const char **units);

/* last-error scratch (small, single-context use) */
const char *kw_strerror(int rc);

#ifdef __cplusplus
}
#endif

#endif /* KW1281_H */
