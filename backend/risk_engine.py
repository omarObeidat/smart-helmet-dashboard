"""
risk_engine.py
---------------
يحتوي على المنطق الكامل لحساب الخطورة النهائية لكل قراءة:

1. predict_air_quality -> يصنف خطورة الهواء (Safe / Moderate / Dangerous)
   بناءً على القراءة الخام لحساس MQ135 (Rule-based) - راجع الملاحظة أدناه
2. fall_model         -> يصنف نوع الحركة (idle / motion / step / fall)
3. age_factor / health_factor -> معاملات معرفية (Rule-based) بناءً على
   ملف العامل (مش AI، قواعد واضحة مفسّرة بالكامل)

المعادلة النهائية:
    final_risk_score = base_risk_score(air_status, fall_status) * age_factor * health_factor

المرجع العلمي لمعامل العمر:
    بيانات BLS/NSC الرسمية (2023-2024) تُظهر أن العمال 20-24 سنة لديهم أعلى معدل
    حوادث (DAFW=111.1) مقارنة بكل الفئات العمرية الأخرى، وأن الأفراد تحت 25 سنة
    أكثر عرضة للإصابات المهنية بشكل عام. لذلك تم استخدام نموذج منحنى-U بدل
    افتراض خطي بسيط (كبار السن = أخطر فقط).

============================================================================
ملاحظة منهجية مهمة (تصنيف جودة الهواء):
============================================================================
تصنيف جودة الهواء يعتمد على القراءة الخام لحساس MQ135 (Raw ADC، النطاق
0-4095) مباشرة، بمنطق Rule-based واضح (عتبتان تفصلان Safe/Moderate/Dangerous).

سبب استخدام القراءة الخام بدل PPM محسوب:
معامل المعايرة R0 الخاص بالحساس لم تنجح معايرته فعليًا بعد (كانت قراءات ADC
غير مستقرة أثناء محاولة المعايرة)، لذلك أي رقم PPM مطلق مشتق من معادلة
datasheet يكون غير موثوق ويعطي إحساسًا زائفًا بالدقة. القراءة الخام أصدق
منهجيًا: تعكس تركيز الغاز نسبيًا بشكل مباشر (هواء نظيف = قراءة منخفضة، وكلما
زاد الغاز ارتفعت القراءة)، وهي كل ما يلزم لتصنيف موثوق لثلاث حالات.

(الفيرموير يرسل هذه القراءة الخام تحت مفتاح gas_ppm للحفاظ على توافق باقي
طبقات النظام - اسم الحقل ثابت، لكن قيمته الآن خام وليست PPM.)
"""

import joblib
import numpy as np
from pathlib import Path

MODELS_DIR = Path(__file__).parent.parent / "models"

# تحميل موديل Fall Detection فقط (موديل Air Quality استُبدل بمنطق Rule-based - راجع الملاحظة أعلاه)
fall_detection_model = joblib.load(MODELS_DIR / "fall_detection_rf_model.pkl")

FALL_FEATURES = ["x_mean", "x_std", "x_max", "x_min",
                  "y_mean", "y_std", "y_max", "y_min",
                  "z_mean", "z_std", "z_max", "z_min", "sma"]

# تحويل تصنيف الخطورة النصي لرقم أساسي (Base risk score من 0 إلى 100)
AIR_RISK_SCORE = {"Safe": 10, "Moderate": 50, "Dangerous": 90}
FALL_RISK_SCORE = {"idle": 5, "step": 10, "motion": 15, "fall": 100}

# --- عتبات تصنيف جودة الهواء على القراءة الخام (Raw ADC 0-4095) ---
# ⚠️ يجب أن تطابق نفس العتبات المعرّفة بالفيرموير (GAS_MODERATE/DANGEROUS_THRESHOLD).
# اضبطها على خط الأساس الفعلي لحساسك (راجع تعليمات المعايرة في الفيرموير).
GAS_MODERATE_THRESHOLD = 500      # فوق هذا الرقم الخام: Moderate
GAS_DANGEROUS_THRESHOLD = 1000    # فوق هذا الرقم الخام: Dangerous

# --- كشف الارتفاع المفاجئ (Spike Detection) ---
# منطق إضافي: بغض النظر عن القيمة المطلقة، إذا قفزت القراءة بشكل مفاجئ وكبير
# مقارنة بالقراءة السابقة مباشرة، فهذا مؤشر خطر (مثلًا: تعرّض فجائي لمصدر غاز
# كثيف - قدّاحة، تسرّب، دخان مفاجئ). القفزة السريعة أخطر من قيمة عالية ثابتة
# قد تكون مجرد بيئة صناعية معتادة. العتبة = مقدار الزيادة الخام بين قراءتين.
GAS_SPIKE_DELTA = 300     # قفزة بمقدار 300+ نقطة خام بين قراءتين متتاليتين = خطر


