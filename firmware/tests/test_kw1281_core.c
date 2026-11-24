/*
 * test_kw1281_core.c — host tests for the portable KW1281 core.
 *
 * gcc -o test_kw1281_core test_kw1281_core.c fake_ecu.c ../kw1281/kw1281.c -I../kw1281 -I. && ./test_kw1281_core
 *
 * The fake ECU validates the complement-ACK discipline STRICTLY and
 * in order — any deviation by the core turns into write failures and
 * error codes, so a green run proves the interleave discipline, not
 * just the happy path.
 */
#include <stdio.h>
#include <string.h>
#include "kw1281.h"
#include "fake_ecu.h"

static fake_ecu_t f;
static int failures = 0;

#define CHECK(cond, msg) do { \
    if (!(cond)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, msg); failures++; } \
} while (0)

static const uint8_t g68_data[7] = {0x68, 0x14, 0x40, 0x64, 0x36, 0x00, 0x07};

static void group68_resp(void *ctx)
{
    (void)ctx;
    /* group 068 block: title 0xF4, data = [group echo][0x14 0x40 0x64]
     * [0x36 0x00 0x07] -> 49.0 % load coeff, 7 count */
    fake_enqueue_block(&f, 5, 0xF4, g68_data, 7);
}

static void group68_resp_bad_end(void *ctx)
{
    (void)ctx;
    fake_enqueue_block(&f, 5, 0xF4, g68_data, 7);
    f.q[f.q_tail - 1] = 0x04; /* sabotage the end marker */
}

static void group68_header_then_data(void *ctx)
{
    (void)ctx;
    if (f.req_complete == 1) {
        static const uint8_t hdr[1] = {0x68};
        fake_enqueue_block(&f, 5, 0x02, hdr, 1);
    } else {
        fake_enqueue_block(&f, 6, 0xF4, g68_data, 7);
    }
}

static void group_ack_resp(void *ctx)
{
    (void)ctx;
    fake_enqueue_block(&f, 5, 0x09, NULL, 0); /* unsupported group -> ECU ACK */
}

static void group_str_resp(void *ctx)
{
    (void)ctx;
    /* echo + numeric triplet + 2-char string triplet (7 bytes total) */
    static const uint8_t data[7] = {0x68, 0x14, 0x40, 0x64, 0x3F, 'O', 'K'};
    fake_enqueue_block(&f, 5, 0xF4, data, 7);
}

static void group_str_resp_unaligned(void *ctx)
{
    (void)ctx;
    /* echo + numeric triplet + 0x3F + 4-char tail: 9 bytes, dlen 8 not %3.
     * A strict pre-check used to reject this valid block (2026-11-24). */
    static const uint8_t data[9] = {0x68, 0x14, 0x40, 0x64, 0x3F, 'O', 'K', 'A', 'Y'};
    fake_enqueue_block(&f, 5, 0xF4, data, 9);
}

static void arm_group(uint8_t group, void (*cb)(void *))
{
    f.group_arm = 1;
    f.req_state = 0;
    f.req_complete = 0;
    f.group_byte = group;
    f.group_cb = cb;
    f.group_ctx = NULL;
}

static kw_io_t io_keep = {fake_io_write, fake_io_read, fake_io_wake, fake_io_sleep};

static void fresh_session(kw1281_t *k, int connect)
{
    fake_reset(&f);
    memset(k, 0, sizeof(*k));
    k->io = &io_keep;
    if (connect)
        CHECK(kw_connect(k, 0x01) == KW_OK, "setup connect should succeed");
}

