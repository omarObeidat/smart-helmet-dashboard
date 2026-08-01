# Smart Helmet — Engineering Audit & Improvement Report (v7)

Audit date: 2026-07-02
Scope: Frontend, Backend, ESP32-S3 Firmware, System Architecture.
**AI models untouched** — `models/*.pkl` are byte-identical to the originals (verified by MD5), and `risk_engine.py` (the inference contract with the trained fall model, including feature names and ordering) was not modified.

All changes were verified by running the full backend against the real fall-detection model: sensor ingestion, validation rejection, remote-reset command delivery, incident cooldown, worker CRUD error paths, Excel export, and static frontend serving all pass. Inline JavaScript in both pages passes `node --check`.

---

## CRITICAL FIXES

### 1. Firmware — Safety functions froze during Wi-Fi reconnection
- **Change:** Replaced the blocking `connectToWiFi()` call inside `loop()` with a new non-blocking `maintainWiFi()` state machine (retry every 8 s, returns immediately).
- **Reason:** The old code blocked for up to 15 seconds on every reconnect attempt. During that window the **emergency button, the buzzer state machine, and 80 Hz accelerometer sampling all froze**. In a safety device, the local protection functions must never depend on network availability — a worker pressing the emergency button during a Wi-Fi outage got no buzzer response.
- **Benefits:** Emergency button, T3 alarm, and fall-feature sampling now run uninterrupted regardless of network state. Reconnection happens silently in the background.
- **Files Modified:** `firmware/smart_helmet_firmware/smart_helmet_firmware.ino`
- **Breaking Change?** No. Boot-time behavior unchanged (`connectToWiFi()` still used once in `setup()`).

### 2. Backend — Database connection leak locked the entire system
- **Change:** Wrapped `add_worker()` / `update_worker()` in `try/finally` so connections always close.
- **Reason:** Found during testing: if an `INSERT` raised `IntegrityError` (e.g., assigning an already-used helmet), the connection was abandoned with an open transaction holding a write lock — **every subsequent write, including live sensor readings, then failed with "database is locked"** until the server was restarted. One bad form submission could silently kill data collection for all helmets.
- **Benefits:** A rejected worker submission is now harmless; ingestion continues normally.
- **Files Modified:** `backend/database.py`
- **Breaking Change?** No.

### 3. Frontend — Stale data displayed as "Connected" (false sense of safety)
- **Change:** Connection status now distinguishes three states: **Server Offline** (fetch fails), **Awaiting First Reading** (HTTP 404, amber dot), and **Device Offline — last seen Xs ago** (last reading older than 12 s). When live, the header shows the last update time.
- **Reason:** Previously, a helmet that stopped transmitting (battery dead, out of range, crashed) kept showing "Connected" with frozen readings — the most dangerous possible failure mode for a safety monitor: a supervisor believes a worker is safe when the device is actually dead.
- **Benefits:** Device liveness is now explicit and honest; "no data yet" is no longer misreported as a server failure.
- **Files Modified:** `frontend/index.html`
- **Breaking Change?** No.

### 4. Frontend — Stored XSS vulnerabilities
- **Change:** Added `escapeHtml()` and applied it to every database-sourced string rendered via `innerHTML` (worker names, helmet IDs, incident types, blood types, etc.). Replaced the `onclick='editWorker(${JSON.stringify(w)})'` pattern with a cached array + `data-index` event delegation.
- **Reason:** A worker name entered as `<img src=x onerror=...>` executed as script in the browser of every supervisor viewing the dashboard. The JSON-in-onclick pattern was both an injection vector and fragile (broke on any name containing quotes).
- **Benefits:** User-entered data is rendered as text, never executed. Edit/Delete buttons work for any name.
- **Files Modified:** `frontend/index.html`, `frontend/workers.html`
- **Breaking Change?** No.

---

## HIGH-IMPACT RELIABILITY FIXES

### 5. Backend — SQLite WAL mode + busy timeout
- **Change:** `PRAGMA journal_mode=WAL` and `busy_timeout=3000` on every connection.
- **Reason:** The ESP32 POSTs every ~2 s while the dashboard fires 3 GETs every 2 s. In default journal mode, concurrent read/write produces intermittent "database is locked" errors under load (multiple helmets make it worse).
- **Benefits:** Readers and the writer no longer block each other; transient contention waits instead of failing.
- **Files Modified:** `backend/database.py`
- **Breaking Change?** No (WAL creates `-wal`/`-shm` sidecar files next to the DB — normal).

