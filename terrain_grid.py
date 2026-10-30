# terrain_grid.py
"""
SukSmaVrsti: real micro-terrain loader for the 64 Bhulekh micro-cells.

Reads the real NASA-DEM micro-terrain file (one row per 1 km x 1 km cell,
carrying elevation, slope, aspect, TWI, roughness, thermal anomaly, moisture
feedback, updraft distance, drainage density and soil saturation) and writes
`terrain_grid_8x8.csv`, the terrain table consumed by train_xgboost.py and
predict_grid.py.

Every cell is validated against the Bhulekh cadastral records before export:
the cell must exist in the Bhulekh mapping, its plot count must match, and
every sample khasra number listed for the cell must belong to that same cell.
The joined khasra list is written out as `khasra_numbers`, so downstream
scripts can map terrain variables to a cell *and* its khasra numbers directly.

Usage:
    python terrain_grid.py                      # default source file
    python terrain_grid.py --source path/to.csv # explicit micro-terrain file
    python terrain_grid.py --check              # validate only, write nothing
"""

import argparse
import os
import sys

import pandas as pd

# Default source: the real NASA-DEM micro-terrain export (project copy first,
# then the original download location).
DEFAULT_SOURCE_NAME = "nasadem_real_micro_terrain_64_cells.csv"
DEFAULT_SOURCE_FALLBACK = r"C:\Users\Dell\Downloads\nasadem_real_micro_terrain_64_cells.csv"
OUTPUT_FILE = "terrain_grid_8x8.csv"

BHULEKH_FILES = [
    "bhulekh_khasra_grid_mapping_demo_names.csv",
    "bhulekh_khasra_grid_mapping_2.csv",
    "bhulekh_khasra_grid_mapping.csv",
]

# Columns train_xgboost.py / predict_grid.py rely on
REQUIRED_TERRAIN_COLS = [
    "cell_id", "grid_row", "grid_col", "latitude", "longitude",
    "village_mauza_en", "village_mauza_hi", "zone_descriptor",
    "elevation_m", "slope_deg", "aspect_deg", "twi", "roughness_z0",
    "temp_anomaly_c", "moisture_feedback", "dist_updraft_km",
    "drainage_density_km_km2", "soil_saturation_cap_pct",
    "sample_khasra_numbers", "khasra_plots_count",
]

GRID_SIZE = 8


def resolve_source(path=None):
    """Pick the micro-terrain source file: explicit arg > project copy > Downloads."""
    candidates = [path] if path else [DEFAULT_SOURCE_NAME, DEFAULT_SOURCE_FALLBACK]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return None


def find_bhulekh(explicit=None):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    return next((f for f in BHULEKH_FILES if os.path.exists(f)), None)


def norm_khasra(value):
    """Normalised khasra key for membership tests (' 21KHA ' -> '21kha')."""
    return str(value).strip().lower()


