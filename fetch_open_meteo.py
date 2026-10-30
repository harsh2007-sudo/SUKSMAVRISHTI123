# fetch_open_meteo.py
"""
SukSmaVrsti: live synoptic weather ingest from the Open-Meteo forecast API.

`fetch_synoptic_weather()` pulls the latest synoptic vector (rain, humidity,
wind, gusts, cloud, temperature, CAPE, pressure, radiation) for the Barabanki
micro-region, and `save_synoptic_weather_json()` persists it to
`latest_synoptic_weather.json`.

predict_grid.py reads that JSON before running the 64-cell downscaling, so the
JSON file is the single hand-off between the regional ingest and the model.
"""

import json
import math
import os
from datetime import datetime

import pandas as pd
import requests

# Target coordinates for Barabanki, Uttar Pradesh (District Center)
LATITUDE = 26.92
LONGITUDE = 81.18
TIMEZONE = "Asia/Kolkata"

# Where the latest synoptic vector is persisted for downstream scripts
SYNOPTIC_JSON_FILE = "latest_synoptic_weather.json"
SYNOPTIC_SCHEMA_VERSION = 1

# Synoptic variables requested from the Open-Meteo forecast endpoint
SYNOPTIC_HOURLY_VARS = [
    "precipitation",
    "relative_humidity_2m",
    "cape",
    "cloud_cover",
    "temperature_2m",
    "apparent_temperature",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "pressure_msl",
    "shortwave_radiation",
]

# Unit metadata stored alongside the vector in the JSON file
SYNOPTIC_UNITS = {
    "precip_coarse_mm": "mm/hr",
    "humidity_pct": "%",
    "cape_jkg": "J/kg",
    "cloud_cover_pct": "%",
    "temp_c": "degC",
    "apparent_temp_c": "degC",
    "wind_speed_kmh": "km/h",
    "wind_direction_deg": "degree",
    "wind_gust_kmh": "km/h",
    "pressure_hpa": "hPa",
    "shortwave_radiation_wm2": "W/m2",
}


def _f(value, default=0.0):
    """Safe float coercion: None / NaN / '' fall back to `default`."""
    try:
        if value is None:
            return float(default)
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return float(default)
        return f
    except (TypeError, ValueError):
        return float(default)


def fetch_synoptic_weather(lat=LATITUDE, lon=LONGITUDE, timeout=10):
    """
    Fetch the latest synoptic atmospheric vector from Open-Meteo.

    Returns a flat dict of variables (same keys as the legacy
    `get_live_regional_forcing`, plus gusts/pressure/radiation and
    provenance fields), or None when the API is unreachable.
    """
    endpoint = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": SYNOPTIC_HOURLY_VARS,
        "timezone": TIMEZONE,
        "forecast_days": 1,
    }

    try:
        response = requests.get(endpoint, params=params, timeout=timeout)
        response.raise_for_status()
        data = response.json()

        df = pd.DataFrame(data["hourly"])
        df["time"] = pd.to_datetime(df["time"])

        # Get the current hour's row
        df["time_diff"] = (df["time"] - pd.Timestamp.now()).abs()
        current_record = df.sort_values("time_diff").iloc[0].drop("time_diff")

        vector = {
            "timestamp": str(current_record["time"]),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "latitude": _f(data.get("latitude"), lat),
            "longitude": _f(data.get("longitude"), lon),
            "timezone": str(data.get("timezone", TIMEZONE)),
            "precip_coarse_mm": _f(current_record.get("precipitation"), 0.0),
            "humidity_pct": _f(current_record.get("relative_humidity_2m"), 0.0),
            "cape_jkg": _f(current_record.get("cape"), 0.0),
            "cloud_cover_pct": _f(current_record.get("cloud_cover"), 0.0),
            "temp_c": _f(current_record.get("temperature_2m"), 0.0),
            "apparent_temp_c": _f(current_record.get("apparent_temperature"), 0.0),
            "wind_speed_kmh": _f(current_record.get("wind_speed_10m"), 0.0),
            "wind_direction_deg": _f(current_record.get("wind_direction_10m"), 0.0),
            "wind_gust_kmh": _f(current_record.get("wind_gusts_10m"), 0.0),
            "pressure_hpa": _f(current_record.get("pressure_msl"), 1010.0),
            "shortwave_radiation_wm2": _f(current_record.get("shortwave_radiation"), 0.0),
        }
        return vector

    except requests.exceptions.RequestException as e:
        print(f"Error fetching live Open-Meteo data: {e}")
        return None


