"""
server.py
----------
السيرفر الرئيسي (FastAPI). يوفر:

1. POST /api/sensor-data        -> ESP32 يبعت قراءة جديدة هون
2. GET  /api/latest/{helmet_id} -> آخر قراءة لخوذة معينة (للداشبورد اللحظي)
3. GET  /api/history/{helmet_id}-> آخر N قراءة (للرسومات البيانية)
4. GET  /api/incidents          -> سجل آخر التنبيهات/الحوادث
5. CRUD كامل للعمال: /api/workers

لتشغيله:
    pip install fastapi uvicorn scikit-learn joblib numpy --break-system-packages
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

بعد التشغيل، الداشبورد بيكون متاح عبر:
    http://<IP-الجهاز>:8000   (صفحة index.html)
"""

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

# --- Logging بسيط ومنظم (بدل الاعتماد على print/صمت كامل) ---
# يظهر بطرفية uvicorn نفسها. مفيد جدًا وقت العرض/التجربة لتشخيص أي مشكلة فورًا.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smart_helmet")

app = FastAPI(title="Smart Helmet API")

# يسمح للـ Frontend (حتى لو شغال من مصدر/منفذ مختلف) بالاتصال بالـ API بدون مشاكل CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# تهيئة قاعدة البيانات أول ما يشتغل السيرفر
db.init_db()


# ---------------------------------------------------------------------------
# نماذج البيانات (Pydantic) - تحدد شكل JSON المتوقع من ESP32 ومن الفرونت إند
# ---------------------------------------------------------------------------
class SensorPayload(BaseModel):
    # حدود منطقية واسعة (Sanity Bounds) لكل قيمة - الهدف ليس تضييق القيم
    # الطبيعية، بل رفض القراءات الفاسدة تمامًا (بت مقلوب أثناء الإرسال، حساس
    # معطّل يرجع قيمًا مستحيلة فيزيائيًا...) قبل أن تُخزَّن وتشوّه الرسوم
    # البيانية أو تطلق إنذارًا كاذبًا.
    helmet_id: str = Field(min_length=1, max_length=64)
    # gas_ppm يحمل الآن القراءة الخام لـ MQ135 (ADC 12-bit، النطاق 0-4095).
    # الاسم ثابت للتوافق مع باقي الطبقات؛ الحد الأعلى 4095 يرفض أي قيمة خارج
    # نطاق الـ ADC (قراءة فاسدة أثناء الإرسال).
    gas_ppm: float = Field(ge=0, le=4095)
    temperature: float = Field(ge=-40, le=125)     # نطاق تشغيل BME280 الفعلي من الداتاشيت
    humidity: float = Field(ge=0, le=100)
    helmet_worn: bool
    # حالة البزر الحالية على الخوذة (اختياري - يصل الآن من الفيرموير). يُستخدم
    # لجعل ليد الداشبورد أحمر متزامنًا مع الليد الفيزيائي عند أي إنذار (طوارئ،
    # سقوط، هواء خطير). القيمة الافتراضية False للتوافق مع أي عميل قديم.
    buzzer_on: bool = False
    # --- 13 feature جاهزة، محسوبة محليًا على ESP32 من نافذة ~2 ثانية (160 عينة @ ~80Hz) ---
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


# ---------------------------------------------------------------------------
# حساب لون ليد الحالة على الخوذة (أخضر/أصفر/أحمر)
# المنطق مركزي هنا بالسيرفر (Single Source of Truth) - الفيرموير ينفّذ فقط،
# والداشبورد تعرض نفس اللون. الأحمر يطابق شروط البزر تمامًا (اتساق الإنذارات).
# ---------------------------------------------------------------------------
def compute_led_color(air_status: str, fall_status: str, helmet_worn: bool,
                       buzzer_on: bool = False) -> str:
    if fall_status == "fall" or air_status == "Dangerous" or buzzer_on:
        return "red"       # خطر فعلي: سقوط، هواء خطير، أو البزر مُشغَّل على الخوذة (طوارئ)
    if air_status == "Moderate" or not helmet_worn:
        return "yellow"    # تحذير: هواء متوسط التلوث أو الخوذة مخلوعة
    return "green"         # كل شيء سليم


