import os
import re
import csv
import io
import time
import json
import base64
from datetime import datetime
from urllib.parse import quote
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import security
import farmer_auth
import chief_auth

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="SUKSMAVRISHTI | सूक्ष्मवृष्टि",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# -----------------------------------------------------------------------------
# INTEGRATION WITH PREDICT_GRID & ADVISORY ENGINE
# -----------------------------------------------------------------------------
FORECAST_FILE = "latest_khasra_weather_forecast.csv"
INFERENCE_FILE = "latest_inference_output.csv"


@st.cache_data(show_spinner=False, max_entries=6)
def _load_forecast_lookup(path, mtime):
    """Parse a forecast CSV once per file change into khasra / cell lookups."""
    df = pd.read_csv(path)
    by_khasra, by_cell = {}, {}
    rain = pd.to_numeric(df.get("predicted_rain_mm"), errors="coerce")
    wind = pd.to_numeric(df.get("predicted_wind_kmh"), errors="coerce")
    hum = pd.to_numeric(df.get("humidity"), errors="coerce") if "humidity" in df.columns else None

    if "khasra_gata_number" in df.columns:
        for i, kh in enumerate(df["khasra_gata_number"]):
            key = str(kh).strip().lower()
            if not key or key in by_khasra or pd.isna(rain.iloc[i]):
                continue
            h = None if hum is None or pd.isna(hum.iloc[i]) else float(hum.iloc[i])
            w = 0.0 if pd.isna(wind.iloc[i]) else float(wind.iloc[i])
            by_khasra[key] = (float(rain.iloc[i]), w, h)

    if "cell_id" in df.columns:
        for i, cid in enumerate(df["cell_id"]):
            cid = str(cid)
            if cid in by_cell or pd.isna(rain.iloc[i]):
                continue
            h = None if hum is None or pd.isna(hum.iloc[i]) else float(hum.iloc[i])
            w = 0.0 if pd.isna(wind.iloc[i]) else float(wind.iloc[i])
            by_cell[cid] = (float(rain.iloc[i]), w, h)

    return {"khasra": by_khasra, "cell": by_cell}


def _cell_humidity(cell_id: str):
    """Regional/cell humidity from the inference output (khasra file has none)."""
    try:
        mtime = os.path.getmtime(INFERENCE_FILE)
    except OSError:
        return None
    try:
        lookup = _load_forecast_lookup(INFERENCE_FILE, mtime)
    except Exception:
        return None
    hit = lookup["cell"].get(str(cell_id))
    return None if hit is None else hit[2]


def get_live_prediction_for_cell(cell_id: str, village_name: str = "Rampur", khasra_no: str = None):
    """Return (rain, wind, humidity, source_file, updated_at) for a cell/khasra.

    Values come only from the configured model output files. When no dataset row
    exists we return (None, None, None, "", "") so the UI can say the data is
    unavailable instead of inventing a rainfall figure.
    """
    for path in (FORECAST_FILE, INFERENCE_FILE):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        try:
            lookup = _load_forecast_lookup(path, mtime)
        except Exception as exc:
            security.audit("forecast_data_read_failed", outcome="error",
                           detail={"file": path, "error": str(exc)})
            continue
        updated = datetime.fromtimestamp(mtime).strftime("%d %b %Y %H:%M")
        if khasra_no:
            hit = lookup["khasra"].get(str(khasra_no).strip().lower())
            if hit:
                humidity = hit[2] if hit[2] is not None else _cell_humidity(cell_id)
                return hit[0], hit[1], humidity, path, updated
        hit = lookup["cell"].get(str(cell_id))
        if hit:
            humidity = hit[2] if hit[2] is not None else _cell_humidity(cell_id)
            return hit[0], hit[1], humidity, path, updated

    return None, None, None, "", ""


try:
    from agromet_advisory_engine import generate_farmer_advisory
except ImportError:
    def generate_farmer_advisory(village_name, rain_mm, wind_kmh, humidity_pct, khasra_no=None, zone_descriptor=None):
        if rain_mm >= 3.0:
            level = "RED"
            en_head = f"🚨 URGENT: Heavy convective downpour ({rain_mm:.1f} mm/hr) expected within 40 mins over {village_name}!"
            en_actions = [
                "Cover open grain threshing floors with plastic tarpaulins immediately.",
                "Suspend combine harvesting and field reaping to prevent grain lodging.",
                "Avoid transit of harvested produce in uncovered tractor trolleys."
            ]
            hi_head = f"🚨 आपातकालीन चेतावनी: अगले 40 मिनट में {village_name} में भारी वर्षा ({rain_mm:.1f} मिमी/घंटा) की प्रबल आशंका!"
            hi_actions = [
                "खलिहान में कटी रखी फसल व अनाज के ढेरों को तत्काल मजबूत तिरपाल से ढंकें।",
                "खेतों में कंबाइन हार्वेस्टर और कटाई का कार्य तुरंत बंद कर दें।",
                "खुली ट्रॉलियों में अनाज भरकर मंडी ले जाने का जोखिम न लें।"
            ]
        elif rain_mm >= 1.0:
            level = "AMBER"
            en_head = f"⚠️ ADVISORY: Moderate rainfall ({rain_mm:.1f} mm/hr) forecasted over {village_name}."
            en_actions = [
                "Postpone foliar pesticide and urea top-dressing for at least 8 hours.",
                "Halt scheduled tube-well irrigation to avoid localized root saturation.",
                "Ensure drainage channels around fields and bunds are unclogged."
            ]
            hi_head = f"⚠️ कृषि चेतावनी: {village_name} में मध्यम वर्षा ({rain_mm:.1f} मिमी/घंटा) का अनुमान।"
            hi_actions = [
                "कीटनाशक, पोटाश व यूरिया का छिड़काव अगले 8 घंटे तक टाल दें।",
                "खेतों में नलकूप से सिंचाई का कार्य आज स्थगित रखें।",
                "खेत की मेड़ों और जल निकासी नालियों से रुकावटें हटाएं।"
            ]
        else:
            level = "GREEN"
            en_head = f"☀️ SAFE: Clear and favorable weather conditions across {village_name}."
            en_actions = [
                "Optimal conditions for outdoor crop drying and threshing operations.",
                "Safe window for nutrient and crop protection spray application.",
                "Clear conditions for transportation and grain storage in local mandis."
            ]
            hi_head = f"☀️ मौसम अनुकूल: {village_name} में मौसम पूरी तरह साफ और शुष्क है।"
            hi_actions = [
                "कटी फसल को धूप में सुखाने और खलिहान मड़ाई हेतु उपयुक्त समय है।",
                "पोषक तत्वों एवं आवश्यक कीटनाशक छिड़काव के लिए सुरक्षित अवधि।",
                "मंडी में अनाज परिवहन व भंडारण के लिए परिस्थितियां सर्वथा अनुकूल हैं।"
            ]
        return {
            "village": village_name,
            "khasra_no": khasra_no,
            "level": level,
            "rain_mm": rain_mm,
            "wind_kmh": wind_kmh,
            "humidity_pct": humidity_pct,
            "en": {"headline": en_head, "actions": en_actions},
            "hi": {"headline": hi_head, "actions": hi_actions}
        }

# -----------------------------------------------------------------------------
# IMAGE HELPER (resized + cached: the raw assets are 2-3 MB each, so embedding
# them at full size on every rerun was the main cause of the page lag)
# -----------------------------------------------------------------------------
def get_image_src(image_path, max_width=1600, fmt="jpeg", quality=82):
    """Return a small, cached data-URI for an image, or '' when unavailable."""
    try:
        mtime = os.path.getmtime(image_path)
    except OSError:
        return ""
    return _render_image_src(image_path, mtime, max_width, fmt, quality)


@st.cache_data(show_spinner=False, max_entries=8)
def _render_image_src(image_path, mtime, max_width, fmt, quality):
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            img.load()
            if img.width > max_width:
                ratio = max_width / float(img.width)
                img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
            buf = io.BytesIO()
            if fmt == "png":
                if img.mode not in ("RGBA", "LA", "P"):
                    img = img.convert("RGBA")
                img.save(buf, format="PNG", optimize=True)
            else:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        mime = "image/png" if fmt == "png" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def get_base64_image(image_path, max_width=1600, fmt="jpeg"):
    """Backwards-compatible helper: data-URI prefix stripped."""
    src = get_image_src(image_path, max_width=max_width, fmt=fmt)
    return src.split(",", 1)[1] if "," in src else ""

LOGO_FILE = "logo.png"
if not os.path.exists(LOGO_FILE):
    for candidate in os.listdir("."):
        if candidate.lower().endswith((".png", ".jpg", ".jpeg")) and ("logo" in candidate.lower() or "gemini" in candidate.lower() or "input_file" in candidate.lower()):
            LOGO_FILE = candidate
            break

# -----------------------------------------------------------------------------
# SESSION STATE INITIALIZATION
# -----------------------------------------------------------------------------
if "splash_done" not in st.session_state:
    st.session_state.splash_done = False

if "current_page" not in st.session_state:
    st.session_state.current_page = "welcome"

if "language" not in st.session_state:
    st.session_state.language = "English"

# First-run flow: language -> role (farmer / sarpanch) -> that role's home page
if "lang_chosen" not in st.session_state:
    st.session_state.lang_chosen = False
if "role" not in st.session_state:
    st.session_state.role = None

# Voice assistant session flags (UI only)
if "sv_voice_langgate" not in st.session_state:
    st.session_state.sv_voice_langgate = False
if "sv_voice_role_lang" not in st.session_state:
    st.session_state.sv_voice_role_lang = ""
if "sv_voice_home_lang" not in st.session_state:
    st.session_state.sv_voice_home_lang = ""
if "sv_voice_chief_lang" not in st.session_state:
    st.session_state.sv_voice_chief_lang = ""
if "sv_voice_fc_lang" not in st.session_state:
    st.session_state.sv_voice_fc_lang = ""
if "sv_voice_fc_pending" not in st.session_state:
    st.session_state.sv_voice_fc_pending = False

WHATSAPP_NUMBER = "917303190089"

# Splash screen duration. It used to block the script for a full 5 seconds on
# every new session, which made the whole site feel frozen.
_SPLASH_SECONDS = max(0.0, min(5.0, float(os.getenv("SV_SPLASH_SECONDS", "1.2"))))

# -----------------------------------------------------------------------------
# 1. 5-SECOND SPLASH SCREEN (CODE-BLOCK FORMATTING BUG FIXED)
# -----------------------------------------------------------------------------
if not st.session_state.splash_done:
    splash_placeholder = st.empty()
    logo_src = get_image_src(LOGO_FILE, max_width=260, fmt="png")

    if logo_src:
        img_html = f'''<div class="logo-circle-container"><img src="{logo_src}" class="splash-logo-circle" alt="Suksmavrishti Logo" decoding="async"></div>'''
    else:
        img_html = '''<div class="splash-icon">🌾</div>'''

    with splash_placeholder.container():
        # Notice: Unindented string to ensure Streamlit Markdown never treats it as a code block
        st.markdown(
f"""<style>
.splash-wrapper {{
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 82vh;
    text-align: center;
    animation: fadeIn 0.8s ease-in-out;
}}
.logo-circle-container {{
    width: 240px;
    height: 240px;
    flex: 0 0 auto;
    border-radius: 50%;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 4px solid #2e7d32;
    box-shadow: 0 0 35px rgba(46, 125, 50, 0.45);
    margin: 0 auto 25px auto;
    background-color: #04141f;
    padding: 6px;
    box-sizing: border-box;
}}
.splash-logo-circle {{
    width: 100%;
    height: 100%;
    object-fit: contain;
    object-position: center center;
    display: block;
}}
.splash-icon {{
    font-size: 110px;
    margin-bottom: 20px;
}}
.splash-title {{
    font-size: 3.4rem;
    font-weight: 900;
    letter-spacing: 5px;
    color: #4ade80 !important;
    text-shadow: 0 2px 14px rgba(74, 222, 128, 0.35);
    margin: 0 0 10px 0;
    text-transform: uppercase;
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
}}
.splash-slogan-hindi {{
    font-size: 1.85rem;
    font-weight: 700;
    color: #facc15 !important;
    letter-spacing: 1.5px;
    margin-bottom: 25px;
    text-shadow: 0 2px 10px rgba(250, 204, 21, 0.4);
}}
.splash-loader {{
    width: 240px;
    height: 6px;
    background-color: rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 10px;
}}
.splash-loader-bar {{
    width: 100%;
    height: 100%;
    background: linear-gradient(90deg, #4ade80, #facc15);
    animation: loadProgress {_SPLASH_SECONDS}s linear forwards;
}}
@keyframes loadProgress {{
    from {{ width: 0%; }}
    to {{ width: 100%; }}
}}
@keyframes fadeIn {{
    from {{ opacity: 0; transform: scale(0.97); }}
    to {{ opacity: 1; transform: scale(1); }}
}}
/* Splash gets the same dark canvas as the app (the main CSS loads after it) */
.stApp:has(.splash-wrapper) {{
    background: radial-gradient(1200px 700px at 85% -10%, #01293c 0%, #001421 48%, #000f18 100%) fixed !important;
}}
header[data-testid="stHeader"] {{
    display: none !important;
}}
@media (max-width: 560px) {{
    .splash-wrapper {{ min-height: 72vh; padding: 0 16px; }}
    .logo-circle-container {{ width: 170px; height: 170px; padding: 5px; margin-bottom: 18px; }}
    .splash-title {{ font-size: 2rem; letter-spacing: 2.5px; }}
    .splash-slogan-hindi {{ font-size: 1.2rem; margin-bottom: 18px; }}
    .splash-loader {{ width: 190px; }}
}}
</style>
<div class="splash-wrapper">
    {img_html}
    <div class="splash-title">SUKSMAVRISHTI</div>
    <div class="splash-slogan-hindi">सर्वप्रयत्नेन धान्यसंरक्षणम्</div>
    <div class="splash-loader">
        <div class="splash-loader-bar"></div>
    </div>
</div>""",
            unsafe_allow_html=True
        )
        time.sleep(_SPLASH_SECONDS)
        st.session_state.splash_done = True
        splash_placeholder.empty()
        st.rerun()

# -----------------------------------------------------------------------------
# FRONTEND DESIGN SYSTEM (STYLES ONLY - NO BACKEND LOGIC)
# -----------------------------------------------------------------------------
HERO_FILE = "hero section background.png"
if not os.path.exists(HERO_FILE):
    for candidate in sorted(os.listdir(".")):
        if candidate.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and ("hero" in candidate.lower() or "background" in candidate.lower()):
            HERO_FILE = candidate
            break

# Farmer panel background: any image in the project folder whose name has "farmer" in it
FARMER_FILE = None
for candidate in sorted(os.listdir(".")):
    low = candidate.lower()
    if low.endswith((".png", ".jpg", ".jpeg", ".webp")) and "farmer" in low:
        FARMER_FILE = candidate
        break
if FARMER_FILE is None:
    FARMER_FILE = HERO_FILE  # graceful fallback until the farmer image is added