def validate_and_join(df_terrain, df_bhulekh):
    """
    Validate the 64-cell grid and attach each cell's Bhulekh khasra list.

    Returns (errors, warnings). Adds a `khasra_numbers` column to
    `df_terrain` (comma-separated khasra numbers of that cell, in plot order).
    """
    errors, warnings = [], []

    missing_cols = [c for c in REQUIRED_TERRAIN_COLS if c not in df_terrain.columns]
    if missing_cols:
        errors.append(f"Terrain file is missing required columns: {', '.join(missing_cols)}")
        return errors, warnings

    if len(df_terrain) != GRID_SIZE * GRID_SIZE:
        errors.append(f"Expected {GRID_SIZE * GRID_SIZE} micro-cells, found {len(df_terrain)}")

    # cell_id must match grid_row/grid_col and the 8x8 lattice must be complete
    seen_positions = set()
    for _, row in df_terrain.iterrows():
        r, c = int(row["grid_row"]), int(row["grid_col"])
        expected_id = f"cell_{r}_{c}"
        if str(row["cell_id"]) != expected_id:
            errors.append(f"cell_id '{row['cell_id']}' does not match grid position [{r},{c}]")
        if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
            errors.append(f"Cell '{row['cell_id']}' is outside the {GRID_SIZE}x{GRID_SIZE} lattice")
        if (r, c) in seen_positions:
            errors.append(f"Duplicate grid position [{r},{c}]")
        seen_positions.add((r, c))

    expected_positions = {(r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE)}
    for r, c in sorted(expected_positions - seen_positions):
        errors.append(f"Missing cell at grid position [{r},{c}] (cell_{r}_{c})")

    # --- Bhulekh cross-check: cells, plot counts and sample khasra numbers ---
    khasra_col = "khasra_gata_number" if "khasra_gata_number" in df_bhulekh.columns else "khasra_number"
    terrain_ids = set(df_terrain["cell_id"].astype(str))

    bhulekh_cells = set(df_bhulekh["cell_id"].astype(str))
    orphan_cells = sorted(bhulekh_cells - terrain_ids)
    if orphan_cells:
        errors.append(f"Bhulekh cells absent from the terrain file: {', '.join(orphan_cells)}")

    khasra_lists = {}
    for cell_id, grp in df_bhulekh.groupby(df_bhulekh["cell_id"].astype(str)):
        plot_khasras = [str(k).strip() for k in grp[khasra_col] if str(k).strip()]
        khasra_lists[cell_id] = plot_khasras

        match = df_terrain[df_terrain["cell_id"].astype(str) == cell_id]
        if match.empty:
            continue  # already reported as an orphan cell above
        trow = match.iloc[0]

        declared = int(trow["khasra_plots_count"])
        if declared != len(plot_khasras):
            errors.append(
                f"{cell_id}: terrain file declares {declared} khasra plots, "
                f"Bhulekh has {len(plot_khasras)}"
            )

        have = {norm_khasra(k) for k in plot_khasras}
        samples = [s.strip() for s in str(trow["sample_khasra_numbers"]).split(",") if s.strip()]
        missing_samples = [s for s in samples if norm_khasra(s) not in have]
        if missing_samples:
            errors.append(
                f"{cell_id}: sample khasra number(s) {', '.join(missing_samples)} "
                f"not found in the Bhulekh records for this cell"
            )

    if not errors:
        df_terrain["khasra_numbers"] = df_terrain["cell_id"].astype(str).map(
            lambda cid: ", ".join(khasra_lists.get(cid, []))
        )
        empty = (df_terrain["khasra_numbers"] == "").sum()
        if empty:
            warnings.append(f"{empty} cell(s) have no Bhulekh khasra plots mapped")

    return errors, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Load real micro-terrain variables per cell/khasra and write terrain_grid_8x8.csv."
    )
    parser.add_argument("--source", default=None,
                        help=f"Micro-terrain CSV (default: {DEFAULT_SOURCE_NAME}, "
                             f"falling back to {DEFAULT_SOURCE_FALLBACK})")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Output terrain grid CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--bhulekh", default=None,
                        help="Bhulekh khasra mapping CSV (default: auto-detect)")
    parser.add_argument("--check", action="store_true",
                        help="Validate only; do not write the output file")
    args = parser.parse_args(argv)

    source = resolve_source(args.source)
    if not source:
        print(f"ERROR: micro-terrain source file not found "
              f"(tried: {', '.join([args.source, DEFAULT_SOURCE_NAME, DEFAULT_SOURCE_FALLBACK] if not args.source else [args.source])})")
        return 1

    bhulekh_path = find_bhulekh(args.bhulekh)
    if not bhulekh_path:
        print("ERROR: no Bhulekh khasra mapping CSV found (use --bhulekh to point at one).")
        return 1

    print(f"Loading real micro-terrain from : {source}")
    print(f"Cross-checking Bhulekh records  : {bhulekh_path}")

    try:
        df_terrain = pd.read_csv(source)
        df_bhulekh = pd.read_csv(bhulekh_path)
    except (OSError, ValueError) as e:
        print(f"ERROR: could not read input CSV: {e}")
        return 1

    errors, warnings = validate_and_join(df_terrain, df_bhulekh)

    for w in warnings:
        print(f"WARNING: {w}")
    if errors:
        print(f"\nVALIDATION FAILED ({len(errors)} problem(s)):")
        for e in errors:
            print(f"  • {e}")
        return 1

    n_cells = len(df_terrain)
    n_plots = int(df_terrain["khasra_plots_count"].sum())
    villages = df_terrain["village_mauza_en"].nunique()
    print(f"\nValidation OK: {n_cells} micro-cells, {n_plots} khasra plots, {villages} villages")
    print(f"Elevation {df_terrain['elevation_m'].min():.2f}-{df_terrain['elevation_m'].max():.2f} m | "
          f"slope {df_terrain['slope_deg'].min():.2f}-{df_terrain['slope_deg'].max():.2f} deg | "
          f"TWI {df_terrain['twi'].min():.2f}-{df_terrain['twi'].max():.2f}")

    if args.check:
        print("--check given: no file written.")
        return 0

    df_terrain.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"Micro-terrain grid written -> '{args.output}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
