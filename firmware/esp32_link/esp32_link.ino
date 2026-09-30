// NervLynx Link v1 firmware: ESP32 (Arduino core 2.x or 3.x) + L298N + two wheel encoders.
//
// Status: written against the protocol in robot_core/link.py and its SimulatedLinkBoard,
// NOT yet tested on hardware. Please report what works and what does not.
//
// Needs the ArduinoJson library (version 7, from the Library Manager).
// Protocol and wiring: docs/ESP32_LINK.md.

#include <ArduinoJson.h>

// ---- wiring: change to match yours (avoid GPIO 0, 2, 12, 15, which affect booting) ----
const int ENA = 25, IN1 = 26, IN2 = 27;  // left motor on the L298N (ENA jumper removed)
const int ENB = 14, IN3 = 32, IN4 = 33;  // right motor (ENB jumper removed)
const int ENC_L = 34, ENC_R = 35;        // encoder signal wires; 34/35 have no pull-ups,
                                         // so use encoder boards that drive the line

const unsigned long WATCHDOG_MS = 300;   // stop the motors if drive commands stop
const unsigned long ENC_PERIOD_MS = 20;  // report encoder counts at 50 Hz

volatile long ticksL = 0, ticksR = 0;
volatile int dirL = 1, dirR = 1;
unsigned long lastCmd = 0, lastEnc = 0;
bool armed = false, tripped = false;
String line;

// Single-channel encoders cannot sense direction, so count in the commanded direction.
void IRAM_ATTR onEncL() { ticksL += dirL; }
void IRAM_ATTR onEncR() { ticksR += dirR; }

void setMotor(int en, int a, int b, float speed, volatile int &dir) {
  speed = constrain(speed, -1.0f, 1.0f);
  dir = speed >= 0 ? 1 : -1;
  digitalWrite(a, speed > 0 ? HIGH : LOW);
  digitalWrite(b, speed < 0 ? HIGH : LOW);
  analogWrite(en, (int)(fabsf(speed) * 255.0f));
}

void drive(float left, float right) {
  setMotor(ENA, IN1, IN2, left, dirL);
  setMotor(ENB, IN3, IN4, right, dirR);
}

void handle(const String &text) {
  JsonDocument doc;
  if (deserializeJson(doc, text)) {
    Serial.println("{\"t\":\"err\",\"msg\":\"bad json\"}");
    return;
  }
  const char *type = doc["t"] | "";
  if (strcmp(type, "hello") == 0) {
    Serial.println("{\"t\":\"hello\",\"fw\":\"nervlynx-link\",\"v\":1,\"board\":\"esp32\"}");
  } else if (strcmp(type, "drive") == 0) {
    drive(doc["l"] | 0.0f, doc["r"] | 0.0f);
    lastCmd = millis();
    armed = true;
    tripped = false;
  }
}

void setup() {
  Serial.begin(115200);
  int outputs[] = {ENA, IN1, IN2, ENB, IN3, IN4};
  for (int pin : outputs) pinMode(pin, OUTPUT);
  drive(0, 0);
  pinMode(ENC_L, INPUT);
  pinMode(ENC_R, INPUT);
  attachInterrupt(digitalPinToInterrupt(ENC_L), onEncL, RISING);
  attachInterrupt(digitalPinToInterrupt(ENC_R), onEncR, RISING);
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      handle(line);
      line = "";
    } else if (line.length() < 256) {
      line += c;
    }
  }
  unsigned long now = millis();
  if (armed && !tripped && now - lastCmd > WATCHDOG_MS) {
    drive(0, 0);
    tripped = true;
    Serial.println("{\"t\":\"wd\"}");
  }
  if (now - lastEnc >= ENC_PERIOD_MS) {
    lastEnc = now;
    noInterrupts();
    long left = ticksL, right = ticksR;
    interrupts();
    Serial.printf("{\"t\":\"enc\",\"l\":%ld,\"r\":%ld}\n", left, right);
  }
}
