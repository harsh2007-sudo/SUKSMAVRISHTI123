"""Measured performance numbers for the security/performance pass."""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import base64

FILES = ["logo.png", "hero section background.png", "farmer panel background.png"]

print("--- BEFORE (raw files embedded as-is on every rerun) ---")
raw_total = 0
for f in FILES:
    b64 = base64.b64encode(open(f, "rb").read()).decode()
    raw_total += len(b64)
    print(f"{f:<34} {len(b64)/1024:8.0f} KB base64")
print(f"{'total per page load':<34} {raw_total/1024:8.0f} KB")

print("\n--- AFTER (resized + JPEG + cached data-URI) ---")
import streamlit as st
st.session_state["splash_done"] = True  # skip the splash when importing App.py
from App import get_image_src  # executes the real Streamlit script in bare mode

new_total = 0
t0 = time.perf_counter()
for f, kw in ((FILES[0], dict(max_width=260, fmt="png")),
              (FILES[1], dict(max_width=1600, quality=80)),
              (FILES[2], dict(max_width=1600, quality=80))):
    src = get_image_src(f, **kw)
    new_total += len(src)
    print(f"{f:<34} {len(src)/1024:8.0f} KB data-URI")
cold = time.perf_counter() - t0

t0 = time.perf_counter()
for f, kw in ((FILES[0], dict(max_width=260, fmt="png")),
              (FILES[1], dict(max_width=1600, quality=80)),
              (FILES[2], dict(max_width=1600, quality=80))):
    get_image_src(f, **kw)
warm = time.perf_counter() - t0

print(f"{'total per page load':<34} {new_total/1024:8.0f} KB")
print(f"payload reduction: {100 * (1 - new_total / raw_total):.1f}%")
print(f"image encode first run: {cold*1000:.0f} ms, cached rerun: {warm*1000:.1f} ms")
