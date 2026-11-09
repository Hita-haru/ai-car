// Do NOT use special characters (e.g. Kanji, Hiragana, Katakana, Hangul etc.) in this program!!
// ==========================================================================================
// AI Car ESP32 Firmware (esp32.ino)
// Role: SPI Slave (RPi Zero Master) <--> WebSocket Client (FastAPI / Gemini Robotics Server)
// Hardware: ESP32 CH340C Type-C
//
// SPI Pin Assign (VSPI):
//   MOSI: GPIO 23 (RPi BCM 10 / Pin 19)
//   MISO: GPIO 19 (RPi BCM 9  / Pin 21)
//   SCLK: GPIO 18 (RPi BCM 11 / Pin 23)
//   CS:   GPIO 5  (RPi BCM 8  / Pin 24) [or GPIO 4 depending on wiring]
//   HANDSHAKE (SPI_READY): GPIO 22 (RPi BCM 25 / Pin 22)
//
// Packet Protocol: MSprotocol.md & ai-car-summary.md specifications
//   1. INI packet  -> Slave prepares buffer, replies ACK
//   2. STR packets -> Slave receives image chunk (4KB), verifies checksum, replies ACK
//   3. FIN packet  -> Slave completes image assembly, sends via WebSocket to server,
//                     and replies ACK with control decision (speed, angle, duration)
// ==========================================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "driver/spi_slave.h"

// ------------------------------------------------------------------------------------------
// Pin Definitions
// ------------------------------------------------------------------------------------------
#define SPI_MOSI      23
#define SPI_MISO      19
#define SPI_SCLK      18
#define SPI_CS        5
#define SPI_HANDSHAKE 22  // Handshake signal (Active HIGH: ESP32 ready for SPI transfer)

// ------------------------------------------------------------------------------------------
// Buffer & Size Constants
// ------------------------------------------------------------------------------------------
#define CHUNKSIZE             4096   // Max SPI RX buffer size per transaction
#define ACK_BUFFER_SIZE       256    // Fixed S -> M SPI TX buffer size (MSprotocol spec)
#define MAX_BASE64_BUF_SIZE   32768  // Buffer for assembled 160x120 JPEG Base64 image

// ------------------------------------------------------------------------------------------
// Wi-Fi & WebSocket Configuration
// Please update WIFI_SSID and WIFI_PASS to match your network environment.
// ------------------------------------------------------------------------------------------
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";

// Server endpoint configuration (FastAPI server running programs/server/main.py)
#define SERVER_HOST "192.168.1.100"
#define SERVER_PORT 8000
#define SERVER_PATH "/ws/control"
#define SERVER_URL  "ws://192.168.1.100:8000/ws/control"

// ------------------------------------------------------------------------------------------
// DMA Buffers (Must be 4-byte word aligned for ESP32 SPI Slave DMA)
// ------------------------------------------------------------------------------------------
WORD_ALIGNED_ATTR uint8_t rx_buf[CHUNKSIZE];
WORD_ALIGNED_ATTR uint8_t tx_buf[ACK_BUFFER_SIZE];

// WebSocket Client Instance
WebSocketsClient webSocket;

// ------------------------------------------------------------------------------------------
// Vehicle Control State (Updated from server responses)
// ------------------------------------------------------------------------------------------
struct VehicleControl {
  int speed;             // -255 to 255 (0 = stop, cruise default = 160-180)
  int angle;             // 0 to 180 (90 = straight, 0 = left, 180 = right)
  int duration_ms;       // Driving duration per cycle (default 500ms)
  char reason[128];      // Spatial reasoning text from Gemini Robotics ER 2
  char status[32];       // "success", "failsafe", "idle"
  uint32_t sequence;     // Packet sequence number
  bool updated;          // True when a new decision has arrived
};

VehicleControl g_control = {
  0,                     // speed: stop initially
  90,                    // angle: straight center
  500,                   // duration_ms
  "Initial idle state",  // reason
  "idle",                // status
  0,                     // sequence
  false                  // updated
};

// Image Assembly State
String g_assembled_image = "";
int g_total_chunks = 3;
int g_received_chunks = 0;
uint32_t g_current_session_seq = 0;
bool g_server_connected = false;

