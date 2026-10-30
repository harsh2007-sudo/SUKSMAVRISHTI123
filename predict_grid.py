# predict_grid.py
"""
SukSmaVrsti: Hyperlocal Grid Weather Predictor

Reads the latest synoptic weather JSON (written by fetch_open_meteo.py),
loads the three XGBoost models trained by train_xgboost.py, and predicts for
all 64 micro-cells:

  * predicted_rain_mm       downscaled rainfall intensity (mm/hr)
  * predicted_waterlog_pct  waterlogging possibility (0-100 %)
  * predicted_wind_kmh      surface wind speed (km/h)

Outputs:
  * latest_inference_output.csv        64 micro-cells, full features + predictions
  * latest_khasra_weather_forecast.csv one row per Bhulekh khasra plot with the
                                       weather of the cell it belongs to

Village-level advisories are aggregated with the Max-Risk protocol (the peak
cell drives the alert) and linked to the Bhulekh revenue villages.
"""

import os
import sys

import joblib
import numpy as np
import pandas as pd

from agromet_advisory_engine import generate_farmer_advisory

# ------------------------------------------------------------------------------
# 1. LOAD MODELS, TERRAIN GRID & BHULEKH REVENUE DATABASE
# ------------------------------------------------------------------------------
MODEL_FILES = {
    "rain": "xgb_model_rain.pkl",
    "waterlog": "xgb_model_waterlog.pkl",
    "wind": "xgb_model_wind.pkl",
}
SYNOPTIC_JSON_FILE = "latest_synoptic_weather.json"
TERRAIN_FILE = "terrain_grid_8x8.csv"

BHULEKH_FILES = [
    "bhulekh_khasra_grid_mapping_demo_names.csv",
    "bhulekh_khasra_grid_mapping_2.csv",
    "bhulekh_khasra_grid_mapping.csv",
]
bhulekh_path = next((f for f in BHULEKH_FILES if os.path.exists(f)), None)

# Fallback feature vector (matches train_xgboost.py) for legacy model pickles
FEATURE_COLS = [
    "precip_coarse", "humidity", "wind_speed", "cloud_cover",
    "elevation", "slope", "aspect", "twi", "roughness_z0",
    "temp_anomaly", "moisture_feedback", "dist_updraft",
    "drainage_density", "soil_saturation_cap",
]

DEFAULT_DRAINAGE = 1.0     # km/km2
DEFAULT_SOIL_SAT = 55.0    # %