# Sarpanch weather-verification photo uploads (used to retrain the forecast model)
UPLOAD_DIR = "training_uploads"
FEEDBACK_CSV = os.path.join(UPLOAD_DIR, "weather_feedback_log.csv")


@st.cache_data(show_spinner=False, ttl=10)
def load_feedback_log(path, mtime):
    """Read the chief feedback log once per file change (not on every rerun)."""
    if not os.path.exists(path):
        return None
    try:
        return pd.read_csv(path)
    except Exception:
        return None


def feedback_for_chief(chief_id):
    """Only this chief's submissions (prevents cross-area data exposure)."""
    try:
        mtime = os.path.getmtime(FEEDBACK_CSV)
    except OSError:
        return None
    df = load_feedback_log(FEEDBACK_CSV, mtime)
    if df is None or getattr(df, "empty", True) or "uploaded_by" not in df.columns:
        return None
    return df[df["uploaded_by"].astype(str) == str(chief_id)]

BASE_CSS = """<style>
:root {
    --navy-dark: #001827;
    --navy-header: #001b2e;
    --navy-overlay: #001421;
    --primary-teal: #00e5b4;
    --accent-cyan: #00dfff;
    --accent-yellow: #ffd21a;
    --text-white: #f5f8ff;
    --text-muted: #c8d8e8;
    --border-blue: rgba(0, 176, 224, 0.30);
}

/* App canvas + hide the default Streamlit toolbar so the navbar sits at the very top */
.stApp {
    background: radial-gradient(1200px 700px at 85% -10%, #01293c 0%, #001421 48%, #000f18 100%) fixed;
}
header[data-testid="stHeader"] {
    display: none !important;
}
div[data-testid="stMain"] {
    overflow-x: hidden;
}
div[data-testid="stMainBlockContainer"] {
    padding-top: 0 !important;
}
div[data-testid="stMainBlockContainer"]:not(:has(.sv-hero-marker)) {
    padding-top: 2.2rem !important;
}
/* Full-bleed hero page: the navbar and hero span the whole viewport */
div[data-testid="stMainBlockContainer"]:has(.sv-hero-marker):has(.sv-navbar-marker) {
    padding-left: 0 !important;
    padding-right: 0 !important;
    padding-bottom: 0 !important;
    max-width: 100% !important;
}
div[data-testid="stMainBlockContainer"]:has(.sv-hero-marker):has(.sv-navbar-marker) > div[data-testid="stVerticalBlock"] {
    gap: 0 !important;
}

/* ============================ NAVBAR ============================ */
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) {
    width: 100%;
    background: var(--navy-header);
    border-bottom: 1px solid var(--border-blue);
    box-shadow: 0 8px 26px rgba(0, 0, 0, 0.35);
    padding: 0 34px;
    min-height: 70px;
    align-items: center;
    position: relative;
    z-index: 20;
}
/* Inner pages: keep the navbar edge-to-edge even inside the padded page */
div[data-testid="stMainBlockContainer"]:not(:has(.sv-hero-marker)) div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) {
    width: 100vw;
    max-width: 100vw !important;
    margin-left: calc(50% - 50vw);
}

/* Invisible markers used for CSS targeting - never take space */
.sv-globe-marker,
.sv-voice-marker {
    display: none !important;
}
div[data-testid="stElementContainer"]:has(> div > div > .sv-globe-marker),
div[data-testid="stElementContainer"]:has(> div > div > .sv-voice-marker),
div[data-testid="stElementContainer"]:has(> div > .sv-globe-marker),
div[data-testid="stElementContainer"]:has(> div > .sv-voice-marker) {
    display: none !important;
    height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) > div[data-testid="stColumn"] {
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 68px;
}
.sv-brand {
    display: flex;
    align-items: center;
    gap: 13px;
}
.sv-brand-logo {
    width: 58px;
    height: 58px;
    border-radius: 50%;
    overflow: hidden;
    border: 1.5px solid rgba(0, 229, 180, 0.45);
    box-shadow: 0 0 16px rgba(0, 229, 180, 0.22);
    flex: 0 0 auto;
    background: #04202f;
}
.sv-brand-logo img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    object-position: center center;
    display: block;
    padding: 2px;
    box-sizing: border-box;
}
.sv-brand-words {
    display: flex;
    flex-direction: column;
    line-height: 1.12;
}
.sv-brand-en {
    font-size: 1.32rem;
    font-weight: 800;
    letter-spacing: 1.6px;
    color: var(--primary-teal);
}
.sv-brand-hi {
    font-size: 0.86rem;
    font-weight: 600;
    letter-spacing: 0.6px;
    color: var(--accent-yellow);
}

/* Language selector (native Streamlit selectbox restyled as a pill) */
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] {
    position: relative;
    margin: 0;
    width: 152px;
    min-width: 0;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] label {
    display: none !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] [data-baseweb="select"],
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] div[role="group"] {
    position: relative;
}
/* Streamlit 1.4x+ react-aria selectbox */
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] div[role="group"] {
    background: rgba(0, 229, 180, 0.06) !important;
    border: 1px solid var(--border-blue) !important;
    border-radius: 999px !important;
    height: 40px !important;
    min-height: 40px !important;
    padding: 0 10px 0 34px !important;
    display: flex;
    align-items: center;
    box-shadow: none !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] div[role="group"]:hover {
    border-color: rgba(0, 229, 180, 0.6) !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] input {
    background: transparent !important;
    border: none !important;
    color: var(--text-white) !important;
    font-size: 0.9rem !important;
    font-weight: 600 !important;
    height: 38px !important;
    padding: 0 !important;
    box-shadow: none !important;
    cursor: pointer;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] div[role="group"] button {
    background: transparent !important;
    border: none !important;
    color: var(--text-muted) !important;
    box-shadow: none !important;
}
div[data-testid="stColumn"]:has(.sv-globe-marker) div[data-testid="stSelectbox"] div[role="group"]::before {
    content: "🌐";
    position: absolute;
    left: 13px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 13px;
    line-height: 1;
    z-index: 3;
    pointer-events: none;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] [data-baseweb="select"] > div {
    background: rgba(0, 229, 180, 0.06) !important;
    border: 1px solid var(--border-blue) !important;
    border-radius: 999px !important;
    min-height: 40px !important;
    height: 40px !important;
    padding-left: 34px !important;
    padding-right: 12px !important;
    color: var(--text-white) !important;
    font-weight: 600;
    font-size: 0.9rem;
    box-shadow: none !important;
    cursor: pointer;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] [data-baseweb="select"] > div:hover {
    border-color: rgba(0, 229, 180, 0.6) !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] [data-baseweb="select"] span,
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] [data-baseweb="select"] div[aria-live] {
    color: var(--text-white) !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] svg {
    color: var(--text-muted) !important;
    fill: var(--text-muted) !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stSelectbox"] .sv-globe-marker {
    display: none;
}
div[data-testid="stColumn"]:has(.sv-globe-marker) div[data-testid="stSelectbox"] [data-baseweb="select"]::before {
    content: "\\1F310";
    position: absolute;
    left: 13px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 13px;
    line-height: 1;
    z-index: 3;
    pointer-events: none;
}

/* Language dropdown menu (rendered in a portal, scoped by its aria-label) */
[role="listbox"][aria-label*="Language"] {
    background: #001b2e !important;
    border: 1px solid var(--border-blue) !important;
    border-radius: 12px !important;
    box-shadow: 0 16px 38px rgba(0, 0, 0, 0.5) !important;
    padding: 5px !important;
    margin-top: 6px !important;
}
[role="listbox"][aria-label*="Language"] [role="option"] {
    color: var(--text-white) !important;
    border-radius: 8px !important;
    padding: 7px 12px !important;
    font-size: 0.88rem !important;
    cursor: pointer;
}
[role="listbox"][aria-label*="Language"] [role="option"]:hover,
[role="listbox"][aria-label*="Language"] [role="option"][data-focused="true"] {
    background: rgba(0, 229, 180, 0.16) !important;
}

/* Location chip */
.sv-location {
    display: flex;
    width: fit-content;
    margin: 0;
    align-items: center;
    gap: 7px;
    height: 40px;
    padding: 0 16px;
    border-radius: 999px;
    border: 1px solid var(--border-blue);
    background: rgba(0, 229, 180, 0.06);
    color: var(--text-muted);
    font-size: 0.88rem;
    font-weight: 600;
    white-space: nowrap;
}
.sv-location svg {
    flex: 0 0 auto;
}

/* ============================ HERO ============================ */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) {
    position: relative;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: flex-start;
    gap: 0;
    min-height: 420px;
    padding: 30px 48px 34px 48px;
    overflow: hidden;
    background-color: #001421;
    border-bottom: 1px solid rgba(0, 176, 224, 0.16);
    box-sizing: border-box;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) > div[data-testid="stElementContainer"] {
    width: auto;
    max-width: 100%;
    position: static !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) > div[data-testid="stElementContainer"]:has(div[data-testid="stButton"]) {
    position: relative !important;
    z-index: 3;
}
.sv-hero-bg {
    position: absolute;
    inset: 0;
    z-index: 0;
    overflow: hidden;
}
.sv-hero-bg img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    object-position: center 42%;
    display: block;
}
.sv-hero-bg::after {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, rgba(0, 18, 33, 0.94) 0%, rgba(0, 22, 38, 0.74) 32%, rgba(0, 22, 38, 0.20) 66%, rgba(0, 22, 38, 0.04) 100%);
}
.sv-hero-copy {
    position: relative;
    z-index: 2;
}
.sv-hero-copy {
    max-width: 620px;
}
.sv-hero-tag {
    display: inline-block;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.9px;
    color: #8fe9ff;
    margin-bottom: 14px;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.45);
}
.sv-hero-title {
    margin: 0 0 12px 0 !important;
    padding: 0 !important;
    font-size: 2.4rem !important;
    line-height: 1.15 !important;
    font-weight: 800 !important;
    letter-spacing: 0.2px;
    color: var(--text-white) !important;
    text-shadow: 0 2px 14px rgba(0, 0, 0, 0.4);
}
.sv-hero-title .sv-teal {
    color: var(--primary-teal) !important;
}
.sv-hero-title .sv-yellow {
    color: var(--accent-yellow) !important;
}
.sv-hero-lead {
    margin: 0 0 9px 0;
    font-size: 1rem;
    line-height: 1.45;
    font-weight: 600;
    color: #eaf4ff;
    max-width: 560px;
    text-shadow: 0 1px 10px rgba(0, 0, 0, 0.45);
}
.sv-hero-desc {
    margin: 0;
    font-size: 0.92rem;
    line-height: 1.62;
    font-weight: 400;
    color: var(--text-muted);
    max-width: 560px;
    text-shadow: 0 1px 10px rgba(0, 0, 0, 0.5);
}
.sv-hero-slogan {
    position: absolute;
    z-index: 2;
    top: 15%;
    right: 5.5%;
    transform: rotate(-5deg);
    text-align: right;
    line-height: 1.12;
    color: #001827;
    font-family: Georgia, 'Times New Roman', serif;
    font-style: italic;
    font-weight: 700;
    font-size: 1.5rem;
    letter-spacing: 0.3px;
    text-shadow: 0 1px 14px rgba(255, 255, 255, 0.35);
    pointer-events: none;
}
.sv-hero-slogan .sv-leaf {
    display: inline-block;
    font-style: normal;
    font-family: 'Segoe UI Emoji', 'Segoe UI', sans-serif;
    font-size: 1rem;
    color: #0d7f74;
    margin-right: 5px;
    vertical-align: 2px;
}

/* Hero CTA */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) div[data-testid="stButton"] button {
    background: linear-gradient(135deg, #00e5b4 0%, #00d9c4 100%) !important;
    color: #001827 !important;
    border: none !important;
    border-radius: 10px !important;
    height: 44px !important;
    min-height: 44px !important;
    padding: 0 22px !important;
    font-weight: 800 !important;
    font-size: 0.94rem !important;
    box-shadow: 0 8px 22px rgba(0, 229, 180, 0.28) !important;
    transition: transform 0.18s ease, box-shadow 0.18s ease, filter 0.18s ease;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) div[data-testid="stButton"] button p {
    color: #001827 !important;
    font-weight: 800 !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) div[data-testid="stButton"] button:hover {
    transform: translateY(-1px);
    box-shadow: 0 12px 28px rgba(0, 229, 180, 0.42) !important;
    filter: brightness(1.05);
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) div[data-testid="stButton"] {
    margin-top: 20px;
}

/* Supporting content under the hero */
.sv-below-hero {
    max-width: 900px;
    margin: 30px auto 0 auto;
    padding: 0 24px 26px 24px;
}

/* ==================== VOICE / WHATSAPP / FORECAST PAGE ==================== */
/* Navbar controls: one flat row — brand | area | voice | login (no nested columns). */
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) {
    gap: 10px !important;
    flex-wrap: nowrap !important;
    align-items: center !important;
    row-gap: 8px !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) > div[data-testid="stColumn"] {
    display: flex !important;
    flex-direction: column !important;
    justify-content: center !important;
    min-width: 0 !important;
    min-height: 0 !important;
    padding: 0 !important;
    width: auto !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) > div[data-testid="stColumn"]:nth-child(1) {
    flex: 1 1 auto !important;
    align-items: flex-start !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) > div[data-testid="stColumn"]:not(:nth-child(1)) {
    flex: 0 0 auto !important;
    align-items: flex-end !important;
}
/* Streamlit adds a vertical gap inside each column; zero it so all controls share one baseline */
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) > div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] {
    gap: 0 !important;
    width: auto !important;
    align-items: flex-end !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stElementContainer"] {
    width: auto !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stElementContainer"]:has(.sv-location) {
    height: 40px !important;
    min-height: 40px !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stButton"] {
    margin: 0 !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stButton"] button {
    background: rgba(0, 229, 180, 0.06) !important;
    border: 1px solid var(--border-blue) !important;
    border-radius: 999px !important;
    color: var(--text-white) !important;
    height: 40px !important;
    min-height: 40px !important;
    box-shadow: none !important;
    cursor: pointer;
    text-align: center !important;
}
/* Voice guide: round icon-only speaker button */
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-voice_replay button {
    width: 42px !important;
    min-width: 42px !important;
    padding: 0 !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-voice_replay button span[role="img"] {
    font-size: 21px !important;
    line-height: 1 !important;
    color: var(--primary-teal) !important;
    -webkit-text-fill-color: var(--primary-teal) !important;
}
/* Login / farmer-name pill */
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-nav_login button,
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-nav_logout button {
    min-width: 0 !important;
    padding: 0 18px !important;
    font-size: 0.9rem !important;
    font-weight: 700 !important;
    white-space: nowrap !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stButton"] button p {
    color: var(--text-white) !important;
}
div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stButton"] button:hover {
    background: rgba(0, 229, 180, 0.16) !important;
    border-color: rgba(0, 229, 180, 0.6) !important;
}
/* Any button row placed inside a hero stays above the background image */
div[data-testid="stVerticalBlock"]:has(.sv-hero-marker) div[data-testid="stHorizontalBlock"] {
    position: relative;
    z-index: 3;
}

/* ==================== FORM LABELS ON THE DARK CANVAS ==================== */
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] span,
[data-testid="stWidgetLabel"] label {
    color: #dbe9f6 !important;
    font-weight: 600 !important;
}
div[data-testid="stRadio"] label p,
div[data-testid="stRadio"] div[role="radiogroup"] p,
div[data-testid="stCheckbox"] label p {
    color: #eaf4ff !important;
}
div[data-testid="stCaptionContainer"] p,
div[data-testid="stCaptionContainer"] {
    color: #9fb6c8 !important;
}

/* ==================== GATE PAGES (language + role choice) ==================== */
.sv-gate {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    padding: 30px 18px 10px 18px;
}
.sv-gate-logo {
    width: 126px;
    height: 126px;
    border-radius: 50%;
    overflow: hidden;
    border: 3px solid rgba(0, 229, 180, 0.5);
    background: #04141f;
    box-shadow: 0 0 26px rgba(0, 229, 180, 0.22);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 4px;
    box-sizing: border-box;
    margin-bottom: 14px;
}
.sv-gate-logo img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
}
.sv-gate-title {
    font-size: 1.85rem;
    font-weight: 900;
    letter-spacing: 3px;
    color: var(--primary-teal);
    text-shadow: 0 2px 14px rgba(0, 229, 180, 0.3);
    line-height: 1.1;
}
.sv-gate-sub {
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--accent-yellow);
    margin-top: 4px;
    letter-spacing: 1px;
}
.sv-gate-q {
    font-size: 1.28rem;
    font-weight: 700;
    color: var(--text-white);
    margin: 20px 0 4px 0;
    line-height: 1.4;
}
.sv-gate-q span {
    display: block;
    font-size: 1.02rem;
    color: var(--text-muted);
    font-weight: 600;
    margin-top: 4px;
}
.sv-gate-hint {
    color: var(--text-muted);
    font-size: 0.86rem;
    margin: 10px 0 0 0;
}
/* Big choice buttons on the gate pages */
div[data-testid="stMainBlockContainer"]:has(.sv-gate) div[data-testid="stButton"] button {
    height: 62px !important;
    min-height: 62px !important;
    border-radius: 14px !important;
    background: rgba(0, 229, 180, 0.08) !important;
    border: 1px solid rgba(0, 229, 180, 0.45) !important;
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.3) !important;
    transition: transform 0.18s ease, background 0.18s ease, border-color 0.18s ease;
}
div[data-testid="stMainBlockContainer"]:has(.sv-gate) div[data-testid="stButton"] button p {
    color: var(--text-white) !important;
    font-size: 1.05rem !important;
    font-weight: 800 !important;
}
div[data-testid="stMainBlockContainer"]:has(.sv-gate) div[data-testid="stButton"] button:hover {
    background: rgba(0, 229, 180, 0.18) !important;
    border-color: var(--primary-teal) !important;
    transform: translateY(-1px);
}
/* Small secondary link buttons on the gate page (change language / switch role) */
div[data-testid="stMainBlockContainer"]:has(.sv-gate) .st-key-gate_lang_back button {
    background: transparent !important;
    border: 1px dashed rgba(0, 176, 224, 0.45) !important;
    box-shadow: none !important;
    height: 46px !important;
    min-height: 46px !important;
}
div[data-testid="stMainBlockContainer"]:has(.sv-gate) .st-key-gate_lang_back button p {
    font-size: 0.88rem !important;
    font-weight: 600 !important;
    color: var(--text-muted) !important;
}

/* ==================== SARPANCH: VERIFY / UPLOAD CARD ==================== */
.sv-upload-card {
    background: rgba(0, 22, 36, 0.62);
    border: 1px solid rgba(0, 176, 224, 0.28);
    border-radius: 16px;
    padding: 18px 20px;
    margin-top: 14px;
    box-shadow: 0 18px 40px rgba(0, 0, 0, 0.32);
}
.sv-upload-card h4 {
    color: var(--accent-yellow) !important;
    margin: 0 0 8px 0 !important;
    font-size: 1.05rem !important;
}
.sv-upload-card p {
    color: var(--text-muted);
    margin: 0;
    font-size: 0.94rem;
    line-height: 1.6;
}
.sv-chief-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin: 16px 0 4px 0;
}
.sv-chief-stat {
    flex: 1 1 150px;
    background: rgba(0, 229, 180, 0.07);
    border: 1px solid rgba(0, 229, 180, 0.28);
    border-radius: 12px;
    padding: 12px 14px;
}
.sv-chief-stat b {
    display: block;
    font-size: 1.3rem;
    color: var(--primary-teal);
}
.sv-chief-stat span {
    font-size: 0.84rem;
    color: var(--text-muted);
}

/* Panel footer links (switch panel / change language) */
.st-key-foot_switch_role button,
.st-key-foot_switch_lang button {
    background: transparent !important;
    border: 1px solid var(--border-blue) !important;
    border-radius: 999px !important;
    color: var(--text-muted) !important;
    height: 38px !important;
    min-height: 38px !important;
    box-shadow: none !important;
    font-weight: 600 !important;
}
.st-key-foot_switch_role button p,
.st-key-foot_switch_lang button p {
    color: var(--text-muted) !important;
    font-size: 0.82rem !important;
    font-weight: 600 !important;
}
.st-key-foot_switch_role button span[role="img"],
.st-key-foot_switch_lang button span[role="img"] {
    color: var(--primary-teal) !important;
    -webkit-text-fill-color: var(--primary-teal) !important;
}
.st-key-foot_switch_role button:hover,
.st-key-foot_switch_lang button:hover {
    border-color: rgba(0, 229, 180, 0.6) !important;
}

/* Voice guide highlight pulse */
.sv-highlight-target {
    animation: svPulseTarget 1.15s ease-in-out infinite;
    border-radius: 12px;
}
@keyframes svPulseTarget {
    0%, 100% {
        box-shadow: 0 0 0 3px rgba(255, 210, 26, 0.92) !important;
    }
    50% {
        box-shadow: 0 0 0 13px rgba(255, 210, 26, 0.10) !important;
    }
}

/* Translucent result panel (forecast page) */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-result-marker) {
    background: rgba(0, 22, 36, 0.55);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(0, 176, 224, 0.28);
    border-radius: 16px;
    padding: 20px 22px 22px 22px;
    margin-top: 18px;
    box-shadow: 0 18px 40px rgba(0, 0, 0, 0.35);
}
.sv-share-row {
    margin-top: 6px;
}
.sv-share-row a {
    display: inline-flex !important;
    align-items: center;
    gap: 8px;
    background: #25D366;
    color: #05220f !important;
    font-weight: 700;
    font-size: 0.95rem;
    padding: 10px 20px;
    border-radius: 10px;
    text-decoration: none !important;
    box-shadow: 0 8px 20px rgba(37, 211, 102, 0.3);
    transition: transform 0.18s ease, filter 0.18s ease;
}
.sv-share-row a:hover {
    transform: translateY(-1px);
    filter: brightness(1.05);
}

/* Forecast page hero background */
.stApp:has(.sv-page-bg) {
    background: transparent !important;
}
div[data-testid="stAppViewContainer"]:has(.sv-page-bg) {
    background: transparent !important;
}
div[data-testid="stMarkdownContainer"]:has(> .sv-page-bg) {
    height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    overflow: hidden;
}
div[data-testid="stElementContainer"]:has(> div > div > .sv-page-bg),
div[data-testid="stElementContainer"]:has(> div > p > .sv-page-bg) {
    height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    overflow: hidden;
}
div[data-testid="stVerticalBlock"]:has(.sv-page-bg) {
    gap: 0 !important;
}
div[data-testid="stMainBlockContainer"]:has(.sv-page-bg) {
    padding-top: 0 !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] > div > .sv-page-bg) > div:not(:has(.sv-navbar-marker)) {
    padding-top: 14px;
}
.sv-page-bg {
    position: fixed;
    inset: 0;
    z-index: -1;
    overflow: hidden;
    pointer-events: none;
}
.sv-page-bg img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    object-position: center 45%;
    display: block;
}
.sv-page-bg::after {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(180deg, rgba(0, 16, 28, 0.88) 0%, rgba(0, 16, 28, 0.90) 55%, rgba(0, 16, 28, 0.94) 100%);
}

/* Floating WhatsApp button */
.sv-wa-wrap {
    position: fixed;
    bottom: 26px;
    right: 26px;
    z-index: 99999;
    margin: 0 !important;
    padding: 0 !important;
    line-height: 0;
}
.sv-wa-float {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: #25D366;
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.45);
    transition: transform 0.2s ease;
}
.sv-wa-float:hover {
    transform: scale(1.08);
}
.sv-wa-float svg {
    fill: #ffffff;
}

/* ============================ RESPONSIVE ============================ */
/* Laptop: a little tighter, everything still on one line */
@media (max-width: 1280px) {
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) {
        padding: 0 22px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand {
        gap: 10px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-logo {
        width: 46px;
        height: 46px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-en {
        font-size: 1.14rem;
        letter-spacing: 1.2px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-hi {
        font-size: 0.8rem;
    }
    .sv-gate-title {
        font-size: 1.7rem;
    }
}
/* Tablet: smaller brand + compact controls */
@media (max-width: 980px) {
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) {
        padding: 0 16px;
        gap: 8px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-logo {
        width: 42px;
        height: 42px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-en {
        font-size: 1rem;
        letter-spacing: 0.8px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-hi {
        font-size: 0.74rem;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-location {
        padding: 0 12px;
        font-size: 0.78rem;
        gap: 5px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-nav_login button,
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-nav_logout button {
        padding: 0 14px !important;
        font-size: 0.84rem !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-voice_replay button {
        width: 38px !important;
        min-width: 38px !important;
    }
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) {
        padding: 30px 32px 34px 32px;
    }
    .sv-hero-slogan {
        font-size: 1.3rem;
        right: 4%;
    }
}
/* Small tablet / big phone */
@media (max-width: 820px) {
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) {
        min-height: 62px;
        padding: 6px 14px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand {
        gap: 9px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-hi {
        display: none;
    }
    .sv-hero-slogan {
        display: none;
    }
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-hero-marker) {
        min-height: 400px;
        padding: 26px 20px 30px 20px;
        background-position: center center;
    }
    .sv-hero-title {
        font-size: 1.8rem !important;
    }
    .sv-hero-lead {
        font-size: 0.95rem !important;
    }
    .sv-hero-desc {
        font-size: 0.85rem !important;
    }
    .sv-gate-title {
        font-size: 1.45rem;
        letter-spacing: 2px;
    }
    .sv-gate-logo {
        width: 104px;
        height: 104px;
    }
    .sv-gate-q {
        font-size: 1.12rem;
    }
}
/* Phone */
@media (max-width: 640px) {
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) {
        min-height: 56px;
        padding: 6px 12px !important;
        gap: 6px !important;
        row-gap: 6px !important;
        /* safety net: if a very narrow screen still cannot fit, wrap instead of overlapping */
        flex-wrap: wrap !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-logo {
        width: 38px;
        height: 38px;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-brand-en {
        font-size: 0.88rem;
        letter-spacing: 0.5px;
    }
    /* Phone: the pin becomes a 38px circle drawn on the container itself (always centred) */
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) div[data-testid="stElementContainer"]:has(.sv-location) {
        width: 34px !important;
        height: 38px !important;
        min-height: 38px !important;
        border: 1px solid var(--border-blue);
        border-radius: 999px;
        background-color: rgba(0, 229, 180, 0.06);
        background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='15' height='15' viewBox='0 0 24 24' fill='none' stroke='%2300e5b4' stroke-width='2.1' stroke-linecap='round' stroke-linejoin='round'><path d='M21 10c0 6-9 12-9 12s-9-6-9-12a9 9 0 0 1 18 0z'/><circle cx='12' cy='10' r='3'/></svg>");
        background-repeat: no-repeat;
        background-position: center center;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .sv-location {
        display: none !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-voice_replay button {
        width: 38px !important;
        min-width: 38px !important;
        height: 38px !important;
        min-height: 38px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-nav_login button,
    div[data-testid="stHorizontalBlock"]:has(.sv-navbar-marker) .st-key-nav_logout button {
        padding: 0 12px !important;
        font-size: 0.8rem !important;
        height: 38px !important;
        min-height: 38px !important;
    }
    div[data-testid="stMainBlockContainer"]:has(.sv-gate) div[data-testid="stButton"] button {
        height: 54px !important;
        min-height: 54px !important;
    }
    div[data-testid="stMainBlockContainer"]:has(.sv-gate) div[data-testid="stButton"] button p {
        font-size: 0.95rem !important;
    }
    .sv-gate {
        padding: 18px 10px 6px 10px;
    }
    .sv-gate-title {
        font-size: 1.22rem;
        letter-spacing: 1.5px;
    }
    .sv-gate-sub {
        font-size: 0.92rem;
    }
    .sv-gate-q {
        font-size: 1.02rem;
        margin-top: 14px;
    }
    .sv-gate-q span {
        font-size: 0.9rem;
    }
    .sv-gate-logo {
        width: 88px;
        height: 88px;
        margin-bottom: 10px;
    }
    .sv-wa-float {
        width: 50px;
        height: 50px;
    }
    .sv-wa-wrap {
        bottom: 18px;
        right: 18px;
    }
}

/* ============================ PERFORMANCE ============================ */
/* Blurred glass panels are expensive on phone GPUs - use a flat panel there. */
@media (max-width: 820px) {
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .sv-result-marker) {
        backdrop-filter: none !important;
        -webkit-backdrop-filter: none !important;
        background: rgba(0, 22, 36, 0.92) !important;
    }
}
/* Respect the OS "reduce motion" setting: no endless pulsing animation. */
@media (prefers-reduced-motion: reduce) {
    .sv-highlight-target { animation: none !important; }
    .splash-wrapper, .splash-loader-bar { animation-duration: 0.01s !important; }
}
</style>"""
st.markdown(BASE_CSS, unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# VOICE ASSISTANT (browser speech) + WHATSAPP HELPERS  [FRONTEND ONLY]
# -----------------------------------------------------------------------------
VOICE_JS = r"""
(function () {
  var cfg = (typeof SVCFG !== "undefined") ? SVCFG : {};

  function doc() {
    try { if (window.parent && window.parent.document) return window.parent.document; } catch (e) {}
    return document;
  }
  function win() {
    try { if (window.parent && window.parent.speechSynthesis) return window.parent; } catch (e) {}
    return window;
  }

  function clearHL() {
    var els = doc().querySelectorAll(".sv-highlight-target");
    for (var i = 0; i < els.length; i++) els[i].classList.remove("sv-highlight-target");
  }

  function highlight(el, ms) {
    if (!el) return;
    clearHL();
    el.classList.add("sv-highlight-target");
    setTimeout(function () { el.classList.remove("sv-highlight-target"); }, ms || 9000);
  }

  function pick(kind) {
    var d = doc(), i, btns;
    try {
      if (kind === "cta") {
        btns = Array.prototype.slice.call(d.querySelectorAll("div[data-testid='stButton'] button"));
        for (i = 0; i < btns.length; i++) if (/check weather|\u092e\u094c\u0938\u092e/i.test(btns[i].textContent || "")) return btns[i];
        return null;
      }
      if (kind === "khasra") {
        var inp = d.querySelector("input[aria-label*='khasra' i], input[aria-label*='\u0916\u0938\u0930\u093e']");
        if (!inp) inp = d.querySelector("div[data-testid='stTextInput'] input");
        if (!inp) return null;
        return inp.closest("[data-testid='stTextInput']") || inp.parentElement;
      }
      if (kind === "village") {
        var boxes = Array.prototype.slice.call(d.querySelectorAll("[data-testid='stSelectbox']"));
        for (i = 0; i < boxes.length; i++) {
          var row = boxes[i].closest("[data-testid='stHorizontalBlock']");
          if (row && row.querySelector(".sv-navbar-marker")) continue;
          return boxes[i];
        }
        return null;
      }
      if (kind === "button") {
        btns = Array.prototype.slice.call(d.querySelectorAll("div[data-testid='stButton'] button"));
        for (i = btns.length - 1; i >= 0; i--) if (/weather forecast|\u092e\u094c\u0938\u092e/i.test(btns[i].textContent || "")) return btns[i];
        return null;
      }
    } catch (e) {}
    return null;
  }

  function speak(text, code, onDone) {
    var w = win();
    var done = false;
    var finish = function () { if (done) return; done = true; try { onDone && onDone(); } catch (e) {} };
    try { w.speechSynthesis.cancel(); } catch (e) {}
    var U = (w.SpeechSynthesisUtterance || window.SpeechSynthesisUtterance);
    if (!U || !w.speechSynthesis) { setTimeout(finish, 300); return; }
    var u = new U(text);
    u.lang = code || "en-IN";
    u.rate = 0.97;
    var vs = [];
    try { vs = w.speechSynthesis.getVoices() || []; } catch (e) {}
    var base = (code || "en-IN").toLowerCase().split("-")[0];
    var v = null, k, l;
    for (k = 0; k < vs.length; k++) {
      l = (vs[k].lang || "").toLowerCase().replace("_", "-");
      if (l === (code || "").toLowerCase()) { v = vs[k]; break; }
      if (!v && l.indexOf(base) === 0) v = vs[k];
    }
    if (v) u.voice = v;
    u.onend = finish;
    u.onerror = finish;
    try { w.speechSynthesis.speak(u); } catch (e) { finish(); return; }
    setTimeout(finish, Math.max(2500, Math.min(16000, text.length * 80)));
  }

  function run(i) {
    var steps = cfg.steps || [];
    if (i >= steps.length) return;
    var s = steps[i];
    if (s.h) highlight(pick(s.h), 9000);
    speak(s.t, s.c || cfg.code, function () { setTimeout(function () { run(i + 1); }, 350); });
  }

  try { win().speechSynthesis.getVoices(); } catch (e) {}
  setTimeout(function () { run(0); }, 600);
})();
"""

VOICE_STYLE_CSS = """<style>
iframe[data-testid="stIFrame"], div[data-testid="stElementContainer"] > div > iframe {
    margin: 0 !important;
}
</style>"""


def render_voice_agent(steps, lang_key):
    """Injects the browser speech assistant for the current page (UI only)."""
    if not steps:
        return
    cfg = {
        "lang": lang_key,
        "code": "hi-IN" if lang_key == "hi" else "en-IN",
        "steps": steps,
    }
    components.html(
        f"<script>var SVCFG = {json.dumps(cfg, ensure_ascii=False)};</script><script>{VOICE_JS}</script>",
        height=0
    )


def build_voice_steps(lang_key, page):
    """Voice script + which control must glow, for every step of the flow."""
    if page == "language":
        # Language is not chosen yet, so the prompt is spoken in both languages.
        return [
            {"t": "Welcome to Suksmavrishti. Please choose your language.", "h": "", "c": "en-IN"},
            {"t": "\u0938\u0942\u0915\u094d\u0937\u094d\u092e\u0935\u0943\u0937\u094d\u091f\u093f \u092e\u0947\u0902 \u0906\u092a\u0915\u093e \u0938\u094d\u0935\u093e\u0917\u0924 \u0939\u0948\u0964 \u0915\u0943\u092a\u092f\u093e \u0905\u092a\u0928\u0940 \u092d\u093e\u0937\u093e \u091a\u0941\u0928\u0947\u0902\u0964", "h": "", "c": "hi-IN"},
        ]

    hi = lang_key == "hi"

    if page == "role":
        if hi:
            return [
                {"t": "\u0906\u092a\u0928\u0947 \u0939\u093f\u0902\u0926\u0940 \u092d\u093e\u0937\u093e \u091a\u0941\u0928\u0940 \u0939\u0948\u0964", "h": ""},
                {"t": "\u0905\u092c \u092c\u0924\u093e\u090f\u0901 \u0906\u092a \u0915\u094c\u0928 \u0939\u0948\u0902\u0964 \u0915\u093f\u0938\u093e\u0928 \u0939\u0948\u0902 \u0924\u094b \u0915\u093f\u0938\u093e\u0928 \u0932\u0949\u0917\u093f\u0928 \u0926\u092c\u093e\u090f\u0901\u0964", "h": ""},
                {"t": "\u0938\u0930\u092a\u0902\u091a \u0939\u0948\u0902 \u0924\u094b \u0938\u0930\u092a\u0902\u091a \u0932\u0949\u0917\u093f\u0928 \u0926\u092c\u093e\u090f\u0901\u0964", "h": ""},
            ]
        return [
            {"t": "You have selected English.", "h": ""},
            {"t": "Now choose who you are.", "h": ""},
            {"t": "Press Login as Farmer if you are a farmer, or Login as Chief if you are the village sarpanch.", "h": ""},
        ]

    if page == "home":
        if hi:
            return [
                {"t": "\u0928\u092e\u0938\u094d\u0915\u093e\u0930! \u0938\u0942\u0915\u094d\u0937\u094d\u092e\u0935\u0943\u0937\u094d\u091f\u093f \u092e\u0947\u0902 \u0906\u092a\u0915\u093e \u0938\u094d\u0935\u093e\u0917\u0924 \u0939\u0948\u0964", "h": ""},
                {"t": "\u0905\u0917\u0930 \u0906\u092a\u0915\u094b \u0905\u092a\u0928\u0947 \u0907\u0932\u093e\u0915\u0947 \u0915\u0947 \u092e\u094c\u0938\u092e \u0915\u093e \u092a\u0942\u0930\u094d\u0935\u093e\u0928\u0941\u092e\u093e\u0928 \u0926\u0947\u0916\u0928\u093e \u0939\u0948\u090c, \u0924\u094b \u0928\u0940\u091a\u0947 \u0926\u093f\u090f \u0917\u092f\u0947 \u092c\u091f\u0928 \u092a\u0930 \u0915\u094d\u0932\u093f\u0915 \u0915\u0930\u0947\u0964", "h": "cta"},
            ]
        return [
            {"t": "Welcome to Suksmavrishti!", "h": ""},
            {"t": "If you want to see the weather forecast of your area, please click the Check Weather Forecast button below.", "h": "cta"},
        ]

    if page == "chief":
        if hi:
            return [
                {"t": "\u0938\u0930\u092a\u0902\u091a \u092a\u0948\u0928\u0932 \u092e\u0947\u0902 \u0906\u092a\u0915\u093e \u0938\u094d\u0935\u093e\u0917\u0924 \u0939\u0948\u0964", "h": ""},
                {"t": "\u092f\u0939\u093e\u0902 \u0938\u0947 \u0906\u092a \u092e\u094c\u0938\u092e \u092a\u0942\u0930\u094d\u0935\u093e\u0928\u0941\u092e\u093e\u0928 \u0926\u0947\u0916 \u0938\u0915\u0924\u0947 \u0939\u0948\u0902\u0964", "h": "cta"},
                {"t": "\u0930\u093f\u092a\u094b\u0930\u094d\u091f \u0938\u0939\u0940 \u092f\u093e \u0917\u0932\u0924 \u0939\u094b\u0928\u0947 \u092a\u0930 \u0916\u0947\u0924 \u0915\u0940 \u092b\u094b\u091f\u094b \u0905\u092a\u0932\u094b\u0921 \u0915\u0930\u0915\u0947 \u092e\u094c\u0938\u092e \u090f\u0906\u0908 \u0915\u094b \u0914\u0930 \u0938\u091f\u0940\u0915 \u092c\u0928\u093e\u0928\u0947 \u092e\u0947\u0902 \u092e\u0926\u0926 \u0915\u0930\u0947\u0902\u0964", "h": ""},
            ]
        return [
            {"t": "Welcome to the Sarpanch panel.", "h": ""},
            {"t": "From here you can check the weather forecast of your village.", "h": "cta"},
            {"t": "You can also upload a photo of the field to tell the AI whether the report was right or wrong, so the forecast becomes more accurate.", "h": ""},
        ]

    if page == "forecast":
        if hi:
            return [
                {"t": "\u092f\u0939\u093e\u0902 \u0906\u092a \u0905\u092a\u0928\u0947 \u0907\u0932\u093e\u0915\u0947 \u0915\u0947 \u092e\u094c\u0938\u092e \u0915\u093e \u092a\u0942\u0930\u094d\u0935\u093e\u0928\u0941\u092e\u093e\u0928 \u0926\u0947\u0916 \u0938\u0915\u0924\u0947 \u0939\u0948\u0902\u0964", "h": ""},
                {"t": "\u0938\u092c\u0938\u0947 \u092a\u0939\u0932\u0947 \u0906\u092a\u0928\u093e \u0916\u0938\u0930\u093e \u0928\u0902\u092c\u0930 \u0926\u0930\u094d\u091c \u0915\u0930\u0947\u0964", "h": "khasra"},
                {"t": "\u0905\u092a\u0928\u0947 \u0907\u0932\u093e\u0915\u0947 \u0915\u093e \u0928\u093e\u092e \u091a\u0941\u0928\u0947\u0902\u0964", "h": "village"},
                {"t": "\u0914\u0930 \u0905\u092c \u0928\u0940\u091a\u0947 \u0926\u093f\u090f \u0917\u092f\u0947 \u0932\u093e\u0932 \u092c\u091f\u0928 \u0915\u094b \u0926\u092c\u093e\u090f\u0902\u0964", "h": "button"},
            ]
        return [
            {"t": "Here you can see the weather forecast of your area.", "h": ""},
            {"t": "First of all, please enter your Khasra number.", "h": "khasra"},
            {"t": "Now choose the name of your area.", "h": "village"},
            {"t": "And finally, press the red button below.", "h": "button"},
        ]
    return []


# Floating WhatsApp button (available on every page)
wa_greeting = "\u0928\u092e\u0938\u094d\u0915\u093e\u0930! \u092e\u0941\u091d\u0947 \u0938\u0942\u0915\u094d\u0937\u094d\u092e\u0935\u0943\u0937\u094d\u091f\u093f \u092a\u0930 \u092e\u094c\u0938\u092e \u092a\u0942\u0930\u094d\u0935\u093e\u0928\u0941\u092e\u093e\u0928 \u091a\u093e\u0939\u093f\u090f\u0964"
st.markdown(
    f"""<div class="sv-wa-wrap">
        <a class="sv-wa-float" href="https://wa.me/{WHATSAPP_NUMBER}?text={quote(wa_greeting)}" target="_blank" rel="noopener" aria-label="WhatsApp" title="WhatsApp par baat karein">
            <svg width="28" height="28" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                <path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2 22l5.25-1.38c1.45.79 3.08 1.21 4.79 1.21 5.46 0 9.91-4.45 9.91-9.91S17.5 2 12.04 2zm5.8 14.16c-.24.68-1.4 1.3-1.94 1.38-.5.07-1.13.1-1.82-.11-.42-.13-.96-.31-1.65-.61-2.9-1.25-4.79-4.17-4.94-4.37-.15-.2-1.19-1.58-1.19-3.02 0-1.43.75-2.14 1.02-2.43.27-.29.58-.36.78-.36.19 0 .39 0 .56.01.18.01.42-.07.66.5.24.58.82 2 .89 2.15.07.15.12.32.02.52-.09.2-.14.32-.28.49-.14.17-.3.38-.43.51-.14.14-.29.29-.12.57.17.29.76 1.25 1.63 2.03 1.12 1 2.07 1.31 2.36 1.46.29.14.46.12.63-.07.17-.2.73-.85.92-1.14.19-.29.39-.24.66-.15.27.1 1.69.8 1.98.94.29.14.48.22.55.34.07.12.07.68-.17 1.36z"/>
            </svg>
        </a>
    </div>""",
    unsafe_allow_html=True
)
st.markdown(VOICE_STYLE_CSS, unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# NAVBAR (brand + area + voice guide + login) — shown after language & role
# -----------------------------------------------------------------------------
voice_replay = False

if st.session_state.lang_chosen and st.session_state.role is not None:
    nav_is_hi = (st.session_state.language != "English")
    nav_brand_col, nav_loc_col, nav_voice_col, nav_acct_col = st.columns(
        [6.6, 1.9, 0.7, 1.5], vertical_alignment="center"
    )

    with nav_brand_col:
        nav_logo_src = get_image_src(LOGO_FILE, max_width=260, fmt="png")
        logo_html = f'<img src="{nav_logo_src}" alt="Suksmavrishti logo" decoding="async" />' if nav_logo_src else '<div style="font-size:28px;">🌾</div>'
        st.markdown(
            f"""<div class="sv-navbar-marker"></div>
            <div class="sv-brand">
                <div class="sv-brand-logo">{logo_html}</div>
                <div class="sv-brand-words">
                    <span class="sv-brand-en">SUKSMAVRISHTI</span>
                    <span class="sv-brand-hi">सूक्ष्मवृष्टि</span>
                </div>
            </div>""",
            unsafe_allow_html=True
        )

    with nav_loc_col:
        st.markdown(
            """<div class="sv-location">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#00e5b4" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 10c0 6-9 12-9 12s-9-6-9-12a9 9 0 0 1 18 0z"></path>
                    <circle cx="12" cy="10" r="3"></circle>
                </svg>
                <span>Barabanki, UP</span>
            </div>""",
            unsafe_allow_html=True
        )

    with nav_voice_col:
        voice_replay = st.button(
            ":material/volume_up:",
            key="voice_replay",
            help="Voice guide dobara sunayein / वॉइस गाइड फिर सुनें"
        )

    with nav_acct_col:
        nav_is_chief = (st.session_state.role == "chief")
        nav_logged_in = (
            chief_auth.is_logged_in() if nav_is_chief else farmer_auth.is_logged_in()
        )
        if nav_logged_in:
            nav_name = (
                chief_auth.current_chief().get("area", "") if nav_is_chief
                else farmer_auth.current_farmer()["name"]
            )
            acct_label = f":material/person: {nav_name[:14]}"
            if st.button(acct_label, key="nav_logout", help="Logout / लॉगआउट करने के लिए क्लिक करें"):
                if nav_is_chief:
                    chief_auth.logout()
                    st.session_state.current_page = "chief_login"
                else:
                    farmer_auth.logout()
                    st.session_state.current_page = "welcome"
                st.rerun()
        else:
            if nav_is_chief:
                login_label = "सरपंच लॉगिन" if nav_is_hi else "Chief Login"
            else:
                login_label = "लॉगिन" if nav_is_hi else "Login"
            if st.button(login_label, key="nav_login", help="Login / Register · लॉगिन / रजिस्ट्रेशन"):
                st.session_state.current_page = "chief_login" if nav_is_chief else "login"
                st.rerun()

# -----------------------------------------------------------------------------
# LOAD BHULEKH CADASTRE
# -----------------------------------------------------------------------------
@st.cache_data
def load_khasra_dataset():
    candidates = [
        "bhulekh_khasra_grid_mapping_demo_names.csv",
        "bhulekh_khasra_grid_mapping_2.csv",
        "bhulekh_khasra_grid_mapping.csv"
    ]
    for fn in candidates:
        if os.path.exists(fn):
            return pd.read_csv(fn)
    return None

df_khasra = load_khasra_dataset()

# -----------------------------------------------------------------------------
# SECURITY: deny-by-default page access (server-side authorization)
# The role is server-side session state; Chief pages additionally require an
# authenticated Chief session. Editing frontend JS / cookies / the request
# cannot grant access because every render re-checks this table.
# -----------------------------------------------------------------------------
PAGE_ROLES = {
    "welcome": {"farmer", "chief"},
    "forecast": {"farmer", "chief"},
    "login": {"farmer"},
    "chief_login": {"chief"},
    "chief": {"chief"},
    "upload": {"chief"},
}

if st.session_state.lang_chosen and st.session_state.role is not None:
    _page = st.session_state.get("current_page", "welcome")
    _role = st.session_state.get("role")
    if _page not in PAGE_ROLES or _role not in PAGE_ROLES.get(_page, frozenset()):
        security.audit("authz_denied", actor=str(_role or "anonymous"), outcome="denied",
                       detail={"page": _page, "role": _role, "reason": "page_not_allowed"})
        st.session_state.current_page = "welcome"
        st.rerun()
    if _page in ("chief", "upload") and not chief_auth.is_logged_in():
        security.audit("authz_denied", actor="chief", outcome="denied",
                       detail={"page": _page, "reason": "no_chief_session"})
        st.session_state.current_page = "chief_login"
        st.rerun()


def render_flow_footer(is_hi):
    """Small link at the bottom of a panel: switch panel (language is chosen once, at startup)."""
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    foot_a, _foot_sp = st.columns([1.25, 5.5])
    with foot_a:
        switch_label = (
            ":material/switch_account: " + ("भूमिका बदलें" if is_hi else "Switch panel")
        )
        if st.button(switch_label, key="foot_switch_role"):
            st.session_state.role = None
            st.session_state.current_page = "welcome"
            st.rerun()


def gate_header(question_main, question_sub, hint=""):
    """Shared centred logo + question header for the language / role pages."""
    gate_logo = get_image_src(LOGO_FILE, max_width=260, fmt="png")
    gate_logo_html = (
        f'<img src="{gate_logo}" alt="Suksmavrishti logo" decoding="async" />'
        if gate_logo else "🌾"
    )
    hint_html = f'<div class="sv-gate-hint">{hint}</div>' if hint else ""
    st.markdown(
        f"""<div class="sv-gate">
            <div class="sv-gate-logo">{gate_logo_html}</div>
            <div class="sv-gate-title">SUKSMAVRISHTI</div>
            <div class="sv-gate-sub">सूक्ष्मवृष्टि</div>
            <div class="sv-gate-q">{question_main}<span>{question_sub}</span></div>
            {hint_html}
        </div>""",
        unsafe_allow_html=True,
    )


# -----------------------------------------------------------------------------
# 1. LANGUAGE PAGE (first page after the splash screen)
# -----------------------------------------------------------------------------
if not st.session_state.lang_chosen:
    gate_header(
        "Choose your language",
        "अपनी भाषा चुनें",
        "The whole website (weather reports, advisories and voice guide) will run in the "
        "language you pick. / पूरी वेबसाइट, रिपोर्ट और वॉइस गाइड इसी भाषा में चलेंगे।",
    )

    _sp_l, lang_col_en, lang_col_hi, _sp_r = st.columns([1.1, 1, 1, 1.1])
    with lang_col_en:
        if st.button("English", key="gate_lang_en", use_container_width=True):
            st.session_state.language = "English"
            st.session_state.lang_chosen = True
            st.session_state.sv_voice_role_lang = ""
            st.rerun()
    with lang_col_hi:
        if st.button("हिंदी", key="gate_lang_hi", use_container_width=True):
            st.session_state.language = "हिंदी"
            st.session_state.lang_chosen = True
            st.session_state.sv_voice_role_lang = ""
            st.rerun()

    if not st.session_state.sv_voice_langgate:
        st.session_state.sv_voice_langgate = True
        render_voice_agent(build_voice_steps("en", "language"), "en")

# -----------------------------------------------------------------------------
# 2. ROLE PAGE (farmer / sarpanch)
# -----------------------------------------------------------------------------
elif st.session_state.role is None:
    is_hi = (st.session_state.language == "हिंदी")

    gate_header(
        "आप कौन हैं?" if is_hi else "Who are you?",
        "अपना पैनल चुनें / Choose your panel" if is_hi else "Choose your panel / अपना पैनल चुनें",
        "किसान के लिए मौसम पूर्वानुमान और फसल सलाह। सरपंच के लिए गांव की निगरानी और रिपोर्ट सत्यापन।"
        if is_hi else
        "Farmers get the weather forecast and crop advisory. The sarpanch also verifies "
        "reports and uploads field photos to train the forecasting AI.",
    )

    _sp_l, role_col_farmer, role_col_chief, _sp_r = st.columns([0.7, 1.3, 1.3, 0.7])
    farmer_label = "किसान लॉगिन" if is_hi else "Login as Farmer"
    chief_label = "सरपंच लॉगिन" if is_hi else "Login as Chief"
    farmer_note = "मौसम पूर्वानुमान एवं फसल सलाह" if is_hi else "Weather forecast & crop advisory"
    chief_note = "गांव की निगरानी एवं रिपोर्ट सत्यापन" if is_hi else "Village monitoring & report verification"

    with role_col_farmer:
        if st.button(farmer_label, key="gate_role_farmer", use_container_width=True):
            security.audit("role_selected", actor="farmer", outcome="info")
            st.session_state.role = "farmer"
            st.session_state.current_page = "welcome"
            st.session_state.sv_voice_home_lang = ""
            st.rerun()
        st.markdown(
            f"<p style='text-align:center; color:#c8d8e8; font-size:0.86rem; margin-top:6px;'>{farmer_note}</p>",
            unsafe_allow_html=True,
        )
    with role_col_chief:
        if st.button(chief_label, key="gate_role_chief", use_container_width=True):
            security.audit("role_selected", actor="chief", outcome="info")
            st.session_state.role = "chief"
            st.session_state.current_page = "chief_login"
            st.session_state.sv_voice_chief_lang = ""
            st.rerun()
        st.markdown(
            f"<p style='text-align:center; color:#c8d8e8; font-size:0.86rem; margin-top:6px;'>{chief_note}</p>",
            unsafe_allow_html=True,
        )

    _bl, lang_back_col, _br = st.columns([1, 1.4, 1])
    with lang_back_col:
        lang_back_label = (
            ":material/language: भाषा बदलें" if is_hi else ":material/language: Change language"
        )
        if st.button(lang_back_label, key="gate_lang_back", use_container_width=True):
            st.session_state.lang_chosen = False
            st.session_state.sv_voice_langgate = True
            st.rerun()

    role_lang_key = "hi" if is_hi else "en"
    if st.session_state.sv_voice_role_lang != role_lang_key:
        st.session_state.sv_voice_role_lang = role_lang_key
        render_voice_agent(build_voice_steps(role_lang_key, "role"), role_lang_key)

# -----------------------------------------------------------------------------
# 3. FARMER HOME PAGE (DARK NAVY DESIGN - FRONTEND ONLY)
# -----------------------------------------------------------------------------
elif st.session_state.current_page == "welcome":
    is_hi = (st.session_state.language == "हिंदी")

    # Farmer panel background: the "farmer" image from the project folder
    hero_src = get_image_src(FARMER_FILE, max_width=1600, quality=80)
    hero_img_html = (
        f'<div class="sv-hero-bg"><img src="{hero_src}" alt="Farmer in the field" decoding="async" /></div>'
        if hero_src else ""
    )

    if not is_hi:
        tagline_text = "Smarter Forecasts | Healthier Crops | Stronger Farmers"
        welcome_word = "Welcome to"
        lead_text = "AI-powered hyperlocal weather forecasting and agricultural advisories for a sustainable tomorrow."
        desc_text = (
            "Suksmavrishti provides 1 km × 1 km micro-precision weather data, real-time rain alerts, "
            "and crop-specific advisories designed for the agricultural landscape of Barabanki, Uttar Pradesh."
        )
        cta_label = "☁️  Check Weather Forecast  →"
        cap_title = "Key Capabilities:"
        cap_items = [
            ("1 km × 1 km Micro-Precision:", "Downscaled rain, wind, and convective alerts via machine learning."),
            ("Bhulekh Cadastral Integration:", "Query plot-level advisories directly using your official Khasra number."),
            ("Harvest Protection Directives:", "Actionable alerts for threshing floors, crop drying, and irrigation management."),
        ]
    else:
        tagline_text = "सटीक पूर्वानुमान | स्वस्थ फसलें | समृद्ध किसान"
        welcome_word = "स्वागत है"
        lead_text = "AI-संचालित हाइपरलोकल मौसम पूर्वानुमान एवं कृषि परामर्श — एक सतत कल के लिए।"
        desc_text = (
            "सूक्ष्मवृष्टि उत्तर प्रदेश के बाराबंकी जनपद हेतु 1 किमी × 1 किमी सूक्ष्म-सटीक मौसम आंकड़े, "
            "तात्कालिक वर्षा चेतावनी एवं फसल-विशिष्ट कृषि परामर्श प्रदान करती है।"
        )
        cta_label = "☁️  मौसम का पूर्वानुमान देखें  →"
        cap_title = "प्रमुख विशेषताएं:"
        cap_items = [
            ("1 किमी × 1 किमी सूक्ष्म सटीकता:", "मशीन लर्निंग द्वारा गांव व खेत स्तर पर बारिश व आंधी की सटीक चेतावनी।"),
            ("भूलेख खसरा समन्वय:", "अपने खसरा / गाटा संख्या से अपने खेत के तात्कालिक मौसम की सीधी जानकारी।"),
            ("धान्य संरक्षण निर्देश:", "खलिहान सुरक्षा, कटी फसल तिरपाल आच्छादन और सिंचाई हेतु तात्कालिक दिशा-निर्देश।"),
        ]

    cap_html = "".join(
        [f"<li><b style='color: #38bdf8;'>{title}</b> {body}</li>" for title, body in cap_items]
    )

    # ---- Voice guide: the process walkthrough for the farmer panel ----
    voice_lang_key = "hi" if is_hi else "en"
    voice_steps = None
    if voice_replay:
        voice_steps = build_voice_steps(voice_lang_key, "home")
    elif st.session_state.sv_voice_home_lang != voice_lang_key:
        st.session_state.sv_voice_home_lang = voice_lang_key
        voice_steps = build_voice_steps(voice_lang_key, "home")

    with st.container():
        st.markdown(
            f"""<div class="sv-hero-marker"></div>
            {hero_img_html}
            <div class="sv-hero-copy">
                <div class="sv-hero-tag">{tagline_text}</div>
                <h1 class="sv-hero-title">{welcome_word}<br>
                    <span class="sv-teal">Suksmavrishti</span> <span class="sv-yellow">सूक्ष्मवृष्टि</span>
                </h1>
                <p class="sv-hero-lead">{lead_text}</p>
                <p class="sv-hero-desc">{desc_text}</p>
            </div>
            <div class="sv-hero-slogan"><span class="sv-leaf">🌿</span>Better Weather<br>Better Harvests</div>""",
            unsafe_allow_html=True
        )
        if st.button(cta_label, type="primary"):
            st.session_state.current_page = "forecast"
            st.session_state.sv_voice_fc_pending = True
            st.rerun()

    st.markdown(
        f"""<div class="sv-below-hero">
            <div style="background: rgba(30, 41, 59, 0.55); padding: 18px 22px; border-radius: 12px; border-left: 5px solid #00e5b4;">
                <h4 style="color: #ffd21a; margin-top: 0; margin-bottom: 10px;">{cap_title}</h4>
                <ul style="font-size: 1rem; line-height: 1.75; color: #eaf4ff; margin-bottom: 0;">
                    {cap_html}
                </ul>
            </div>
        </div>""",
        unsafe_allow_html=True
    )

    render_voice_agent(voice_steps, voice_lang_key)
    render_flow_footer(is_hi)

# -----------------------------------------------------------------------------
# 4. WEATHER FORECAST PAGE (TRANSLATED VILLAGE SELECTION)
# -----------------------------------------------------------------------------
elif st.session_state.current_page == "forecast":
    is_hi = (st.session_state.language == "हिंदी")

    # Same hero image as the landing section, used as page background
    fc_bg_src = get_image_src(
        HERO_FILE if st.session_state.role == "chief" else FARMER_FILE,
        max_width=1400, quality=78
    )
    if fc_bg_src:
        st.markdown(
            f'<div class="sv-page-bg"><img src="{fc_bg_src}" alt="" decoding="async" /></div>',
            unsafe_allow_html=True
        )

    back_col, _ = st.columns([3, 7])
    with back_col:
        back_text = "⬅️ वापस स्वागत पृष्ठ पर जाएं" if is_hi else "⬅️ Back to Welcome Page"
        if st.button(back_text, use_container_width=False):
            st.session_state.current_page = "welcome"
            st.rerun()

    title_text = "🌾 खसरा वार मौसम पूर्वानुमान एवं कृषि परामर्श" if is_hi else "🌾 Plot-Level Weather Forecast & Agromet Advisory"
    sub_text = "अपने खेत का खसरा नंबर दर्ज करें और तत्काल 1 किमी x 1 किमी सटीक मौसम व सलाह प्राप्त करें।" if is_hi else "Enter your Khasra Number to get instant 1 km x 1 km downscaled weather and protective directives."
    
    st.markdown(f"<h2 style='color: #4ade80; margin-bottom: 2px;'>{title_text}</h2>", unsafe_allow_html=True)
    st.markdown(f"<p style='color: #cbd5e1; font-size: 1.05rem; margin-bottom: 22px;'>{sub_text}</p>", unsafe_allow_html=True)

    # Search inputs
    f_col1, f_col2 = st.columns([5, 5])
    
    with f_col1:
        khasra_label = "खसरा / गाटा नंबर दर्ज करें (Enter Khasra Number)" if is_hi else "Enter Khasra Number (खसरा नंबर)"
        
        sample_placeholder = "142, 21, 50, 101"
        if df_khasra is not None:
            khasra_col = "khasra_gata_number" if "khasra_gata_number" in df_khasra.columns else "khasra_number"
            samples = [str(x) for x in df_khasra[khasra_col].dropna().unique()[:8]]
            if len(samples) > 2:
                sample_placeholder = f"उदा: {samples[0]}, {samples[1]}, {samples[2]}"

        khasra_input = st.text_input(
            label=khasra_label,
            value="",
            placeholder=sample_placeholder,
            help="Type your cadastral Khasra / Gata number (e.g. 142, 21, 50, 101)"
        )

    with f_col2:
        village_filter_label = "गांव / मौजा चुनें (वैकल्पिक फिल्टर)" if is_hi else "Select Village / Mauza (Optional Filter)"
        
        # Build village choices translated according to selected language
        if df_khasra is not None:
            if is_hi:
                default_option = "सभी गांव"
                if "village_mauza_hi" in df_khasra.columns:
                    v_options = [default_option] + sorted(list(df_khasra["village_mauza_hi"].dropna().unique()))
                else:
                    v_options = [default_option] + sorted(list(df_khasra.get("village_mauza_en", df_khasra.get("village_mauza")).dropna().unique()))
            else:
                default_option = "All Villages"
                v_col_name = "village_mauza_en" if "village_mauza_en" in df_khasra.columns else "village_mauza"
                v_options = [default_option] + sorted(list(df_khasra[v_col_name].dropna().unique()))
            
            selected_village_choice = st.selectbox(
                village_filter_label,
                options=v_options,
                index=0
            )
        else:
            default_option = "सभी गांव" if is_hi else "All Villages"
            selected_village_choice = default_option
            st.selectbox(village_filter_label, options=[default_option], index=0)

    farmer_logged_in = farmer_auth.is_logged_in()
    chief_viewing = (st.session_state.role == "chief" and chief_auth.is_logged_in())
    if not (farmer_logged_in or chief_viewing):
        gate_msg = ("मौसम रिपोर्ट पाने के लिए पहले अपना रजिस्ट्रेशन करें और लॉगिन करें — ताकि OTP और रिपोर्ट आपके नंबर पर SMS से भेजी जा सकें।"
                  if is_hi else
                  "Please register and login first so your OTP and report can be sent "
                  "to your number by SMS.")
        st.info(gate_msg)
        if st.button("🔐 Login / रजिस्ट्रेशन करें", type="primary"):
            st.session_state.current_page = "login"
            st.rerun()
        tell_clicked = False
    else:
        btn_tell_label = "🌤️ मौसम का पूर्वानुमान बताएं (Tell Weather Forecast)" if is_hi else "🌤️ Tell Weather Forecast"
        tell_clicked = st.button(btn_tell_label, type="primary")

    if tell_clicked:
        ok_khasra, clean_input, khasra_err = security.validate_khasra(khasra_input)
        allowed, retry_after = security.limiter.hit(
            f"forecast:{security.client_key()}", *security.FORECAST_RATE_LIMIT
        )

        if not ok_khasra:
            msg = ("कृपया एक मान्य खसरा नंबर दर्ज करें (जैसे 142, 22/1)।" if is_hi
                   else khasra_err)
            st.warning(msg)
        elif not allowed:
            st.warning(
                f"बहुत अधिक अनुरोध। {max(1, retry_after // 60)} मिनट बाद प्रयास करें।" if is_hi
                else f"Too many requests. Please try again in {max(1, retry_after // 60)} minute(s)."
            )
        else:
            matched_row = None
            if df_khasra is not None:
                khasra_col = "khasra_gata_number" if "khasra_gata_number" in df_khasra.columns else "khasra_number"
                filtered_df = df_khasra[df_khasra[khasra_col].astype(str).str.strip().str.lower() == clean_input.lower()]
                
                # Apply village filter if a specific village was chosen
                if selected_village_choice not in ["All Villages", "सभी गांव"] and not filtered_df.empty:
                    if is_hi and "village_mauza_hi" in filtered_df.columns:
                        filtered_df = filtered_df[filtered_df["village_mauza_hi"] == selected_village_choice]
                    else:
                        v_col_name = "village_mauza_en" if "village_mauza_en" in filtered_df.columns else "village_mauza"
                        filtered_df = filtered_df[filtered_df[v_col_name] == selected_village_choice]

                if not filtered_df.empty:
                    matched_row = filtered_df.iloc[0]

            # Extract plot metadata
            if matched_row is not None:
                v_name = matched_row.get("village_mauza_hi" if is_hi else "village_mauza_en", matched_row.get("village_mauza", "Rampur"))
                cell_id = str(matched_row.get("cell_id", "cell_3_2"))
                holder = matched_row.get("landholder_name_hi" if is_hi else "landholder_name_en", "Demo Farmer")
                area_ha = matched_row.get("plot_area_ha", matched_row.get("plot_area_hectares", 1.25))
                area_bigha = matched_row.get("plot_area_bigha", round(float(area_ha) * 3.95, 2))
                crop = matched_row.get("primary_crop", "Mustard / सरसों")
                zone = matched_row.get("zone_descriptor", "Cropland")
                lgd = matched_row.get("village_lgd_code", "147610")
            else:
                v_name = "रामपुर (कटरा)" if is_hi else "Rampur Katra"
                cell_id = "cell_3_2"
                holder = "डेमो खातेदार" if is_hi else "Demo Landholder"
                area_ha = 1.25
                area_bigha = 4.94
                crop = "Mustard (सरसों)"
                zone = "Settlement & Drying Yards"
                lgd = "147610"

            # FETCH PREDICTION FROM THE CONFIGURED MODEL OUTPUT FILES
            (rain_val, wind_val, humid_val,
             data_source, data_updated) = get_live_prediction_for_cell(
                cell_id=cell_id,
                village_name=str(v_name),
                khasra_no=clean_input
            )

            if rain_val is None:
                security.audit("forecast_unavailable", outcome="error",
                               detail={"cell": cell_id, "khasra": clean_input})
                st.warning(
                    "अभी मौसम डेटा उपलब्ध नहीं है, इसलिए कोई अनुमान नहीं दिखाया जा रहा है। "
                    "कृपया बाद में प्रयास करें।"
                    if is_hi else
                    "Weather data is unavailable right now, so no forecast is shown "
                    "rather than a guessed value. Please try again later."
                )
                st.stop()

            humid_txt = "n/a" if humid_val is None else f"{humid_val:.0f}"

            # Rule-based advisory (no model-generated text is rendered)
            advisory = generate_farmer_advisory(
                village_name=str(v_name),
                rain_mm=rain_val,
                wind_kmh=wind_val,
                humidity_pct=humid_val if humid_val is not None else 0.0,
                khasra_no=clean_input,
                zone_descriptor=str(zone)
            )

            # ---- AUTO-DELIVERY: send this report to the registered farmer's ----
            # ---- number via WhatsApp + SMS as soon as the forecast is checked ----
            deliver_status = None
            fm = farmer_auth.current_farmer()
            if farmer_logged_in and fm and fm.get("phone"):
                rep_title = ("\u0938\u0942\u0915\u094d\u0937\u094d\u092e\u0935\u0943\u0937\u094d\u091f\u093f \u092e\u094c\u0938\u092e \u0930\u093f\u092a\u094b\u0930\u094d\u091f" if is_hi else "Suksmavrishti Weather Report")
                report_body = "\n".join([
                    rep_title,
                    ("\u0916\u0938\u0930\u093e" if is_hi else "Khasra") + f": {clean_input}",
                    ("\u0917\u094d\u0930\u093e\u092e / \u092e\u094c\u091c\u093e" if is_hi else "Village") + f": {v_name}",
                    ("\u0935\u0930\u094d\u0937\u093e" if is_hi else "Rain") + f": {rain_val:.1f} mm/hr",
                    ("\u0939\u0935\u093e" if is_hi else "Wind") + f": {wind_val:.1f} km/h",
                    ("\u0906\u0930\u094d\u0926\u094d\u0930\u0924\u093e" if is_hi else "Humidity") + f": {humid_txt}%",
                    ("\u0938\u094d\u0925\u093f\u0924\u093f" if is_hi else "Status") + f": {advisory['level']}",
                    advisory["hi" if is_hi else "en"]["headline"],
                    "- \u0938\u0942\u0915\u094d\u0937\u094d\u092e\u0935\u0943\u0937\u094d\u091f\u093f (Suksmavrishti)",
                ])
                sms_body = (
                    f"Suksmavrishti: Khasra {clean_input}, {v_name} | "
                    f"Rain {rain_val:.1f} mm/hr, Wind {wind_val:.1f} km/h, "
                    f"RH {humid_txt}% | {advisory['level']}"
                )
                wa_avail = farmer_auth.whatsapp_configured()
                sms_avail = farmer_auth.sms_configured()
                wa_ok, wa_err, sms_ok, sms_err = False, "", False, ""
                if wa_avail:
                    wa_ok, wa_err = farmer_auth.send_whatsapp_report(fm["phone"], report_body)
                if sms_avail:
                    sms_ok, sms_err = farmer_auth.send_sms_report(fm["phone"], sms_body)
                deliver_status = (wa_ok, wa_err, sms_ok, sms_err, wa_avail, sms_avail)

            # High-contrast theme styling for both Dark and Light modes
            color_map = {
                "RED": {
                    "bg": "rgba(239, 68, 68, 0.15)",
                    "border": "#ef4444",
                    "badge": "#fca5a5",
                    "label": "🚨 RED ALERT / आपातकालीन चेतावनी"
                },
                "AMBER": {
                    "bg": "rgba(245, 158, 11, 0.15)",
                    "border": "#f59e0b",
                    "badge": "#fde68a",
                    "label": "⚠️ AMBER ADVISORY / मौसम चेतावनी"
                },
                "GREEN": {
                    "bg": "rgba(34, 197, 94, 0.15)",
                    "border": "#22c55e",
                    "badge": "#86efac",
                    "label": "☀️ GREEN / मौसम अनुकूल"
                }
            }
            theme = color_map.get(advisory["level"], color_map["GREEN"])

            # Translucent result panel (glass) with WhatsApp share
            result_panel = st.container()
            with result_panel:
                st.markdown('<div class="sv-result-marker"></div>', unsafe_allow_html=True)
            
                # Advisory Card
                st.markdown(
                    f"""<div style="background-color: {theme['bg']}; border-left: 8px solid {theme['border']}; border: 1px solid {theme['border']}; border-left-width: 8px; padding: 22px; border-radius: 12px; margin-bottom: 25px;">
                        <div style="font-size: 1.15rem; font-weight: 800; color: {theme['badge']}; text-transform: uppercase; margin-bottom: 8px; letter-spacing: 1px;">
                            {theme['label']}
                        </div>
                        <div style="font-size: 1.45rem; font-weight: 700; color: #f8fafc; margin-bottom: 14px; line-height: 1.4;">
                            {security.esc(advisory['hi' if is_hi else 'en']['headline'])}
                        </div>
                        <div style="font-size: 1.12rem; color: #facc15; font-weight: 700; margin-bottom: 8px;">
                            {'किसान के लिए त्वरित निर्देश (Immediate Actions):' if is_hi else 'Recommended Farmer Actions:'}
                        </div>
                        <ul style="font-size: 1.05rem; line-height: 1.8; color: #f1f5f9; margin-top: 4px;">
                            {''.join([f"<li>{security.esc(act)}</li>" for act in advisory['hi' if is_hi else 'en']['actions']])}
                        </ul>
                    </div>""",
                    unsafe_allow_html=True
                )

                # Metrics Row (Bright High-Contrast Cards)
                m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                m_col1.metric("वर्षा दर (Rainfall)" if is_hi else "Rainfall Rate", f"{rain_val:.1f} mm/hr")
                m_col2.metric("हवा की गति (Wind Speed)" if is_hi else "Wind Speed", f"{wind_val:.1f} km/h")
                m_col3.metric(
                    "आर्द्रता (Humidity)" if is_hi else "Relative Humidity",
                    "n/a" if humid_val is None else f"{humid_val:.0f}%"
                )
                m_col4.metric("माइक्रो-सेल (Micro-Cell)" if is_hi else "Assigned Cell", cell_id)

                if data_source:
                    st.caption(
                        f"स्रोत: {data_source} · अंतिम अद्यतन {data_updated} · यह मॉडल आउटपुट है "
                        f"(आधिकारिक IMD पूर्वानुमान नहीं)। सलाह सांकेतिक है, गारंटी नहीं।"
                        if is_hi else
                        f"Source: {data_source} · last updated {data_updated} · model output "
                        f"(not an official IMD forecast). Advisory is indicative, not a guarantee."
                    )

                # Cadastral & Plot Details Card
                st.markdown("<div style='margin-top: 22px;'></div>", unsafe_allow_html=True)
                with st.expander("📋 भूलेख भूखंड विवरण (Cadastral Plot Details)" if is_hi else "📋 Cadastral & Bhulekh Plot Details", expanded=True):
                    p_c1, p_c2, p_c3 = st.columns(3)
                    p_c1.markdown(f"<p style='color: #f1f5f9;'><b>{'खसरा / गाटा सं.' if is_hi else 'Khasra Number'}:</b> {security.esc(clean_input)}</p>", unsafe_allow_html=True)
                    p_c1.markdown(f"<p style='color: #f1f5f9;'><b>{'ग्राम / मौजा' if is_hi else 'Village / Mauza'}:</b> {security.esc(v_name)} (LGD: {security.esc(lgd)})</p>", unsafe_allow_html=True)
                    p_c2.markdown(f"<p style='color: #f1f5f9;'><b>{'खातेदार का नाम' if is_hi else 'Landholder Name'}:</b> {security.esc(holder)}</p>", unsafe_allow_html=True)
                    p_c2.markdown(f"<p style='color: #f1f5f9;'><b>{'रकबा (क्षेत्रफल)' if is_hi else 'Plot Area'}:</b> {security.esc(area_ha)} Ha ({security.esc(area_bigha)} Bigha)</p>", unsafe_allow_html=True)
                    p_c3.markdown(f"<p style='color: #f1f5f9;'><b>{'प्रमुख फसल' if is_hi else 'Primary Crop'}:</b> {security.esc(crop)}</p>", unsafe_allow_html=True)
                    p_c3.markdown(f"<p style='color: #f1f5f9;'><b>{'माइक्रो-ज़ोन' if is_hi else 'Micro-Zone'}:</b> {security.esc(zone)}</p>", unsafe_allow_html=True)
                # Direct WhatsApp share of the forecast result
                wa_head = ("सूक्ष्मवृष्टि खसरा-वार मौसम पूर्वानुमान" if is_hi else "Suksmavrishti Plot-Level Weather Forecast")
                khasra_lbl = "खसरा नंबर" if is_hi else "Khasra No."
                village_lbl = "ग्राम / मौजा" if is_hi else "Village / Mauza"
                rain_lbl = "वर्षा दर" if is_hi else "Rainfall"
                wind_lbl = "हवा की गति" if is_hi else "Wind Speed"
                humid_lbl = "आर्द्रता" if is_hi else "Humidity"
                alert_lbl = "स्थिति" if is_hi else "Alert Level"
                adv_lbl = "सलाह" if is_hi else "Advisory"
                wa_lines = [
                    wa_head,
                    f"{khasra_lbl}: {clean_input}",
                    f"{village_lbl}: {v_name}",
                    f"{rain_lbl}: {rain_val:.1f} mm/hr",
                    f"{wind_lbl}: {wind_val:.1f} km/h",
                    f"{humid_lbl}: {humid_txt}%",
                    f"{alert_lbl}: {advisory['level']}",
                    f"{adv_lbl}: {advisory['hi' if is_hi else 'en']['headline']}",
                    "- Suksmavrishti (सूक्ष्मवृष्टि)",
                ]
                wa_link = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + quote("\n".join(wa_lines))
                share_label = "📲 WhatsApp पर भेजें" if is_hi else "📲 Send to WhatsApp"
                st.markdown(
                    f'''<div class="sv-share-row"><a href="{wa_link}" target="_blank" rel="noopener">{share_label}</a></div>''',
                    unsafe_allow_html=True
                )

                # Delivery status: report was auto-sent to the registered number
                if deliver_status is not None:
                    wa_ok, wa_err, sms_ok, sms_err, wa_avail, sms_avail = deliver_status
                    if not wa_avail and not sms_avail:
                        st.warning(
                            "⚠️ कोई डिलीवरी चैनल सेट नहीं है — .env में TWILIO_WHATSAPP_FROM या TWILIO_SMS_FROM भरें।"
                            if is_hi else
                            "⚠️ No delivery channel configured — set TWILIO_WHATSAPP_FROM or TWILIO_SMS_FROM in .env."
                        )
                    else:
                        sent_to = []
                        if wa_avail and wa_ok:
                            sent_to.append("WhatsApp")
                        if sms_avail and sms_ok:
                            sent_to.append("SMS")
                        if sent_to:
                            st.success(
                                f"📲 {' और '.join(sent_to)} से रिपोर्ट {fm['phone']} पर भेज दी गई है।"
                                if is_hi else
                                f"📲 Report sent to {fm['phone']} via {' and '.join(sent_to)}."
                            )
                        if wa_avail and not wa_ok:
                            st.error(
                                f"WhatsApp नहीं भेजा जा सका: {wa_err}"
                                if is_hi else f"WhatsApp could not be sent: {wa_err}"
                            )
                        if sms_avail and not sms_ok:
                            st.error(
                                f"SMS नहीं भेजा जा सका: {sms_err}"
                                if is_hi else f"SMS could not be sent: {sms_err}"
                            )
                        if not sms_avail:
                            st.info(
                                "SMS अभी निष्क्रिय है (TWILIO_SMS_FROM सेट नहीं) — रिपोर्ट सिर्फ WhatsApp पर भेजी गई।"
                                if is_hi else
                                "SMS is inactive (TWILIO_SMS_FROM not set) — the report was sent on WhatsApp only."
                            )

    # ---- Voice guide on the forecast page (plays on arrival, replay with the navbar) ----
    voice_lang_key = "hi" if is_hi else "en"
    voice_steps = None
    if voice_replay:
        voice_steps = build_voice_steps(voice_lang_key, "forecast")
    elif st.session_state.sv_voice_fc_pending or st.session_state.sv_voice_fc_lang != voice_lang_key:
        voice_steps = build_voice_steps(voice_lang_key, "forecast")
        st.session_state.sv_voice_fc_pending = False
        st.session_state.sv_voice_fc_lang = voice_lang_key
    render_voice_agent(voice_steps, voice_lang_key)

# -----------------------------------------------------------------------------
# 4. LOGIN / REGISTRATION PAGE (Phone + OTP, backed by Supabase + Twilio)
# -----------------------------------------------------------------------------
elif st.session_state.current_page == "login":
    is_hi = (st.session_state.language == "हिंदी")

    fc_bg_src = get_image_src(
        HERO_FILE if st.session_state.role == "chief" else FARMER_FILE,
        max_width=1400, quality=78
    )
    if fc_bg_src:
        st.markdown(
            f'<div class="sv-page-bg"><img src="{fc_bg_src}" alt="" decoding="async" /></div>',
            unsafe_allow_html=True
        )

    back_col, _ = st.columns([3, 7])
    with back_col:
        back_text = "⬅️ वापस स्वागत पृष्ठ पर जाएं" if is_hi else "⬅️ Back to Welcome Page"
        if st.button(back_text, key="login_back"):
            st.session_state.current_page = "welcome"
            st.rerun()

    st.markdown(
        "<h2 style='color: #4ade80; margin-bottom: 2px;'>🔐 "
        + ("किसान लॉगिन / रजिस्ट्रेशन" if is_hi else "Farmer Login / Registration")
        + "</h2>",
        unsafe_allow_html=True
    )
    st.markdown(
        "<p style='color: #cbd5e1; font-size: 1.02rem; margin-bottom: 20px;'>"
        + ("अपना नाम और मोबाइल नंबर दर्ज करें। OTP आपके नंबर पर SMS से आएगा, और सत्यापित होते ही आपका खाता बन जाएगा — हर मौसम रिपोर्ट भी SMS से भेजी जाएगी।"
           if is_hi else
           "Enter your name and mobile number. The OTP arrives on your number by SMS; "
           "once it is verified your account is created and every weather report is "
           "delivered to your number by SMS.")
        + "</p>",
        unsafe_allow_html=True
    )

    flash = farmer_auth.pop_flash()
    if flash:
        st.success(flash)

    if farmer_auth.is_logged_in():
        fm = farmer_auth.current_farmer()
        st.success(
            f"✅ लॉगिन सफल — स्वागत है {fm['name']} (+91{fm['phone']})"
            if is_hi else
            f"✅ Logged in — welcome {fm['name']} (+91{fm['phone']})"
        )
        g_c1, g_c2 = st.columns(2)
        if g_c1.button("🌤️ मौसम पूर्वानुमान देखें" if is_hi else "🌤️ Check Weather Forecast", type="primary"):
            st.session_state.current_page = "forecast"
            st.session_state.sv_voice_fc_pending = True
            st.rerun()
        if g_c2.button("🚪 लॉगआउट" if is_hi else "🚪 Logout"):
            farmer_auth.logout()
            st.rerun()
    else:
        if not farmer_auth.twilio_ready():
            st.warning(
                "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN सेट नहीं हैं — OTP नहीं जा सकेगा। (.env देखें)"
                if is_hi else
                "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set — the OTP cannot be sent. (See .env)"
            )
        elif not farmer_auth.otp_ready():
            st.warning(
                "OTP भेजने का कोई चैनल नहीं — .env में TWILIO_VERIFY_SERVICE_SID (या TWILIO_SMS_FROM) भरें।"
                if is_hi else
                "No OTP channel available — set TWILIO_VERIFY_SERVICE_SID (or TWILIO_SMS_FROM) in .env."
            )
        if not farmer_auth.supabase_ready():
            st.warning(
                "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY सेट नहीं हैं — रजिस्ट्रेशन सेव नहीं होगा। (.env देखें)"
                if is_hi else
                "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set — registrations cannot be saved. (See .env)"
            )

        if not farmer_auth.otp_pending():
            with st.form("sv_register_form"):
                reg_name = st.text_input(
                    "आपका नाम / Your Name" if is_hi else "Your Name (आपका नाम)",
                    placeholder="e.g. Ram Kumar"
                )
                reg_phone = st.text_input(
                    "मोबाइल नंबर / Mobile Number",
                    placeholder="10-digit mobile number",
                    max_chars=12
                )
                reg_village = st.text_input(
                    "गांव / Village (वैकल्पिक)" if is_hi else "Village (optional) / गांव (वैकल्पिक)",
                    placeholder="e.g. Rampur Katra"
                )
                reg_submitted = st.form_submit_button(
                    "📨 OTP भेजें / Send OTP" if is_hi else "📨 Send OTP / OTP भेजें",
                    type="primary"
                )
            if reg_submitted:
                ok, msg = farmer_auth.start_login(reg_phone, reg_name, st.session_state.language)
                if ok:
                    if reg_village.strip():
                        st.session_state["sv_reg_village"] = reg_village.strip()
                    farmer_auth.set_flash(msg)
                    st.rerun()
                else:
                    st.error(msg)
        else:
            st.info(
                f"OTP +91{farmer_auth.pending_phone()} पर भेजा गया है।"
                if is_hi else f"OTP sent to +91{farmer_auth.pending_phone()}."
            )
            otp_col1, otp_col2 = st.columns([3, 2])
            with otp_col1:
                otp_value = st.text_input(
                    "OTP दर्ज करें / Enter OTP",
                    placeholder="6-digit OTP",
                    max_chars=6
                )
            with otp_col2:
                otp_village = st.text_input(
                    "गांव / Village (वैकल्पिक)" if is_hi else "Village (optional) / गांव (वैकल्पिक)",
                    placeholder="e.g. Rampur Katra"
                )
            a_c1, a_c2, a_c3 = st.columns([2, 2, 2])
            if a_c1.button("✔️ लॉगिन करें / Verify & Login", type="primary"):
                if len(otp_value.strip()) != 6:
                    st.error("कृपया 6 अंकों का OTP दर्ज करें।" if is_hi else "Please enter the 6-digit OTP.")
                else:
                    village_final = otp_village.strip() or st.session_state.get("sv_reg_village", "")
                    ok, msg = farmer_auth.verify_otp(otp_value, village_final, st.session_state.language)
                    if ok:
                        st.session_state.current_page = "forecast"
                        st.session_state.sv_voice_fc_pending = True
                        st.rerun()
                    else:
                        st.error(msg)
            if a_c2.button("🔄 नया OTP / Resend OTP"):
                ok, msg = farmer_auth.resend_otp(st.session_state.language)
                farmer_auth.set_flash(msg)
                st.rerun()
            if a_c3.button("✏️ नंबर बदलें / Change Number"):
                farmer_auth.clear_otp()
                st.rerun()

# -----------------------------------------------------------------------------
# 4B. CHIEF LOGIN (demo ID + password per forecast area — NO OTP)
# -----------------------------------------------------------------------------
elif st.session_state.current_page == "chief_login":
    is_hi = (st.session_state.language == "हिंदी")

    fc_bg_src = get_image_src(HERO_FILE, max_width=1400, quality=78)
    if fc_bg_src:
        st.markdown(
            f'<div class="sv-page-bg"><img src="{fc_bg_src}" alt="" decoding="async" /></div>',
            unsafe_allow_html=True
        )

    back_col, _ = st.columns([3, 7])
    with back_col:
        back_text = "⬅️ भूमिका बदलें / Change Panel" if is_hi else "⬅️ Change Panel / भूमिका बदलें"
        if st.button(back_text, key="chief_login_back"):
            st.session_state.role = None
            st.session_state.current_page = "welcome"
            st.rerun()

    st.markdown(
        "<h2 style='color: #4ade80; margin-bottom: 2px;'>🛡️ "
        + ("सरपंच लॉगिन / Chief Login" if is_hi else "Chief Login / सरपंच लॉगिन")
        + "</h2>",
        unsafe_allow_html=True
    )
    st.markdown(
        "<p style='color: #cbd5e1; font-size: 1.02rem; margin-bottom: 20px;'>"
        + ("अपना क्षेत्र (गाँव / मौजा) चुनें और व्यवस्थापक द्वारा दिया गया ID व पासवर्ड डालें — सरपंच लॉगिन में OTP की जरूरत नहीं है।"
           if is_hi else
           "Choose your area (village / mauza) and enter the ID and password issued "
           "by the administrator. The chief login does NOT need an OTP.")
        + "</p>",
        unsafe_allow_html=True
    )

    flash = farmer_auth.pop_flash()
    if flash:
        st.success(flash)

    if chief_auth.is_logged_in():
        ch = chief_auth.current_chief()
        st.success(
            f"✅ लॉगिन सफल — क्षेत्र {ch.get('area', '')} (ID: {ch.get('id', '')})"
            if is_hi else
            f"✅ Logged in — area {ch.get('area', '')} (ID: {ch.get('id', '')})"
        )
        g_c1, g_c2 = st.columns(2)
        if g_c1.button("🛡️ सरपंच पैनल खोलें" if is_hi else "🛡️ Open Chief Panel", type="primary"):
            st.session_state.current_page = "chief"
            st.session_state.sv_voice_chief_lang = ""
            st.rerun()
        if g_c2.button("🚪 लॉगआउट" if is_hi else "🚪 Logout"):
            chief_auth.logout()
            st.rerun()
    else:
        chief_areas = chief_auth.areas_from_khasra(df_khasra)
        chief_auth.ensure_credentials(chief_areas)  # issues/repairs hashed records only

        def _chief_area_label(a):
            return f"{a['en']} ({a['hi']})" if is_hi and a.get("hi", a["en"]) != a["en"] else a["en"]

        with st.form("sv_chief_login_form"):
            ch_area_labels = [_chief_area_label(a) for a in chief_areas]
            ch_area_label = st.selectbox(
                "क्षेत्र चुनें / Choose Area (गाँव / मौजा)",
                options=ch_area_labels,
                index=0,
            )
            ch_id = st.text_input("ID", placeholder="chief_…")
            ch_pass = st.text_input(
                "पासवर्ड / Password" if is_hi else "Password / पासवर्ड",
                placeholder="••••••••",
                type="password",
            )
            ch_submitted = st.form_submit_button(
                "🔓 लॉगिन करें / Login" if is_hi else "🔓 Login / लॉगिन करें",
                type="primary",
            )
        if ch_submitted:
            chosen_area = ch_area_labels.index(ch_area_label)
            chosen_area = chief_areas[chosen_area]["en"]
            ok, msg = chief_auth.login(chosen_area, ch_id, ch_pass, chief_areas)
            if ok:
                farmer_auth.set_flash(msg)
                st.rerun()
            else:
                st.error(msg)

        # Security: credentials are never displayed in the browser. Passwords are
        # hashed on disk and can only be set/rotated from the server.
        st.caption(chief_auth.credentials_hint(is_hi))

# -----------------------------------------------------------------------------
# 5. SARPANCH PANEL (village monitoring + report verification for AI training)
# -----------------------------------------------------------------------------
elif st.session_state.current_page == "chief":
    if not chief_auth.is_logged_in():
        st.session_state.current_page = "chief_login"
        st.rerun()

    is_hi = (st.session_state.language == "हिंदी")

    chief_src = get_image_src(HERO_FILE, max_width=1600, quality=80)
    chief_img_html = (
        f'<div class="sv-hero-bg"><img src="{chief_src}" alt="Village fields" decoding="async" /></div>'
        if chief_src else ""
    )

    # Verification photos collected for THIS chief's area only
    ch_me = chief_auth.current_chief()
    ch_area = ch_me.get("area", "")
    ch_id = ch_me.get("id", "")
    my_feedback = feedback_for_chief(ch_id)
    sample_count = 0 if my_feedback is None else len(my_feedback)

    if chief_auth.needs_password_change():
        st.info(
            "🔐 आप अभी स्वतः बनाया गया पासवर्ड उपयोग कर रहे हैं — कृपया सर्वर पर "
            "`python setup_chief_credentials.py --area \"" + str(ch_area) + "\"` चलाकर नया पासवर्ड सेट करें।"
            if is_hi else
            "🔐 You are signed in with an auto-generated password. Please set your own on "
            "the server: `python setup_chief_credentials.py --area \"" + str(ch_area) + "\"`."
        )

    if is_hi:
        chief_tag = "सरपंच पैनल  |  गाँव की मौसम निगरानी"
        chief_title = "गाँव का मौसम<br><span class='sv-teal'>सत्यापन</span> <span class='sv-yellow'>एवं सुधार</span>"
        chief_lead = "मौसम पूर्वानुमान देखें, और रिपोर्ट सही या गलत होने पर खेत की फोटो अपलोड करके पूर्वानुमान को और सटीक बनाएँ।"
        chief_desc = (
            "सरपंच पैनल में किसान के सभी विकल्प उपलब्ध हैं — खसरा वार पूर्वानुमान और फसल सलाह। "
            "साथ ही आप रिपोर्ट का सत्यापन कर सकते हैं: रिपोर्ट सही थी या गलत, यह बताकर खेत की फोटो जमा करें। "
            "यही फोटो और फीडबैक आगे मौसम पूर्वानुमान मॉडल को प्रशिक्षित करने में काम आते हैं।"
        )
        chief_slogan = "<span class='sv-leaf'>🌿</span>गाँव का मौसम<br>सरपंच द्वारा सत्यापित"
        fc_label = "☁️  मौसम का पूर्वानुमान देखें  →"
        up_label = "📷  रिपोर्ट सत्यापन / फोटो अपलोड"
        cap_title = "सरपंच के काम:"
        cap_items = [
            ("पूर्वानुमान जाँचें:", "अपने गाँव के खसरा नंबर से मौसम और फसल सलाह देखें।"),
            ("सही रिपोर्ट की पुष्टि:", "मौसम रिपोर्ट सही निकली तो हरी पुष्टि के साथ खेत की फोटो अपलोड करें।"),
            ("गलत रिपोर्ट सुधारें:", "रिपोर्ट गलत निकली तो लाल फीडबैक के साथ फोटो भेजें, जिससे AI अगली बार और सटीक हो।"),
        ]
        stat_photos = "जमा फोटो"
        stat_panel = "पैनल"
        stat_place = "स्थान"
    else:
        chief_tag = "Sarpanch Panel  |  Village weather monitoring"
        chief_title = "Village Weather<br><span class='sv-teal'>Verification</span> <span class='sv-yellow'>& Improvement</span>"
        chief_lead = "Check the forecast, and upload a photo of the field to tell whether the report was right or wrong — so the next forecast is more accurate."
        chief_desc = (
            "The Sarpanch panel keeps every farmer option — plot-level forecast and crop advisory. "
            "On top of that you can verify a report: mark it correct or wrong and attach a photo of the field. "
            "These photos and feedback are used to retrain the weather forecasting model."
        )
        chief_slogan = "<span class='sv-leaf'>🌿</span>Village Weather<br>Verified by Sarpanch"
        fc_label = "☁️  Check Weather Forecast  →"
        up_label = "📷  Verify Report / Upload Photo"
        cap_title = "What the Sarpanch does:"
        cap_items = [
            ("Check the forecast:", "Open the khasra-wise forecast and crop advisory for your village."),
            ("Confirm a correct report:", "If the forecast was right, upload a field photo with a green confirmation."),
            ("Correct a wrong report:", "If it was wrong, send a red feedback photo so the AI learns and improves."),
        ]
        stat_photos = "Photos collected"
        stat_panel = "Panel"
        stat_place = "Location"

    cap_html = "".join(
        [f"<li><b style='color: #38bdf8;'>{title}</b> {body}</li>" for title, body in cap_items]
    )

    # ---- Voice guide for the sarpanch panel ----
    voice_lang_key = "hi" if is_hi else "en"
    voice_steps = None
    if voice_replay:
        voice_steps = build_voice_steps(voice_lang_key, "chief")
    elif st.session_state.sv_voice_chief_lang != voice_lang_key:
        st.session_state.sv_voice_chief_lang = voice_lang_key
        voice_steps = build_voice_steps(voice_lang_key, "chief")

    with st.container():
        st.markdown(
            f"""<div class="sv-hero-marker"></div>
            {chief_img_html}
            <div class="sv-hero-copy">
                <div class="sv-hero-tag">{chief_tag}</div>
                <h1 class="sv-hero-title">{chief_title}</h1>
                <p class="sv-hero-lead">{chief_lead}</p>
                <p class="sv-hero-desc">{chief_desc}</p>
            </div>
            <div class="sv-hero-slogan">{chief_slogan}</div>""",
            unsafe_allow_html=True
        )
        chief_c1, chief_c2 = st.columns(2)
        with chief_c1:
            if st.button(fc_label, type="primary", key="chief_forecast", width="stretch"):
                st.session_state.current_page = "forecast"
                st.session_state.sv_voice_fc_pending = True
                st.rerun()
        with chief_c2:
            if st.button(up_label, type="primary", key="chief_upload_go", width="stretch"):
                st.session_state.current_page = "upload"
                st.rerun()

    st.markdown(
        f"""<div class="sv-below-hero">
            <div class="sv-chief-stats">
                <div class="sv-chief-stat"><b>{sample_count}</b><span>{stat_photos}</span></div>
                <div class="sv-chief-stat"><b>{'सरपंच' if is_hi else 'Sarpanch'}</b><span>{stat_panel}</span></div>
                <div class="sv-chief-stat"><b>Barabanki</b><span>{stat_place}</span></div>
            </div>
            <div style="background: rgba(30, 41, 59, 0.55); padding: 18px 22px; border-radius: 12px; border-left: 5px solid #ffd21a; margin-top: 16px;">
                <h4 style="color: #ffd21a; margin-top: 0; margin-bottom: 10px;">{cap_title}</h4>
                <ul style="font-size: 1rem; line-height: 1.75; color: #eaf4ff; margin-bottom: 0;">
                    {cap_html}
                </ul>
            </div>
        </div>""",
        unsafe_allow_html=True
    )

    render_voice_agent(voice_steps, voice_lang_key)
    render_flow_footer(is_hi)

# -----------------------------------------------------------------------------
# 6. SARPANCH: WEATHER REPORT VERIFICATION + PHOTO UPLOAD (AI training data)
# -----------------------------------------------------------------------------
elif st.session_state.current_page == "upload":
    is_hi = (st.session_state.language == "हिंदी")

    up_bg = get_image_src(HERO_FILE, max_width=1400, quality=78)
    if up_bg:
        st.markdown(
            f'<div class="sv-page-bg"><img src="{up_bg}" alt="" decoding="async" /></div>',
            unsafe_allow_html=True
        )

    back_col, _ = st.columns([3, 7])
    with back_col:
        back_text = "⬅️ सरपंच पैनल पर वापस जाएँ" if is_hi else "⬅️ Back to Sarpanch Panel"
        if st.button(back_text, key="upload_back"):
            st.session_state.current_page = "chief"
            st.rerun()

    if not (st.session_state.role == "chief" and chief_auth.is_logged_in()):
        st.warning(
            "यह पेज सिर्फ लॉगिन किए हुए सरपंच के लिए है।" if is_hi
            else "This page is only available to a logged-in Sarpanch."
        )
    else:
        chief_me = chief_auth.current_chief()
        chief_actor = chief_me.get("id", "")
        chief_area = chief_me.get("area", "")

        up_title = "📷 मौसम रिपोर्ट सत्यापन एवं फोटो अपलोड" if is_hi else "📷 Verify the Weather Report & Upload a Photo"
        up_sub = (
            "रिपोर्ट सही थी या गलत — बताइए और खेत की फोटो जमा कीजिए। यही फोटो मौसम पूर्वानुमान AI को प्रशिक्षित करने में उपयोग होगी।"
            if is_hi else
            "Tell us whether the report was right or wrong and attach a photo of the field. "
            "These photos are used to train the weather forecasting AI."
        )
        st.markdown(f"<h2 style='color: #4ade80; margin-bottom: 2px;'>{up_title}</h2>", unsafe_allow_html=True)
        st.markdown(f"<p style='color: #cbd5e1; font-size: 1.02rem; margin-bottom: 18px;'>{up_sub}</p>", unsafe_allow_html=True)

        form_col, preview_col = st.columns([3, 2])

        with form_col:
            photo = st.file_uploader(
                ("खेत / मौसम की फोटो चुनें (JPG, PNG)" if is_hi else "Choose a field / weather photo (JPG, PNG)"),
                type=["png", "jpg", "jpeg", "webp"],
                key="chief_photo",
            )
            verdict_options = [
                "सही थी / Correct" if is_hi else "Correct / सही थी",
                "गलत थी / Wrong" if is_hi else "Wrong / गलत थी",
            ]
            verdict = st.radio(
                ("मौसम रिपोर्ट सही थी या गलत?" if is_hi else "Was the weather report correct or wrong?"),
                options=verdict_options,
                index=None,
                horizontal=True,
                key="chief_verdict",
            )
            khasra_fb = st.text_input(
                ("खसरा / गाटा नंबर (वैकल्पिक)" if is_hi else "Khasra / Gata number (optional)"),
                key="chief_khasra",
                placeholder="142",
            )
            village_fb = st.text_input(
                ("गांव / मौजा (वैकल्पिक)" if is_hi else "Village / Mauza (optional)"),
                key="chief_village",
                placeholder="Rampur Katra",
            )
            note_fb = st.text_area(
                ("अपनी बात लिखें (वैकल्पिक)" if is_hi else "Your note (optional)"),
                key="chief_note",
                placeholder=(
                    "जैसे: दोपहर में तेज़ हवा चली, बारिश नहीं हुई।"
                    if is_hi else
                    "e.g. Strong wind at noon, but there was no rain."
                ),
                height=90,
            )
            submit_label = "✅ फीडबैक जमा करें" if is_hi else "✅ Submit feedback"
            submitted = st.button(submit_label, type="primary", key="chief_submit", width="stretch")

        with preview_col:
            if photo is not None:
                st.image(photo, width="stretch")
            else:
                no_photo = "फोटो चुनने पर यहाँ प्रीव्यू दिखेगा।" if is_hi else "The photo preview appears here."
                st.markdown(
                    f"<div class='sv-upload-card'><h4>{'फोटो प्रीव्यू' if is_hi else 'Photo preview'}</h4><p>{no_photo}</p></div>",
                    unsafe_allow_html=True,
                )

        if submitted:
            ok_khasra, khasra_clean, khasra_err = (
                security.validate_khasra(khasra_fb) if str(khasra_fb).strip()
                else (True, "", "")
            )
            ok_village, village_clean, village_err = security.validate_village(village_fb)
            ok_note, note_clean, note_err = security.validate_note(note_fb)
            allowed, retry_after = security.limiter.hit(
                f"upload:{security.client_key()}", *security.UPLOAD_RATE_LIMIT
            )

            if photo is None:
                st.error("कृपया पहले खेत की फोटो चुनें।" if is_hi else "Please choose a field photo first.")
            elif verdict is None:
                st.error(
                    "कृपया बताएँ रिपोर्ट सही थी या गलत।" if is_hi else
                    "Please tell us whether the report was correct or wrong."
                )
            elif not (ok_khasra and ok_village and ok_note):
                detail_err = khasra_err or village_err or note_err
                st.error("दर्ज की गई जानकारी मान्य नहीं है। " + security.esc(detail_err)
                         if is_hi else detail_err)
            elif not allowed:
                st.warning(
                    f"बहुत अधिक अपलोड। {max(1, retry_after // 60)} मिनट बाद प्रयास करें।" if is_hi
                    else f"Too many uploads. Please try again in {max(1, retry_after // 60)} minute(s)."
                )
            else:
                # Server-side verification: real decodable image, size cap, then
                # re-encode as JPEG so no payload or metadata is stored.
                raw_bytes = photo.getvalue()
                content, img_err = security.validate_image_upload(photo.name, raw_bytes)
                if content is None:
                    security.audit("upload_rejected", actor=chief_actor, outcome="denied",
                                   detail={"reason": img_err, "size": len(raw_bytes)})
                    st.error(
                        "फोटो स्वीकार नहीं की गई — केवल असली JPG/PNG/WEBP फोटो (5 MB तक)।"
                        if is_hi else
                        f"That photo was rejected — only real JPG/PNG/WEBP images up to 5 MB. ({img_err})"
                    )
                else:
                    try:
                        os.makedirs(UPLOAD_DIR, exist_ok=True)
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        verdict_key = "correct" if verdict == verdict_options[0] else "wrong"
                        area_slug = security.safe_slug(chief_area, 24)
                        fname = (
                            f"{stamp}_{verdict_key}_{area_slug}_"
                            f"{security.make_session_token()[:8]}.jpg"
                        )
                        target = os.path.join(UPLOAD_DIR, fname)
                        if not security.inside_directory(target, UPLOAD_DIR):
                            raise ValueError("unsafe upload path")
                        with open(target, "wb") as fh:
                            fh.write(content)

                        file_exists = os.path.exists(FEEDBACK_CSV)
                        with open(FEEDBACK_CSV, "a", newline="", encoding="utf-8") as fh:
                            writer = csv.writer(fh)
                            if not file_exists:
                                writer.writerow([
                                    "timestamp", "verdict", "khasra", "village", "note",
                                    "photo_file", "uploaded_by", "language"
                                ])
                            writer.writerow([
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                verdict_key,
                                security.csv_safe(khasra_clean),
                                security.csv_safe(village_clean),
                                security.csv_safe(note_clean),
                                fname,
                                security.csv_safe(chief_actor),
                                security.csv_safe(st.session_state.language),
                            ])
                        security.audit("report_submitted", actor=chief_actor, outcome="allow",
                                       detail={"area": chief_area, "verdict": verdict_key,
                                               "file": fname, "khasra": khasra_clean})
                        st.success(
                            f"✅ फीडबैक जमा हो गया ({verdict_key})। फोटो {fname} के नाम से सुरक्षित है। "
                            f"यह एक अपुष्ट फील्ड अवलोकन है — इसे बारिश के माप के प्रमाण न मानें; मॉडल इसे सत्यापन के बाद ही प्रयोग करता है।"
                            if is_hi else
                            f"✅ Feedback saved ({verdict_key}). The photo is stored as {fname}. "
                            f"It is an unverified field observation — a photo alone is not proof of a "
                            f"rainfall measurement, and reports are only used for training after review."
                        )
                        load_feedback_log.clear()
                    except Exception as exc:
                        security.audit("upload_failed", actor=chief_actor, outcome="error",
                                       detail={"error": str(exc)})
                        st.error(
                            "फीडबैक सेव नहीं हो सका। कृपया दोबारा प्रयास करें।" if is_hi
                            else "Could not save the feedback. Please try again."
                        )

        # Last few submissions of THIS chief only (no cross-area exposure)
        my_rows = feedback_for_chief(chief_actor)
        if my_rows is not None:
            try:
                if not my_rows.empty:
                    recent = my_rows.tail(5).iloc[::-1]
                    st.markdown(
                        f"<h4 style='color:#ffd21a; margin-top:22px;'>{'हाल के सत्यापन' if is_hi else 'Recent verifications'}</h4>",
                        unsafe_allow_html=True,
                    )
                    st.dataframe(recent, width="stretch", hide_index=True)
            except Exception:
                pass

        render_flow_footer(is_hi)
