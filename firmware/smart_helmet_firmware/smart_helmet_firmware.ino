
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_ADXL345_U.h>
#include <Adafruit_BME280.h>
#include <ArduinoJson.h>

const char* WIFI_SSID     = "YOUR_WIFI_NAME";       // <-- set your Wi-Fi SSID
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";   // <-- set your Wi-Fi password

const char* SERVER_URL = "http://YOUR_SERVER_IP:8000/api/sensor-data";    // <-- set your backend IP
const char* EMERGENCY_URL = "http://YOUR_SERVER_IP:8000/api/emergency";   // <-- set your backend IP

const char* HELMET_ID = "helmet-001";

#define MQ135_PIN     4    // Analog input
#define IR_PIN        5    // Digital input
#define EMERGENCY_PIN 6
#define BUZZER_PIN    7

#define LED_RED_PIN     15
#define LED_YELLOW_PIN  16
#define LED_GREEN_PIN   17

#define ADXL_SDA    11
#define ADXL_SCL    12

#define BME_SDA     8
#define BME_SCL     9

const int WINDOW_SIZE = 160;
const unsigned long ACCEL_SAMPLE_INTERVAL_MS = 13;   // ~76-80Hz (1000ms / 80 ≈ 12.5ms)

TwoWire I2C_BME = TwoWire(1);

Adafruit_ADXL345_Unified accel = Adafruit_ADXL345_Unified(12345);
Adafruit_BME280 bme;

float windowX[WINDOW_SIZE];
float windowY[WINDOW_SIZE];
float windowZ[WINDOW_SIZE];
int sampleCount = 0;
unsigned long lastSampleTime = 0;

const unsigned long T3_PULSE_MS  = 500;
const unsigned long T3_GAP_MS    = 500;
const unsigned long T3_PAUSE_MS  = 1500;

bool alarmActive = false;

const unsigned long ALARM_DURATION_MS = 10000;
unsigned long alarmUntil = 0;

bool sensorAlarmActive = false;

int  t3PulseIndex = 0;
bool t3PulseOn = false;
unsigned long t3LastChangeTime = 0;

bool emergencyButtonLatched = false;
unsigned long lastEmergencySentTime = 0;
const unsigned long EMERGENCY_RESEND_INTERVAL_MS = 5000;

const unsigned long BUTTON_DEBOUNCE_MS = 50;
unsigned long buttonLowSince = 0;

unsigned long lastWiFiAttemptTime = 0;
const unsigned long WIFI_RETRY_INTERVAL_MS = 8000;

float lastValidTemperature = 25.0;
float lastValidHumidity = 50.0;

char serverLedColor = 'y';
bool ledBlinkState = false;
unsigned long lastLedBlinkTime = 0;
const unsigned long LED_BLINK_INTERVAL_MS = 500;

// ---------------------------------------------------------------------------
// SETUP
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== Smart Helmet ESP32-S3 ===");

  pinMode(IR_PIN, INPUT);
  pinMode(MQ135_PIN, INPUT);
  pinMode(EMERGENCY_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  pinMode(LED_RED_PIN, OUTPUT);
  pinMode(LED_YELLOW_PIN, OUTPUT);
  pinMode(LED_GREEN_PIN, OUTPUT);
  digitalWrite(LED_RED_PIN, LOW);
  digitalWrite(LED_YELLOW_PIN, LOW);
  digitalWrite(LED_GREEN_PIN, LOW);

  Wire.begin(ADXL_SDA, ADXL_SCL);

  I2C_BME.begin(BME_SDA, BME_SCL);

  if (!accel.begin()) {
    Serial.println("!! ERROR: ADXL345 not found - check wiring (default Wire: GPIO11/12)");
    haltWithErrorBeep();
  }
  accel.setRange(ADXL345_RANGE_4_G);
  Serial.println("ADXL345 ready (separate I2C bus, GPIO11/12).");

  if (!bme.begin(0x76, &I2C_BME)) {
    Serial.println("!! ERROR: BME280 not found - check wiring (separate I2C bus: GPIO8/9) or address");
    haltWithErrorBeep();
  }
  Serial.println("BME280 ready (separate I2C bus, GPIO8/9).");

  connectToWiFi();
}

// ---------------------------------------------------------------------------
// LOOP
// ---------------------------------------------------------------------------
void loop() {
  maintainWiFi();

  checkEmergencyButton();
  updateAlarmState();
  updateBuzzer();
  updateStatusLeds();

  unsigned long now = millis();
  if (now - lastSampleTime >= ACCEL_SAMPLE_INTERVAL_MS) {
    lastSampleTime = now;

    sensors_event_t event;
    accel.getEvent(&event);
    windowX[sampleCount] = event.acceleration.x;
    windowY[sampleCount] = event.acceleration.y;
    windowZ[sampleCount] = event.acceleration.z;
    sampleCount++;

    if (sampleCount >= WINDOW_SIZE) {
      computeFeaturesAndSend();
      sampleCount = 0;
    }
  }
}

