import sqlite3
from datetime import datetime
from pathlib import Path
DB_PATH = Path(__file__).parent / "smart_helmet.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=3.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.row_factory = sqlite3.Row  
    return conn

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            department TEXT,
            role TEXT,
            blood_type TEXT,
            emergency_contact_name TEXT,
            emergency_contact_phone TEXT,
            known_allergies TEXT,
            age INTEGER,
            weight_kg REAL,
            height_cm REAL,
            bmi REAL,
            has_respiratory_condition INTEGER DEFAULT 0,
            has_cardiac_condition INTEGER DEFAULT 0,
            years_of_experience INTEGER,
            helmet_id TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
            helmet_id TEXT NOT NULL,
            worker_id TEXT,
            timestamp TEXT DEFAULT (datetime('now')),
            gas_ppm REAL,
            temperature REAL,
            humidity REAL,
            accel_x REAL,
            accel_y REAL,
            accel_z REAL,
            helmet_worn INTEGER,
            buzzer_on INTEGER DEFAULT 0,
            air_quality_status TEXT,   -- Safe / Moderate / Dangerous
            fall_status TEXT,          -- idle / motion / step / fall
            base_risk_score REAL,
            final_risk_score REAL,
            risk_level TEXT,           -- Low / Medium / High / Critical
            FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS incident_log (
            incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
            helmet_id TEXT NOT NULL,
            worker_id TEXT,
            timestamp TEXT DEFAULT (datetime('now')),
            incident_type TEXT,    -- e.g. "Fall Detected", "Dangerous Air Quality", "Helmet Removed"
            severity TEXT,         -- Medium / High / Critical
            details TEXT,
            FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS device_commands (
            helmet_id TEXT PRIMARY KEY,
            pending_command TEXT DEFAULT 'none',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_helmet_id
        ON sensor_readings (helmet_id, reading_id DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_time
        ON incident_log (incident_id DESC)
    """)
    conn.commit()
    cur.execute("PRAGMA table_info(sensor_readings)")
    existing_columns = [row[1] for row in cur.fetchall()]
    if "gas_index" in existing_columns and "gas_ppm" not in existing_columns:
        cur.execute("ALTER TABLE sensor_readings RENAME COLUMN gas_index TO gas_ppm")
        conn.commit()
        print("Migration: renamed legacy gas_index column to gas_ppm")
    if "buzzer_on" not in existing_columns:
        cur.execute("ALTER TABLE sensor_readings ADD COLUMN buzzer_on INTEGER DEFAULT 0")
        conn.commit()
        print("Migration: added buzzer_on column")
    conn.close()
    print(f"Database initialized at {DB_PATH}")

def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    if not weight_kg or not height_cm:
        return None
    height_m = height_cm / 100
    return round(weight_kg / (height_m ** 2), 2)

def add_worker(data: dict):
    conn = get_connection()
    try:
        cur = conn.cursor()
        bmi = calculate_bmi(data.get("weight_kg"), data.get("height_cm"))
        cur.execute("""
        INSERT INTO workers (
            worker_id, full_name, department, role,
            blood_type, emergency_contact_name, emergency_contact_phone, known_allergies,
            age, weight_kg, height_cm, bmi,
            has_respiratory_condition, has_cardiac_condition, years_of_experience,
            helmet_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["worker_id"], data["full_name"], data.get("department"), data.get("role"),
            data.get("blood_type"), data.get("emergency_contact_name"),
            data.get("emergency_contact_phone"), data.get("known_allergies"),
            data.get("age"), data.get("weight_kg"), data.get("height_cm"), bmi,
            int(data.get("has_respiratory_condition", False)),
            int(data.get("has_cardiac_condition", False)),
            data.get("years_of_experience"), data.get("helmet_id")
        ))
        conn.commit()
    finally:
        conn.close()

def update_worker(worker_id: str, data: dict):
    conn = get_connection()
    try:
        cur = conn.cursor()
        bmi = calculate_bmi(data.get("weight_kg"), data.get("height_cm"))
        cur.execute("""
        UPDATE workers SET
            full_name=?, department=?, role=?,
            blood_type=?, emergency_contact_name=?, emergency_contact_phone=?, known_allergies=?,
            age=?, weight_kg=?, height_cm=?, bmi=?,
            has_respiratory_condition=?, has_cardiac_condition=?, years_of_experience=?,
            helmet_id=?
        WHERE worker_id=?
        """, (
            data["full_name"], data.get("department"), data.get("role"),
            data.get("blood_type"), data.get("emergency_contact_name"),
            data.get("emergency_contact_phone"), data.get("known_allergies"),
            data.get("age"), data.get("weight_kg"), data.get("height_cm"), bmi,
            int(data.get("has_respiratory_condition", False)),
            int(data.get("has_cardiac_condition", False)),
            data.get("years_of_experience"), data.get("helmet_id"),
            worker_id
        ))
        conn.commit()
    finally:
        conn.close()

def delete_worker(worker_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM workers WHERE worker_id=?", (worker_id,))
    conn.commit()
    conn.close()

def get_all_workers():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM workers ORDER BY created_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_worker_by_helmet(helmet_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM workers WHERE helmet_id=?", (helmet_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_worker_by_id(worker_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def insert_reading(reading: dict):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sensor_readings (
            helmet_id, worker_id, gas_ppm,
            temperature, humidity, accel_x, accel_y, accel_z, helmet_worn,
            buzzer_on,
            air_quality_status, fall_status, base_risk_score, final_risk_score, risk_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        reading["helmet_id"], reading.get("worker_id"), reading["gas_ppm"],
        reading["temperature"], reading["humidity"],
        reading["accel_x"], reading["accel_y"], reading["accel_z"],
        int(reading["helmet_worn"]),
        int(reading.get("buzzer_on", 0)),
        reading["air_quality_status"], reading["fall_status"],
        reading["base_risk_score"], reading["final_risk_score"], reading["risk_level"]
    ))
    conn.commit()
    reading_id = cur.lastrowid
    conn.close()
    return reading_id

def get_latest_reading(helmet_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM sensor_readings
        WHERE helmet_id=?
        ORDER BY reading_id DESC LIMIT 1
    """, (helmet_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_readings_history(helmet_id: str, limit: int = 100):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM sensor_readings
        WHERE helmet_id=?
        ORDER BY reading_id DESC LIMIT ?
    """, (helmet_id, limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return list(reversed(rows))  

def delete_readings(helmet_id: str = None):
    conn = get_connection()
    cur = conn.cursor()
    if helmet_id:
        cur.execute("DELETE FROM sensor_readings WHERE helmet_id=?", (helmet_id,))
    else:
        cur.execute("DELETE FROM sensor_readings")
    conn.commit()
    conn.close()

def get_all_readings(helmet_id: str = None):
    conn = get_connection()
    cur = conn.cursor()
    if helmet_id:
        cur.execute("""
            SELECT * FROM sensor_readings WHERE helmet_id=? ORDER BY timestamp ASC
        """, (helmet_id,))
    else:
        cur.execute("SELECT * FROM sensor_readings ORDER BY timestamp ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def has_recent_incident(helmet_id: str, incident_type: str, cooldown_seconds: int = 60) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM incident_log
        WHERE helmet_id = ? AND incident_type = ?
          AND timestamp >= datetime('now', ?)
        LIMIT 1
    """, (helmet_id, incident_type, f"-{int(cooldown_seconds)} seconds"))
    row = cur.fetchone()
    conn.close()
    return row is not None

def get_all_helmet_ids():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT helmet_id FROM workers WHERE helmet_id IS NOT NULL
        UNION
        SELECT DISTINCT helmet_id FROM sensor_readings
        ORDER BY helmet_id
    """)
    rows = [r["helmet_id"] for r in cur.fetchall()]
    conn.close()
    return rows

def log_incident(helmet_id: str, worker_id: str, incident_type: str, severity: str, details: str = ""):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO incident_log (helmet_id, worker_id, incident_type, severity, details)
        VALUES (?, ?, ?, ?, ?)
    """, (helmet_id, worker_id, incident_type, severity, details))
    conn.commit()
    conn.close()

def get_recent_incidents(limit: int = 20):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM incident_log ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def delete_all_incidents():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM incident_log")
    conn.commit()
    conn.close()

def set_pending_command(helmet_id: str, command: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO device_commands (helmet_id, pending_command, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(helmet_id) DO UPDATE SET
            pending_command = excluded.pending_command,
            updated_at = excluded.updated_at
    """, (helmet_id, command))
    conn.commit()
    conn.close()

def get_and_clear_pending_command(helmet_id: str) -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT pending_command FROM device_commands WHERE helmet_id=?", (helmet_id,))
    row = cur.fetchone()
    command = row["pending_command"] if row else "none"
    if command != "none":
        cur.execute("""
            INSERT INTO device_commands (helmet_id, pending_command, updated_at)
            VALUES (?, 'none', datetime('now'))
            ON CONFLICT(helmet_id) DO UPDATE SET
                pending_command = 'none',
                updated_at = datetime('now')
        """, (helmet_id,))
        conn.commit()
    conn.close()
    return command
if __name__ == "__main__":
    init_db()