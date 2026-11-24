/* fake_ecu.c — see fake_ecu.h */
#include <string.h>
#include <stdio.h>
#include "fake_ecu.h"

fake_ecu_t *fake_active = NULL;

void fake_init(fake_ecu_t *f)
{
    memset(f, 0, sizeof(*f));
    f->keyword_byte = 0x8A;
    fake_active = f;
}

void fake_reset(fake_ecu_t *f) { fake_init(f); }

static void q_push(fake_ecu_t *f, uint8_t b)
{
    if (f->q_tail < FK_QUEUE_CAP)
        f->q[f->q_tail++] = b;
    else
        f->last_error = "queue overflow";
}

/* The bus echo is the IMMEDIATE next byte the tester reads: push it
 * in front of any pending ECU bytes, not behind them. */
static void q_push_front(fake_ecu_t *f, uint8_t b)
{
    if (f->q_head > 0) {
        f->q_head--;
        f->q[f->q_head] = b;
        return;
    }
    if (f->q_tail >= FK_QUEUE_CAP) {
        f->last_error = "queue overflow";
        return;
    }
    memmove(f->q + 1, f->q, f->q_tail);
    f->q_tail++;
    f->q[0] = b;
}

static void expect_push(fake_ecu_t *f, uint8_t b)
{
    if (f->expect_tail < FK_EXPECT_CAP)
        f->expect[f->expect_tail++] = b;
}

void fake_enqueue_raw(fake_ecu_t *f, uint8_t b) { q_push(f, b); }

void fake_enqueue_block(fake_ecu_t *f, uint8_t ctr, uint8_t title,
                        const uint8_t *data, size_t dlen)
{
    uint8_t len = (uint8_t)(2 + dlen + 1); /* ctr, title, data, end */
    size_t i;
    uint8_t frame[3 + 255];
    size_t flen = 0;

    frame[flen++] = len;
    frame[flen++] = ctr;
    frame[flen++] = title;
    for (i = 0; i < dlen; i++)
        frame[flen++] = data[i];
    frame[flen++] = 0x03;

    for (i = 0; i < flen; i++) {
        q_push(f, frame[i]);
        if (frame[i] != 0x03 || i != flen - 1)
            expect_push(f, (uint8_t)(frame[i] ^ 0xFF));
    }
}

int fake_io_write(uint8_t b)
{
    fake_ecu_t *f = fake_active;
    if (!f) return -1;

    /* transcript */
    if (f->log_len < FK_LOG_CAP)
        f->log[f->log_len++] = b;

    /* bus echo FIRST and IN FRONT: the tester's kw_send_byte consumes
     * it before any response bytes enqueued by the hooks below */
    {
        uint8_t echo = f->corrupt_echo ? (uint8_t)(b ^ 0xFF) : b;
        q_push_front(f, echo);
    }

    /* strict complement discipline */
    if (f->expect_head < f->expect_tail) {
        if (b != f->expect[f->expect_head]) {
            static char errbuf[64];
            snprintf(errbuf, sizeof(errbuf),
                     "discipline: wrote %02x, expected complement %02x",
                     b, f->expect[f->expect_head]);
            f->last_error = errbuf;
            return -1;
        }
        f->expect_head++;
    }

    /* keyword ack observed: the ECU sends ident 1; further idents and
     * its own final ACK arrive only after the tester ACKs each block */
    if (f->awaiting_keyword_ack && b == 0x75) {
        static const uint8_t ident1[12] = {'0','4','5','9','0','6','0','1','3','D',0,0};
        static const uint8_t ident2[12] = {'R','3',' ','1','.','4','.','E','D','C',0,0};
        static const uint8_t ident3[12] = {'G','0','0','0','S','G',' ','0','4','7','7',0};
        fake_enqueue_block(f, 1, 0xF6, ident1, 12);
        f->ident2 = ident2;
        f->ident3 = ident3;
        f->ident_acked = 0;
        f->awaiting_keyword_ack = 0;
    }

    /* Recognize the tester's ACK block [03][ctr][09][03] — a real ECU
     * sends the next ident only after the tester's ACK completes.
     * Complements can't collide: b^FF==03 needs an ECU byte 0xFC,
     * which no scripted frame contains. */
    if (f->ident2) {
        switch (f->ack_state) {
        case 0: f->ack_state = (b == 0x03) ? 1 : 0; break;
        case 1: f->ack_state = 2; break;               /* counter */
        case 2: f->ack_state = (b == 0x09) ? 3 : 0; break;
        case 3:
            if (b == 0x03) {
                f->ident_acked++;
                if (f->ident_acked == 1)
                    fake_enqueue_block(f, 2, 0xF6, f->ident2, 12);
                else if (f->ident_acked == 2)
                    fake_enqueue_block(f, 3, 0xF6, f->ident3, 12);
                else if (f->ident_acked == 3)
                    fake_enqueue_block(f, 4, 0x09, NULL, 0); /* ECU's own ACK */
            }
            f->ack_state = 0;
            break;
        }
    }

    /* Recognize a COMPLETE tester group-request frame [04][ctr][29]
     * [group][03] — the ECU answers only after the request fully
     * lands. No complement collisions: these bytes as complements
     * would need ECU bytes FB/D6/97 in scripted frames — none exist. */
    if (f->group_cb) {
        switch (f->req_state) {
        case 0: f->req_state = (b == 0x04) ? 1 : 0; break;
        case 1: f->req_state = 2; break;                /* counter */
        case 2: f->req_state = (b == 0x29) ? 3 : 0; break;
        case 3: f->req_state = 4; f->req_group = b; break;
        case 4:
            if (b == 0x03) {
                f->req_complete++;
                if (f->req_complete >= f->group_arm) {
                    f->group_cb(f->group_ctx);
                }
            }
            f->req_state = 0;
            break;
        }
    }
    return 0;
}

int fake_io_read(uint8_t *out, uint32_t timeout_ms)
{
    fake_ecu_t *f = fake_active;
    (void)timeout_ms;
    if (!f) return -1;
    if (f->q_head >= f->q_tail)
        return -1; /* line silent */
    *out = f->q[f->q_head++];
    return 0;
}

int fake_io_wake(uint8_t addr)
{
    fake_ecu_t *f = fake_active;
    if (!f) return -1;
    if (f->q_head != f->q_tail) {
        f->last_error = "wake on a non-idle line";
        return -1;
    }
    /* 5-baud wake; ECU answers 55 <addr> <keyword>, no complements */
    if (addr == 0x01) {
        q_push(f, 0x55);
        q_push(f, 0x01);
        q_push(f, f->keyword_byte);
        f->awaiting_keyword_ack = (f->keyword_byte == 0x8A);
        return 0;
    }
    return -1; /* no ECU at this address */
}

void fake_io_sleep(uint32_t ms) { (void)ms; }