int main(void)
{
    kw1281_t k;
    kw_field_t fields[10];
    int rc, n;

    /* ---- 1. full handshake: 3 idents + ECU ACK, counters mirrored ---- */
    fresh_session(&k, 0);
    rc = kw_connect(&k, 0x01);
    CHECK(rc == KW_OK, "connect should succeed");
    CHECK(k.n_ident == 3, "expected 3 ident strings");
    CHECK(strcmp(k.ident[0], "045906013D") == 0, "ident[0] should be the VAG number");
    CHECK(strcmp(k.ident[1], "R3 1.4.EDC") == 0, "ident[1] should be the component");
    CHECK(strcmp(k.ident[2], "G000SG 0477") == 0, "ident[2] should be the soft version");
    CHECK(k.counter == 4, "counter should mirror the ECU (4 after its ACK block)");
    CHECK(f.last_error == NULL, "no fake-side discipline error");

    /* ---- 2. group read: 0xF4 with echo byte + triplets ---- */
    arm_group(0x68, group68_resp);
    n = kw_read_group(&k, 0x68, fields, 10);
    CHECK(n == 2, "group 068 should yield 2 fields");
    if (n == 2) {
        CHECK(fields[0].id == 0x14, "field 0 id should be 0x14");
        CHECK(fields[0].value == 49.0, "field 0 decode: 0x40*0x64/128-1 = 49.0 %");
        CHECK(strcmp(fields[0].units, "%") == 0, "field 0 units should be %");
        CHECK(fields[1].id == 0x36, "field 1 id should be 0x36");
        CHECK(fields[1].value == 7.0, "field 1 decode: 0*256+7 = 7 count");
    }

    /* ---- 3. group read with 0x02 header -> resend -> 0xF4 ---- */
    fresh_session(&k, 1);
    arm_group(0x68, group68_header_then_data);
    n = kw_read_group(&k, 0x68, fields, 10);
    CHECK(n == 2, "header path should yield 2 fields");
    if (n == 2)
        CHECK(fields[0].value == 49.0, "header path decode value");
    CHECK(f.last_error == NULL, "no fake-side discipline error (header path)");

    /* ---- 4. unsupported group: ECU answers plain ACK ---- */
    fresh_session(&k, 1);
    arm_group(0x42, group_ack_resp);
    n = kw_read_group(&k, 0x42, fields, 10);
    CHECK(n == 0, "unsupported group should yield 0 fields (ACK)");

    /* ---- 5. string field 0x3F ends the group ---- */
    fresh_session(&k, 1);
    arm_group(0x68, group_str_resp);
    n = kw_read_group(&k, 0x68, fields, 10);
    CHECK(n == 2, "string group should yield 2 fields");
    if (n == 2) {
        CHECK(fields[0].id == 0x14 && fields[0].value == 49.0, "string group numeric field");
        CHECK(fields[1].id == 0x3F, "string field id");
        CHECK(strcmp(fields[1].text, "OK") == 0, "string field text should be OK");
    }

    /* ---- 5b. unaligned string tail parses (2026-11-24 fix) ---- */
    fresh_session(&k, 1);
    arm_group(0x68, group_str_resp_unaligned);
    n = kw_read_group(&k, 0x68, fields, 10);
    CHECK(n == 2, "unaligned string group should yield 2 fields");
    if (n == 2) {
        CHECK(fields[0].id == 0x14 && fields[0].value == 49.0, "unaligned numeric field");
        CHECK(fields[1].id == 0x3F, "unaligned string id");
        CHECK(strcmp(fields[1].text, "OKAY") == 0, "unaligned string text OKAY");
    }

    /* ---- 6. echo corruption -> KW_E_ECHO ---- */
    fresh_session(&k, 0);
    f.corrupt_echo = 1;
    rc = kw_connect(&k, 0x01);
    CHECK(rc == KW_E_ECHO, "corrupt echo must fail with KW_E_ECHO");

    /* ---- 7. no ECU at the address -> KW_E_SYNC ---- */
    fresh_session(&k, 0);
    rc = kw_connect(&k, 0x03);
    CHECK(rc == KW_E_SYNC, "missing ECU must fail with KW_E_SYNC");

    /* ---- 8. wrong keyword -> KW_E_KEYWORD ---- */
    fresh_session(&k, 0);
    f.keyword_byte = 0x9B; /* sabotage the sync triple's keyword byte */
    rc = kw_connect(&k, 0x01);
    CHECK(rc == KW_E_KEYWORD, "wrong keyword must fail with KW_E_KEYWORD");

    /* ---- 9. bad end marker -> KW_E_BLOCK ---- */
    fresh_session(&k, 1);
    arm_group(0x68, group68_resp_bad_end);
    n = kw_read_group(&k, 0x68, fields, 10);
    CHECK(n == KW_E_BLOCK, "bad end marker must fail with KW_E_BLOCK");

    /* ---- 10. disconnect sends the 0x06 block ---- */
    fresh_session(&k, 1);
    rc = kw_disconnect(&k);
    CHECK(rc == KW_OK, "disconnect should succeed");
    CHECK(f.log_len >= 4, "disconnect wrote bytes");
    CHECK(f.log[f.log_len - 2] == 0x06 && f.log[f.log_len - 1] == 0x03,
          "transcript tail should be the 0x06 disconnect title + end marker");

    /* ---- summary ---- */
    if (failures) {
        printf("%d FAILURE(S)\n", failures);
        return 1;
    }
    printf("ALL KW1281 CORE TESTS PASS\n");
    return 0;
}
