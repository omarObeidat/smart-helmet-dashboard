import joblib
import numpy as np
from pathlib import Path
MODELS_DIR = Path(__file__).parent.parent / "models"
fall_detection_model = joblib.load(MODELS_DIR / "fall_detection_rf_model.pkl")
FALL_FEATURES = ["x_mean", "x_std", "x_max", "x_min",
                  "y_mean", "y_std", "y_max", "y_min",
                  "z_mean", "z_std", "z_max", "z_min", "sma"]
AIR_RISK_SCORE = {"Safe": 10, "Moderate": 50, "Dangerous": 90}
FALL_RISK_SCORE = {"idle": 5, "step": 10, "motion": 15, "fall": 100}
GAS_MODERATE_THRESHOLD = 500      
GAS_DANGEROUS_THRESHOLD = 1000    
GAS_SPIKE_DELTA = 300     

def predict_air_quality(gas_raw: float, temp: float, humidity: float,
                         prev_gas_raw: float | None = None) -> str:
    if prev_gas_raw is not None and (gas_raw - prev_gas_raw) >= GAS_SPIKE_DELTA:
        return "Dangerous"
    if gas_raw >= GAS_DANGEROUS_THRESHOLD:
        return "Dangerous"
    elif gas_raw >= GAS_MODERATE_THRESHOLD:
        return "Moderate"
    else:
        return "Safe"

def predict_fall_status(features: dict) -> str:
    X = np.array([[features[f] for f in FALL_FEATURES]])
    return fall_detection_model.predict(X)[0]

def age_factor(age: int) -> float:
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
    if years_of_experience is None:
        return 1.0
    if years_of_experience < 1:
        return 1.10
    return 1.0

def health_factor(has_respiratory: bool, has_cardiac: bool) -> float:
    factor = 1.0
    if has_respiratory:
        factor += 0.2
    if has_cardiac:
        factor += 0.2
    return factor

def compute_final_risk(air_status: str, fall_status: str, worker: dict | None) -> dict:
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