// ------------------------------------------------------------------------------------------
// Function Prototypes
// ------------------------------------------------------------------------------------------
uint8_t xor_checksum(const uint8_t* data, size_t length);
void request_resend_spi(const int chunk_id);
void prepare_ack_packet(const char* status_str, int chunk_or_seq, int checksum_or_resend);
void prepare_control_ack_packet(uint32_t seq);
void send_assembled_image_to_server(uint32_t seq);
void webSocketEvent(WStype_t type, uint8_t* payload, size_t length);
void handle_spi_transaction();
void assert_handshake(bool active);

// ==========================================================================================
// Setup
// ==========================================================================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n==========================================");
  Serial.println("[Info ] AI Car ESP32 SPI-WebSocket Bridge");
  Serial.println("==========================================");

  // 1. Initialize Handshake pin
  pinMode(SPI_HANDSHAKE, OUTPUT);
  digitalWrite(SPI_HANDSHAKE, LOW);

  // 2. Clear Buffers
  memset(rx_buf, 0, CHUNKSIZE);
  memset(tx_buf, 0, ACK_BUFFER_SIZE);

  // Prepare initial Ready ACK packet in tx_buf
  prepare_ack_packet("READY", 0, 0);

  // 3. Configure and Initialize SPI Slave
  spi_bus_config_t buscfg = {
    .mosi_io_num = SPI_MOSI,
    .miso_io_num = SPI_MISO,
    .sclk_io_num = SPI_SCLK,
    .quadwp_io_num = -1,
    .quadhd_io_num = -1,
    .max_transfer_sz = CHUNKSIZE
  };

  spi_slave_interface_config_t slvcfg = {
    .spics_io_num = SPI_CS,
    .flags = 0,
    .queue_size = 1,
    .mode = 0,               // SPI Mode 0 (CPOL=0, CPHA=0)
    .post_setup_cb = NULL,
    .post_trans_cb = NULL
  };

  esp_err_t ret = spi_slave_initialize(VSPI_HOST, &buscfg, &slvcfg, SPI_DMA_CH_AUTO);
  if (ret != ESP_OK) {
    Serial.printf("[Error] SPI Slave init failed (code: 0x%X)\n", ret);
  } else {
    Serial.println("[Info ] SPI Slave initialized successfully on VSPI");
  }

  // 4. Initialize Wi-Fi
  Serial.printf("[Info ] Connecting to Wi-Fi SSID: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  // Note: We don't block indefinitely on Wi-Fi so SPI tests can still run offline
  uint32_t wifi_start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - wifi_start < 5000)) {
    delay(200);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[Info ] Wi-Fi connected! IP: %s\n", WiFi.localIP().toString().c_str());
    // 5. Initialize WebSocket client
    webSocket.begin(SERVER_HOST, SERVER_PORT, SERVER_PATH);
    webSocket.onEvent(webSocketEvent);
    webSocket.setReconnectInterval(3000);
    Serial.printf("[Info ] WebSocket target: %s\n", SERVER_URL);
  } else {
    Serial.println("[Warn ] Wi-Fi connection timed out. Will retry in loop.");
  }

  g_assembled_image.reserve(MAX_BASE64_BUF_SIZE);
  Serial.println("[Info ] ESP32 setup complete. Ready for operation.");
}

// ==========================================================================================
// Main Loop
// ==========================================================================================
void loop() {
  // 1. Maintain WebSocket Connection
  if (WiFi.status() == WL_CONNECTED) {
    webSocket.loop();
  } else {
    // Attempt non-blocking Wi-Fi reconnect periodically
    static uint32_t last_wifi_retry = 0;
    if (millis() - last_wifi_retry > 10000) {
      last_wifi_retry = millis();
      Serial.println("[Info ] Attempting Wi-Fi reconnection...");
      WiFi.reconnect();
    }
  }

  // 2. Handle Master-Slave SPI Communication
  handle_spi_transaction();

  yield();
}

// ==========================================================================================
// Handshake Control
// ==========================================================================================
void assert_handshake(bool active) {
  digitalWrite(SPI_HANDSHAKE, active ? HIGH : LOW);
}

