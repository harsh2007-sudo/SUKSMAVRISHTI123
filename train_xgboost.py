# train_xgboost.py
"""
SukSmaVrsti: micro-weather model trainer.

Trains three XGBoost models from the real micro-terrain grid plus the
historical regional forcing sample:

  * xgb_model_rain.pkl     -> downscaled rainfall intensity (mm/hr)
  * xgb_model_waterlog.pkl -> waterlogging possibility (0..1)
  * xgb_model_wind.pkl     -> surface wind speed (km/h)

Each pickle is a bundle dict {"model", "features", "target", "unit", "kind",
"trained_at", "terrain_file", "n_rows"} so predict_grid.py always scores with
the exact feature columns the model was trained on.

Run order of the pipeline:
    python fetch_open_meteo.py     # writes latest_synoptic_weather.json
    python terrain_grid.py         # writes terrain_grid_8x8.csv (real DEM)
    python train_xgboost.py        # writes the three .pkl models
    python predict_grid.py         # writes the 64-cell + khasra forecast CSVs
"""

import os
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from xgboost import XGBRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    mean_squared_error,
    r2_score,
)

TERRAIN_FILE = "terrain_grid_8x8.csv"
HISTORICAL_FILE = "barabanki_historical_sample.csv"

MODEL_FILES = {
    "rain": "xgb_model_rain.pkl",
    "waterlog": "xgb_model_waterlog.pkl",
    "wind": "xgb_model_wind.pkl",
}

# Shared feature vector: regional forcing + real micro-terrain variables
# (the last two come from the real NASA-DEM export; defaults keep the script
# working with a synthetic grid produced by generate_terrain_grid.py)
FEATURE_COLS = [
    "precip_coarse", "humidity", "wind_speed", "cloud_cover",
    "elevation", "slope", "aspect", "twi", "roughness_z0",
    "temp_anomaly", "moisture_feedback", "dist_updraft",
    "drainage_density", "soil_saturation_cap",
]

DEFAULT_DRAINAGE = 1.0     # km/km2
DEFAULT_SOIL_SAT = 55.0    # %


def _clip01(x):
    return float(min(1.0, max(0.0, x)))


def load_terrain():
    if not os.path.exists(TERRAIN_FILE):
        raise FileNotFoundError(
            f"Missing '{TERRAIN_FILE}'! Run terrain_grid.py first "
            f"(it builds the grid from the real NASA-DEM micro-terrain file)."
        )
    df = pd.read_csv(TERRAIN_FILE)

    if "drainage_density_km_km2" not in df.columns:
        print(f"WARNING: no drainage density column in '{TERRAIN_FILE}'; using {DEFAULT_DRAINAGE} km/km2.")
        df["drainage_density_km_km2"] = DEFAULT_DRAINAGE
    if "soil_saturation_cap_pct" not in df.columns:
        print(f"WARNING: no soil saturation column in '{TERRAIN_FILE}'; using {DEFAULT_SOIL_SAT}%.")
        df["soil_saturation_cap_pct"] = DEFAULT_SOIL_SAT
    return df


def load_coarse_forcing():
    """Historical regional observations, or a representative synthetic series."""
    if os.path.exists(HISTORICAL_FILE):
        df = pd.read_csv(HISTORICAL_FILE)
        print(f"Loaded {len(df)} historical regional observations from '{HISTORICAL_FILE}'.")
        return df

    print("Generating representative Barabanki monsoon and pre-monsoon convective forcing series...")
    np.random.seed(42)
    n_samples = 400
    return pd.DataFrame({
        "precipitation": np.random.choice(
            [0.0, 0.0, 0.0, 1.2, 3.5, 6.8, 12.0],
            size=n_samples, p=[0.45, 0.20, 0.10, 0.10, 0.08, 0.05, 0.02]),
        "relative_humidity_2m": np.random.uniform(45.0, 95.0, size=n_samples),
        "wind_speed_10m": np.random.uniform(4.0, 32.0, size=n_samples),
        "cloud_cover": np.random.uniform(20.0, 100.0, size=n_samples),
    })


