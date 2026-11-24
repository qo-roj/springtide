/*
 * springtide_node.ino — ESP32 K-line node firmware (skeleton).
 *
 * Permanent in-car node per the project's power plan: ignition-
 * switched 12 V through a buck (display = system-on lamp), a K-line
 * transceiver on UART2, this firmware polling the ECU's KW1281
 * measuring blocks and streaming decoded fields as JSON lines to the
 * monitor phone over WiFi.
 *
 * The KW1281 core is the same protocol layer the python tools use
 * (tools/kwp1281.py), C-ported and host-tested against a strict fake
 * ECU (firmware/tests). build.sh syncs the core into this folder
 * before invoking arduino-cli.
 *
 * Poll set (see docs/esp32-node.md):
 *   001  coolant cross-check + rpm/speed context
 *   068  DPF soot load % + carbon mass g
 *   069/070  regen status bitfields + failed/successful counters
 *   073  km + time since last regeneration
 */
#include <Arduino.h>
#include <WiFi.h>
#include "kw1281.h"
#include "node_config.h"

static kw_io_t   io;
static kw1281_t  k;
static WiFiClient uplink;

/* ---- kw_io_t bindings -------------------------------------------- */

static int io_write(uint8_t b)
{
    Serial2.write(b);
    return 0;
}

static int io_read(uint8_t *out, uint32_t timeout_ms)
{
    uint32_t t0 = millis();
    while (millis() - t0 < timeout_ms) {
        if (Serial2.available()) {
            *out = (uint8_t)Serial2.read();
            return 0;
        }
        delay(1);
    }
    return -1;
}

static void io_sleep_ms(uint32_t ms) { delay(ms); }

/*
 * ISO 14230-2 5-baud slow init, bit-banged on the TX pin (identical
 * train to the proven python/FTDI implementation):
 *   start(0) + 7 data bits LSB-first + parity(1 XOR data bits) + stop(1),
 *   200 ms per bit. The transceiver drives the K-line from TXD; the
 *   wake train's own echo comes back as garbage at 10400 baud — the
 *   RX buffer is flushed before the sync read.
 */
static int io_wake(uint8_t addr)
{
    uint8_t seq[10];
    uint8_t parity = 1;
    int i, j = 0;

    seq[j++] = 0; /* start bit: line low */
    for (i = 0; i < 7; i++) {
        uint8_t bit = (addr >> i) & 1;
        parity ^= bit;
        seq[j++] = bit;
    }
    seq[j++] = parity;
    seq[j++] = 1; /* stop bit: line released */

    Serial2.end();
    pinMode(KLINE_TX_PIN, OUTPUT);
    digitalWrite(KLINE_TX_PIN, HIGH);
    delay(300); /* idle high before the train */

    for (i = 0; i < j; i++) {
        digitalWrite(KLINE_TX_PIN, seq[i] ? HIGH : LOW);
        delay(200);
    }
    digitalWrite(KLINE_TX_PIN, HIGH); /* stop released; line idles high */
    delay(10);

    Serial2.begin(10400, SERIAL_8N1, KLINE_RX_PIN, KLINE_TX_PIN);
    while (Serial2.available())
        Serial2.read(); /* flush the wake-train echo */
    return 0;
}

/* ---- uplink ------------------------------------------------------ */

static void wifi_up(void)
{
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    WiFi.setAutoReconnect(true); /* deterministic across core versions */
    /* non-blocking connect; poll loop tolerates a down link */
}

static void emit_line(const char *s)
{
    if (uplink.connected()) {
        uplink.println(s);
    }
    /* v1: no buffering when the link is down — the next cycle
     * carries fresh values; the receiver tolerates gaps */
}

static void emit_group(uint8_t group, kw_field_t *f, int n)
{
    char line[512];
    int off = 0, i;

    off += snprintf(line + off, sizeof(line) - off,
                    "{\"g\":%u,\"f\":[", (unsigned)group);
    for (i = 0; i < n && off < (int)sizeof(line) - 64; i++) {
        if (i)
            off += snprintf(line + off, sizeof(line) - off, ",");
        if (f[i].id == 0x3F)
            off += snprintf(line + off, sizeof(line) - off,
                            "{\"i\":63,\"s\":\"%s\"}", f[i].text);
        else
            off += snprintf(line + off, sizeof(line) - off,
                            "{\"i\":%u,\"v\":%.2f,\"u\":\"%s\"}",
                            (unsigned)f[i].id, f[i].value,
                            f[i].units ? f[i].units : "");
    }
    snprintf(line + off, sizeof(line) - off, "]}");
    emit_line(line);
}

/* ---- poll cycle -------------------------------------------------- */

static const uint8_t poll_groups[] = { 1, 68, 69, 70, 73 };
#define N_POLL ((int)(sizeof(poll_groups) / sizeof(poll_groups[0])))
static uint32_t last_link_ms = 0;  /* uplink connect-attempt backoff */

static int poll_cycle(void)
{
    kw_field_t fields[KW_MAX_FIELDS];
    int g, n;

    for (g = 0; g < N_POLL; g++) {
        n = kw_read_group(&k, poll_groups[g], fields, KW_MAX_FIELDS);
        if (n < 0) {
            char line[96];
            snprintf(line, sizeof(line),
                     "{\"g\":%u,\"err\":%d,\"e\":\"%s\"}",
                     (unsigned)poll_groups[g], n, kw_strerror(n));
            emit_line(line);
            return n;
        }
        if (n > 0)
            emit_group(poll_groups[g], fields, n);
    }
    return 0;
}

void setup()
{
    Serial.begin(115200); /* debug console */
    io.write = io_write;
    io.read = io_read;
    io.wake = io_wake;
    io.sleep_ms = io_sleep_ms;
    k.io = &io;
    wifi_up();
}

void loop()
{
    int rc = kw_connect(&k, ECU_ADDR);

    if (rc != KW_OK) {
        Serial.printf("[node] connect: %s\n", kw_strerror(rc));
        delay(30000); /* ignition off / line silent: re-probe slowly */
        return;
    }
    Serial.printf("[node] ECU up: %s / %s / %s\n",
                  k.ident[0], k.ident[1], k.ident[2]);

    while (true) {
        if (WiFi.status() != WL_CONNECTED) {
            if (millis() - last_link_ms > 10000) {
                last_link_ms = millis();
                WiFi.reconnect();   /* explicit + backed off: never rely
                                       on core defaults, never flood */
            }
        } else if (!uplink.connected() && millis() - last_link_ms > 10000) {
            last_link_ms = millis();
            uplink.connect(UPLINK_HOST, UPLINK_PORT);  /* <=1 try / 10 s */
        }

        rc = poll_cycle();
        if (rc < 0) {
            Serial.printf("[node] poll error: %s\n", kw_strerror(rc));
            break;
        }
        delay(POLL_S * 1000);
    }

    kw_disconnect(&k);
    uplink.stop();
    delay(30000); /* session lost: re-wake and retry */
}
