"""
database.py
------------
يحتوي على تعريف قاعدة البيانات (SQLite) والدوال الأساسية للتعامل معها:
- إنشاء الجدولين: workers و sensor_readings
- دوال CRUD (إضافة/قراءة/تعديل/حذف) للعمال
- دوال لحفظ واسترجاع قراءات السنسورات
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "smart_helmet.db"


def get_connection():
    """يرجع اتصال جديد بقاعدة البيانات (مع تفعيل دعم Foreign Keys)

    - WAL mode: يسمح بالقراءة والكتابة بنفس الوقت بدون تعارض. مهم جدًا هنا لأن
      ESP32 يبعت POST كل ~2 ثانية بينما الداشبورد يعمل 3 طلبات GET كل 2 ثانية -
      بدون WAL قد يظهر خطأ "database is locked" عشوائيًا تحت الحمل.
    - busy_timeout: لو صادف قفل لحظي، ينتظر حتى 3 ثوانٍ بدل ما يفشل فورًا.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=3.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.row_factory = sqlite3.Row  # يسمح بالوصول للنتائج كـ dict
    return conn


def init_db():
    """ينشئ الجدولين إذا لم يكونا موجودين بالفعل"""
    conn = get_connection()
    cur = conn.cursor()

    # جدول العمال (Worker Profile)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            department TEXT,
            role TEXT,

            -- بيانات إسعافية (لا تدخل بالـ AI، تُعرض فقط وقت الطارئ)
            blood_type TEXT,
            emergency_contact_name TEXT,
            emergency_contact_phone TEXT,
            known_allergies TEXT,

            -- معاملات الخطورة (Risk Multiplier Inputs)
            age INTEGER,
            weight_kg REAL,
            height_cm REAL,
            bmi REAL,
            has_respiratory_condition INTEGER DEFAULT 0,
            has_cardiac_condition INTEGER DEFAULT 0,
            years_of_experience INTEGER,

            -- ربط بالهاردوير
            helmet_id TEXT UNIQUE,

            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # جدول قراءات السنسورات
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
            helmet_id TEXT NOT NULL,
            worker_id TEXT,
            timestamp TEXT DEFAULT (datetime('now')),

            -- القراءات الخام من السنسورات
            gas_ppm REAL,            -- قراءة MQ135 الخام (Raw ADC 0-4095)؛ الاسم ثابت للتوافق
            temperature REAL,
            humidity REAL,
            accel_x REAL,
            accel_y REAL,
            accel_z REAL,
            helmet_worn INTEGER,  -- 1 = ملبوسة، 0 = مخلوعة
            buzzer_on INTEGER DEFAULT 0,  -- 1 = البزر مُشغَّل على الخوذة وقت القراءة (لتزامن ليد الداشبورد)

            -- مخرجات الموديلات
            air_quality_status TEXT,   -- Safe / Moderate / Dangerous
            fall_status TEXT,          -- idle / motion / step / fall
            base_risk_score REAL,
            final_risk_score REAL,     -- بعد تطبيق age/health multiplier
            risk_level TEXT,           -- Low / Medium / High / Critical

            FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
        )
    """)

    # جدول سجل الحوادث/التنبيهات (Incident Log)
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

    # جدول الأوامر المعلّقة لكل خوذة (Reset عن بعد)
    # ESP32 يتفقّد هذا الجدول ضمن استجابة كل POST دوري (لا يحتاج web server منفصل على الجهاز)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS device_commands (
            helmet_id TEXT PRIMARY KEY,
            pending_command TEXT DEFAULT 'none',   -- 'none' أو 'reset'
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- فهارس (Indexes) للاستعلامات الساخنة التي يكررها الداشبورد كل ثانيتين ---
    # بدونها، SQLite يعمل Full Table Scan على كل قراءات كل الخوذات مع كل Poll،
    # فيتباطأ الداشبورد تدريجيًا كلما كبرت قاعدة البيانات (آلاف القراءات باليوم).
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_helmet_id
        ON sensor_readings (helmet_id, reading_id DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_time
        ON incident_log (incident_id DESC)
    """)

    conn.commit()

    # --- Migration: التعامل مع قواعد بيانات قديمة كانت تستخدم عمود "gas_index" ---
    # (قبل التحديث لاستخدام معادلة PPM فيزيائية حقيقية بدل مؤشر تقريبي)
    cur.execute("PRAGMA table_info(sensor_readings)")
    existing_columns = [row[1] for row in cur.fetchall()]
    if "gas_index" in existing_columns and "gas_ppm" not in existing_columns:
        cur.execute("ALTER TABLE sensor_readings RENAME COLUMN gas_index TO gas_ppm")
        conn.commit()
        print("Migration: تم تحويل عمود gas_index القديم إلى gas_ppm")

    # --- Migration: إضافة عمود buzzer_on لقواعد البيانات القديمة (لتزامن ليد الداشبورد) ---
    if "buzzer_on" not in existing_columns:
        cur.execute("ALTER TABLE sensor_readings ADD COLUMN buzzer_on INTEGER DEFAULT 0")
        conn.commit()
        print("Migration: تمت إضافة عمود buzzer_on")

    conn.close()
    print(f"Database initialized at {DB_PATH}")


