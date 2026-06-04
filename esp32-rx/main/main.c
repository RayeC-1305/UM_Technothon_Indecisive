/*
 * WaveSense ESP32-S3 RX v7 — JSON fix (phase array correctly embedded)
 * CSI_ACCEPT_ALL = false — MAC filter works because TX sends probe frames
 *
 * FIX: MQTT topic/payload strings now properly null‑terminated.
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
#include "freertos/queue.h"

static const char *TAG = "wavesense-rx";

#define WIFI_SSID           "Wavesense"
#define WIFI_PASS           "Hemsembo1ism3"
#define MQTT_BROKER_URI     "mqtt://pi.local"
#define WIFI_CHANNEL        1
#define USB_BAUD_RATE       921600
#define CALIBRATION_SEC     30
#define RELAY_GPIO          GPIO_NUM_18
#define MAX_SUBCARRIERS     128
#define CSI_QUEUE_DEPTH     100

// TX MAC — probe frames show this MAC in info->mac correctly
static const uint8_t TX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x01};
#define CSI_ACCEPT_ALL      false

#define WIFI_CONNECTED_BIT  BIT0
#define MQTT_CONNECTED_BIT  BIT1
#define CSI_READY_BIT       BIT2
#define CAL_DONE_BIT        BIT3

static EventGroupHandle_t       s_evt_group    = NULL;
static esp_mqtt_client_handle_t s_mqtt_client  = NULL;

#define CAL_SAMPLES_TARGET  (CALIBRATION_SEC * 100)

static double   s_cal_sum[MAX_SUBCARRIERS];
static double   s_cal_sq[MAX_SUBCARRIERS];
static uint32_t s_cal_count  = 0;
static bool     s_calibrated = false;
static float    s_baseline_amp[MAX_SUBCARRIERS];
static float    s_baseline_std[MAX_SUBCARRIERS];

typedef struct {
    int64_t  ts_ms;
    uint8_t  mac[6];
    int8_t   rssi;
    uint8_t  noise_floor;
    int      nsub;
    int8_t   buf[MAX_SUBCARRIERS * 2];
} csi_packet_t;

static QueueHandle_t     s_csi_queue  = NULL;
static volatile uint32_t s_cb_total   = 0;
static volatile uint32_t s_cb_accepted = 0;
static volatile uint32_t s_cb_dropped  = 0;

static void cal_accumulate(const float *amps, int nsub)
{
    if (s_calibrated) return;
    int n = nsub < MAX_SUBCARRIERS ? nsub : MAX_SUBCARRIERS;
    for (int i = 0; i < n; i++) {
        s_cal_sum[i] += amps[i];
        s_cal_sq[i]  += (double)amps[i] * amps[i];
    }
    s_cal_count++;
    if (s_cal_count >= CAL_SAMPLES_TARGET) {
        for (int i = 0; i < MAX_SUBCARRIERS; i++) {
            float mean = (float)(s_cal_sum[i] / s_cal_count);
            float var  = (float)(s_cal_sq[i] / s_cal_count) - mean * mean;
            s_baseline_amp[i] = mean;
            s_baseline_std[i] = sqrtf(var > 0.0f ? var : 0.0f);
            if (s_baseline_std[i] < 0.1f) s_baseline_std[i] = 0.1f;
        }
        s_calibrated = true;
        xEventGroupSetBits(s_evt_group, CAL_DONE_BIT);
        ESP_LOGI(TAG, "=== Calibration complete (%lu samples) ===",
                 (unsigned long)s_cal_count);
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_evt_group, WIFI_CONNECTED_BIT | CSI_READY_BIT);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_evt_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
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
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    xEventGroupWaitBits(s_evt_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdFALSE, portMAX_DELAY);

    uint8_t ch; wifi_second_chan_t sec;
    esp_wifi_get_channel(&ch, &sec);
    ESP_LOGI(TAG, "Associated on channel %d", ch);
}

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf || info->len == 0) return;
    s_cb_total++;

    if (!CSI_ACCEPT_ALL) {
        if (memcmp(info->mac, TX_MAC, 6) != 0) return;
    }
    s_cb_accepted++;

    csi_packet_t pkt;
    pkt.ts_ms       = esp_timer_get_time() / 1000LL;
    pkt.rssi        = (int8_t)info->rx_ctrl.rssi;
    pkt.noise_floor = (uint8_t)info->rx_ctrl.noise_floor;
    memcpy(pkt.mac, info->mac, 6);
    pkt.nsub = info->len / 2;
    if (pkt.nsub > MAX_SUBCARRIERS) pkt.nsub = MAX_SUBCARRIERS;
    memcpy(pkt.buf, info->buf, pkt.nsub * 2);

    BaseType_t hp = pdFALSE;
    if (xQueueSendFromISR(s_csi_queue, &pkt, &hp) != pdTRUE)
        s_cb_dropped++;
    portYIELD_FROM_ISR(hp);
}

static void csi_init_after_connect(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));

    uint8_t ch; wifi_second_chan_t sec;
    esp_wifi_get_channel(&ch, &sec);
    ESP_LOGI(TAG, "CSI channel: primary=%d secondary=%d", ch, sec);

    wifi_csi_config_t csi_cfg = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
    xEventGroupSetBits(s_evt_group, CSI_READY_BIT);
    ESP_LOGI(TAG, "CSI active — waiting for TX frames");
}

static void relay_gpio_init(void)
{
    gpio_reset_pin(RELAY_GPIO);
    gpio_set_direction(RELAY_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(RELAY_GPIO, 0);   // start with relay OFF
}

static void mqtt_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    esp_mqtt_event_handle_t ev = (esp_mqtt_event_handle_t)data;
    switch (id) {
        case MQTT_EVENT_CONNECTED:
            xEventGroupSetBits(s_evt_group, MQTT_CONNECTED_BIT);
            // Subscribe to relay control topic
            esp_mqtt_client_subscribe(s_mqtt_client, "wavesense/relay/control", 1);
            ESP_LOGI(TAG, "MQTT connected, subscribed to wavesense/relay/control");
            break;
        case MQTT_EVENT_DISCONNECTED:
            xEventGroupClearBits(s_evt_group, MQTT_CONNECTED_BIT);
            break;
        case MQTT_EVENT_DATA: {
            if (!ev->topic || !ev->data) break;

            // --- FIX: null-terminate topic ---
            char topic[64];
            int topic_len = ev->topic_len < sizeof(topic)-1 ? ev->topic_len : sizeof(topic)-1;
            memcpy(topic, ev->topic, topic_len);
            topic[topic_len] = '\0';

            if (strcmp(topic, "wavesense/relay/control") == 0) {
                // --- FIX: null-terminate payload ---
                char payload[64];
                int data_len = ev->data_len < sizeof(payload)-1 ? ev->data_len : sizeof(payload)-1;
                memcpy(payload, ev->data, data_len);
                payload[data_len] = '\0';

                bool on = (strstr(payload, "true") != NULL);
                // Relay is active LOW: occupied -> LOW (0), vacant -> HIGH (1)
                // If your relay module is active HIGH, change the line below to:
                // gpio_set_level(RELAY_GPIO, on ? 1 : 0);
                gpio_set_level(RELAY_GPIO, on ? 0 : 1);
                ESP_LOGI(TAG, "Relay set to %d (payload='%s')", on, payload);
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
    mc.credentials.client_id        = "wavesense-rx";
    mc.session.keepalive             = 60;
    mc.network.reconnect_timeout_ms  = 5000;
    s_mqtt_client = esp_mqtt_client_init(&mc);
    esp_mqtt_client_register_event(s_mqtt_client, MQTT_EVENT_ANY,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_mqtt_client);
}

static void csi_publish_task(void *arg)
{
    csi_packet_t pkt;
    static char json[6400];
    uint32_t pub_usb  = 0;
    uint32_t pub_mqtt = 0;
    TickType_t last_diag = xTaskGetTickCount();

    while (1) {
        if (xQueueReceive(s_csi_queue, &pkt, pdMS_TO_TICKS(200)) != pdTRUE) {
            TickType_t now = xTaskGetTickCount();
            if ((now - last_diag) >= pdMS_TO_TICKS(5000)) {
                ESP_LOGI(TAG,
                    "CSI | total=%lu accepted=%lu dropped=%lu "
                    "usb=%lu mqtt=%lu cal=%lu/%d",
                    (unsigned long)s_cb_total,
                    (unsigned long)s_cb_accepted,
                    (unsigned long)s_cb_dropped,
                    (unsigned long)pub_usb,
                    (unsigned long)pub_mqtt,
                    (unsigned long)s_cal_count,
                    CAL_SAMPLES_TARGET);
                last_diag = now;
            }
            continue;
        }

        float amps[MAX_SUBCARRIERS];
        float sum = 0.0f;
        for (int i = 0; i < pkt.nsub; i++) {
            int8_t q = pkt.buf[i * 2];
            int8_t r = pkt.buf[i * 2 + 1];
            amps[i] = sqrtf((float)(r * r + q * q));
            sum    += amps[i];
        }
        float mean = pkt.nsub > 0 ? sum / (float)pkt.nsub : 0.0f;

        if (!s_calibrated)
            cal_accumulate(amps, pkt.nsub);

        float mean_deviation = 0.0f;
        if (s_calibrated) {
            float dev_sum = 0.0f;
            for (int i = 0; i < pkt.nsub; i++) {
                float z = fabsf(amps[i] - s_baseline_amp[i]) / s_baseline_std[i];
                dev_sum += z;
            }
            mean_deviation = pkt.nsub > 0 ? dev_sum / (float)pkt.nsub : 0.0f;
        }

        int off = snprintf(json, sizeof(json),
            "{"
            "\"ts\":%lld,"
            "\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\","
            "\"rssi\":%d,"
            "\"nf\":%d,"
            "\"nsub\":%d,"
            "\"mean\":%.2f,"
            "\"dev\":%.3f,"
            "\"cal\":%s,"
            "\"cal_pct\":%.1f,"
            "\"amps\":[",
            (long long)pkt.ts_ms,
            pkt.mac[0], pkt.mac[1], pkt.mac[2],
            pkt.mac[3], pkt.mac[4], pkt.mac[5],
            (int)pkt.rssi,
            (int)pkt.noise_floor,
            pkt.nsub,
            mean,
            mean_deviation,
            s_calibrated ? "true" : "false",
            s_cal_count >= CAL_SAMPLES_TARGET ? 100.0f :
                100.0f * (float)s_cal_count / (float)CAL_SAMPLES_TARGET
        );

        for (int i = 0; i < pkt.nsub; i++) {
            int rem = (int)sizeof(json) - off - 8;
            if (rem <= 0) break;
            off += snprintf(json + off, rem, "%.1f%s",
                            amps[i], i < pkt.nsub - 1 ? "," : "");
        }

        if (off < (int)sizeof(json) - 3)
            off += snprintf(json + off, sizeof(json) - off, "],");

        off += snprintf(json + off, sizeof(json) - off, "\"phase\":[");
        for (int i = 0; i < pkt.nsub; i++) {
            int8_t q = pkt.buf[i * 2];
            int8_t r = pkt.buf[i * 2 + 1];
            float ph = atan2f((float)q, (float)r);
            int rem = (int)sizeof(json) - off - 8;
            if (rem <= 0) break;
            off += snprintf(json + off, rem, "%.3f%s", ph,
                            (i < pkt.nsub - 1) ? "," : "");
        }

        if (off < (int)sizeof(json) - 3)
            off += snprintf(json + off, sizeof(json) - off, "]}");

        Serial.println(json);
        pub_usb++;

        EventBits_t bits = xEventGroupGetBits(s_evt_group);
        if ((bits & MQTT_CONNECTED_BIT) && s_mqtt_client) {
            esp_mqtt_client_publish(s_mqtt_client,
                "wavesense/csi/raw", json, off, 0, 0);
            pub_mqtt++;
        }

        TickType_t now = xTaskGetTickCount();
        if ((now - last_diag) >= pdMS_TO_TICKS(5000)) {
            ESP_LOGI(TAG,
                "CSI | total=%lu accepted=%lu dropped=%lu usb=%lu mqtt=%lu",
                (unsigned long)s_cb_total,
                (unsigned long)s_cb_accepted,
                (unsigned long)s_cb_dropped,
                (unsigned long)pub_usb,
                (unsigned long)pub_mqtt);
            last_diag = now;
        }
    }
}

void setup()
{
    Serial.begin(USB_BAUD_RATE);
    delay(2000);
    Serial.println("{\"boot\":true,\"mode\":\"dual\",\"baud\":921600}");

    memset(s_cal_sum,      0, sizeof(s_cal_sum));
    memset(s_cal_sq,       0, sizeof(s_cal_sq));
    memset(s_baseline_amp, 0, sizeof(s_baseline_amp));
    memset(s_baseline_std, 0, sizeof(s_baseline_std));

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

    wifi_init_sta();

    s_csi_queue = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(csi_packet_t));
    if (!s_csi_queue) {
        ESP_LOGE(TAG, "FATAL: queue alloc failed");
        esp_restart();
    }

    xTaskCreate(csi_publish_task, "csi_pub", 8192, NULL, 5, NULL);
    csi_init_after_connect();
    mqtt_init();
    relay_gpio_init();

    ESP_LOGI(TAG, "=== WaveSense RX v7 ready ===");
    ESP_LOGI(TAG, "KEEP ROOM EMPTY for %d seconds (calibration)",
             CALIBRATION_SEC);
}

void loop()
{
    vTaskDelay(pdMS_TO_TICKS(1000));
}