def build_training_set(df_terrain, df_coarse):
    """
    Pair every regional forcing hour with sampled micro-cells and derive the
    three physical targets (rain, waterlogging possibility, wind).
    """
    np.random.seed(42)
    rows = []

    for _, w in df_coarse.iterrows():
        p_coarse = float(w.get("precipitation", 0.0))
        humid = float(w.get("relative_humidity_2m", 65.0))
        wind = float(w.get("wind_speed_10m", 12.0))
        cloud = float(w.get("cloud_cover", 60.0))

        # Sample micro-cells to model spatial convective variance across village terrains
        sample_cells = df_terrain.sample(n=10)
        for _, t in sample_cells.iterrows():
            slope = float(t["slope_deg"])
            twi = float(t["twi"])
            z0 = float(t["roughness_z0"])
            drainage = float(t["drainage_density_km_km2"])
            soil_sat = float(t["soil_saturation_cap_pct"])

            # Orographic uplift: wind interacting with micro-slope gradient
            orographic = (slope / 3.0) * (wind / 15.0)

            # Thermal convective lift: warm bare ground / drying yards under humid conditions
            thermal_lift = max(0.0, float(t["temp_anomaly_c"]) * (humid / 70.0))

            # Moisture feedback: transpiring paddy/sugarcane canopies feed local convective cells
            canopy_moisture = (float(t["moisture_feedback"]) - 1.0) * 0.8

            # Aerodynamic friction: surface roughness slowing boundary layer winds
            friction_convergence = (z0 / 0.5) * 0.3

            if p_coarse > 0.1:
                # Regional rain is present: local terrain modulates the intensity
                down_rain = (p_coarse * 1.10) + orographic + thermal_lift + canopy_moisture + friction_convergence
            else:
                # Coarse radar is dry (0 mm), but micro-convective rainburst triggers
                # over hot, humid zones
                if humid > 78.0 and float(t["temp_anomaly_c"]) > 1.0:
                    down_rain = np.random.uniform(2.5, 6.2)
                else:
                    down_rain = 0.0
            target_rain = round(max(0.0, down_rain), 2)

            # --- Waterlogging possibility (0..1) --------------------------------
            # Rain fills the profile; high TWI / saturated soil / sparse drainage /
            # flat ground keep it ponded.
            rain_term = min(target_rain, 8.0) / 8.0
            twi_term = _clip01((twi - 9.5) / 3.0)
            sat_term = _clip01((soil_sat - 45.0) / 35.0)
            drain_term = 1.0 - _clip01(drainage / 2.5)
            slope_term = 1.0 - _clip01(slope / 4.0)
            humid_term = _clip01((humid - 60.0) / 35.0)
            target_waterlog = round(_clip01(
                0.40 * rain_term + 0.22 * twi_term + 0.16 * sat_term
                + 0.10 * drain_term + 0.07 * slope_term + 0.05 * humid_term
            ), 3)

            # --- Surface wind (km/h) -------------------------------------------
            # Roughness drags the flow; exposed slopes accelerate it.
            local_wind = wind * (1.1 - 0.25 * z0) + (slope / 3.0) * (wind / 15.0) * 0.6
            target_wind = round(max(0.0, local_wind), 1)

            rows.append({
                "precip_coarse": p_coarse,
                "humidity": humid,
                "wind_speed": wind,
                "cloud_cover": cloud,
                "elevation": float(t["elevation_m"]),
                "slope": slope,
                "aspect": float(t["aspect_deg"]),
                "twi": twi,
                "roughness_z0": z0,
                "temp_anomaly": float(t["temp_anomaly_c"]),
                "moisture_feedback": float(t["moisture_feedback"]),
                "dist_updraft": float(t["dist_updraft_km"]),
                "drainage_density": drainage,
                "soil_saturation_cap": soil_sat,
                "target_rain_mm": target_rain,
                "target_waterlog": target_waterlog,
                "target_wind_kmh": target_wind,
            })

    return pd.DataFrame(rows)