# ---------------------------------------------------------------------------
# 1) استقبال قراءة جديدة من ESP32 - هذا أهم endpoint بالمشروع
# ---------------------------------------------------------------------------
@app.post("/api/sensor-data")
def receive_sensor_data(payload: SensorPayload):
    helmet_id = payload.helmet_id

    # --- تجميع الميزات الـ13 الجاهزة (محسوبة محليًا على ESP32) بصيغة dict ---
    accel_features = {
        "x_mean": payload.x_mean, "x_std": payload.x_std, "x_max": payload.x_max, "x_min": payload.x_min,
        "y_mean": payload.y_mean, "y_std": payload.y_std, "y_max": payload.y_max, "y_min": payload.y_min,
        "z_mean": payload.z_mean, "z_std": payload.z_std, "z_max": payload.z_max, "z_min": payload.z_min,
        "sma": payload.sma,
    }

    # --- جلب القراءة السابقة مباشرة (لكشف الارتفاع المفاجئ في الغاز) ---
    prev_reading = db.get_latest_reading(helmet_id)
    prev_gas = prev_reading["gas_ppm"] if prev_reading else None

    # --- التنبؤ بجودة الهواء (Rule-based على القراءة الخام + كشف الارتفاع المفاجئ) ---
    air_status = risk.predict_air_quality(
        payload.gas_ppm, payload.temperature, payload.humidity, prev_gas
    )

    # --- التنبؤ بالسقوط، مع فلترة بالثقة (Confidence-based) - راجع risk_engine.py ---
    # بدل الاعتماد على تصنيف نافذة واحدة (argmax) مباشرة، منتحقق من احتمالية
    # fall نفسها عبر predict_proba(): لو الثقة أوطأ من العتبة المحددة، منتجاهل
    # التصنيف كنتيجة غير موثوقة بدل ما نطلق الإنذار مباشرة.
    fall_status = risk.predict_fall_status_confident(accel_features)

    # --- جلب بيانات العامل المرتبط بهذه الخوذة (لتطبيق معامل العمر/الصحة) ---
    worker = db.get_worker_by_helmet(helmet_id)
    worker_id = worker["worker_id"] if worker else None

    # --- حساب الخطورة النهائية ---
    risk_result = risk.compute_final_risk(air_status, fall_status, worker)

    # --- حساب لون الليد مرة واحدة: يُخزَّن مع القراءة ويُرجَع بالاستجابة معًا ---
    # هكذا يحصل الداشبورد على نفس اللون تمامًا عبر /api/latest (يقرأ من القاعدة)،
    # فيتطابق ليد الداشبورد مع الليد الفيزيائي على الخوذة. buzzer_on يأتي من
    # الفيرموير ويعكس حالة البزر الفعلية لحظة القراءة.
    led_color = compute_led_color(air_status, fall_status, payload.helmet_worn, payload.buzzer_on)

    # --- حفظ القراءة كاملة بقاعدة البيانات ---
    reading = {
        "helmet_id": helmet_id,
        "worker_id": worker_id,
        "gas_ppm": payload.gas_ppm,
        "temperature": payload.temperature,
        "humidity": payload.humidity,
        "accel_x": payload.x_mean,   # نخزّن x_mean كقيمة تمثيلية للنافذة (بدل قراءة لحظية واحدة)
        "accel_y": payload.y_mean,
        "accel_z": payload.z_mean,
        "helmet_worn": payload.helmet_worn,
        "buzzer_on": payload.buzzer_on,
        "air_quality_status": air_status,
        "fall_status": fall_status,
        **risk_result,
    }
    db.insert_reading(reading)

    # --- تسجيل حادثة لو الوضع خطير (Incident Log + يستخدمه الفرونت إند للتنبيهات) ---
    # مع Cooldown لمنع فيضان التنبيهات: الحالة المستمرة (هواء خطير لمدة 10 دقائق
    # مثلًا) تسجّل حادثة واحدة بالدقيقة بدل حادثة كل ~2 ثانية مع كل قراءة.
    # السقوط cooldown أقصر (15 ثانية) لأن كل سقوط حدث منفصل حرج بذاته.
    if fall_status == "fall":
        if not db.has_recent_incident(helmet_id, "Fall Detected", 15):
            db.log_incident(helmet_id, worker_id, "Fall Detected", "Critical",
                             "تم اكتشاف سقوط من خلال مستشعر التسارع")
            log.warning("FALL detected on %s (worker=%s)", helmet_id, worker_id)
    elif air_status == "Dangerous":
        if not db.has_recent_incident(helmet_id, "Dangerous Air Quality", 60):
            db.log_incident(helmet_id, worker_id, "Dangerous Air Quality", "High",
                             f"gas_raw={payload.gas_ppm:.0f}")
            log.warning("Dangerous air on %s (gas_raw=%.0f)", helmet_id, payload.gas_ppm)
    elif not payload.helmet_worn:
        if not db.has_recent_incident(helmet_id, "Helmet Removed", 60):
            db.log_incident(helmet_id, worker_id, "Helmet Removed", "Medium", "")

    # --- جلب أمر معلّق لهذه الخوذة إن وُجد (buzz / reset)، ليُرفَق بالاستجابة ---
    pending_command = db.get_and_clear_pending_command(helmet_id)

    # ليد الداشبورد يتطابق مع الليد الفيزيائي: الخوذة تُبلّغ حالة بزرها الفعلية
    # (buzzer_on) مع كل قراءة، فأي إنذار نشط عليها (طوارئ/سقوط/هواء) = ليد أحمر
    # هنا أيضًا. هذا يحل عدم تطابق الليد بين الخوذة والداشبورد نهائيًا.
    return {
        "status": "ok", **risk_result,
        "air_quality_status": air_status, "fall_status": fall_status,
        "led_color": led_color,
        "command": pending_command,
    }