void checkEmergencyButton() {
  bool rawLow = (digitalRead(EMERGENCY_PIN) == LOW);
  unsigned long now = millis();

  if (rawLow) {
    if (buttonLowSince == 0) buttonLowSince = now;
  } else {
    buttonLowSince = 0;
  }

  bool pressed = rawLow && (now - buttonLowSince >= BUTTON_DEBOUNCE_MS);

  if (pressed) {
    alarmUntil = now + ALARM_DURATION_MS;
    if (!emergencyButtonLatched || (now - lastEmergencySentTime >= EMERGENCY_RESEND_INTERVAL_MS)) {
      emergencyButtonLatched = true;
      sendEmergencyAlert();
      lastEmergencySentTime = millis();
    }
  } else if (!rawLow) {
    emergencyButtonLatched = false;
  }
}

void updateAlarmState() {
  bool timedAlarm = (millis() < alarmUntil);
  alarmActive = timedAlarm || sensorAlarmActive;
}

void updateBuzzer() {
  if (!alarmActive) {
    digitalWrite(BUZZER_PIN, LOW);
    t3PulseIndex = 0;
    t3PulseOn = false;
    return;
  }

  unsigned long now = millis();
  unsigned long elapsed = now - t3LastChangeTime;

  if (t3PulseOn) {
    if (elapsed >= T3_PULSE_MS) {
      digitalWrite(BUZZER_PIN, LOW);
      t3PulseOn = false;
      t3LastChangeTime = now;
      t3PulseIndex++;
    }
  } else {
    unsigned long requiredGap = (t3PulseIndex >= 3) ? T3_PAUSE_MS : T3_GAP_MS;
    if (elapsed >= requiredGap) {
      if (t3PulseIndex >= 3) t3PulseIndex = 0;
      digitalWrite(BUZZER_PIN, HIGH);
      t3PulseOn = true;
      t3LastChangeTime = now;
    }
  }
}

void updateStatusLeds() {
  char color;
  bool blinking = false;

  if (alarmActive) {
    color = 'r';
  } else if (WiFi.status() != WL_CONNECTED) {
    color = 'y';
    blinking = true;
  } else {
    color = serverLedColor;
  }

  bool ledOn = true;
  if (blinking) {
    unsigned long now = millis();
    if (now - lastLedBlinkTime >= LED_BLINK_INTERVAL_MS) {
      lastLedBlinkTime = now;
      ledBlinkState = !ledBlinkState;
    }
    ledOn = ledBlinkState;
  }

  digitalWrite(LED_RED_PIN,    (color == 'r' && ledOn) ? HIGH : LOW);
  digitalWrite(LED_YELLOW_PIN, (color == 'y' && ledOn) ? HIGH : LOW);
  digitalWrite(LED_GREEN_PIN,  (color == 'g' && ledOn) ? HIGH : LOW);
}

void computeFeaturesAndSend() {
  float x_mean, x_std, x_max, x_min;
  float y_mean, y_std, y_max, y_min;
  float z_mean, z_std, z_max, z_min;
  float sma;

  computeAxisStats(windowX, x_mean, x_std, x_max, x_min);
  computeAxisStats(windowY, y_mean, y_std, y_max, y_min);
  computeAxisStats(windowZ, z_mean, z_std, z_max, z_min);

  float smaSum = 0.0;
  for (int i = 0; i < WINDOW_SIZE; i++) {
    smaSum += fabs(windowX[i]) + fabs(windowY[i]) + fabs(windowZ[i]);
  }
  sma = smaSum / WINDOW_SIZE;

  float gas_raw = readGasRaw();
  float temperature = bme.readTemperature();
  float humidity = bme.readHumidity();
  bool helmet_worn = (digitalRead(IR_PIN) == LOW);

  if (isnan(temperature)) {
    temperature = lastValidTemperature;
    Serial.println("[WARN] BME280 returned NaN for temperature - using last valid value");
  } else {
    lastValidTemperature = temperature;
  }
  if (isnan(humidity)) {
    humidity = lastValidHumidity;
    Serial.println("[WARN] BME280 returned NaN for humidity - using last valid value");
  } else {
    lastValidHumidity = humidity;
  }

  StaticJsonDocument<768> doc;
  doc["helmet_id"]   = HELMET_ID;
  doc["gas_ppm"]     = gas_raw;
  doc["temperature"] = temperature;
  doc["humidity"]    = humidity;
  doc["helmet_worn"] = helmet_worn;
  doc["buzzer_on"]   = alarmActive;

  doc["x_mean"] = x_mean; doc["x_std"] = x_std; doc["x_max"] = x_max; doc["x_min"] = x_min;
  doc["y_mean"] = y_mean; doc["y_std"] = y_std; doc["y_max"] = y_max; doc["y_min"] = y_min;
  doc["z_mean"] = z_mean; doc["z_std"] = z_std; doc["z_max"] = z_max; doc["z_min"] = z_min;
  doc["sma"]    = sma;

  String jsonString;
  serializeJson(doc, jsonString);

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(2000);

    int httpCode = http.POST(jsonString);

    if (httpCode > 0) {
      String responseBody = http.getString();
      Serial.printf("[OK] HTTP %d | Gas(raw)=%.0f | T=%.1f H=%.1f | x_std=%.3f y_std=%.3f z_std=%.3f sma=%.3f | Helmet=%s\n",
                    httpCode, gas_raw, temperature, humidity, x_std, y_std, z_std, sma,
                    helmet_worn ? "WORN" : "REMOVED");
      handleServerResponse(responseBody);
    } else {
      Serial.printf("[ERROR] Send failed: %s\n", http.errorToString(httpCode).c_str());
    }
    http.end();
  } else {
    Serial.println("[SKIP] No WiFi connection, this batch was skipped");
  }
}