// ==========================================================================================
// XOR Checksum Calculation (Consistent with Raspi_spi_master.py)
// ==========================================================================================
uint8_t xor_checksum(const uint8_t* data, size_t length) {
  uint8_t checksum = 0;
  for (size_t i = 0; i < length; i++) {
    checksum ^= data[i];
  }
  return checksum;
}

// ==========================================================================================
// S -> M Packet Builders (256-byte fixed size, NULL-padded)
// ==========================================================================================

// Build standard ACK or RESEND_REQ packet
void prepare_ack_packet(const char* status_str, int chunk_or_seq, int checksum_or_resend) {
  memset(tx_buf, 0, ACK_BUFFER_SIZE);

  JsonDocument doc;
  doc["header"]["type"] = "ACK";
  doc["header"]["status"] = status_str;
  doc["header"]["seq"] = chunk_or_seq;

  if (strcmp(status_str, "RESEND_REQ") == 0) {
    doc["body"]["resend_chunk"] = chunk_or_seq;
    doc["body"]["reason"] = "checksum_mismatch";
  } else {
    doc["body"]["ack_chunk"] = chunk_or_seq;
    doc["body"]["received_checksum"] = checksum_or_resend;
  }

  // Calculate body checksum
  String body_str;
  serializeJson(doc["body"], body_str);
  uint8_t body_cs = xor_checksum((const uint8_t*)body_str.c_str(), body_str.length());
  doc["footer"]["checksum"] = body_cs;

  // Serialize to fixed tx_buf with null terminator
  serializeJson(doc, (char*)tx_buf, ACK_BUFFER_SIZE - 1);
  tx_buf[ACK_BUFFER_SIZE - 1] = '\0';
}

// Build CONTROL_READY feedback packet containing latest server decisions
void prepare_control_ack_packet(uint32_t seq) {
  memset(tx_buf, 0, ACK_BUFFER_SIZE);

  JsonDocument doc;
  doc["header"]["type"] = "ACK";
  doc["header"]["status"] = g_server_connected ? "CONTROL_READY" : "FAILSAFE_STOP";
  doc["header"]["seq"] = seq;

  // Body containing speed, angle, duration_ms, reason
  doc["body"]["speed"] = g_control.speed;
  doc["body"]["angle"] = g_control.angle;
  doc["body"]["duration_ms"] = g_control.duration_ms;
  doc["body"]["reason"] = g_control.reason;

  // Body checksum
  String body_str;
  serializeJson(doc["body"], body_str);
  uint8_t body_cs = xor_checksum((const uint8_t*)body_str.c_str(), body_str.length());
  doc["footer"]["checksum"] = body_cs;

  serializeJson(doc, (char*)tx_buf, ACK_BUFFER_SIZE - 1);
  tx_buf[ACK_BUFFER_SIZE - 1] = '\0';
}

// Request packet retransmission on failure
void request_resend_spi(const int chunk_id) {
  Serial.printf("[Warn ] Requesting resend for chunk #%d\n", chunk_id);
  prepare_ack_packet("RESEND_REQ", chunk_id, 0);
}