# ---------------------------------------------------------------------------
# 1.5) Health check - يفرّق للفرونت إند بين "السيرفر واقف" و"لا توجد بيانات بعد"
#      (قبل هذا الـ endpoint، كان 404 من /api/latest يظهر خطأً كـ "Disconnected"
#      حتى لو السيرفر شغال تمامًا والخوذة فقط لم ترسل أول قراءة بعد)
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health_check():
    return {"status": "ok", "server_time_utc": datetime.utcnow().isoformat() + "Z"}


# ---------------------------------------------------------------------------
# 1.6) قائمة كل الخوذات المعروفة (مسجّلة بملف عامل، أو أرسلت قراءات فعلًا)
#      تسمح للداشبورد بعرض خوذة جديدة فور أول قراءة، حتى قبل تسجيل عاملها
# ---------------------------------------------------------------------------
@app.get("/api/helmets")
def list_helmets():
    return db.get_all_helmet_ids()


# ---------------------------------------------------------------------------
# 2) آخر قراءة لحظية (يستدعيها الفرونت إند كل ثانية أو ثانيتين)
# ---------------------------------------------------------------------------
@app.get("/api/latest/{helmet_id}")
def get_latest(helmet_id: str):
    reading = db.get_latest_reading(helmet_id)
    if not reading:
        raise HTTPException(status_code=404, detail="لا توجد قراءات لهذه الخوذة بعد")
    return reading


# ---------------------------------------------------------------------------
# 3) تاريخ القراءات (للرسومات البيانية Time-series)
#    limit مقيّد بين 1 و500 - يحمي السيرفر من طلب عرضي/خبيث بـ limit=10000000
# ---------------------------------------------------------------------------
@app.get("/api/history/{helmet_id}")
def get_history(helmet_id: str, limit: int = Query(default=50, ge=1, le=500)):
    return db.get_readings_history(helmet_id, limit)


# ---------------------------------------------------------------------------
# 4) سجل الحوادث/التنبيهات الأخيرة
# ---------------------------------------------------------------------------
@app.get("/api/incidents")
def get_incidents(limit: int = 20):
    return db.get_recent_incidents(limit)


# ---------------------------------------------------------------------------
# 4.0) مسح كل سجل التنبيهات (زر "Clear Alerts" بالداشبورد)
#      يُستخدم لبدء جلسة تنبيهات جديدة من الصفر (مثلًا قبل بدء عرض تجريبي)
# ---------------------------------------------------------------------------
@app.delete("/api/incidents")
def clear_incidents():
    db.delete_all_incidents()
    return {"status": "incidents_cleared"}


# ---------------------------------------------------------------------------
# 4.1) استقبال تنبيه طوارئ فوري من زر الطوارئ الفيزيائي على الخوذة
#      مستقل تمامًا عن دورة جمع/إرسال بيانات التسارع (~2 ثانية) - يصل فورًا
# ---------------------------------------------------------------------------
class EmergencyPayload(BaseModel):
    helmet_id: str


