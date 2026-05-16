/*
 * WaveSense ESP32-RX — Receiver Firmware
 *
 * Captures CSI (Channel State Information) from ESP-NOW frames sent by the TX,
 * extracts I/Q data from each subcarrier, computes amplitudes, and publishes
 * JSON payloads to MQTT for the Python backend to process.
 *
 * Also subscribes to a relay control topic to toggle a 5V relay on GPIO 4.
 *
 * Hardware: ESP32-WROOM-32, powered by USB battery bank.
 * Relay module connected to GPIO 4 (active HIGH).
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "mqtt_client.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

static const char *TAG = "wavesense-rx";

/* ──────────────────────────────────────────────
 *  CONFIGURATION — Update these for your setup
 * ────────────────────────────────────────────── */

/* Wi-Fi credentials for your laptop's mobile hotspot */
#define WIFI_SSID       "WaveSenseAP"
#define WIFI_PASS       "wavesense123"

/* MQTT broker IP — this is your laptop's hotspot gateway IP.
 * On Windows, check with: ipconfig → look for "Mobile Hotspot" adapter.
 * Default is usually 192.168.137.1 */
#define MQTT_BROKER_URI "mqtt://192.168.137.1"

/* Wi-Fi channel — must match TX and laptop hotspot */
#define WIFI_CHANNEL    6

/* Relay GPIO pin (active HIGH) */
#define RELAY_GPIO      GPIO_NUM_4

/* MAC address of the TX node — used to filter CSI callbacks */
static const uint8_t TX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

/* ────────────────────────────────────────────── */

/* FreeRTOS event group bits */
#define WIFI_CONNECTED_BIT BIT0

static EventGroupHandle_t s_wifi_event_group;
static esp_mqtt_client_handle_t s_mqtt_client = NULL;

/* ──────────────────────────────────────────────
 *  Wi-Fi + IP Event Handlers
 * ────────────────────────────────────────────── */

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "Wi-Fi disconnected, reconnecting...");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/* ──────────────────────────────────────────────
 *  Wi-Fi Initialization (STA mode)
 * ────────────────────────────────────────────── */

static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    /* Register event handlers for connection management */
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    /* STA mode with HT40 bandwidth */
    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT40));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Disable power save for consistent CSI timing */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    /* Set channel to match TX */
    ESP_ERROR_CHECK(esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_BELOW));

    ESP_LOGI(TAG, "Wi-Fi STA initialized, connecting to %s...", WIFI_SSID);

    /* Block until we get an IP address */
    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdFALSE, portMAX_DELAY);
}

/* ──────────────────────────────────────────────
 *  CSI Callback — The Heart of the System
 * ────────────────────────────────────────────── */

/*
 * This callback fires for every received Wi-Fi frame when promiscuous mode
 * is enabled. We filter for our TX's MAC, extract I/Q pairs from the CSI
 * buffer, compute per-subcarrier amplitudes, and publish to MQTT.
 *
 * Buffer format for ESP32-WROOM-32: [imag0, real0, imag1, real1, ...]
 * Each pair is signed int8. Amplitude = sqrt(real^2 + imag^2).
 */
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf || info->len == 0) {
        return;
    }

    /* MAC filter: only process frames from our transmitter */
    if (memcmp(info->mac, TX_MAC, 6) != 0) {
        return;
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

    /* Each subcarrier uses 2 bytes (I and Q) */
    int subcarrier_count = info->len / 2;
    if (subcarrier_count > 64) {
        subcarrier_count = 64;
    }

    /* Extract amplitudes from I/Q pairs */
    float amplitudes[64];
    float sum = 0.0f;

    for (int i = 0; i < subcarrier_count; i++) {
        int8_t imag = info->buf[i * 2];       /* Q (imaginary) */
        int8_t real = info->buf[i * 2 + 1];   /* I (real) */
        amplitudes[i] = sqrtf((float)(real * real + imag * imag));
        sum += amplitudes[i];
    }

    float mean_amplitude = (subcarrier_count > 0) ? (sum / subcarrier_count) : 0.0f;

    /* Build JSON payload with CSI data */
    char json[1024];
    int off = snprintf(json, sizeof(json),
        "{\"ts\":%lld,\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\","
        "\"rssi\":%d,\"nsub\":%d,\"mean\":%.2f,\"amps\":[",
        (long long)(esp_timer_get_time() / 1000),
        info->mac[0], info->mac[1], info->mac[2],
        info->mac[3], info->mac[4], info->mac[5],
        rx_ctrl->rssi, subcarrier_count, mean_amplitude);

    for (int i = 0; i < subcarrier_count && off < (int)sizeof(json) - 20; i++) {
        off += snprintf(json + off, sizeof(json) - off, "%.1f%s",
            amplitudes[i], (i < subcarrier_count - 1) ? "," : "");
    }
    off += snprintf(json + off, sizeof(json) - off, "]}");

    /* Publish to MQTT (QoS 0, fire-and-forget for speed) */
    if (s_mqtt_client) {
        esp_mqtt_client_publish(s_mqtt_client, "wavesense/csi/raw", json, 0, 0, 0);
    }
}