# ---------------------------------------------------------------------------
# 1) تصنيف جودة الهواء (Rule-based، مبني على القراءة الخام لـ MQ135)
# ---------------------------------------------------------------------------
def predict_air_quality(gas_raw: float, temp: float, humidity: float,
                         prev_gas_raw: float | None = None) -> str:
    """يرجع: 'Safe' | 'Moderate' | 'Dangerous'
    gas_raw: القراءة الخام لحساس MQ135 (0-4095) - تصل تحت مفتاح gas_ppm.
    prev_gas_raw: القراءة الخام السابقة مباشرة لنفس الخوذة (لكشف الارتفاع المفاجئ).
    (temp, humidity غير مستخدمين بالتصنيف حاليًا - محفوظين بالتوقيع لإمكانية
    توسيع المنطق لاحقًا، مثلًا لرفع حساسية التصنيف بحرارة/رطوبة عالية)
    """
    # --- كشف الارتفاع المفاجئ أولًا: قفزة كبيرة سريعة = خطر فوري بغض النظر عن القيمة ---
    if prev_gas_raw is not None and (gas_raw - prev_gas_raw) >= GAS_SPIKE_DELTA:
        return "Dangerous"

    if gas_raw >= GAS_DANGEROUS_THRESHOLD:
        return "Dangerous"
    elif gas_raw >= GAS_MODERATE_THRESHOLD:
        return "Moderate"
    else:
        return "Safe"


# ---------------------------------------------------------------------------
# 2) تصنيف الحركة/السقوط
#    ملاحظة مهمة: الميزات الـ13 (mean/std/max/min/sma) تُحسب الآن محليًا على
#    ESP32-S3 نفسه من نافذة تسارع مدتها ~2 ثانية (160 عينة @ ~80Hz) - تطابق
#    تمامًا نافذة التدريب الأصلية في Walker Fall Detection Dataset. لذلك هذه
#    الدالة تستقبل الميزات جاهزة مباشرة (dict) وليس قراءات خام (raw window).
# ---------------------------------------------------------------------------
def predict_fall_status(features: dict) -> str:
    """يرجع: 'idle' | 'motion' | 'step' | 'fall'
    features: dict فيه المفاتيح الـ13 المعرّفة في FALL_FEATURES بالأعلى
    ⚠️ هاي تصنيف الموديل الخام (argmax) بدون أي فلترة بالثقة. للحصول على
    الحالة المعتمدة فعليًا (اللي لازم تتحكم بالبزر/الإنذار)، استخدم
    predict_fall_status_confident بالأسفل بدلها.
    """
    X = np.array([[features[f] for f in FALL_FEATURES]])
    return fall_detection_model.predict(X)[0]


# ---------------------------------------------------------------------------
# 2.1) فلترة بالثقة (Confidence-based Filtering) فوق تصنيف السقوط
# ---------------------------------------------------------------------------
# المشكلة اللي عالجناها: هزة راس بسيطة (مش سقوط فعلي) ممكن أحيانًا تُصنَّف
# fall، لأنها حركة قصيرة وحادة تشبه بداية السقطة إحصائيًا - لكنها غالبًا
# حالة "حدّية" بالنسبة للموديل (احتمالية fall مش عالية جدًا، حتى لو كانت
# أعلى صنف).
#
# الحل: بدل ما نعتمد على التصنيف النهائي (argmax) لحاله، منستخدم
# predict_proba() لنجيب احتمالية صنف fall تحديدًا. الإنذار ما بينطلق إلا لو
# هاي الاحتمالية أعلى من عتبة معينة (FALL_CONFIDENCE_THRESHOLD). لو الموديل
# توقع fall بس بثقة واطية، منعتبرها نتيجة غير موثوقة ومنتجاهلها.
#
# مثال:
#   احتمالية fall = 0.55  → أوطأ من العتبة (0.75 افتراضيًا) → تجاهل
#   احتمالية fall = 0.85  → أعلى من العتبة → تأكيد → إنذار
#
# لم نمس الموديل ولا الـ features ولا النافذة (160 sample) ولا الهاردوير
# إطلاقًا - هاي طبقة قرار إضافية فوق مخرجات الموديل الخام فقط (نفس الـ
# .predict() الأصلي + استدعاء .predict_proba() الإضافي من نفس الموديل بالظبط).
#
# ⚠️ ملاحظة: 0.75 هون رقم افتراضي بس (منتصف المجال المطلوب 0.70-0.80)، مش
# محسوب من بيانات حقيقية - على عكس حل z_std السابق يلي حسبناه من
# adxl_datasetnew.xlsx مباشرة. سبب الفرق: حساب احتمالية fall الفعلية
# (predict_proba) يحتاج الموديل المدرَّب نفسه (.pkl)، مش بس بيانات
# الـ features الخام يلي عنا. لو رفعتوا ملف الموديل (.pkl)، فيني أحسب
# التوزيع الحقيقي لاحتمالية fall عبر كل سجلات التدريب وأقترح threshold
# مبني على أرقام فعلية، بدل ما نعتمد نص المجال المقترح كتخمين.
#
# ملاحظة تصميم: لو fall انرفضت لضعف الثقة، منرجّع "motion" كقيمة غير منبّهة
# بدل fall، بنفس مبدأ الحلول السابقة - ما بدنا نخترع تصنيف خامس غير موجود
# أصلًا بمفردات النظام (يوجّه للفرونت إند وملف Excel وقاعدة البيانات).
# ---------------------------------------------------------------------------
FALL_CONFIDENCE_THRESHOLD = 0.75   # قابلة للتعديل (المطلوب: بين 0.70 و0.80) - راجع الملاحظة أعلاه

