// nudge band: two vibration motors that steer a blind traveller, fed by the phone companion over ble.
// board: esp32 (arduino-esp32 core 2.x or 3.x), ble: the core's built-in BLEDevice library (no extra deps).
// protocol + haptic grammar live in protocol.h / haptics.h (host-tested in firmware/test).
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>

#include "haptics.h"
#include "protocol.h"

// pins (see firmware/README.md wiring)
const int PIN_MOTOR_L = 25;   // npn base via 1k, motor + flyback diode on the collector
const int PIN_MOTOR_R = 26;
const int PIN_BUTTON = 27;    // to gnd, internal pull-up
const int PIN_PIEZO = 14;     // optional
const bool USE_PIEZO = false;
const int PIN_LED = 2;        // on-board led: lit while connected

// keep in sync with web/companion.html
#define SERVICE_UUID "7a6e0001-1d6e-4c1a-9b5e-6e7564676500"
#define RX_UUID      "7a6e0002-1d6e-4c1a-9b5e-6e7564676500"  // phone -> band, write
#define TX_UUID      "7a6e0003-1d6e-4c1a-9b5e-6e7564676500"  // band -> phone, notify

const uint32_t LONG_PRESS_MS = 800;
const uint32_t DEBOUNCE_MS = 30;

nudge::Engine engine;
BLECharacteristic* tx = nullptr;
volatile bool connected = false;

// small ring of incoming lines; the ble callback runs on another task
const int QN = 8;
char queue_[QN][32];
volatile int q_head = 0, q_tail = 0;
portMUX_TYPE q_mux = portMUX_INITIALIZER_UNLOCKED;

void enqueue(const char* s, size_t n) {
  if (n == 0 || n >= sizeof(queue_[0])) return;
  portENTER_CRITICAL(&q_mux);
  int next = (q_head + 1) % QN;
  if (next != q_tail) {  // drop when full; live lines are refreshed constantly anyway
    memcpy(queue_[q_head], s, n);
    queue_[q_head][n] = 0;
    q_head = next;
  }
  portEXIT_CRITICAL(&q_mux);
}

bool dequeue(char* out) {
  bool ok = false;
  portENTER_CRITICAL(&q_mux);
  if (q_tail != q_head) {
    strcpy(out, queue_[q_tail]);
    q_tail = (q_tail + 1) % QN;
    ok = true;
  }
  portEXIT_CRITICAL(&q_mux);
  return ok;
}

class ServerCb : public BLEServerCallbacks {
  void onConnect(BLEServer*) override { connected = true; }
  void onDisconnect(BLEServer* s) override {
    connected = false;
    s->getAdvertising()->start();  // let the phone reconnect
  }
};

class RxCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    // one write is normally one line, but split on newlines in case the phone batches
    String v = c->getValue().c_str();
    int start = 0;
    for (int i = 0; i <= (int)v.length(); ++i) {
      if (i == (int)v.length() || v[i] == '\n') {
        String part = v.substring(start, i);
        part.trim();
        if (part.length()) enqueue(part.c_str(), part.length());
        start = i + 1;
      }
    }
  }
};

void notify(const char* s) {
  if (!tx || !connected) return;
  tx->setValue((uint8_t*)s, strlen(s));
  tx->notify();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_MOTOR_L, OUTPUT);
  pinMode(PIN_MOTOR_R, OUTPUT);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  digitalWrite(PIN_MOTOR_L, LOW);
  digitalWrite(PIN_MOTOR_R, LOW);

  BLEDevice::init("Nudge Band");
  BLEServer* server = BLEDevice::createServer();
  server->setCallbacks(new ServerCb());
  BLEService* svc = server->createService(SERVICE_UUID);
  BLECharacteristic* rx = svc->createCharacteristic(
      RX_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rx->setCallbacks(new RxCb());
  tx = svc->createCharacteristic(TX_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  tx->addDescriptor(new BLE2902());
  svc->start();
  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
  engine.play("arrive", millis());  // power-on hello
  Serial.println("nudge band advertising");
}

// button: short press = repeat, long press = lost. debounced, non-blocking.
void poll_button(uint32_t now) {
  static bool stable = HIGH, last_raw = HIGH, long_sent = false;
  static uint32_t changed_at = 0, pressed_at = 0;
  bool raw = digitalRead(PIN_BUTTON);
  if (raw != last_raw) { last_raw = raw; changed_at = now; }
  if (now - changed_at < DEBOUNCE_MS || raw == stable) {
    if (stable == LOW && !long_sent && now - pressed_at >= LONG_PRESS_MS) {
      notify("B,lost");
      engine.play("off_route", now);  // feel that it registered
      long_sent = true;
    }
    return;
  }
  stable = raw;
  if (stable == LOW) { pressed_at = now; long_sent = false; }
  else if (!long_sent) { notify("B,repeat"); engine.play("straight", now); }
}

void handle(const char* line, uint32_t now) {
  nudge::Msg m;
  if (!nudge::parse(line, m)) { Serial.printf("bad line: %s\n", line); return; }
  switch (m.kind) {
    case nudge::LIVE: engine.on_live(m.live, now); break;
    case nudge::CUE: engine.play(m.cue, now); break;
    case nudge::FLOOR:
      if (USE_PIEZO) tone(PIN_PIEZO, m.floor.up ? 880 : 440, 150);
      break;
    case nudge::STEP:  // route steps are informational on the band; the live line drives haptics
    default: break;
  }
}

void loop() {
  uint32_t now = millis();
  char line[32];
  while (dequeue(line)) handle(line, now);
  poll_button(now);
  uint8_t motors = engine.tick(now);
  digitalWrite(PIN_MOTOR_L, (motors & nudge::M_L) ? HIGH : LOW);
  digitalWrite(PIN_MOTOR_R, (motors & nudge::M_R) ? HIGH : LOW);
  digitalWrite(PIN_LED, connected ? HIGH : LOW);
}