### 6. Backend — Latest-reading ordering bug
- **Change:** `ORDER BY timestamp DESC` → `ORDER BY reading_id DESC` in `get_latest_reading()` and `get_readings_history()`.
- **Reason:** `datetime('now')` has 1-second resolution. Two readings landing in the same second have identical timestamps, so "latest" was arbitrary — the dashboard could display an older reading (e.g., show "Safe" after a "Dangerous" reading arrived in the same second). `reading_id` is strictly monotonic.
- **Benefits:** The live view is guaranteed to show the newest reading; chart ordering is deterministic.
- **Files Modified:** `backend/database.py`
- **Breaking Change?** No.

### 7. Backend — Incident log flooding (alert fatigue)
- **Change:** Added `has_recent_incident()` cooldown: a sustained condition logs one incident per 60 s (falls: 15 s) instead of one every ~2 s.
- **Reason:** Ten minutes of dangerous air previously produced ~300 identical incident rows, burying any genuinely new alert (a fall on another helmet) below hundreds of duplicates. Alert flooding is a classic industrial-alarm-system anti-pattern (see ISA-18.2 alarm management).
- **Benefits:** The alert feed stays readable and meaningful; each entry represents a distinct event or a sustained condition sampled at a sane rate. Verified: two consecutive dangerous readings → exactly one incident.
- **Files Modified:** `backend/database.py`, `backend/server.py`
- **Breaking Change?** No — detection and buzzer behavior unchanged; only duplicate log rows are suppressed.

### 8. Firmware — BME280 NaN dropped fall-detection data
- **Change:** `isnan()` guards on temperature/humidity with last-valid-value fallback.
- **Reason:** A transient I2C glitch makes `bme.readTemperature()` return NaN. ArduinoJson serializes NaN as `null`, Pydantic then rejects the **entire payload with 422 — including the 13 fall-detection features**. A flaky environmental sensor was silently disabling fall detection for that window.
- **Benefits:** A degraded environmental sensor can no longer take down the safety-critical fall pipeline; the substitution is logged on Serial.
- **Files Modified:** `firmware/smart_helmet_firmware/smart_helmet_firmware.ino`
- **Breaking Change?** No.

### 9. Firmware — Emergency button debounce
- **Change:** The button must read LOW continuously for 50 ms before registering as pressed.
- **Reason:** A raw level read can be triggered by electrical noise or mechanical vibration — on this input, one noise spike logged a *Critical* emergency incident and sounded the site alarm.
- **Benefits:** No false Critical alarms from contact bounce/noise; genuine presses feel identical (50 ms is imperceptible).
- **Files Modified:** `firmware/smart_helmet_firmware/smart_helmet_firmware.ino`
- **Breaking Change?** No.

### 10. Firmware — Silent halt on sensor init failure
- **Change:** `while(1) delay(10)` replaced with `haltWithErrorBeep()` — a distinctive short chirp every second.
- **Reason:** The device is sealed inside a helmet with no visible Serial output. A loose I2C wire previously produced a helmet that *looked* powered but protected no one, with no way to notice in the field.
- **Benefits:** Hardware faults are audibly self-announcing at boot; the chirp pattern is clearly distinct from the T3 danger alarm.
- **Files Modified:** `firmware/smart_helmet_firmware/smart_helmet_firmware.ino`
- **Breaking Change?** No (device still refuses to run without its sensors — correct for a safety device).

---

## ROBUSTNESS & UX IMPROVEMENTS