void handleServerResponse(const String& responseBody) {
  StaticJsonDocument<512> resDoc;
  DeserializationError err = deserializeJson(resDoc, responseBody);
  if (err) {
    Serial.printf("[WARN] Could not parse server response: %s\n", err.c_str());
    return;
  }

  const char* fallStatus = resDoc["fall_status"] | "";
  const char* airStatus  = resDoc["air_quality_status"] | "";
  sensorAlarmActive = (strcmp(fallStatus, "fall") == 0) || (strcmp(airStatus, "Dangerous") == 0);

  const char* ledColor = resDoc["led_color"] | "";
  if      (strcmp(ledColor, "red") == 0)    serverLedColor = 'r';
  else if (strcmp(ledColor, "yellow") == 0) serverLedColor = 'y';
  else if (strcmp(ledColor, "green") == 0)  serverLedColor = 'g';

  const char* command = resDoc["command"] | "none";
  if (strcmp(command, "buzz") == 0) {
    alarmUntil = millis() + ALARM_DURATION_MS;
    Serial.println("[CMD] Emergency (buzz) command from dashboard - starting timed alarm");
  }
  else if (strcmp(command, "reset") == 0) {
    Serial.println("[CMD] Reset command received from dashboard - restarting in 2 seconds...");
    digitalWrite(BUZZER_PIN, LOW);
    delay(2000);
    ESP.restart();
  }
}

void sendEmergencyAlert() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[EMERGENCY] No WiFi connection - buzzer triggered locally only");
    return;
  }

  StaticJsonDocument<128> doc;
  doc["helmet_id"] = HELMET_ID;
  String jsonString;
  serializeJson(doc, jsonString);

  HTTPClient http;
  http.begin(EMERGENCY_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(2000);
  int httpCode = http.POST(jsonString);

  if (httpCode > 0) {
    Serial.printf("[EMERGENCY] Emergency alert sent successfully (HTTP %d)\n", httpCode);
  } else {
    Serial.printf("[EMERGENCY] Failed to send alert: %s\n", http.errorToString(httpCode).c_str());
  }
  http.end();
}

void computeAxisStats(float window[], float &mean, float &stdDev, float &maxVal, float &minVal) {
  float sum = 0.0;
  maxVal = window[0];
  minVal = window[0];

  for (int i = 0; i < WINDOW_SIZE; i++) {
    sum += window[i];
    if (window[i] > maxVal) maxVal = window[i];
    if (window[i] < minVal) minVal = window[i];
  }
  mean = sum / WINDOW_SIZE;

  float sqDiffSum = 0.0;
  for (int i = 0; i < WINDOW_SIZE; i++) {
    float diff = window[i] - mean;
    sqDiffSum += diff * diff;
  }
  stdDev = sqrt(sqDiffSum / WINDOW_SIZE);
}

void connectToWiFi() {
  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nConnected!");
    Serial.print("Device IP address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n!! WiFi connection failed, will retry in the main loop");
  }
}

void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  unsigned long now = millis();
  if (now - lastWiFiAttemptTime >= WIFI_RETRY_INTERVAL_MS) {
    lastWiFiAttemptTime = now;
    Serial.println("[WiFi] Connection lost - attempting background reconnect (non-blocking)...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}

void haltWithErrorBeep() {
  while (true) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(100);
    digitalWrite(BUZZER_PIN, LOW);
    delay(900);
  }
}

//
// ============================================================================
//
// ============================================================================
const int GAS_MODERATE_THRESHOLD  = 500;
const int GAS_DANGEROUS_THRESHOLD = 1000;

const int GAS_SAMPLES = 10;

float readGasRaw() {
  long sum = 0;
  for (int i = 0; i < GAS_SAMPLES; i++) {
    sum += analogRead(MQ135_PIN);
  }
  return (float)(sum / GAS_SAMPLES);
}