// ==========================================================================================
// SPI Slave Transaction Handling
// ==========================================================================================
void handle_spi_transaction() {
  memset(rx_buf, 0, CHUNKSIZE);

  spi_slave_transaction_t t;
  memset(&t, 0, sizeof(t));
  t.length = CHUNKSIZE * 8;      // Max bit length to receive
  t.rx_buffer = rx_buf;
  t.tx_buffer = tx_buf;          // Pre-assigned TX buffer for full duplex response

  // 1. Assert Handshake (HIGH) to notify RPi Master that ESP32 is ready
  assert_handshake(true);

  // 2. Wait for Master SCLK transfer (non-blocking timeout 50ms to yield to WebSocket loop)
  esp_err_t ret = spi_slave_transmit(VSPI_HOST, &t, pdMS_TO_TICKS(50));

  // 3. De-assert Handshake (LOW) after transaction completes or times out
  assert_handshake(false);

  if (ret != ESP_OK) {
    // Timeout is normal when Master is not actively transmitting
    return;
  }

  size_t received_bytes = t.trans_len / 8;
  if (received_bytes == 0) {
    return;
  }

  // Ensure NULL-termination for string parsing
  if (received_bytes < CHUNKSIZE) {
    rx_buf[received_bytes] = '\0';
  } else {
    rx_buf[CHUNKSIZE - 1] = '\0';
  }

  // 4. Parse Received JSON Packet
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, (const char*)rx_buf);
  if (err) {
    Serial.printf("[Error] JSON parse error: %s\n", err.c_str());
    prepare_ack_packet("ERROR", 0, -1);
    return;
  }

  const char* type_str = doc["header"]["type"] | "";
  uint32_t seq = doc["header"]["seq"] | 0;

  // ----------------------------------------------------------------------------------------
  // Case 1: INI Packet (Session Initialization)
  // ----------------------------------------------------------------------------------------
  if (strcmp(type_str, "INI") == 0) {
    Serial.println("[Info ] >>> Received INI packet");
    g_assembled_image = "";
    g_received_chunks = 0;
    g_current_session_seq = seq;
    g_total_chunks = doc["body"]["total_chunks"] | 3;

    // Reply with OK ACK
    prepare_ack_packet("OK", 0, 0);
    Serial.println("[Info ] <<< Prepared INI ACK");
  }
  // ----------------------------------------------------------------------------------------
  // Case 2: STR Packet (Image Chunk Transfer)
  // ----------------------------------------------------------------------------------------
  else if (strcmp(type_str, "STR") == 0) {
    int chunk_id = doc["body"]["chunk_id"] | seq;
    const char* chunk_data = doc["body"]["data"] | "";
    uint8_t expected_cs = doc["footer"]["checksum"] | 0;

    // Verify chunk integrity using XOR checksum
    String body_str;
    serializeJson(doc["body"], body_str);
    uint8_t calculated_cs = xor_checksum((const uint8_t*)body_str.c_str(), body_str.length());

    if (calculated_cs != expected_cs && expected_cs != 0) {
      Serial.printf("[Warn ] Checksum mismatch on chunk #%d (calc=%d, exp=%d)\n",
                    chunk_id, calculated_cs, expected_cs);
      request_resend_spi(chunk_id);
    } else {
      g_assembled_image += chunk_data;
      g_received_chunks++;
      Serial.printf("[Info ] >>> Received STR chunk #%d (%d chars, total len=%d)\n",
                    chunk_id, strlen(chunk_data), g_assembled_image.length());
      prepare_ack_packet("OK", chunk_id, calculated_cs);
    }
  }
  // ----------------------------------------------------------------------------------------
  // Case 3: FIN Packet (Transfer End & Request Control Values)
  // ----------------------------------------------------------------------------------------
  else if (strcmp(type_str, "FIN") == 0) {
    Serial.printf("[Info ] >>> Received FIN packet. Full image length: %d\n", g_assembled_image.length());

    // Send complete image to server via WebSocket if connected
    if (g_assembled_image.length() > 0 && g_server_connected) {
      send_assembled_image_to_server(g_current_session_seq);
    } else if (!g_server_connected) {
      Serial.println("[Warn ] Server not connected. Applying failsafe stop control.");
      g_control.speed = 0;
      g_control.angle = 90;
      strncpy(g_control.reason, "Server disconnected failsafe", sizeof(g_control.reason) - 1);
    }

    // Prepare control feedback ACK in tx_buf for the Master's subsequent clock query
    prepare_control_ack_packet(g_current_session_seq);
    Serial.printf("[Info ] <<< Prepared CONTROL_READY ACK (speed=%d, angle=%d)\n",
                  g_control.speed, g_control.angle);

    // Optional: Immediately do a dedicated ACK exchange if Master is ready
    // Handshake will inform Master that CONTROL_READY is loaded in tx_buf
    assert_handshake(true);
    spi_slave_transaction_t ack_t;
    memset(&ack_t, 0, sizeof(ack_t));
    ack_t.length = ACK_BUFFER_SIZE * 8;
    ack_t.tx_buffer = tx_buf;
    ack_t.rx_buffer = rx_buf;
    spi_slave_transmit(VSPI_HOST, &ack_t, pdMS_TO_TICKS(100));
    assert_handshake(false);
  }
  // ----------------------------------------------------------------------------------------
  // Fallback: Legacy Ping / Test Message
  // ----------------------------------------------------------------------------------------
  else {
    const char* raw_str = (const char*)rx_buf;
    if (strstr(raw_str, "Ping") != NULL) {
      Serial.printf("[Info ] Received Ping: %s\n", raw_str);
      snprintf((char*)tx_buf, ACK_BUFFER_SIZE, "ACK Received data: %s\n", raw_str);
    }
  }
}

