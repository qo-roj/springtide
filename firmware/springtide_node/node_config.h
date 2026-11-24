/*
 * node_config.h — Springtide ESP32 node configuration.
 *
 * PLACEHOLDER VALUES — fill in at flash time, never commit real ones
 * (sanitize rule: no real SSIDs, hostnames or IPs in the repo).
 */
#ifndef NODE_CONFIG_H
#define NODE_CONFIG_H

/* uplink WiFi (the pocket hotspot the monitor phone shares) */
#define WIFI_SSID     "CHANGE_ME"
#define WIFI_PASS     "CHANGE_ME"

/* where JSON lines go (the monitor phone's SSH host/port of the
 * springtide daemon listener, or any TCP sink) */
#define UPLINK_HOST   "192.168.0.0"
#define UPLINK_PORT   35000

/* poll cadence */
#define POLL_S        5

/* K-line wiring — match docs/esp32-node.md; classic ESP32 UART2.
 * The transceiver (K-line driver) is a dumb UART part: TXD/RXD only. */
#define KLINE_TX_PIN  17
#define KLINE_RX_PIN  16

/* ECU target */
#define ECU_ADDR      0x01

#endif /* NODE_CONFIG_H */
