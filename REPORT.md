# SUKSMAVRISHTI — Project Report
**Date:** 30 October 2026 (updated after the security + performance pass — see `SECURITY.md`)
**Scope:** Aaj ka kaam (process + changes), website ka poora overview, aur uska workflow.

---

## 1. Website kya hai

**Suksmavrishti (सूक्ष्मवृष्टि)** — Barabanki (Uttar Pradesh) ke liye **micro-level weather forecasting website**:

- **Khasra-wise (खसरा) weather forecast** — official Bhulekh land data par aadharit (12 villages/mauza, 3 tehsils: Fatehpur, Nawabganj, Ramnagar)
- **Agromet / crop advisory** (फसल सलाह) aur **voice guide** (हिंदी + English)
- **SMS report delivery** (Twilio) aur **farmer registration** (Supabase)
- **AI training loop**: sarpanch ki report verification + field photos se model retrain hota hai

### Tech stack / files

| Layer | Files |
|---|---|
| UI (Streamlit, EN/HI) | `App.py` (~2,660 lines) |
| Auth — farmer OTP (Twilio Verify + Supabase) | `farmer_auth.py` |
| Auth — chief login (demo ID/password, no OTP) | `chief_auth.py` + `chief_credentials.json` (नया) |
| Weather data fetch | `fetch_open_meteo.py` |
| ML training / inference | `train_xgboost.py`, `predict_grid.py`, `downscaler_xgboost.pkl` |
| Terrain grid | `generate_terrain_grid.py`, `terrain_grid_8x8.csv` |
| Advisory engine | `agromet_advisory.py`, `agromet_advisory_engine.py` |
| Alert/report dispatch | `alert_dispatcher.py` |
| Data | `bhulekh_khasra_grid_mapping_demo_names.csv`, `barabanki_historical_sample.csv`, `latest_inference_output.csv`, `latest_khasra_weather_forecast.csv` |
| Config | `.env` (Supabase + Twilio), `.env.example` |

---

## 2. Aaj ka process & changes (step by step)

### Step 1 — Naya Twilio account connect
- `.env` updated: `TWILIO_ACCOUNT_SID=AC7aa5a83…`, `TWILIO_AUTH_TOKEN=82c8e68…` (naya account), API auth verified
- User decision: **sirf SMS OTP, WhatsApp nahi; messaging baad mein** → `TWILIO_SMS_FROM` aur `TWILIO_WHATSAPP_FROM` dono khali chhode

### Step 2 — Twilio Verify (OTP service) app me connect (`farmer_auth.py`)
- Naye functions: `verify_configured()`, `otp_ready()`, `_verify_service()`
- `start_login()` → Verify API se OTP (`channel=sms`, `ttl=10 min`); service: **"AAYUVORA OTP", 6-digit code**
- `verify_otp()` → Verify `verification_checks` se check; **approved hone par hi** Supabase me farmer row + login session
- Galat OTP → "Wrong OTP. 5 attempt(s) left." · zyada attempts → rok · Verify SID na mile to purana local-OTP fallback
- `App.py` ki config warning ab `otp_ready()` par · `.env.example` SMS-only documented

### Step 3 — UI copy SMS-only
- Login/gate pages se "WhatsApp व SMS" hataya → sirf **SMS** (EN + HI)

### Step 4 — Alag-alag login pages (farmer vs chief)

**Pehle:** ek hi login page (farmer OTP wala) dono ke liye, aur sarpanch ka panel **bina login** khulta tha.

**Ab:**

| Change | File |
|---|---|
| Naya `chief_auth.py` — demo ID+password per forecast area, no OTP; area list Bhulekh CSV se; credentials auto-generate | new file |
| `chief_credentials.json` — 12 areas ke demo credentials (editable) | new file |
| Role page → "Login as Chief" ab **chief login page** par le jata hai (pehle seedha panel) | `App.py` |
| Naya **Chief Login page**: kshetra dropdown + ID + password + "Show demo ID/password" expander | `App.py` |
| **Panel guard**: bina login chief panel nahi khulega → login par redirect | `App.py` |
| Navbar: role ke hisab se sahi login — chief ko "Chief Login"/area name, farmer ko "Login"/name; logout bhi sahi page par | `App.py` |
| Forecast page: logged-in chief bhi forecast dekh sakta hai | `App.py` |
| **Farmer login bilkul jaisa tha waisa hi chhoda** (phone + OTP) | — |