def predict_fall_status_confident(features: dict) -> str:
    """
    نفس الموديل ونفس الـ features بالضبط - بس بدل الاعتماد على التصنيف الخام
    (argmax) لحاله، بيتحقق من احتمالية fall تحديدًا قبل ما يعتمدها.
    يرجع الحالة المعتمدة اللي لازم تُستخدم بكل مكان تاني بالنظام (البزر،
    الليد، تسجيل الحوادث، حساب الخطورة، قاعدة البيانات).
    """
    X = np.array([[features[f] for f in FALL_FEATURES]])
    predicted_class = fall_detection_model.predict(X)[0]

    if predicted_class != "fall":
        return predicted_class   # الموديل ما توقع fall أصلًا - ما في داعي نتحقق من الثقة

    # الموديل توقع fall - نتحقق من مدى ثقته بهالتحديد تحديدًا
    probabilities = fall_detection_model.predict_proba(X)[0]
    fall_index = list(fall_detection_model.classes_).index("fall")
    fall_probability = probabilities[fall_index]

    if fall_probability >= FALL_CONFIDENCE_THRESHOLD:
        return "fall"       # ثقة كافية - تأكيد
    else:
        return "motion"     # ثقة منخفضة (fall_probability أوطأ من العتبة) - تجاهل كنتيجة غير موثوقة


# ---------------------------------------------------------------------------
# 3) معاملات العمر والصحة (Rule-based, ليست AI)
# ---------------------------------------------------------------------------
def age_factor(age: int) -> float:
    """
    معامل خطورة العمر، مبني على نموذج منحنى-U المستند لبيانات BLS/NSC:
    العمال الشباب (<25) وكبار السن (>55) أعلى خطورة من منتصف العمر.
    """
    if age is None:
        return 1.0
    if age < 25:
        return 1.25
    elif age < 45:
        return 1.0
    elif age < 55:
        return 1.10
    else:
        return 1.20


def experience_factor(years_of_experience: int) -> float:
    """عامل تكميلي بسيط: أقل من سنة خبرة = خطورة إضافية"""
    if years_of_experience is None:
        return 1.0
    if years_of_experience < 1:
        return 1.10
    return 1.0


def health_factor(has_respiratory: bool, has_cardiac: bool) -> float:
    """
    معامل صحي تراكمي. القيم (+0.2 لكل حالة) قيم افتراضية معقولة
    قابلة للتعديل لاحقًا بناءً على استشارة طبية/مصدر علمي أدق.
    """
    factor = 1.0
    if has_respiratory:
        factor += 0.2
    if has_cardiac:
        factor += 0.2
    return factor


# ---------------------------------------------------------------------------
# 4) دمج كل شيء بمعادلة واحدة
# ---------------------------------------------------------------------------
def compute_final_risk(air_status: str, fall_status: str, worker: dict | None) -> dict:
    """
    يحسب الخطورة النهائية بدمج:
      - الخطورة الأساسية من الموديلين (Worst-case بينهم)
      - معامل العمر، الخبرة، والحالة الصحية (لو فيه بيانات عامل مرتبطة)

    يرجع dict فيه: base_risk_score, final_risk_score, risk_level
    """
    base_score = max(AIR_RISK_SCORE.get(air_status, 10), FALL_RISK_SCORE.get(fall_status, 5))

    if worker:
        af = age_factor(worker.get("age"))
        ef = experience_factor(worker.get("years_of_experience"))
        hf = health_factor(worker.get("has_respiratory_condition"), worker.get("has_cardiac_condition"))
    else:
        af, ef, hf = 1.0, 1.0, 1.0

    final_score = base_score * af * ef * hf
    final_score = min(final_score, 100)  # سقف أعلى 100

    # تصنيف نهائي لمستوى الخطورة (لعرضه بالداشبورد بألوان)
    if fall_status == "fall":
        risk_level = "Critical"
    elif final_score >= 70:
        risk_level = "High"
    elif final_score >= 35:
        risk_level = "Medium"
    else:
        risk_level = "Low"

    return {
        "base_risk_score": round(base_score, 2),
        "final_risk_score": round(final_score, 2),
        "risk_level": risk_level,
    }
