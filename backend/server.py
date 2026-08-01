from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pathlib import Path
from datetime import datetime
import io
import logging
import sqlite3
import pandas as pd
import database as db
import risk_engine as risk
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smart_helmet")
app = FastAPI(title="Smart Helmet API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
db.init_db()

class SensorPayload(BaseModel):
    helmet_id: str = Field(min_length=1, max_length=64)
    gas_ppm: float = Field(ge=0, le=4095)
    temperature: float = Field(ge=-40, le=125)     
    humidity: float = Field(ge=0, le=100)
    helmet_worn: bool
    buzzer_on: bool = False
    x_mean: float
    x_std: float
    x_max: float
    x_min: float
    y_mean: float
    y_std: float
    y_max: float
    y_min: float
    z_mean: float
    z_std: float
    z_max: float
    z_min: float
    sma: float

class WorkerPayload(BaseModel):
    worker_id: str
    full_name: str
    department: str | None = None
    role: str | None = None
    blood_type: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    known_allergies: str | None = None
    age: int | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    has_respiratory_condition: bool = False
    has_cardiac_condition: bool = False
    years_of_experience: int | None = None
    helmet_id: str | None = None

def compute_led_color(air_status: str, fall_status: str, helmet_worn: bool,
                       buzzer_on: bool = False) -> str:
    if fall_status == "fall" or air_status == "Dangerous" or buzzer_on:
        return "red"       
    if air_status == "Moderate" or not helmet_worn:
        return "yellow"    
    return "green"         

@app.post("/api/sensor-data")

def receive_sensor_data(payload: SensorPayload):
    helmet_id = payload.helmet_id
    accel_features = {
        "x_mean": payload.x_mean, "x_std": payload.x_std, "x_max": payload.x_max, "x_min": payload.x_min,
        "y_mean": payload.y_mean, "y_std": payload.y_std, "y_max": payload.y_max, "y_min": payload.y_min,
        "z_mean": payload.z_mean, "z_std": payload.z_std, "z_max": payload.z_max, "z_min": payload.z_min,
        "sma": payload.sma,
    }
    prev_reading = db.get_latest_reading(helmet_id)
    prev_gas = prev_reading["gas_ppm"] if prev_reading else None
    air_status = risk.predict_air_quality(
        payload.gas_ppm, payload.temperature, payload.humidity, prev_gas
    )
    fall_status = risk.predict_fall_status(accel_features)
    worker = db.get_worker_by_helmet(helmet_id)
    worker_id = worker["worker_id"] if worker else None
    risk_result = risk.compute_final_risk(air_status, fall_status, worker)
    led_color = compute_led_color(air_status, fall_status, payload.helmet_worn, payload.buzzer_on)
    reading = {
        "helmet_id": helmet_id,
        "worker_id": worker_id,
        "gas_ppm": payload.gas_ppm,
        "temperature": payload.temperature,
        "humidity": payload.humidity,
        "accel_x": payload.x_mean,   
        "accel_y": payload.y_mean,
        "accel_z": payload.z_mean,
        "helmet_worn": payload.helmet_worn,
        "buzzer_on": payload.buzzer_on,
        "air_quality_status": air_status,
        "fall_status": fall_status,
        **risk_result,
    }
    db.insert_reading(reading)
    if fall_status == "fall":
        if not db.has_recent_incident(helmet_id, "Fall Detected", 15):
            db.log_incident(helmet_id, worker_id, "Fall Detected", "Critical",
                             "Fall detected via the accelerometer sensor")
            log.warning("FALL detected on %s (worker=%s)", helmet_id, worker_id)
    elif air_status == "Dangerous":
        if not db.has_recent_incident(helmet_id, "Dangerous Air Quality", 60):
            db.log_incident(helmet_id, worker_id, "Dangerous Air Quality", "High",
                             f"gas_raw={payload.gas_ppm:.0f}")
            log.warning("Dangerous air on %s (gas_raw=%.0f)", helmet_id, payload.gas_ppm)
    elif not payload.helmet_worn:
        if not db.has_recent_incident(helmet_id, "Helmet Removed", 60):
            db.log_incident(helmet_id, worker_id, "Helmet Removed", "Medium", "")
    pending_command = db.get_and_clear_pending_command(helmet_id)
    return {
        "status": "ok", **risk_result,
        "air_quality_status": air_status, "fall_status": fall_status,
        "led_color": led_color,
        "command": pending_command,
    }

@app.get("/api/health")

def health_check():
    return {"status": "ok", "server_time_utc": datetime.utcnow().isoformat() + "Z"}

@app.get("/api/helmets")

def list_helmets():
    return db.get_all_helmet_ids()

@app.get("/api/latest/{helmet_id}")

def get_latest(helmet_id: str):
    reading = db.get_latest_reading(helmet_id)
    if not reading:
        raise HTTPException(status_code=404, detail="No readings for this helmet yet")
    return reading

@app.get("/api/history/{helmet_id}")

def get_history(helmet_id: str, limit: int = Query(default=50, ge=1, le=500)):
    return db.get_readings_history(helmet_id, limit)

@app.get("/api/incidents")

def get_incidents(limit: int = 20):
    return db.get_recent_incidents(limit)

@app.delete("/api/incidents")

def clear_incidents():
    db.delete_all_incidents()
    return {"status": "incidents_cleared"}

class EmergencyPayload(BaseModel):
    helmet_id: str

@app.post("/api/emergency")

def receive_emergency_alert(payload: EmergencyPayload):
    worker = db.get_worker_by_helmet(payload.helmet_id)
    worker_id = worker["worker_id"] if worker else None
    db.log_incident(payload.helmet_id, worker_id, "Emergency Button Pressed", "Critical",
                     "Emergency button pressed manually by the worker")
    db.set_pending_command(payload.helmet_id, "buzz")
    log.warning("EMERGENCY button pressed on %s (worker=%s) - buzz queued", payload.helmet_id, worker_id)
    return {"status": "ok"}

@app.post("/api/reset/{helmet_id}")

def reset_helmet(helmet_id: str):
    db.set_pending_command(helmet_id, "reset")
    return {"status": "reset_queued", "helmet_id": helmet_id}

@app.delete("/api/readings")

def reset_readings(helmet_id: str | None = None):
    db.delete_readings(helmet_id)
    return {"status": "readings_cleared", "helmet_id": helmet_id or "all"}

@app.get("/api/export/excel")

def export_readings_excel(helmet_id: str | None = None, tz_offset_minutes: int = 0):
    rows = db.get_all_readings(helmet_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No readings available to export")
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"]) + pd.Timedelta(minutes=tz_offset_minutes)
    column_order = [
        "reading_id", "timestamp", "helmet_id", "worker_id",
        "gas_ppm", "temperature", "humidity",
        "accel_x", "accel_y", "accel_z", "helmet_worn",
        "air_quality_status", "fall_status",
        "base_risk_score", "final_risk_score", "risk_level",
    ]
    df = df[[c for c in column_order if c in df.columns]]
    df["helmet_worn"] = df["helmet_worn"].map({1: "Worn", 0: "Removed"})
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sensor Readings")
        worksheet = writer.sheets["Sensor Readings"]
        for i, col in enumerate(df.columns, start=1):
            content_lengths = df[col].fillna("").astype(str).map(len)
            max_len = max(content_lengths.max(), len(col)) + 2
            worksheet.column_dimensions[worksheet.cell(row=1, column=i).column_letter].width = max_len
    buffer.seek(0)
    filename = f"smart_helmet_readings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/api/workers")

def list_workers():
    return db.get_all_workers()

@app.get("/api/workers/{worker_id}")

def get_worker(worker_id: str):
    worker = db.get_worker_by_id(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker

@app.post("/api/workers")

def create_worker(payload: WorkerPayload):
    existing = db.get_worker_by_id(payload.worker_id)
    if existing:
        raise HTTPException(status_code=400, detail="Worker ID already exists")
    try:
        db.add_worker(payload.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400,
                            detail=f"Helmet '{payload.helmet_id}' is already assigned to another worker")
    log.info("Worker created: %s (%s)", payload.worker_id, payload.full_name)
    return {"status": "created"}

@app.put("/api/workers/{worker_id}")

def edit_worker(worker_id: str, payload: WorkerPayload):
    if not db.get_worker_by_id(worker_id):
        raise HTTPException(status_code=404, detail="Worker not found")
    try:
        db.update_worker(worker_id, payload.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400,
                            detail=f"Helmet '{payload.helmet_id}' is already assigned to another worker")
    return {"status": "updated"}

@app.delete("/api/workers/{worker_id}")

def remove_worker(worker_id: str):
    if not db.get_worker_by_id(worker_id):
        raise HTTPException(status_code=404, detail="Worker not found")
    db.delete_worker(worker_id)
    log.info("Worker deleted: %s", worker_id)
    return {"status": "deleted"}
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")