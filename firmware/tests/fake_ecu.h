/*
 * fake_ecu.h — byte-level KW1281 ECU simulator for host tests.
 *
 * Models the single-wire bus faithfully:
 *  - every tester write echoes back onto the read queue (and is
 *    validated against the pending complement-ACK expectation)
 *  - every ECU block byte except the final 0x03 expects the tester
 *    to answer its complement (strict, in order)
 *  - the wake triple (55 01 8A) expects NO complements, then exactly
 *    one write of 8A^FF
 *
 * Scripted per-test: the test enqueues ident/group responses at the
 * right moments via the write-observation hooks.
 */
#ifndef FAKE_ECU_H
#define FAKE_ECU_H

#include <stdint.h>
#include <stddef.h>

#define FK_QUEUE_CAP 4096
#define FK_EXPECT_CAP 512
#define FK_LOG_CAP   4096

typedef struct {
    /* ecu -> tester byte queue (includes bus echoes) */
    uint8_t  q[FK_QUEUE_CAP];
    size_t   q_head, q_tail;
    /* tester writes the fake demands, in order (complement ACKs) */
    uint8_t  expect[FK_EXPECT_CAP];
    size_t   expect_head, expect_tail;
    /* full transcript of tester writes (for debugging) */
    uint8_t  log[FK_LOG_CAP];
    size_t   log_len;
    /* test scripting hooks */
    int      awaiting_keyword_ack;  /* set after wake triple enqueued */
    const uint8_t *ident2, *ident3; /* staged ident stream */
    int      ident_acked;           /* tester ACK blocks observed */
    int      ack_state;             /* mini-parser for tester ACK frames */
    int      group_arm;             /* fire cb after this many complete requests */
    int      req_state;             /* mini-parser for tester request frames */
    int      req_complete;          /* complete requests observed */
    uint8_t  req_group;             /* group byte of the request in flight */
    uint8_t  group_byte;
    void   (*group_cb)(void *ctx);
    void    *group_ctx;
    /* fault injection */
    int      corrupt_echo;          /* echo comes back ^0xFF */
    uint8_t  keyword_byte;          /* 0x8A default; sabotage for tests */
    const char *last_error;
} fake_ecu_t;

void fake_init(fake_ecu_t *f);
void fake_reset(fake_ecu_t *f);

/* enqueue a raw ecu->tester byte (no complement expected) */
void fake_enqueue_raw(fake_ecu_t *f, uint8_t b);

/* enqueue a full block [len][ctr][title][data...][0x03]; registers
 * complement expectations for every byte except the end marker */
void fake_enqueue_block(fake_ecu_t *f, uint8_t ctr, uint8_t title,
                        const uint8_t *data, size_t dlen);

/* kw_io_t binding */
int  fake_io_write(uint8_t b);   /* pure dispatcher below uses a global */
int  fake_io_read(uint8_t *out, uint32_t timeout_ms);
int  fake_io_wake(uint8_t addr);
void fake_io_sleep(uint32_t ms);

/* the active fake (single instance per test run) */
extern fake_ecu_t *fake_active;

#endif /* FAKE_ECU_H */
