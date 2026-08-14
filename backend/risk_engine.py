"""
risk_engine.py
---------------
Computes the final risk score for each sensor reading:

1. predict_air_quality  -> Safe / Moderate / Dangerous (rule-based on raw MQ135 ADC)
2. predict_fall_status  -> idle / motion / step / fall (ML model)
3. age_factor / health_factor -> rule-based multipliers from the worker profile

final_risk_score = base_risk_score(air_status, fall_status) * age_factor * experience_factor * health_factor

Age factor reference: BLS/NSC data (2023-2024) shows workers aged 20-24 have
the highest injury rate of any age group, so a U-curve model is used instead
of a simple linear "older = riskier" assumption.

Air quality note: classification uses the raw MQ135 ADC reading (0-4095)
directly rather than a calibrated PPM value, because the sensor's R0
calibration was never successfully completed (unstable ADC readings during
calibration attempts). A derived PPM number would be false precision; the
raw reading is a reliable relative indicator for a 3-tier classification.
(The field is still named gas_ppm for compatibility across the system, but
now holds a raw value, not PPM.)
"""

import joblib
import numpy as np
from pathlib import Path

MODELS_DIR = Path(__file__).parent.parent / "models"

# Only the fall detection model is loaded - air quality uses rule-based logic (see note above)
fall_detection_model = joblib.load(MODELS_DIR / "fall_detection_rf_model.pkl")

FALL_FEATURES = ["x_mean", "x_std", "x_max", "x_min",
                  "y_mean", "y_std", "y_max", "y_min",
                  "z_mean", "z_std", "z_max", "z_min", "sma"]

AIR_RISK_SCORE = {"Safe": 10, "Moderate": 50, "Dangerous": 90}
FALL_RISK_SCORE = {"idle": 5, "step": 10, "motion": 15, "fall": 100}

# Raw ADC thresholds (0-4095) for air quality classification.
# Must match GAS_MODERATE/DANGEROUS_THRESHOLD in the firmware.
GAS_MODERATE_THRESHOLD = 500
GAS_DANGEROUS_THRESHOLD = 1000

# Spike detection: a sudden large jump vs. the previous reading (e.g. sudden
# exposure to a gas source) is treated as dangerous regardless of absolute
# value, since a fast jump is riskier than a steady high baseline.
GAS_SPIKE_DELTA = 300


# ---------------------------------------------------------------------------
# 1) Air quality classification (rule-based, raw MQ135 reading)
# ---------------------------------------------------------------------------
def predict_air_quality(gas_raw: float, temp: float, humidity: float,
                         prev_gas_raw: float | None = None) -> str:
    """Returns 'Safe' | 'Moderate' | 'Dangerous'.
    gas_raw: raw MQ135 reading (0-4095), sent under the gas_ppm key.
    prev_gas_raw: previous reading for the same helmet, for spike detection.
    (temp, humidity are unused for now - kept in the signature for future use.)
    """
    if prev_gas_raw is not None and (gas_raw - prev_gas_raw) >= GAS_SPIKE_DELTA:
        return "Dangerous"

    if gas_raw >= GAS_DANGEROUS_THRESHOLD:
        return "Dangerous"
    elif gas_raw >= GAS_MODERATE_THRESHOLD:
        return "Moderate"
    else:
        return "Safe"


# ---------------------------------------------------------------------------
# 2) Fall/motion classification
#    The 13 features are computed on the ESP32-S3 itself from a ~2s
#    acceleration window (160 samples @ ~80Hz), matching the training
#    window from the Walker Fall Detection Dataset.
# ---------------------------------------------------------------------------
def predict_fall_status(features: dict) -> str:
    """Returns 'idle' | 'motion' | 'step' | 'fall'.
    Raw model prediction (argmax), no confidence filtering.
    Use predict_fall_status_confident() instead for the value that should
    drive the buzzer/alerts.
    """
    X = np.array([[features[f] for f in FALL_FEATURES]])
    return fall_detection_model.predict(X)[0]


# ---------------------------------------------------------------------------
# 2.1) Confidence-based filtering on top of the fall classifier
# ---------------------------------------------------------------------------
# A sharp head jerk (not an actual fall) can occasionally be misclassified
# as "fall" since it's statistically similar to the start of a fall. Instead
# of trusting argmax alone, we check predict_proba() for the fall class
# specifically: below FALL_CONFIDENCE_THRESHOLD, the prediction is treated
# as unreliable and downgraded to "motion" rather than triggering an alert.
#
# Example: fall probability 0.55 (below threshold) -> ignored.
#          fall probability 0.85 (above threshold) -> confirmed, alert fires.
#
# This is a decision layer on top of the existing model's output only -
# the model, features, window size, and hardware are unchanged.
#
# Note: 0.75 is a default within the requested 0.70-0.80 range, not derived
# from training data (unlike the z_std threshold, which was computed
# directly from adxl_datasetnew.xlsx). Computing a data-driven threshold
# would require the trained model file to get the actual probability
# distribution across the training set.
# ---------------------------------------------------------------------------
FALL_CONFIDENCE_THRESHOLD = 0.75

def predict_fall_status_confident(features: dict) -> str:
    """
    Same model and features as predict_fall_status(), but verifies the
    fall-class probability before accepting a 'fall' prediction. Returns
    the value that should drive the buzzer, LED, incident log, and risk
    score everywhere else in the system.
    """
    X = np.array([[features[f] for f in FALL_FEATURES]])
    predicted_class = fall_detection_model.predict(X)[0]

    if predicted_class != "fall":
        return predicted_class

    probabilities = fall_detection_model.predict_proba(X)[0]
    fall_index = list(fall_detection_model.classes_).index("fall")
    fall_probability = probabilities[fall_index]

    if fall_probability >= FALL_CONFIDENCE_THRESHOLD:
        return "fall"
    else:
        return "motion"   # low confidence - treat as unreliable, don't alert


# ---------------------------------------------------------------------------
# 3) Age and health factors (rule-based, not ML)
# ---------------------------------------------------------------------------
def age_factor(age: int) -> float:
    """U-curve risk model based on BLS/NSC data: younger (<25) and older
    (>55) workers carry higher risk than mid-career workers."""
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
    """Less than 1 year of experience adds risk."""
    if years_of_experience is None:
        return 1.0
    if years_of_experience < 1:
        return 1.10
    return 1.0


def health_factor(has_respiratory: bool, has_cardiac: bool) -> float:
    """Cumulative health factor. +0.2 per condition is a reasonable default,
    adjustable pending medical/scientific input."""
    factor = 1.0
    if has_respiratory:
        factor += 0.2
    if has_cardiac:
        factor += 0.2
    return factor


# ---------------------------------------------------------------------------
# 4) Combine into a final score
# ---------------------------------------------------------------------------
def compute_final_risk(air_status: str, fall_status: str, worker: dict | None) -> dict:
    """Combines the worst-case base risk from both models with the worker's
    age/experience/health multipliers (if a worker is linked)."""
    base_score = max(AIR_RISK_SCORE.get(air_status, 10), FALL_RISK_SCORE.get(fall_status, 5))

    if worker:
        af = age_factor(worker.get("age"))
        ef = experience_factor(worker.get("years_of_experience"))
        hf = health_factor(worker.get("has_respiratory_condition"), worker.get("has_cardiac_condition"))
    else:
        af, ef, hf = 1.0, 1.0, 1.0

    final_score = base_score * af * ef * hf
    final_score = min(final_score, 100)

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
