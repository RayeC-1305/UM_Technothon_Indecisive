/*
 * WaveSense ESP32-TX — Transmitter Firmware
 *
 * Broadcasts ESP-NOW packets at 100 Hz on a fixed Wi-Fi channel.
 * The RX node captures these frames to extract CSI (Channel State Information).
 *
 * This node is completely standalone — no AP association, no MQTT, no network.
 * Just raw 802.11 frames for the RX to sniff.
 *
 * Hardware: ESP32-WROOM-32, powered by USB battery bank.
 */

#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_now.h"

static const char *TAG = "wavesense-tx";

/* ──────────────────────────────────────────────
 *  CONFIGURATION — Update these for your setup
 * ────────────────────────────────────────────── */

/* Wi-Fi channel: must match your laptop's mobile hotspot channel.
 * Check with: netsh wlan show interfaces */
#define WIFI_CHANNEL 6

/* Source MAC address for ESP-NOW frames.
 * The RX filters on this address to ignore other traffic. */
static const uint8_t TX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

/* Broadcast address — ESP-NOW peer destination */
static const uint8_t BROADCAST_MAC[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

/* Packets per second. 100 Hz gives good CSI resolution without flooding. */
#define TX_RATE_HZ 100

/* ────────────────────────────────────────────── */

static esp_now_peer_info_t broadcast_peer;

static void wifi_init(void)
{
    /* Create default event loop and network interface */
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());

    /* Initialize Wi-Fi with default config */
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    /* STA mode — we don't associate with any AP, just use the radio */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    /* HT40 bandwidth for wider channel (more subcarriers = better CSI) */
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT40));

    ESP_ERROR_CHECK(esp_wifi_start());

    /* Disable power save — critical for consistent 100 Hz timing */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    /* Set channel with HT40 secondary channel below */
    ESP_ERROR_CHECK(esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_BELOW));

    /* Set our custom MAC so the RX can identify us */
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, TX_MAC));

    ESP_LOGI(TAG, "Wi-Fi initialized: channel=%d, MAC=%02x:%02x:%02x:%02x:%02x:%02x",
             WIFI_CHANNEL, TX_MAC[0], TX_MAC[1], TX_MAC[2],
             TX_MAC[3], TX_MAC[4], TX_MAC[5]);
}

static void espnow_init(void)
{
    ESP_ERROR_CHECK(esp_now_init());

    /* Set the Primary Master Key for ESP-NOW */
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));

    /* Add broadcast peer */
    memset(&broadcast_peer, 0, sizeof(broadcast_peer));
    memcpy(broadcast_peer.peer_addr, BROADCAST_MAC, 6);
    broadcast_peer.channel = WIFI_CHANNEL;
    broadcast_peer.ifidx = WIFI_IF_STA;
    broadcast_peer.encrypt = false;

    ESP_ERROR_CHECK(esp_now_add_peer(&broadcast_peer));

    ESP_LOGI(TAG, "ESP-NOW initialized with broadcast peer");
}

void app_main(void)
{
    /* Initialize NVS flash (required by Wi-Fi driver) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    wifi_init();
    espnow_init();

    ESP_LOGI(TAG, "=== WaveSense TX Started ===");
    ESP_LOGI(TAG, "Broadcasting at %d Hz on channel %d", TX_RATE_HZ, WIFI_CHANNEL);

    /* Main loop: send ESP-NOW packets at the configured rate */
    uint32_t packet_count = 0;
    while (1) {
        /* Send a small payload (4-byte counter) via ESP-NOW broadcast */
        esp_err_t send_ret = esp_now_send(
            broadcast_peer.peer_addr,
            (const uint8_t *)&packet_count,
            sizeof(packet_count)
        );

        if (send_ret != ESP_OK) {
            ESP_LOGW(TAG, "Send failed: %s", esp_err_to_name(send_ret));
        }

        packet_count++;

        /* Sleep for the inter-packet interval */
        usleep(1000000 / TX_RATE_HZ);
    }
}