/* ──────────────────────────────────────────────
 *  CSI Initialization
 * ────────────────────────────────────────────── */

static void wifi_csi_init(void)
{
    /* Enable promiscuous mode — required for CSI callback to fire */
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /* CSI configuration */
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,   /* Legacy Long Training Field */
        .htltf_en          = true,   /* HT Long Training Field */
        .stbc_htltf2_en    = true,   /* STBC HT LTF2 */
        .ltf_merge_en      = true,   /* Merge LTF data */
        .channel_filter_en = true,   /* Hardware noise filtering */
        .manu_scale        = false,  /* Automatic scaling */
        .shift             = false,  /* No manual shift */
    };

    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "CSI capture initialized");
}

/* ──────────────────────────────────────────────
 *  Relay GPIO Control
 * ────────────────────────────────────────────── */

static void relay_gpio_init(void)
{
    gpio_reset_pin(RELAY_GPIO);
    gpio_set_direction(RELAY_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(RELAY_GPIO, 0);  /* Start with relay OFF */
    ESP_LOGI(TAG, "Relay GPIO %d initialized (OFF)", RELAY_GPIO);
}

/* ──────────────────────────────────────────────
 *  MQTT Event Handler
 * ────────────────────────────────────────────── */

static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;

    switch (event_id) {
        case MQTT_EVENT_CONNECTED:
            ESP_LOGI(TAG, "MQTT Connected to broker");
            /* Subscribe to relay control topic (QoS 1 for reliability) */
            esp_mqtt_client_subscribe_single(s_mqtt_client, "wavesense/relay/control", 1);
            ESP_LOGI(TAG, "Subscribed to wavesense/relay/control");
            break;

        case MQTT_EVENT_DATA:
            /* Handle relay control messages */
            if (event->topic_len > 0 &&
                strncmp(event->topic, "wavesense/relay/control", event->topic_len) == 0) {
                /* Parse: {"relay": true} or {"relay": false} */
                bool relay_on = (strstr(event->data, "true") != NULL);
                gpio_set_level(RELAY_GPIO, relay_on ? 1 : 0);
                ESP_LOGI(TAG, "Relay %s", relay_on ? "ON" : "OFF");
            }
            break;

        case MQTT_EVENT_DISCONNECTED:
            ESP_LOGW(TAG, "MQTT Disconnected");
            break;

        case MQTT_EVENT_ERROR:
            ESP_LOGE(TAG, "MQTT Error");
            break;

        default:
            break;
    }
}

/* ──────────────────────────────────────────────
 *  MQTT Initialization
 * ────────────────────────────────────────────── */

static void mqtt_init(void)
{
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = MQTT_BROKER_URI,
        .credentials.client_id = "wavesense-rx",
        .session.keepalive = 60,
        .network.reconnect_timeout_ms = 5000,
    };

    s_mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(s_mqtt_client, MQTT_EVENT_ANY, mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_mqtt_client);

    ESP_LOGI(TAG, "MQTT client started, connecting to %s", MQTT_BROKER_URI);
}

/* ──────────────────────────────────────────────
 *  Main Entry Point
 * ────────────────────────────────────────────── */

void app_main(void)
{
    /* Initialize NVS flash (required by Wi-Fi driver) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* Step 1: Connect to laptop hotspot (blocks until IP acquired) */
    wifi_init_sta();

    /* Step 2: Init ESP-NOW so the radio processes ESP-NOW frames (triggers CSI) */
    ESP_ERROR_CHECK(esp_now_init());

    /* Add broadcast peer so we can receive ESP-NOW frames */
    esp_now_peer_info_t peer = {
        .channel = WIFI_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memset(peer.peer_addr, 0xff, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    /* Step 3: Enable CSI capture */
    wifi_csi_init();

    /* Step 4: Connect to MQTT broker */
    mqtt_init();

    /* Step 5: Configure relay GPIO */
    relay_gpio_init();

    ESP_LOGI(TAG, "=== WaveSense RX Started ===");
    ESP_LOGI(TAG, "CSI data publishing to wavesense/csi/raw");
    ESP_LOGI(TAG, "Listening for relay commands on wavesense/relay/control");

    /* Idle loop — all work happens in callbacks */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