@app.post("/api/emergency")
def receive_emergency_alert(payload: EmergencyPayload):
    worker = db.get_worker_by_helmet(payload.helmet_id)
    worker_id = worker["worker_id"] if worker else None
    db.log_incident(payload.helmet_id, worker_id, "Emergency Button Pressed", "Critical",
                     "تم الضغط على زر الطوارئ يدويًا من قبل العامل")
    # --- إرسال أمر buzz للخوذة: يُنفَّذه ESP32 عند أقرب POST دوري فيشغّل البزر ---
    # (قبل هذا، ضغط زر الطوارئ من الداشبورد كان يسجّل حادثة فقط دون تشغيل البزر
    #  على الخوذة فعليًا - الآن يُشغّله عبر نفس آلية الأوامر المستخدمة مع reset)
    db.set_pending_command(payload.helmet_id, "buzz")
    log.warning("EMERGENCY button pressed on %s (worker=%s) - buzz queued", payload.helmet_id, worker_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 4.2) إرسال أمر Reset عن بعد لخوذة معيّنة (يضغطه المستخدم من الداشبورد)
#      الأمر يُخزَّن مؤقتًا، ويُنفَّذه ESP32 فعليًا عند أقرب POST دوري له
#      (راجع device_commands بـ database.py وhandleServerResponse بالفيرموير)
# ---------------------------------------------------------------------------
@app.post("/api/reset/{helmet_id}")
def reset_helmet(helmet_id: str):
    db.set_pending_command(helmet_id, "reset")
    return {"status": "reset_queued", "helmet_id": helmet_id}


# ---------------------------------------------------------------------------
# 4.3) مسح القراءات المخزّنة (زر "Reset Readings" بالداشبورد)
#      يُستخدم لبدء جلسة قراءات نظيفة من الصفر (مثلًا قبل بدء عرض تجريبي)،
#      بحيث تصدير Excel القادم يحتوي فقط القراءات الجديدة بعد الضغط على الزر.
#      بدون helmet_id يمسح قراءات كل الخوذات (مطابق لسلوك تصدير Excel الافتراضي).
# ---------------------------------------------------------------------------
@app.delete("/api/readings")
def reset_readings(helmet_id: str | None = None):
    db.delete_readings(helmet_id)
    return {"status": "readings_cleared", "helmet_id": helmet_id or "all"}


# ---------------------------------------------------------------------------
# 4.5) تصدير كل القراءات لملف Excel (.xlsx) - للتحليل أو الأرشفة
#      إذا تم تمرير helmet_id، يُصدّر قراءات هذه الخوذة فقط، وإلا كل القراءات
# ---------------------------------------------------------------------------
@app.get("/api/export/excel")
def export_readings_excel(helmet_id: str | None = None, tz_offset_minutes: int = 0):
    rows = db.get_all_readings(helmet_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No readings available to export")

    df = pd.DataFrame(rows)

    # --- تصحيح الوقت: نفس مبدأ utcToLocal() بالفرونت إند ---
    # SQLite يخزّن timestamp بصيغة UTC خام دومًا. الفرونت إند يرسل فرق التوقيت
    # المحلي للمتصفح (tz_offset_minutes) مع كل طلب export، فنضيفه هون قبل
    # الكتابة بملف الإكسل، بدل ما تطلع القراءات بتوقيت UTC الخام.
    df["timestamp"] = pd.to_datetime(df["timestamp"]) + pd.Timedelta(minutes=tz_offset_minutes)

    # ترتيب وتسمية الأعمدة بشكل واضح ومقروء بملف الإكسل النهائي
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

        # --- تنسيق بسيط: تعريض الأعمدة تلقائيًا حسب أطول محتوى (متعامل بأمان مع القيم الفاضية) ---
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


# ---------------------------------------------------------------------------
# 5) CRUD العمال
# ---------------------------------------------------------------------------
@app.get("/api/workers")
def list_workers():
    return db.get_all_workers()


@app.get("/api/workers/{worker_id}")
def get_worker(worker_id: str):
    worker = db.get_worker_by_id(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="العامل غير موجود")
    return worker


@app.post("/api/workers")
def create_worker(payload: WorkerPayload):
    existing = db.get_worker_by_id(payload.worker_id)
    if existing:
        raise HTTPException(status_code=400, detail="Worker ID already exists")
    try:
        db.add_worker(payload.model_dump())
    except sqlite3.IntegrityError:
        # helmet_id عليه قيد UNIQUE بقاعدة البيانات - بدون هذا الالتقاط، ربط
        # نفس الخوذة بعاملين كان يرجع 500 Internal Server Error غامض للمستخدم
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


# ---------------------------------------------------------------------------
# تقديم ملفات الـ Frontend (HTML/CSS/JS) كملفات ثابتة
# يجعل الداشبورد متاحة مباشرة عبر: http://<IP>:8000/
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
