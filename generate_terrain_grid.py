# generate_terrain_grid.py
"""
SūkṣmaVṛṣṭi: Hyperlocal Micro-Terrain Generator
Reads official Bhulekh revenue villages/cells and computes physical micro-terrain
variables (Elevation, Slope, Aspect, TWI, Roughness Length z0, Thermal Perturbation,
Canopy Transpiration Factor, and Convective Updraft Distance).
"""

import os
import numpy as np
import pandas as pd

# Base regional topography parameters for Barabanki, UP
BASE_ELEV_M = 124.5  # Mean alluvial elevation in Barabanki
BASE_LAT = 26.9200
BASE_LON = 81.1800
GRID_SIZE = 8

np.random.seed(42)

# Identify the Bhulekh Khasra dataset
BHULEKH_FILES = [
    "bhulekh_khasra_grid_mapping_demo_names.csv",
    "bhulekh_khasra_grid_mapping_2.csv",
    "bhulekh_khasra_grid_mapping.csv"
]

bhulekh_path = None
for f in BHULEKH_FILES:
    if os.path.exists(f):
        bhulekh_path = f
        break

if not bhulekh_path:
    raise FileNotFoundError(
        "Could not find Bhulekh CSV file! Please place 'bhulekh_khasra_grid_mapping_demo_names.csv' in this folder."
    )

print(f"Ingesting cadastral village boundaries from: {bhulekh_path}")
df_bhulekh = pd.read_csv(bhulekh_path)

# Aggregate plot-level Bhulekh records into 64 unique micro-cells (1 km x 1 km)
khasra_col = "khasra_gata_number" if "khasra_gata_number" in df_bhulekh.columns else "khasra_number"
v_en_col = "village_mauza_en" if "village_mauza_en" in df_bhulekh.columns else "village_mauza"
v_hi_col = "village_mauza_hi" if "village_mauza_hi" in df_bhulekh.columns else "village_mauza"

cell_grouped = df_bhulekh.groupby("cell_id").agg({
    "grid_row": "first",
    "grid_col": "first",
    v_en_col: "first",
    v_hi_col: "first",
    "village_lgd_code": "first" if "village_lgd_code" in df_bhulekh.columns else lambda s: "000000",
    "tehsil": "first" if "tehsil" in df_bhulekh.columns else lambda s: "Nawabganj",
    "zone_descriptor": "first",
    "land_classification_shreni": (
        lambda s: s.mode().iloc[0] if not s.empty else "1-क"
    ) if "land_classification_shreni" in df_bhulekh.columns else lambda s: "1-क",
    "primary_crop": lambda s: list(s.dropna().unique()),
    "irrigation_source": (
        lambda s: list(s.dropna().unique())
    ) if "irrigation_source" in df_bhulekh.columns else lambda s: ["Canal"],
    "latitude": "mean",
    "longitude": "mean",
    khasra_col: lambda s: list(s.unique()),
    "plot_area_ha": "sum" if "plot_area_ha" in df_bhulekh.columns else lambda s: 1.0
}).reset_index()

terrain_records = []