def fit_model(X_train, y_train, X_val, y_val):
    model = XGBRegressor(
        n_estimators=220,
        learning_rate=0.035,
        max_depth=6,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.2,
        reg_lambda=1.2,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    return model


def export_bundle(model, target, unit, kind, path, n_rows):
    bundle = {
        "model": model,
        "features": FEATURE_COLS,
        "target": target,
        "unit": unit,
        "kind": kind,           # "regression" | "probability"
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "terrain_file": TERRAIN_FILE,
        "n_rows": int(n_rows),
    }
    joblib.dump(bundle, path)
    return path


def print_importances(model, title):
    print(f"\n{title} feature importances:")
    imp = pd.Series(model.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)
    for feat, score in imp.items():
        print(f"  • {feat:<20}: {score:.4f}")


def main():
    print("1. Loading micro-terrain grid and regional atmospheric baseline...")
    df_terrain = load_terrain()
    df_coarse = load_coarse_forcing()

    df_train = build_training_set(df_terrain, df_coarse)
    print(f"Constructed {len(df_train)} paired training vectors across all micro-terrain parameters.")

    X = df_train[FEATURE_COLS]
    X_train, X_val, y_train_rain, y_val_rain = train_test_split(
        X, df_train["target_rain_mm"], test_size=0.20, random_state=42)
    _, _, y_train_wl, y_val_wl = train_test_split(
        X, df_train["target_waterlog"], test_size=0.20, random_state=42)
    _, _, y_train_wind, y_val_wind = train_test_split(
        X, df_train["target_wind_kmh"], test_size=0.20, random_state=42)

    # ------------------------------------------------------------------
    # 2. RAIN MODEL (mm/hr)
    # ------------------------------------------------------------------
    print("\n2. Training rainfall downscaling model (xgb_model_rain.pkl)...")
    rain_model = fit_model(X_train, y_train_rain, X_val, y_val_rain)
    rain_pred = np.clip(rain_model.predict(X_val), 0.0, None)
    print("\n================ RAIN MODEL EVALUATION ================")
    print(f"R²  : {r2_score(y_val_rain, rain_pred):.4f}")
    print(f"RMSE: {np.sqrt(mean_squared_error(y_val_rain, rain_pred)):.4f} mm/hr")
    print("Rain burst classification (>= 1.0 mm/hr):")
    print(classification_report(
        (y_val_rain >= 1.0).astype(int), (rain_pred >= 1.0).astype(int),
        labels=[0, 1], target_names=["Dry (<1 mm)", "Rain (>=1 mm)"], zero_division=0))
    print_importances(rain_model, "Rain")
    export_bundle(rain_model, "target_rain_mm", "mm/hr", "regression",
                  MODEL_FILES["rain"], len(df_train))

    # ------------------------------------------------------------------
    # 3. WATERLOGGING MODEL (possibility 0..1)
    # ------------------------------------------------------------------
    print("\n3. Training waterlogging possibility model (xgb_model_waterlog.pkl)...")
    wl_model = fit_model(X_train, y_train_wl, X_val, y_val_wl)
    wl_pred = np.clip(wl_model.predict(X_val), 0.0, 1.0)
    print("\n============= WATERLOGGING MODEL EVALUATION ===========")
    print(f"R²  : {r2_score(y_val_wl, wl_pred):.4f}")
    print(f"RMSE: {np.sqrt(mean_squared_error(y_val_wl, wl_pred)):.4f} (probability)")
    print(f"Accuracy @ 0.5 threshold: {accuracy_score((y_val_wl >= 0.5).astype(int), (wl_pred >= 0.5).astype(int)):.4f}")
    print("Waterlogging classification (>= 0.5 possibility):")
    print(classification_report(
        (y_val_wl >= 0.5).astype(int), (wl_pred >= 0.5).astype(int),
        labels=[0, 1], target_names=["No waterlogging", "Waterlogging risk"], zero_division=0))
    print_importances(wl_model, "Waterlogging")
    export_bundle(wl_model, "target_waterlog", "probability", "probability",
                  MODEL_FILES["waterlog"], len(df_train))

    # ------------------------------------------------------------------
    # 4. WIND MODEL (km/h)
    # ------------------------------------------------------------------
    print("\n4. Training surface wind model (xgb_model_wind.pkl)...")
    wind_model = fit_model(X_train, y_train_wind, X_val, y_val_wind)
    wind_pred = np.clip(wind_model.predict(X_val), 0.0, None)
    print("\n=============== WIND MODEL EVALUATION ================")
    print(f"R²  : {r2_score(y_val_wind, wind_pred):.4f}")
    print(f"RMSE: {np.sqrt(mean_squared_error(y_val_wind, wind_pred)):.4f} km/h")
    print_importances(wind_model, "Wind")
    export_bundle(wind_model, "target_wind_kmh", "km/h", "regression",
                  MODEL_FILES["wind"], len(df_train))

    print("\nAll three models exported:")
    for name, path in MODEL_FILES.items():
        size_kb = os.path.getsize(path) / 1024
        print(f"  • {name:<9} -> {path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
