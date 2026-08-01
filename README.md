# Smart Helmet Safety Monitoring System

A real-time IoT safety platform that monitors construction and industrial workers through a sensor-equipped smart helmet. The system detects falls using a machine-learning model, classifies air quality, tracks environmental conditions, and streams everything to a live web dashboard for supervisors.

## Overview

The system is built from four cooperating layers:

- **Firmware** — an ESP32-S3 microcontroller reads the sensors, extracts features on-device, and drives local alarms (buzzer + status LEDs).
- **Backend** — a Python FastAPI server that validates readings, runs the fall-detection model, computes risk scores, and stores data.
- **Machine Learning** — a Random Forest fall-detection classifier trained on statistical features from accelerometer windows.
- **Frontend** — a real-time web dashboard for live monitoring and worker management.

The core design principle is a **Single Source of Truth**: all safety-critical decisions (risk scoring, air-quality classification, LED color) are computed on the backend, so the physical helmet and the dashboard can never disagree.

## Hardware

| Component | Purpose | Connection |
|---|---|---|
| ESP32-S3 | Main microcontroller | — |
| ADXL345 | 3-axis accelerometer (fall detection) | I2C |
| BME280 | Temperature & humidity | I2C (separate bus) |
| MQ135 | Gas sensor (air quality) | GPIO4 (analog, via voltage divider) |
| IR sensor | Helmet-worn detection | GPIO5 |
| Push button | Emergency alarm | GPIO6 |
| Active buzzer | Audible alarm | GPIO7 |
| Status LEDs (R/Y/G) | On-helmet status indicator | GPIO15 / 16 / 17 |

> The MQ135 outputs 0–5 V; a hardware voltage divider (approx. 2/3 ratio) protects the 3.3 V ESP32 pin.

## Technology Stack

- **Firmware:** C++ (Arduino framework)
- **Backend:** Python, FastAPI, Uvicorn, SQLite
- **Machine Learning:** scikit-learn (Random Forest), joblib, NumPy
- **Frontend:** HTML, CSS, vanilla JavaScript, Chart.js

## Project Structure

```
smart_helmet_dashboard/
├── firmware/          ESP32-S3 Arduino firmware (.ino)
├── backend/           FastAPI server, risk engine, database layer
│   ├── server.py
│   ├── risk_engine.py
│   ├── database.py
│   └── requirements.txt
├── frontend/          Dashboard and worker management pages
│   ├── index.html
│   └── workers.html
└── models/            Trained ML model files (.pkl)
```

## Getting Started

### 1. Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

The `--host 0.0.0.0` flag is required so the ESP32 (on the same network) can reach the server. The dashboard is then served at `http://<server-ip>:8000/`.

### 2. Firmware

1. Open the `.ino` file in the Arduino IDE.
2. Set your Wi-Fi credentials and the server IP address at the top of the file.
3. Install the required libraries: Adafruit Unified Sensor, Adafruit ADXL345, Adafruit BME280, ArduinoJson.
4. Select the ESP32-S3 board and upload.

### 3. Dashboard

Once the backend is running, open `http://<server-ip>:8000/` in any browser on the same network.

## Key Features

- **ML-based fall detection** from on-device statistical features (13 features per ~2-second window).
- **Air-quality classification** (Safe / Moderate / Dangerous) with sudden-spike detection.
- **Personalized risk scoring** that combines sensor data with each worker's age, experience, and health profile.
- **Temporal-3 evacuation alarm** — the internationally standardized fire/evacuation buzzer pattern.
- **Synchronized status LEDs** on both the physical helmet and the dashboard.
- **Three-state connection monitoring** that detects a silently-dead device instead of showing frozen data.
- **Remote emergency trigger** and **Excel data export** from the dashboard.

## License

This project was developed as a final-year engineering graduation project.