for _, c in cell_grouped.iterrows():
    cell_id = c["cell_id"]
    r = int(c["grid_row"])
    col = int(c["grid_col"])
    v_en = c[v_en_col]
    v_hi = c[v_hi_col]
    lgd = c["village_lgd_code"]
    tehsil = c["tehsil"]
    zone_desc = c["zone_descriptor"]
    shreni = str(c["land_classification_shreni"])
    crops = c["primary_crop"]
    khasras = c[khasra_col]
    lat = round(float(c["latitude"]), 5)
    lon = round(float(c["longitude"]), 5)

    # 1. Elevation Profile: Slight northern/riverward alluvial gradient with micro-ridges
    elev = round(BASE_ELEV_M + (r * 0.95) - (col * 0.70) + np.random.normal(0, 0.45), 2)

    # 2. Slope Gradient (Degrees): Flatter in floodplains (0.3° - 1.2°), higher on embankments/crests
    if "Crest" in zone_desc or "Teela" in zone_desc:
        slope = round(np.random.uniform(2.4, 4.2), 2)
    elif "Drainage" in zone_desc or "Kachar" in zone_desc or "Basin" in zone_desc:
        slope = round(np.random.uniform(0.2, 0.8), 2)
    else:
        slope = round(np.random.uniform(0.6, 1.8), 2)

    # 3. Slope Aspect (0° - 360°): Determines face orientation against monsoon winds (SE to NW)
    aspect = round((r * 45.0 + col * 30.0 + np.random.uniform(10, 40)) % 360.0, 1)

    # 4. Topographic Wetness Index (TWI): High in drainage lines and riverine basins
    # TWI = ln(a / tan(beta)). Higher values indicate higher water accumulation & pooling
    base_twi = 12.0 - (slope * 1.35)
    if "Basin" in zone_desc or "Drainage" in zone_desc or "Kachar" in zone_desc:
        base_twi += 2.2
    twi = round(max(4.0, min(14.5, base_twi + np.random.normal(0, 0.3))), 2)

    # 5. Aerodynamic Roughness Length z0 (meters):
    # Settlements/Abadi ~ 0.8-1.2 m, Sugarcane/Orchards ~ 0.25 m, Wheat/Paddy ~ 0.08 m, Fallow ~ 0.03 m
    if "6-1" in shreni or "Settlement" in zone_desc or "Abadi" in zone_desc or "Mandi" in zone_desc:
        roughness_z0 = round(np.random.uniform(0.75, 1.15), 3)
    elif any("Sugarcane" in cr for cr in crops):
        roughness_z0 = round(np.random.uniform(0.22, 0.35), 3)
    else:
        roughness_z0 = round(np.random.uniform(0.06, 0.14), 3)

    # 6. Thermal Anomaly Delta T (°C):
    # Built/dry threshing grounds (Khaliyan) heat up rapidly causing localized convective thermal updrafts.
    # Irrigated fields exhibit evaporative cooling.
    if "6-1" in shreni or "Khaliyan" in zone_desc or "Mandi" in zone_desc:
        temp_anomaly = round(np.random.uniform(1.2, 2.4), 2)  # Hot pocket
    elif "Nahar" in zone_desc or "Paddy" in str(crops) or "Canal" in str(c["irrigation_source"]):
        temp_anomaly = round(np.random.uniform(-1.2, -0.2), 2)  # Evaporative cooled pocket
    else:
        temp_anomaly = round(np.random.uniform(-0.1, 0.8), 2)

    # 7. Canopy Transpiration / Latent Heat Moisture Index:
    # High leaf area index in Sugarcane/Paddy supplies moisture to boundary layer
    if any("Paddy" in cr or "Sugarcane" in cr for cr in crops):
        moisture_feedback = round(np.random.uniform(1.25, 1.55), 2)
    else:
        moisture_feedback = round(np.random.uniform(0.85, 1.10), 2)

    # 8. Distance to Local Convective Initiation Center (km):
    # Localized micro-convective cells often initiate near the highway/urban interface (row 5, col 5)
    dist_updraft = round(np.sqrt((r - 4.5)**2 + (col - 4.5)**2), 2)

    terrain_records.append({
        "cell_id": cell_id,
        "grid_row": r,
        "grid_col": col,
        "latitude": lat,
        "longitude": lon,
        "village_mauza_en": v_en,
        "village_mauza_hi": v_hi,
        "village_lgd_code": lgd,
        "tehsil": tehsil,
        "zone_descriptor": zone_desc,
        "land_classification_shreni": shreni,
        "elevation_m": elev,
        "slope_deg": slope,
        "aspect_deg": aspect,
        "twi": twi,
        "roughness_z0": roughness_z0,
        "temp_anomaly_c": temp_anomaly,
        "moisture_feedback": moisture_feedback,
        "dist_updraft_km": dist_updraft,
        "khasra_count": len(khasras),
        "sample_khasras": ", ".join([str(k) for k in khasras[:3]])
    })

df_terrain = pd.DataFrame(terrain_records)
out_csv = "terrain_grid_8x8.csv"
df_terrain.to_csv(out_csv, index=False, encoding="utf-8-sig")

print(f"\nSuccessfully generated 8x8 micro-terrain grid ({len(df_terrain)} cells) -> '{out_csv}'")
print("\nUnique Bhulekh Villages represented in the terrain model:")
print(df_terrain.groupby(["village_mauza_en", "village_mauza_hi"])["cell_id"].count())