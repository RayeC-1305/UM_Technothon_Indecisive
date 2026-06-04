/*
 * WaveSense ESP32-S3 TX v7
 * Uses raw 802.11 probe request frames instead of ESP-NOW
 * This makes TX MAC appear correctly in RX CSI callback
 * so MAC filter works and RX only gets TX frames at 100Hz
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "mqtt_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

static const char *TAG = "wavesense-tx";

#define WIFI_SSID          "Wavesense"
#define WIFI_PASS          "Hemsembo1ism3"
#define MQTT_BROKER_URI    "mqtt://pi.local"
#define WIFI_CHANNEL       1
#define TX_POWER_DEFAULT   20
#define TX_POWER_MIN        1
#define TX_POWER_MAX       20

// This MAC will appear in RX CSI info->mac — must match RX TX_MAC[]
static const uint8_t TX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x01};

#define WIFI_CONNECTED_BIT BIT0
#define MQTT_CONNECTED_BIT BIT1

static EventGroupHandle_t       s_evt_group    = NULL;
static esp_mqtt_client_handle_t s_mqtt_client  = NULL;
static volatile int             s_tx_power_dbm = TX_POWER_DEFAULT;
static uint8_t                  s_radio_channel = WIFI_CHANNEL;
static uint32_t                 s_seq           = 0;

// 802.11 Null Data frame (Allowed by ESP-IDF, forces high-speed OFDM)
static uint8_t s_probe_frame[] = {
    0x40, 0x00,                          // [0-1] Frame control: Null Data (No Payload)
    0x00, 0x00,                          // [2-3] Duration
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff,  // [4-9] Addr1: Receiver (Broadcast)
    0x1a, 0x00, 0x00, 0x00, 0x00, 0x01,  // [10-15] Addr2: TX MAC (Source)
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff,  // [16-21] Addr3: BSSID (Broadcast)
    0x00, 0x00                           // [22-23] Sequence control
};

static void apply_tx_power(int dbm)
{
    if (dbm < TX_POWER_MIN) dbm = TX_POWER_MIN;
    if (dbm > TX_POWER_MAX) dbm = TX_POWER_MAX;
    s_tx_power_dbm = dbm;
    esp_wifi_set_max_tx_power((int8_t)(dbm * 4));
    ESP_LOGI(TAG, "TX power -> %d dBm", dbm);
}

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_evt_group, WIFI_CONNECTED_BIT);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_evt_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    wifi_config_t wc = {};
    strcpy((char *)wc.sta.ssid,     WIFI_SSID);
    strcpy((char *)wc.sta.password, WIFI_PASS);
    wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));

    // Set TX MAC before start
    esp_err_t mac_err = esp_wifi_set_mac(WIFI_IF_STA, TX_MAC);
    if (mac_err != ESP_OK)
        ESP_LOGE(TAG, "set_mac failed: %s", esp_err_to_name(mac_err));

    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    // Verify MAC
    uint8_t actual_mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, actual_mac);
    Serial.printf(">>> TX MAC: %02x:%02x:%02x:%02x:%02x:%02x <<<\n",
                  actual_mac[0], actual_mac[1], actual_mac[2],
                  actual_mac[3], actual_mac[4], actual_mac[5]);

    // Also update Addr2 in probe frame to match actual MAC
    memcpy(&s_probe_frame[10], actual_mac, 6);

    ESP_LOGI(TAG, "Connecting to '%s'...", WIFI_SSID);
    xEventGroupWaitBits(s_evt_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdFALSE, portMAX_DELAY);

    wifi_second_chan_t sec;
    esp_wifi_get_channel(&s_radio_channel, &sec);
    ESP_LOGI(TAG, "Radio on channel %d", s_radio_channel);
}

static void mqtt_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    esp_mqtt_event_handle_t ev = (esp_mqtt_event_handle_t)data;
    switch (id) {
        case MQTT_EVENT_CONNECTED: {
            xEventGroupSetBits(s_evt_group, MQTT_CONNECTED_BIT);
            esp_mqtt_client_subscribe_single(s_mqtt_client,
                                             "wavesense/tx/power", 1);
            char buf[8];
            snprintf(buf, sizeof(buf), "%d", (int)s_tx_power_dbm);
            esp_mqtt_client_publish(s_mqtt_client,
                "wavesense/tx/power/state", buf, 0, 1, 1);
            break;
        }
        case MQTT_EVENT_DISCONNECTED:
            xEventGroupClearBits(s_evt_group, MQTT_CONNECTED_BIT);
            break;
        case MQTT_EVENT_DATA: {
            if (!ev->topic || !ev->data) break;
            char topic[64] = {0};
            memcpy(topic, ev->topic, ev->topic_len < 63 ? ev->topic_len : 63);
            if (strcmp(topic, "wavesense/tx/power") == 0) {
                char payload[16] = {0};
                memcpy(payload, ev->data,
                       ev->data_len < 15 ? ev->data_len : 15);
                apply_tx_power(atoi(payload));
                char ack[8];
                snprintf(ack, sizeof(ack), "%d", (int)s_tx_power_dbm);
                esp_mqtt_client_publish(s_mqtt_client,
                    "wavesense/tx/power/state", ack, 0, 1, 1);
            }
            break;
        }
        default: break;
    }
}

static void mqtt_init(void)
{
    esp_mqtt_client_config_t mc = {};
    mc.broker.address.uri           = MQTT_BROKER_URI;
    mc.credentials.client_id        = "wavesense-tx";
    mc.session.keepalive             = 60;
    mc.network.reconnect_timeout_ms  = 5000;
    s_mqtt_client = esp_mqtt_client_init(&mc);
    esp_mqtt_client_register_event(s_mqtt_client, MQTT_EVENT_ANY,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_mqtt_client);
}

// TX task — pinned to core 1, priority 10
// WiFi stack runs on core 0 — zero interference
static void tx_task(void *arg)
{
    TickType_t last_wake = xTaskGetTickCount();
    uint32_t last_ms = 0;

    ESP_LOGI(TAG, "tx_task running on core %d", xPortGetCoreID());

    while (1) {
        s_seq++;

        // Update sequence number in frame header
        s_probe_frame[22] = (uint8_t)((s_seq << 4) & 0xff);
        s_probe_frame[23] = (uint8_t)((s_seq >> 4) & 0xff);

        // Send raw probe frame — bypasses ESP-NOW throttle
        // true = use system sequence number
        esp_wifi_80211_tx(WIFI_IF_STA, s_probe_frame,
                          sizeof(s_probe_frame), true);

        // Print timing every 100 frames
        // delta MUST be ~1000ms to confirm true 100Hz
        if (s_seq % 100 == 0) {
            uint32_t now_ms = (uint32_t)millis();
            Serial.printf("TX seq=%lu delta=%lums\n",
                          (unsigned long)s_seq,
                          (unsigned long)(now_ms - last_ms));
            last_ms = now_ms;
        }

        // vTaskDelayUntil = fixed period regardless of execution time
        // unlike vTaskDelay which adds delay AFTER execution
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(10));
    }
}

void setup()
{
    Serial.begin(115200);
    delay(2000);
    Serial.println("=== WaveSense TX v7 booting ===");

    s_evt_group = xEventGroupCreate();
    if (!s_evt_group) {
        Serial.println("FATAL: xEventGroupCreate failed");
        esp_restart();
    }

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    wifi_init();
    apply_tx_power(TX_POWER_DEFAULT);
    mqtt_init();

    // Core 1, priority 10 = true 100Hz guaranteed
    xTaskCreatePinnedToCore(tx_task, "tx_task", 4096, NULL, 10, NULL, 1);

    Serial.println("=== WaveSense TX v7 ready ===");
}

void loop()
{
    vTaskDelay(pdMS_TO_TICKS(1000));
}