### 11. Backend — Input validation with physical sanity bounds
- **Change:** Pydantic `Field` constraints on the sensor payload (gas ≥ 0, temperature within the BME280's −40…+125 °C datasheet range, humidity 0–100 %, helmet_id 1–64 chars) and `limit` clamped to 1–500 on `/api/history`.
- **Reason:** Corrupted transmissions or a failing sensor could store physically impossible values, distorting charts and Excel exports, or a stray `limit=10000000` could stall the server.
- **Benefits:** Garbage data is rejected at the boundary with a clear 422; the API is robust against accidental/malicious oversized queries. Verified: humidity = 300 → 422.
- **Files Modified:** `backend/server.py`
- **Breaking Change?** No — all values a healthy device produces pass unchanged.

### 12. Backend — Proper CRUD error semantics
- **Change:** Duplicate helmet assignment now returns **400 with a human-readable message** (was an unhandled 500); `PUT`/`DELETE` on a missing worker return **404** (previously reported success).
- **Reason:** REST correctness and debuggability; the frontend can now tell the user *why* a save failed.
- **Files Modified:** `backend/server.py`
- **Breaking Change?** No.

### 13. Frontend — Worker form surfaces server errors
- **Change:** The workers page now checks `response.ok` on save/delete and shows the server's `detail` message.
- **Reason:** Previously a duplicate Worker ID or already-assigned helmet *appeared* to save successfully (form reset, no error) while nothing was written — silent data loss from the user's perspective.
- **Benefits:** The user immediately sees "Helmet 'helmet-001' is already assigned to another worker" instead of being misled.
- **Files Modified:** `frontend/workers.html`
- **Breaking Change?** No.

### 14. New `/api/helmets` endpoint + auto-discovering helmet selector
- **Change:** New endpoint returning the union of helmets registered to workers **and** helmets that have actually sent readings. The dashboard selector uses it, preserves the current selection on refresh, and re-polls every 10 s.
- **Reason:** Previously a brand-new helmet streaming data was invisible on the dashboard until a worker profile was manually created and linked — backwards for a demo and for real commissioning workflows.
- **Benefits:** Flash a new ESP32 → it appears in the dashboard within seconds, no page reload, no manual registration required first.
- **Files Modified:** `backend/database.py`, `backend/server.py`, `frontend/index.html`
- **Breaking Change?** No.

### 15. Backend — Health endpoint + structured logging
- **Change:** New `GET /api/health`; Python `logging` with timestamps for worker CRUD, emergencies, falls, and dangerous-air events.
- **Reason:** Gives the frontend (and any future monitoring) a cheap liveness probe, and gives you a readable operational log in the uvicorn terminal during demos instead of silence.
- **Files Modified:** `backend/server.py`
- **Breaking Change?** No.

### 16. Database — Indexes on hot polling queries
- **Change:** Index on `sensor_readings(helmet_id, reading_id DESC)` and on `incident_log(incident_id DESC)`.
- **Reason:** The dashboard's 2-second poll did full table scans; after a day of data (~40k rows/helmet) latency grows linearly and the UI degrades.
- **Benefits:** Poll queries stay O(log n); dashboard remains snappy after weeks of accumulated data.
- **Files Modified:** `backend/database.py`
- **Breaking Change?** No (`CREATE INDEX IF NOT EXISTS` applies automatically to existing databases).

---

## VERIFIED-INTACT FUNCTIONALITY (regression test summary)

- Sensor ingestion with real `fall_detection_rf_model.pkl` inference → `idle` prediction correct
- Risk multiplier + cap: age 22 worker × Dangerous air → base 90, final capped at 100 ✓
- Remote reset: queue → delivered exactly once in next response → auto-cleared ✓
- Emergency endpoint, incident log, clear-alerts, reset-readings ✓
- Excel export: 200, correct columns, timezone offset applied ✓
- Static frontend served at `/` ✓

## RECOMMENDED NEXT STEPS (not implemented — would change scope/behavior)

1. **Wi-Fi credentials in a separate `secrets.h`** (git-ignored) so the SSID/password never appear in the main source you submit or publish. Highest-value 5-minute change before uploading the repo anywhere public.
2. **API authentication** (a simple shared token header checked by FastAPI) before this ever leaves a trusted LAN — currently anyone on the network can delete all readings or spoof sensor data. Acceptable for a supervised demo, not for deployment; worth *mentioning* in your report/defense as a known, scoped-out item.
3. **WebSocket or SSE push** instead of 2-second polling — lower latency for fall alerts and less load, at the cost of firmware/back-end complexity. The current polling design is a defensible engineering choice for the ESP32 (you already justify it in the comments); frame it as such in the viva.
4. **MQ135 R0 field calibration** — you already flagged this in the firmware comments; resolving the unstable ADC wiring and running `mq135_calibration.ino` is the single biggest accuracy win remaining.
5. **On-device buffering during outages** — currently readings during a Wi-Fi outage are dropped. A small ring buffer of computed feature windows, flushed on reconnect, would make the data record gap-free.
6. **Browser notifications / audible dashboard alert** on new Critical incidents, so a supervisor not looking at the screen is still alerted.