# ---------------------------------------------------------------------------
# دوال خاصة بالعمال (Workers CRUD)
# ---------------------------------------------------------------------------

def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    """يحسب BMI من الوزن (كغم) والطول (سم)"""
    if not weight_kg or not height_cm:
        return None
    height_m = height_cm / 100
    return round(weight_kg / (height_m ** 2), 2)


def add_worker(data: dict):
    # try/finally ضروري هنا: INSERT قد يفشل بـ IntegrityError (helmet_id مكرر مثلًا).
    # بدون finally، الاتصال كان يبقى مفتوحًا بمعاملة معلّقة تمسك قفل كتابة على
    # قاعدة البيانات كاملة - فتفشل *كل* عمليات الكتابة اللاحقة (بما فيها حفظ
    # قراءات الحساسات!) بـ "database is locked" حتى إعادة تشغيل السيرفر.
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
    # نفس مبدأ try/finally في add_worker (راجع التعليق هناك)
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


# ---------------------------------------------------------------------------
# دوال خاصة بقراءات السنسورات
# ---------------------------------------------------------------------------

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
    # ملاحظة: الترتيب بـ reading_id وليس timestamp - لأن دقة datetime('now')
    # ثانية واحدة فقط، فلو وصلت قراءتان بنفس الثانية يصبح ترتيب timestamp
    # عشوائيًا وقد تظهر قراءة أقدم على أنها "الأحدث". reading_id تزايدي دومًا.
    cur.execute("""
        SELECT * FROM sensor_readings
        WHERE helmet_id=?
        ORDER BY reading_id DESC LIMIT 1
    """, (helmet_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_readings_history(helmet_id: str, limit: int = 100):
    """يرجع آخر N قراءة لرسم الرسومات البيانية (الأقدم أولاً للعرض الصحيح بالرسمة)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM sensor_readings
        WHERE helmet_id=?
        ORDER BY reading_id DESC LIMIT ?
    """, (helmet_id, limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return list(reversed(rows))  # نرجعها بترتيب زمني تصاعدي


def delete_readings(helmet_id: str = None):
    """يمسح قراءات السنسورات (زر 'Reset Readings' بالداشبورد).
    إذا تم تمرير helmet_id، يمسح قراءات هذه الخوذة فقط، وإلا يمسح كل القراءات
    (لكل الخوذات) ليبدأ تصدير Excel القادم من نقطة صفر نظيفة."""
    conn = get_connection()
    cur = conn.cursor()
    if helmet_id:
        cur.execute("DELETE FROM sensor_readings WHERE helmet_id=?", (helmet_id,))
    else:
        cur.execute("DELETE FROM sensor_readings")
    conn.commit()
    conn.close()


def get_all_readings(helmet_id: str = None):
    """يرجع كل القراءات المسجّلة (بدون حد أقصى) - تُستخدم لتصدير Excel.
    إذا تم تمرير helmet_id، يُرجع قراءات هذه الخوذة فقط، وإلا يُرجع كل القراءات."""
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


# ---------------------------------------------------------------------------
# دوال سجل الحوادث
# ---------------------------------------------------------------------------

def has_recent_incident(helmet_id: str, incident_type: str, cooldown_seconds: int = 60) -> bool:
    """يتحقق إذا سُجّلت حادثة من نفس النوع لنفس الخوذة خلال آخر N ثانية.

    الهدف: منع "فيضان التنبيهات" (Alert Flooding). بدون هذا الفحص، حالة مستمرة
    مثل "هواء خطير" أو "خوذة مخلوعة" تسجّل حادثة جديدة كل ~2 ثانية (مع كل
    قراءة)، فيمتلئ السجل بمئات النسخ المكررة ويغرق أي تنبيه جديد مهم بينها.
    الحادثة الواحدة المستمرة = سجل واحد كل دقيقة كحد أقصى.
    """
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
    """يرجع كل معرفات الخوذات المعروفة للنظام: المسجّلة بملفات العمال،
    بالإضافة لأي خوذة أرسلت قراءات فعلًا حتى لو لم تُربط بعامل بعد.
    (يسمح للداشبورد بعرض خوذة جديدة فور أول قراءة منها، قبل تسجيل العامل)"""
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
    """يمسح سجل التنبيهات/الحوادث بالكامل (زر 'Clear Alerts' بالداشبورد)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM incident_log")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# دوال الأوامر المعلّقة (Reset عن بعد)
# ---------------------------------------------------------------------------

def set_pending_command(helmet_id: str, command: str):
    """يخزّن أمرًا معلّقًا لخوذة معيّنة (مثلًا 'reset')، ليتم تنفيذه عند أقرب POST دوري من ESP32"""
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
    """يرجع الأمر المعلّق الحالي لهذه الخوذة، ثم يصفّره فورًا إلى 'none'
    (يُستدعى من endpoint استقبال بيانات السنسورات، لإرفاق الأمر بنفس الاستجابة)"""
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