def save_synoptic_weather_json(vector=None, path=SYNOPTIC_JSON_FILE,
                               lat=LATITUDE, lon=LONGITUDE):
    """
    Persist the latest synoptic vector as pretty-printed JSON.

    Writes atomically (tmp file + rename) so predict_grid.py never reads a
    half-written file. Returns the path on success, None on failure; an
    existing JSON file is left untouched when the fetch fails.
    """
    if vector is None:
        vector = fetch_synoptic_weather(lat, lon)
    if not vector:
        return None

    payload = {
        "schema_version": SYNOPTIC_SCHEMA_VERSION,
        "product": "SukSmaVrsti synoptic ingest",
        "source": "open-meteo.com forecast API",
        "fetched_at": vector.get("fetched_at"),
        "valid_hour": vector.get("timestamp"),
        "coordinates": {
            "latitude": vector.get("latitude", lat),
            "longitude": vector.get("longitude", lon),
            "timezone": vector.get("timezone", TIMEZONE),
        },
        "units": SYNOPTIC_UNITS,
        "synoptic": vector,
    }

    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_path, path)
        return path
    except OSError as e:
        print(f"Error writing synoptic JSON to '{path}': {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return None


def load_synoptic_json(path=SYNOPTIC_JSON_FILE, max_age_hours=6.0):
    """
    Read the saved synoptic JSON.

    Returns the `synoptic` vector dict, or None when the file is missing,
    unreadable, malformed, or older than `max_age_hours` (pass None to accept
    any age, e.g. as an offline fallback).
    """
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"Could not read synoptic JSON '{path}': {e}")
        return None

    if not isinstance(payload, dict):
        return None
    vector = payload.get("synoptic")
    if not isinstance(vector, dict):
        return None

    if max_age_hours is not None:
        fetched_at = payload.get("fetched_at")
        if fetched_at:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(fetched_at)).total_seconds() / 3600.0
                if age_h > max_age_hours:
                    return None
            except ValueError:
                pass  # unparseable timestamp: accept the file rather than discard data

    return vector


def get_live_regional_forcing(lat=LATITUDE, lon=LONGITUDE):
    """
    Legacy accessor used by downstream scripts: returns the current hour's
    coarse regional atmospheric vector directly into RAM.
    """
    return fetch_synoptic_weather(lat, lon)


def download_historical_training_data(
    start_date="2024-07-01",
    end_date="2024-08-31",
    output_csv="barabanki_training_weather.csv",
    lat=LATITUDE,
    lon=LONGITUDE
):
    """
    Downloads historical ERA5 reanalysis data across the monsoon window
    and saves to a local CSV file for model fitting.
    """
    endpoint = "https://archive-api.open-meteo.com/v1/archive"

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": [
            "precipitation",
            "relative_humidity_2m",
            "cloud_cover",
            "temperature_2m",
            "wind_speed_10m",
            "wind_direction_10m"
        ],
        "timezone": TIMEZONE
    }

    print(f"Downloading historical data from {start_date} to {end_date}...")
    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        df_history = pd.DataFrame(data["hourly"])
        df_history["time"] = pd.to_datetime(df_history["time"])

        # Fill missing values
        df_history = df_history.fillna(0.0)

        # Save to CSV
        df_history.to_csv(output_csv, index=False)
        print(f"Successfully saved {len(df_history)} hourly records to '{output_csv}'")
        return df_history

    except requests.exceptions.RequestException as e:
        print(f"Error downloading historical archive: {e}")
        return None


# ==========================================
# EXECUTION DEMO
# ==========================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch the latest synoptic weather as JSON.")
    parser.add_argument("--download", action="store_true",
                        help="Also re-download the historical training sample (one-time setup).")
    parser.add_argument("--output", default=SYNOPTIC_JSON_FILE,
                        help=f"Output JSON path (default: {SYNOPTIC_JSON_FILE})")
    args = parser.parse_args()

    print("--- 1. FETCHING LATEST SYNOPTIC WEATHER ---")
    live_vector = fetch_synoptic_weather()
    if live_vector:
        print("Latest synoptic vector:")
        for k, v in live_vector.items():
            print(f"  • {k}: {v}")
        out = save_synoptic_weather_json(live_vector, path=args.output)
        if out:
            print(f"\nSaved latest synoptic weather -> '{out}'")
    else:
        print("Fetch failed; any existing JSON file was left untouched.")

    if args.download:
        print("\n--- 2. DOWNLOADING TRAINING SET (One-time Setup) ---")
        # Downloads a 2-week monsoon sample (July)
        download_historical_training_data(
            start_date="2024-07-01",
            end_date="2024-07-15",
            output_csv="barabanki_historical_sample.csv"
        )
