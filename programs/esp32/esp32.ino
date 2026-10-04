// Do NOT use special charactor (e.g. Kanji, Hiragana, Katakana, Hangul etc.) in this program!!
#include <Arduino.h>
#include <ArduinoJson.h>
#include "driver/spi_slave.h"

// Pin assign
#define SPI_MOSI 23
#define SPI_MISO 19
#define SPI_SCLK 18
#define SPI_CS 5

#define CHUNKSIZE 4096
#define SERVER_URL <Input server URL here>

WORD_ALIGNED_ATTR uint8_t rx_buf[SPI_CHUNKSIZE];

// Prototype Declaration
uint8_t xor_checksum(const uint8_t* data, size_t length);
void defrost_json(const char* input_json);
void request_resend_spi(const int 

void setup() {
  Serial.begin(115200);
  spi_bus_config_t buscfg = {
    .mosi_io_num = SPI_MOSI,
    .miso_io_num = SPI_MISO,
    .sclk_io_num = SPI_SCLK,
    .quadwp_io_num = -1,
    .quadhd_io_num = -1
  };
  spi_slave_interface_config_t slvcfg = {
    .spics_io_num = SPI_CS,
    .flags = 0,
    .queue_size = 1,
    .mode = 0,
    .post_setup_cb = NULL,
    .post_trans_cb = NULL
  };
  esp_err_t ret = spi_slave_initialize(VSPI_HOST, &buscfg, &slvcfg, SPI_DMA_CH_AUTO);
  if (ret != ESP_OK) {
    Serial.println("[Error] SPI Slave init failed");
  }
  Serial.println("[Info ] ESP32(Slave) Ready");
}

void loop() {
  memset(rx_buf, 0, CHUNKSIZE);

  spi_slave_transaction_t t;
  memset(&t, 0, sizeof(t));
  t.length = CHUNKSIZE * 8;
  t.rx_buffer = rx_buf;

  esp_err_t ret = spi_slave_transmit(VSPI_HOST, &t, portMAX_DELAY);
  
  if (ret == ESP_OK) {
    size_t received_bytes = t.trans_len / 8;
    Serial.printf("[Info ] Received chunk: %d bytes\n", received_bytes);
    JsonDocument received_doc;
    DeserializationError error = deserializeJson(received_doc, rx_buf);
    if (error) {
      Serial.println("[Error] Json parse failed");
    }
    const int rec_type = doc["header"]["type"] | -1;
    if (rec_type == -1) {
      Serial.println("[Error] Key \"type\" is NOT found");
    }
  }
}

uint8_t xor_checksum(const uint8_t* data, size_t length) {
  uint8_t checksum = 0;
  for (size_t i = 0; i < length; i++) {
    checksum ^= data[i];
  }
  return checksum;
}