def load_model(path):
    """Load a model bundle and return (predict_callable, feature_columns)."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Model file '{path}' not found. Run train_xgboost.py first."
        )
    payload = joblib.load(path)
    if isinstance(payload, dict) and "model" in payload:
        return payload["model"], list(payload.get("features", FEATURE_COLS))
    # Legacy pickle: a bare estimator trained on the original 12 features
    legacy = FEATURE_COLS[:12]
    return payload, legacy


if not os.path.exists(TERRAIN_FILE):
    raise FileNotFoundError(
        f"Terrain file '{TERRAIN_FILE}' not found. Run terrain_grid.py first."
    )

print(f"Loading models from '{', '.join(MODEL_FILES.values())}'...")
rain_model, rain_features = load_model(MODEL_FILES["rain"])
waterlog_model, waterlog_features = load_model(MODEL_FILES["waterlog"])
wind_model, wind_features = load_model(MODEL_FILES["wind"])

df_terrain = pd.read_csv(TERRAIN_FILE)

# ------------------------------------------------------------------------------
# 2. INGEST LATEST SYNOPTIC WEATHER (JSON first, live API second)
# ------------------------------------------------------------------------------
live_weather = None
weather_source = ""

try:
    from fetch_open_meteo import (
        SYNOPTIC_JSON_FILE as FETCH_JSON_FILE,
        fetch_synoptic_weather,
        load_synoptic_json,
        save_synoptic_weather_json,
    )
    SYNOPTIC_JSON_FILE = FETCH_JSON_FILE

    # Fresh saved JSON is the normal path (predict_grid never blocks on network
    # when fetch_open_meteo.py has run recently).
    live_weather = load_synoptic_json(SYNOPTIC_JSON_FILE, max_age_hours=6.0)
    if live_weather:
        weather_source = f"'{SYNOPTIC_JSON_FILE}' (saved synoptic JSON)"

    if not live_weather:
        # Refresh from the API and persist for the next run
        live_weather = fetch_synoptic_weather()
        if live_weather:
            save_synoptic_weather_json(live_weather, path=SYNOPTIC_JSON_FILE)
            weather_source = "Open-Meteo live fetch (saved to synoptic JSON)"
        else:
            # Offline: reuse the last saved JSON however old it is
            live_weather = load_synoptic_json(SYNOPTIC_JSON_FILE, max_age_hours=None)
            if live_weather:
                weather_source = f"'{SYNOPTIC_JSON_FILE}' (stale, offline fallback)"
except Exception as exc:  # import/network problems must not kill the offline run
    print(f"Synoptic ingest unavailable ({exc}); using last saved JSON if any.")
    try:
        from fetch_open_meteo import load_synoptic_json
        live_weather = load_synoptic_json(SYNOPTIC_JSON_FILE, max_age_hours=None)
        if live_weather:
            weather_source = f"'{SYNOPTIC_JSON_FILE}' (offline fallback)"
    except Exception:
        live_weather = None

if not live_weather:
    # Last resort: clearly labelled offline constants (no API, no JSON on disk)
    live_weather = {
        "timestamp": "offline-constant",
        "precip_coarse_mm": 0.0,
        "humidity_pct": 70.0,
        "wind_speed_kmh": 10.0,
        "cloud_cover_pct": 60.0,
    }
    weather_source = "offline constants (no synoptic JSON, no network)"

print("\n--- Current Regional Synoptic Weather ---")
print(f"Source              : {weather_source}")
print(f"Valid hour          : {live_weather.get('timestamp', 'n/a')}")
print(f"Regional Coarse Rain: {live_weather.get('precip_coarse_mm', 0.0)} mm/hr")
print(f"Relative Humidity   : {live_weather.get('humidity_pct', 0.0)}%")
print(f"Surface Wind Speed  : {live_weather.get('wind_speed_kmh', 0.0)} km/h")
print(f"Cloud Cover         : {live_weather.get('cloud_cover_pct', 0.0)}%")

# ------------------------------------------------------------------------------
# 3. DOWNSCALE ACROSS ALL 64 MICRO-CELLS (three models)
# ------------------------------------------------------------------------------
inference_df = df_terrain.copy()
inference_df["precip_coarse"] = float(live_weather.get("precip_coarse_mm", 0.0))
inference_df["humidity"] = float(live_weather.get("humidity_pct", 0.0))
inference_df["wind_speed"] = float(live_weather.get("wind_speed_kmh", 0.0))
inference_df["cloud_cover"] = float(live_weather.get("cloud_cover_pct", 0.0))
inference_df["elevation"] = inference_df["elevation_m"]
inference_df["slope"] = inference_df["slope_deg"]
inference_df["aspect"] = inference_df["aspect_deg"]
inference_df["temp_anomaly"] = inference_df["temp_anomaly_c"]
inference_df["dist_updraft"] = (
    inference_df["dist_updraft_km"] if "dist_updraft_km" in inference_df.columns else 0.0
)

if "drainage_density_km_km2" not in inference_df.columns:
    inference_df["drainage_density_km_km2"] = DEFAULT_DRAINAGE
if "soil_saturation_cap_pct" not in inference_df.columns:
    inference_df["soil_saturation_cap_pct"] = DEFAULT_SOIL_SAT
inference_df["drainage_density"] = inference_df["drainage_density_km_km2"]
inference_df["soil_saturation_cap"] = inference_df["soil_saturation_cap_pct"]

# float64 casts: XGBoost predicts in float32, which prints as 8.699999809265137
def _predict(model, features, decimals, lo=None, hi=None):
    values = np.asarray(model.predict(inference_df[features]), dtype=np.float64)
    if lo is not None or hi is not None:
        values = np.clip(values, lo, hi)
    return np.round(values, decimals)


inference_df["predicted_rain_mm"] = _predict(rain_model, rain_features, 2, lo=0.0)
inference_df["predicted_waterlog_pct"] = np.round(
    np.clip(np.asarray(waterlog_model.predict(inference_df[waterlog_features]), dtype=np.float64),
            0.0, 1.0) * 100.0, 1)
inference_df["predicted_wind_kmh"] = _predict(wind_model, wind_features, 1, lo=0.0)

# ------------------------------------------------------------------------------
# 4. PRINT 8x8 HYPERLOCAL WEATHER MATRICES
# ------------------------------------------------------------------------------
def print_matrix(title, values, fmt="{:5.1f}"):
    print("\n=======================================================")
    print(f"  {title}")
    print("=======================================================")
    for r_idx, row in enumerate(values):
        print(f"Row {r_idx}: " + " ".join(fmt.format(v) for v in row))
    print("=======================================================")


def pivot(col):
    return inference_df.pivot(index="grid_row", columns="grid_col", values=col).values


print_matrix("PREDICTED 8x8 RAINFALL (mm/hr)", pivot("predicted_rain_mm"), "{:5.2f}")
print_matrix("PREDICTED 8x8 WATERLOGGING POSSIBILITY (%)", pivot("predicted_waterlog_pct"))
print_matrix("PREDICTED 8x8 SURFACE WIND (km/h)", pivot("predicted_wind_kmh"))

# ------------------------------------------------------------------------------
# 5. AGGREGATE PER BHULEKH REVENUE VILLAGE (MAX-RISK PROTOCOL)
# ------------------------------------------------------------------------------
print("\n==========================================================================")
print("     BHULEKH REVENUE VILLAGE AGROMET ASSESSMENTS & ADVISORIES             ")
print("==========================================================================")

village_groups = inference_df.groupby("village_mauza_en")

for v_en, group in village_groups:
    v_hi = group["village_mauza_hi"].iloc[0]
    cell_count = len(group)
    cell_ids = list(group["cell_id"])

    # Max-Risk Protocol: Max rain across any cell covering the village boundary
    peak_rain = float(group["predicted_rain_mm"].max())
    mean_rain = float(group["predicted_rain_mm"].mean())
    max_wind = float(group["predicted_wind_kmh"].max())
    max_waterlog = float(group["predicted_waterlog_pct"].max())
    zone_desc = group["zone_descriptor"].iloc[0]

    advisory = generate_farmer_advisory(
        village_name=f"{v_en} ({v_hi})",
        rain_mm=peak_rain,
        wind_kmh=max_wind,
        humidity_pct=float(live_weather.get("humidity_pct", 0.0)),
        cell_ids=cell_ids,
        zone_descriptor=zone_desc,
    )

    print(f"\n📍 Village Mauza: {v_en} | {v_hi} [Cells: {cell_count}]")
    print(f"   Peak Rain: {peak_rain:.2f} mm/hr | Areal Mean: {mean_rain:.2f} mm/hr | "
          f"Wind: {max_wind} km/h | Waterlog: {max_waterlog:.0f}%")
    print(f"   Alert Level: [{advisory['level']}]")
    print(f"   EN: {advisory['en']['headline']}")
    print(f"   HI: {advisory['hi']['headline']}")

# ------------------------------------------------------------------------------
# 6. LINK PREDICTIONS TO INDIVIDUAL BHULEKH KHASRA PLOTS & EXPORT
# ------------------------------------------------------------------------------
if bhulekh_path:
    df_khasra = pd.read_csv(bhulekh_path)

    # Merge downscaled cell predictions with each Khasra plot: a plot inherits
    # the predicted weather of the micro-cell it sits in.
    merged_khasra = df_khasra.merge(
        inference_df[[
            "cell_id", "predicted_rain_mm", "predicted_waterlog_pct",
            "predicted_wind_kmh", "humidity",
        ]],
        on="cell_id",
        how="left",
    )

    out_khasra_file = "latest_khasra_weather_forecast.csv"
    merged_khasra.to_csv(out_khasra_file, index=False, encoding="utf-8-sig")
    n_missing = int(merged_khasra["predicted_rain_mm"].isna().sum())
    if n_missing:
        print(f"WARNING: {n_missing} khasra plot(s) had no matching micro-cell prediction.")
    print(f"\nCadastral plot forecasts mapped to all Khasras -> '{out_khasra_file}'")
else:
    print("\nWARNING: no Bhulekh CSV found; skipping the per-khasra forecast export.")

out_inference = "latest_inference_output.csv"
inference_df.to_csv(out_inference, index=False, encoding="utf-8-sig")
print(f"Complete 64 micro-cell dataset saved to '{out_inference}'.")