// ==========================================================================================
// Send Assembled Image to FastAPI Server via WebSocket
// ==========================================================================================
void send_assembled_image_to_server(uint32_t seq) {
  if (!g_server_connected) {
    Serial.println("[Warn ] Cannot send image: WebSocket disconnected");
    return;
  }

  // Format payload matching programs/server/main.py parse_incoming_packet()
  JsonDocument payload_doc;
  payload_doc["image"] = g_assembled_image;
  payload_doc["sequence"] = seq;
  payload_doc["status"]["wifi_rssi"] = WiFi.RSSI();
  payload_doc["status"]["heap_free"] = ESP.getFreeHeap();

  String payload_str;
  serializeJson(payload_doc, payload_str);

  bool success = webSocket.sendTXT(payload_str);
  if (success) {
    Serial.printf("[Info ] Sent image payload to server (len=%d bytes)\n", payload_str.length());
  } else {
    Serial.println("[Error] Failed to send WebSocket message");
  }
}

// ==========================================================================================
// WebSocket Event Handler
// ==========================================================================================
void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      Serial.println("[WebSocket] Disconnected from server");
      g_server_connected = false;
      // Failsafe stop when server disconnects
      g_control.speed = 0;
      g_control.angle = 90;
      strncpy(g_control.reason, "WebSocket disconnected", sizeof(g_control.reason) - 1);
      strncpy(g_control.status, "failsafe", sizeof(g_control.status) - 1);
      break;

    case WStype_CONNECTED:
      Serial.printf("[WebSocket] Connected to: %s\n", payload);
      g_server_connected = true;
      break;

    case WStype_TEXT: {
      Serial.printf("[WebSocket] Received response (%d bytes)\n", length);
      JsonDocument doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) {
        Serial.printf("[Error] Server response JSON parse failed: %s\n", err.c_str());
        return;
      }

      // Update control parameters from server response
      // Compatible with both flat format and nested header/body format from main.py
      int new_speed = 0;
      int new_angle = 90;

      if (doc["body"].is<JsonObject>()) {
        new_speed = doc["body"]["speed"] | 0;
        new_angle = doc["body"]["angle"] | 90;
        g_control.duration_ms = doc["body"]["duration_ms"] | 500;
        const char* r = doc["body"]["reason"] | "";
        strncpy(g_control.reason, r, sizeof(g_control.reason) - 1);
      } else {
        new_speed = doc["speed"] | 0;
        new_angle = doc["angle"] | 90;
        g_control.duration_ms = doc["duration_ms"] | 500;
        const char* r = doc["reason"] | "";
        strncpy(g_control.reason, r, sizeof(g_control.reason) - 1);
      }

      // Clip parameters within safety limits
      g_control.speed = constrain(new_speed, -255, 255);
      g_control.angle = constrain(new_angle, 0, 180);
      const char* st = doc["status"] | "success";
      strncpy(g_control.status, st, sizeof(g_control.status) - 1);
      g_control.updated = true;

      Serial.printf("[Info ] Updated Control: speed=%d, angle=%d, duration=%dms, reason='%s'\n",
                    g_control.speed, g_control.angle, g_control.duration_ms, g_control.reason);
      break;
    }

    case WStype_BIN:
      Serial.println("[WebSocket] Binary data received (unhandled)");
      break;

    case WStype_ERROR:
      Serial.println("[WebSocket] Communication error occurred");
      break;

    default:
      break;
  }
}