---

## 3. Website ka workflow

```
SPLASH → Bhasha (English / हिंदी) → Bhoomika chunein
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
 👨‍🌾 Login as Farmer      🛡️ Login as Chief
        │                       │
 FARMER HOME               CHIEF LOGIN (no OTP)
 → Naam + 10-digit mobile      → Kshetra (gaon/mauza)
 → Village (optional)            chunein + demo ID
 📨 OTP (Twilio Verify SMS)      + password
 → 6-digit verify                │
 → Khasea number + village   SARPANCH PANEL
   filter → forecast +        ├─ ☁️ Check Weather Forecast
   crop advisory              │   (kisan wale saare vikalp)
 → Report SMS se number par   ├─ 📷 Verify Report / Upload Photo
 → Voice guide har step par   │     → sahi/galat feedback + khet
                                │       ki photo → AI retraining
                                └─ Logout / Switch panel
```

**Backend data flow:**

```
fetch_open_meteo.py (weather API)
        ↓
train_xgboost.py (XGBoost downscaler, historical Barabanki data)
        ↓
generate_terrain_grid.py (8×8 micro-terrain + Bhulekh cells)
        ↓
predict_grid.py → latest_inference_output.csv
                → latest_khasra_weather_forecast.csv
        ↓
agromet_advisory_engine.py (crop advisory)
        ↓
App.py (khasra-wise display) + farmer_auth.py (OTP + SMS report)
        ↓
Supabase (farmers table) · Twilio (OTP SMS)
```

---

## 4. 🔑 Chief (Sarpanch) credentials — naya secure system

**Purani `SV2026_<area>` demo passwords ab band ho gayi hain.** Woh password
public report aur committed JSON me the, isliye security pass me sab rotate kar
diye gaye. Ab:

- `chief_credentials.json` me sirf **PBKDF2-HMAC-SHA256 hash** (per-record salt, 600k rounds) hai — plaintext password kahin nahi.
- App khud koi predictable password nahi banata. Naye kshetra ke liye ek **random password** बनता है jo `python setup_chief_credentials.py --list` aur `.chief_credentials_initial.txt` (one-time, git-ignored, chmod 600) se milta hai.
- Password dekhna/badalna sirf server par:

```bash
python setup_chief_credentials.py --area "Rampur"   # naya password set karein
python setup_chief_credentials.py --list            # areas + IDs (password nahi)
python setup_chief_credentials.py --rotate-all      # sab rotate
```

Login page par koi credential **display nahi** hota (purana "Show demo ID/password" expander hata diya gaya hai). 5 galat koshish ke baad 15 minute ka lockout hai.

> ID format waise hi hai: `chief_<area_slug>`, e.g. `chief_rampur`.

---

## 5. Verification status (aaj)

| Check | Result |
|---|---|
| `py_compile` App.py + chief_auth.py | ✅ |
| chief_auth unit tests (12 areas, sahi/galat password, empty, galat area, logout, CSV-missing fallback) | ✅ all pass |
| Browser flow (Chrome/Playwright): role page → both logins → chief login (no OTP form) → galat password reject → sahi credentials → success → panel → navbar "Rampur" → logout → switch → farmer OTP login unchanged | ✅ **7/7 pass, 0 page errors** |
| Screenshots — chief login page + panel | ✅ visually correct |
| Server log | ✅ 0 errors (sirf purana `st.components.v1.html` deprecation warning) |
| Server | ✅ `localhost:8501` live (200) |
| Farmer OTP flow (mocked Verify) | ✅ send → wrong-code attempts → correct → Supabase row + session |

### ⏳ Pending (sirf user ki taraf se — 1 minute)
Twilio **OTP SMS ka live delivery** trial restriction par roka hai: console par
**Try out Verify → Send a verification → To: +91 9837286714 → code enter karein**.
Uske baad pura OTP login browser me live chalega.

### Aage (baad me)
- Report/SMS **messaging** connect (user ne kaha "baad mein")
- Bharat me production ke liye **paid Twilio account + DLT-registered sender + approved OTP template**
- Trial 30 din me khatam hota hai
