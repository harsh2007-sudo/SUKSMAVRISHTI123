# SUKSMAVRISHTI — सूक्ष्मवृष्टि
## Website Analysis & Feature Report

**Tagline:** Smarter Forecasts · Healthier Crops · Stronger Farmers
**Hindi tagline:** सटीक पूर्वानुमान · स्वस्थ फसलें · समृद्ध किसान
**Slogan (splash):** सर्वप्रयत्नेन धान्यसंरक्षणम् — *protect the harvest with every effort*
**Region:** Barabanki district, Uttar Pradesh (Fatehpur · Nawabganj · Ramnagar tehsils)
**Report date:** 30 October 2026 · **Version:** post security + performance hardening

---

## 1. Executive Summary

Suksmavrishti is a **Khasra-level (plot-level) weather intelligence and agromet advisory website** for Indian farmers. A farmer enters the **Khasra/Gata number** from their Bhulekh land record and instantly receives a hyperlocal rain/wind forecast, a colour-coded advisory (RED / AMBER / GREEN) with concrete field actions in **Hindi and English**, and the same report delivered to their phone.

A second role, the **Sarpanch (Chief)**, gets the same forecast plus a **verification workflow**: mark a report correct or wrong, attach a field photo, and feed that back into model retraining.

| Snapshot | Value |
|---|---|
| Users / roles | 2 — Farmer (phone + OTP) and Chief/Sarpanch (ID + password) |
| Pages / screens | 9 screens (splash, language, role, farmer home, forecast, farmer login, chief login, chief panel, upload) |
| Languages | English + हिंदी (full UI, advisories and voice guide) |
| Cadastral coverage in demo data | 192 plots · 174 unique Khasra numbers · 64 micro-cells · 12 villages · 3 tehsils |
| Forecast grid | 8 × 8 = 64 downscaled micro-cells |
| Advisory levels | 3 (RED ≥ 3 mm/hr, AMBER, GREEN) |
| Delivery channels | On-screen · SMS · WhatsApp · WhatsApp share link · browser voice |
| Automation tests | 54 passing (unit + headless app) |
| Security posture | Hardened: RBAC, hashed credentials, rate limits, audit log, verified uploads |
| Measured page weight | 744 KB images vs 10.9 MB before (−93.2 %) |

---

## 2. The Problem It Solves

- Block-level government forecasts are too coarse for a **1–2 hectare plot**. A farmer in Rampur and a farmer in Fatehpur can face very different rain within the same hour.
- Warnings arrive as generic text, not as **actions** (“cover the threshing floor now”, “stop spraying for 8 hours”).
- Farmers think in **Khasra numbers**, not latitude/longitude — so the product starts from the land record.
- Field-level ground truth is missing for model improvement — hence the **Sarpanch verification loop**.

**Value proposition:** *plot-level forecast → crop-specific action → delivered on the farmer's own phone, in their own language.*

---

## 3. What the Website Is (Product Definition)

Suksmavrishti is a **Streamlit web application** (server-rendered, single Python backend) that combines four things:

1. **A cadastral gateway** — Bhulekh Khasra → micro-cell → forecast.
2. **A downscaling engine** — XGBoost model turning coarse regional weather into 64 micro-cell predictions.
3. **An advisory engine** — deterministic agromet rules producing bilingual, actionable directives.
4. **A delivery + feedback layer** — SMS/WhatsApp reports to farmers, verified field photos from sarpanches.

It is **not** a generic weather website: every number shown can be traced to a Khasra record, a micro-cell and a model output file with a timestamp.

---

## 4. Site Map & User Journey

```
                        ┌─────────────────────────┐
                        │  SPLASH (logo + slogan)  │  1.2 s
                        └────────────┬────────────┘
                                     ▼
                        ┌─────────────────────────┐
                        │  CHOOSE LANGUAGE         │  English / हिंदी
                        └────────────┬────────────┘
                                     ▼
                        ┌─────────────────────────┐
                        │  CHOOSE ROLE             │  किसान / सरपंच
                        └───────┬─────────┬───────┘
                                │         │
             ┌──────────────────▼──┐   ┌──▼────────────────────┐
             │  FARMER HOME        │   │  CHIEF LOGIN          │
             │  hero + capabilities│   │  area + ID + password │
             │  voice guide        │   └──────────┬────────────┘
             └──────────┬──────────┘              ▼
                        ▼              ┌───────────────────────┐
             ┌──────────────────────┐  │  SARPANCH PANEL        │
             │  FARMER LOGIN        │  │  stats + 2 actions     │
             │  name + mobile → OTP │  └───────┬───────────────┘
             └──────────┬───────────┘          │
                        ▼                      ▼
             ┌──────────────────────────────────────────────┐
             │  FORECAST PAGE (Khasra + village filter)      │
             │  → advisory card → metrics → plot details     │
             │  → WhatsApp share → report sent to phone      │
             └──────────────────────┬───────────────────────┘
                                    ▼
                        ┌─────────────────────────┐
                        │  CHIEF: VERIFY / UPLOAD  │
                        │  verdict + photo + note  │
                        └─────────────────────────┘
```

**Persistent elements:** navbar (logo, “Barabanki, UP” chip, voice replay, login/user + logout), floating WhatsApp button, footer “switch panel”.

---

## 5. Feature Catalogue

### 5.1 Farmer Panel

| # | Feature | What it does | Notes |
|---|---|---|---|
| 1 | **Language gate** | One choice drives the whole site, advisories and voice | English / हिंदी |
| 2 | **Farmer home** | Hero with field imagery, capability cards, single CTA | Voice guide auto-plays |
| 3 | **Registration + login** | Name + 10-digit mobile (+ optional village) | Phone validated to Indian mobile format |
| 4 | **OTP verification** | 6-digit code over SMS (Twilio Verify) | 10-min expiry · 5 attempts · resend · change number |
| 5 | **Khasra lookup** | Enter `142`, `22/1`, `21क` … | Server-side validated, multilingual-safe |
| 6 | **Village / Mauza filter** | Optional narrowing across 12 villages | Village names in Hindi too |
| 7 | **Weather result** | Rain (mm/hr), wind (km/h), humidity, micro-cell | Source file + last-updated time shown |
| 8 | **Advisory card** | RED / AMBER / GREEN with 3–4 actions | Bilingual, crop- and zone-aware |
| 9 | **Cadastral details** | Khasra, village + LGD code, landholder, area (ha/bigha), crop, micro-zone | Expandable panel |
| 10 | **Report delivery** | Same report auto-sent to the logged-in farmer's number | WhatsApp + SMS when configured |
| 11 | **WhatsApp share** | One tap to forward the forecast | Pre-filled bilingual message |
| 12 | **Session** | 12-hour farmers session, secure logout | No password stored for farmers |

### 5.2 Chief / Sarpanch Panel

| # | Feature | What it does | Notes |
|---|---|---|---|
| 1 | **Area-scoped login** | Each village/mauza has its own ID | 12 areas, hashed passwords |
| 2 | **Panel dashboard** | Photos collected (own area), panel/location stats | No cross-area data shown |
| 3 | **Forecast access** | Same Khasra forecast as farmers | No SMS to farmer from chief view |
| 4 | **Report verification** | “Correct” / “Wrong” verdict | Stored with timestamp + chief ID |
| 5 | **Field photo upload** | JPG/PNG/WEBP up to 5 MB | Content-verified and re-encoded |
| 6 | **Context fields** | Optional Khasra, village, free note | Length-capped, injection-safe |
| 7 | **Recent verifications** | Last 5 of that chief only | Area privacy enforced |
| 8 | **Training loop** | Photo + verdict become retraining candidates | Used only after review |

### 5.3 Shared / Platform Features

| Feature | Description |
|---|---|
| **Bilingual UI** | Every label, advisory and voice script in English + हिंदी |
| **Voice guide** | Browser speech synthesis walks the user through each screen, highlights the control being described, replay button in navbar |
| **Responsive layout** | Dedicated breakpoints for laptop, tablet, small tablet and phone; icon-only controls on narrow screens |
| **Dark agro-tech theme** | Custom CSS design system, high-contrast advisory cards |
| **WhatsApp help button** | Floating button on every page pre-filled with a Hindi greeting |
| **Honest data labelling** | “Model output, not an official IMD forecast”; advisory marked indicative, not a guarantee; photos marked unverified observations |
| **Graceful failure** | If forecast data is missing → explicitly says unavailable instead of showing a guess |

---

## 6. Weather Intelligence Engine

```
Open-Meteo API (live regional forcing)
        │  rain, humidity, wind, cloud, CAPE, temp
        ▼
predict_grid.py  ◄── terrain_grid_8x8.csv  (real NASA-DEM 64-cell grid:
        │                                     elevation, slope, aspect, TWI,
        │                                     roughness, updraft, drainage)
        │  uses downscaler_xgboost.pkl (trained once by train_xgboost.py)
   ├─ downscale all 64 cells (XGBoost + terrain features)
   ├─ micro-wind from surface roughness
   ├─ village aggregation by Max-Risk protocol (peak cell drives the alert)
   └─ merge with Bhulekh records
        │
        ├──► latest_inference_output.csv            (64 cells, full features)
        └──► latest_khasra_weather_forecast.csv     (192 plots, rain + wind per Khasra)
                        │
                        ▼
        agromet_advisory_engine.py  →  bilingual RED / AMBER / GREEN advisory
                        │
                        ▼
        App.py (Khasra lookup, UI) + farmer_auth.py (SMS/WhatsApp delivery)
```

| Component | File | Role |
|---|---|---|
| Regional ingest | `fetch_open_meteo.py` | Open-Meteo forecast/archive API for Barabanki (26.92 N, 81.18 E), 10 s timeout |
| Downscaler training | `train_xgboost.py` | Trains the rain downscaling model |
| Terrain features | `terrain_grid_8x8.csv` (NASA DEM) | 64-cell relief grid; `generate_terrain_grid.py` can rebuild a synthetic grid but would overwrite the real DEM values |
| Inference + mapping | `predict_grid.py` | 64-cell predictions, Max-Risk village aggregation, Khasra merge |
| Advisory rules | `agromet_advisory_engine.py` | Thresholds → bilingual headline + 3–4 actions |
| Alert templates | `alert_dispatcher.py` | Twilio WhatsApp **Content Template** dispatch (CLI/batch use) |
| Website | `App.py` | All screens, validation, RBAC, delivery |

---

## 7. Data Coverage (actual dataset figures)

| Metric | Value |
|---|---|
| Plot records | **192** |
| Unique Khasra/Gata numbers | **174** |
| Micro-cells | **64** (8 × 8) |
| Villages / Mauzas | **12** — Kalyani Basin, Deokhaliya, Masauli, Chhatauni, Rasauli, Satrikh, Rampur, Fatehpur, Safedabad, Nawabganj, Bado Sarai, Zaidpur |
| Tehsils | **3** — Fatehpur (00842), Nawabganj (00845), Ramnagar (00843) |
| Parganas | 6 |
| Crops | 7 classes — Sugarcane 36, Vegetables 36, Mustard 36, Paddy 35, Wheat 34, Non-agricultural 12, Drying yard/post-harvest 3 |
| Irrigation sources | State tube-well 47 · Canal 44 · Rainfed 43 · Private tube-well 43 · N/A 12 |
| Total mapped area | **225.5 ha** (mean plot 1.17 ha) |
| Plot attributes | Khasra, Khata, Bhulekh unique code, land class, landholder, guardian, area (ha/bigha), zone, crop, irrigation, lat/long, LGD code |
| Terrain attributes | 64 cells × 30 fields, incl. **real NASA-DEM** elevation / slope / aspect / TWI (`real_nasadem_*`) |
| Current forecast snapshot | Rain **0.0 – 4.8 mm/hr**, wind 6.2 – 8.2 km/h, humidity **79 %** (live regional) |
| Current advisory split | **21 RED · 0 AMBER · 171 GREEN** (of 192 plots) |
| Highest-rain plot in snapshot | Bado Sarai, Khasra **630**, 4.80 mm/hr |

**Highest-risk villages in the current snapshot:** Bado Sarai 4.80, Nawabganj 4.64, Satrikh 4.61, Fatehpur 4.57, Masauli 4.48, Rampur 4.40 mm/hr.

> Prediction run: 30 Oct 2026 — real NASA-DEM terrain grid + live Open-Meteo forcing
> (regional humidity 79 %, wind 7.6 km/h, cloud cover 100 %). Re-run `python predict_grid.py`
> (after `pip install -r requirements-ml.txt`) to refresh these numbers.

> Note: landholder names are demo values (“Demo Landholder 079”) — the schema mirrors Bhulekh fields, but personal data is not real.

---

## 8. Advisory Decision Rules

| Level | Trigger (evaluated in order) | Message theme |
|---|---|---|
| **RED** | Rain ≥ 3.0 mm/hr | Heavy convective downpour in 35–45 min — cover grain/threshing floors, stop harvesting, don't load trolleys, open drainage |
| **AMBER** | 1.0 ≤ rain < 3.0 **or** rain ≥ 0.5 with RH > 80 % | Moderate rain — postpone spraying 6–8 h, skip irrigation, clear furrows |
| **AMBER** | Wind ≥ 24 km/h **and** rain ≥ 0.3 | Lodging risk — no flooding of tall crops, secure nets/tarpaulins, stake vegetables |
| **GREEN** | Otherwise | Safe window — dry grain, spray, transport to mandi |

Every advisory includes: village + Khasra + zone in the headline, **3–4 concrete actions**, and both languages.

**Real engine output (RED):**
> 🚨 URGENT: Heavy convective downpour (4.8 mm/hr) expected within 35-45 minutes over Rampur (रामपुर कटरा) (Settlement & Drying Yards) | Khasra #142/1!

> 🚨 आपातकालीन चेतावनी: अगले 35-45 मिनट में Rampur (रामपुर कटरा) | खसरा सं. #142/1 में तेज मूसलाधार बारिश (4.8 मिमी/घंटा) की संभावना!

---

## 9. Report Delivery

| Channel | Purpose | Configuration |
|---|---|---|
| On-screen | Full advisory + metrics + plot details | Always available |
| **SMS** | Compact report to the farmer's registered number when they check the forecast | Twilio Verify/SMS sender |
| **WhatsApp (message)** | Full formatted report, same trigger | Twilio WhatsApp sender (optional) |
| **WhatsApp share link** | Farmer forwards the forecast to family/other farmers | No API needed |
| **WhatsApp template (batch)** | Sarpanch/administrator broadcast alerts | `alert_dispatcher.py` + approved template SID |
| **Voice** | Reads the guidance aloud | Browser Web Speech API (no cost) |

Outbound reports are rate-limited (5 per 10 minutes per client) to protect farmers from spam and control SMS cost.

---

## 10. Security & Privacy (post-hardening)

| Control | Status |
|---|---|
| Chief credentials | **Hashed** (PBKDF2-HMAC-SHA256, 600k rounds, per-record salt) — never stored or displayed in plaintext |
| Farmer identity | Phone + OTP only; **no farmer password exists** |
| Role-based access | Server-side page table; Chief pages require **role + valid Chief session** |
| Brute force | 5 failed Chief logins → 15-min lockout; OTP verified in constant time |
| Sessions | Chief 8 h / 45 min idle; Farmer 12 h; invalidated on credential rotation |
| Input validation | Khasra, name, village, note, OTP validated server-side |
| XSS | All dynamic HTML escaped; strict Khasra/name character sets |
| Uploads | Extension + magic-byte + real-decode check, 5 MB cap, re-encoded to JPEG, server-generated filename |
| Rate limits | OTP send/verify, Chief login, forecast lookups, uploads, outbound reports |
| Audit trail | `logs/security_audit.log` — logins, lockouts, denials, uploads, credential changes (secrets redacted, phones masked) |
| Data access | Each Chief sees only their own area's verifications; farmers see only their own account |
| Secrets | Server-side `.env` only; nothing secret rendered in the browser or logged |
| Error handling | Generic messages in UI; details only in server logs |
| Dependencies | `pip-audit`: no known vulnerabilities · `bandit`: 0 High, 0 Medium |

**For production:** HTTPS + security headers must be added by the reverse proxy (`deploy/nginx.conf.example`), and Supabase RLS policies applied (`deploy/supabase_hardening.sql`). Full detail in `SECURITY.md`.

---

## 11. Performance

| Metric | Before hardening | After |
|---|---|---|
| Image payload per page load | 10 898 KB | **744 KB** (−93.2 %) |
| Hero background | 3 080 KB | 325 KB |
| Image processing per rerun | 209 ms (every rerun) | **1.4 ms** (cached) |
| Splash screen blocking delay | 5.0 s | **1.2 s** |
| Forecast CSV parsing | every click | cached per file change |
| Feedback CSV parsing | every rerun | cached (10 s) |
| Glass blur on phones | enabled (GPU-expensive) | disabled ≤ 820 px |
| Browser console errors | — | **none** in the full flow test |

---

## 12. Technology Stack & Project Structure

| Layer | Technology |
|---|---|
| Frontend | Streamlit 1.66 + custom CSS design system + browser Web Speech API |
| Backend | Python 3.13 (Streamlit server process) |
| ML | XGBoost downscaler (`downscaler_xgboost.pkl`), NumPy, pandas |
| Data | Bhulekh cadastral CSV, terrain grid CSV, model output CSVs |
| Auth | Twilio Verify (OTP), Supabase Postgres (`farmers`), PBKDF2 credential store |
| Delivery | Twilio SMS / WhatsApp (+ Content Templates) |
| External data | Open-Meteo API |
| Testing | `unittest`, Streamlit `AppTest`, Playwright (browser) |

```
Suksmavrishti/
├─ App.py                       # website (screens, RBAC, validation, delivery)
├─ security.py                  # validation, escaping, hashing, limits, audit, uploads
├─ farmer_auth.py               # OTP login + SMS/WhatsApp report delivery
├─ chief_auth.py                # Sarpanch auth (hashed, lockout, sessions)
├─ setup_chief_credentials.py   # operator CLI for chief passwords
├─ agromet_advisory_engine.py   # bilingual advisory rules
├─ predict_grid.py              # 64-cell downscaling + Khasra mapping
├─ train_xgboost.py             # model training
├─ generate_terrain_grid.py     # 8×8 terrain features
├─ fetch_open_meteo.py          # regional weather ingest
├─ alert_dispatcher.py          # WhatsApp template broadcasts
├─ *.csv                        # cadastral + forecast + terrain data
├─ training_uploads/            # verified chief photos + feedback log
├─ tests/                       # 54 automated tests
├─ deploy/                      # nginx headers + Supabase SQL
├─ logs/                        # security audit log
└─ SECURITY.md / REPORT.md / WEBSITE_REPORT.md
```

---

## 13. Quality Assurance

| Check | Result |
|---|---|
| Unit + integration tests | **54 passed** (validation, XSS, hashing, lockout, sessions, uploads, audit redaction, RBAC, forecast flow, restart-proof rate limiting) |
| Headless app tests | 12 scenarios — Farmer→Chief denial, unauthenticated upload denial, expired session, malicious Khasra, forecast rendering |
| Browser end-to-end (Chrome) | Splash → language → Chief login (wrong then correct) → panel → no console errors |
| Dependency audit | `pip-audit`: no known vulnerabilities |
| Static analysis | `bandit`: 0 High, 0 Medium, 11 Low (intentional best-effort guards) |
| Performance | Payload and timing measured on the real assets (table above) |

---

## 14. Limitations (stated honestly)

1. **Demo cadastral data** — 192 plots with demo landholder names; the schema is Bhulekh-shaped but not a live Bhulekh feed.
2. **“1 km × 1 km”** is the product positioning; the demo grid is 8 × 8 cells over the sample area. No independent accuracy validation is published in this build.
3. **Humidity** is a single regional value (79 % in the current run) taken from the live weather vector — Khasra rows display the value of their micro-cell, not a plot-level measurement.
4. **Advisory is rule-based**, not AI-generated text — deliberately, so output is deterministic and auditable.
5. **Weather data is model output**, not an official IMD forecast — the UI says so on every result.
6. **OTP delivery** needs a Twilio sender (DLT registration for production SMS in India); live SMS was not sent during this audit.
7. **HTTPS/security headers** depend on deployment (nginx template provided, not active on the dev server).
8. **Rate limiting is in-process** — a multi-instance deployment needs Redis.
9. **Chief feedback is not yet auto-used for training** — photos are stored for review first.

---

## 15. Presentation Kit

### 3-minute demo script (Hinglish)

| Time | Screen | Kya bolein |
|---|---|---|
| 0:00–0:20 | Splash → language | “Suksmavrishti — Khasra-level mausam, kisan ki apni bhasha mein.” |
| 0:20–0:40 | Role page | “Do roles: kisan aur sarpanch. Dono ka apna secure login.” |
| 0:40–1:20 | Chief login → panel | “Sarpanch apne area ke liye login karta hai — password hashed hai, 5 galat attempt par lockout.” |
| 1:20–2:10 | Forecast page | “Khasra 21 daaliye… yahan rain rate, wind, humidity, aur RED/AMBER/GREEN salah — sat saath field actions Hindi mein.” |
| 2:10–2:40 | Plot details + share | “Yeh Bhulekh record hai — khatedar, rakba, fasal, micro-zone. WhatsApp par bhej dijiye.” |
| 2:40–3:00 | Upload page | “Sarpanch photo daal kar batata hai report sahi thi ya galat — yahi data model ko behtar banata hai.” |

### Key talking points

- **Plot-level, not district-level** — Khasra se seedha forecast.
- **Action-oriented** — “kitni baarish” nahi, “ab kya karein”.
- **Two languages + voice** — low-literacy users ke liye bhi accessible.
- **Trust by design** — data source aur timestamp har result par; data na ho to saaf mana karta hai.
- **Closed feedback loop** — sarpanch verification se model improve hota hai.
- **Security-first** — koi password plaintext mein nahi, role server par enforce, audit log.

### Likely judge questions

| Question | Short answer |
|---|---|
| Model ki accuracy kya hai? | Is build mein independent accuracy validation nahi hai; advisory deterministic rules par hai. Next step: IMD station data ke against validation. |
| Data real hai? | Bhulekh schema real hai, demo 192 plots; landholder names demo hain. |
| Farmer ko app chahiye? | Nahi — SMS/WhatsApp par report chali jaati hai, smartphone optional. |
| AI kahan hai? | XGBoost downscaling model; advisory rule-based (auditable), koi LLM hallucination risk nahi. |
| Scale kaise karega? | Model output CSV-based hai; production mein scheduled pipeline + Redis rate limits + Postgres. |

---

## 16. Run & Configure

```bash
# Install (dependencies already pinned)
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# Secrets
cp .env.example .env          # Supabase + Twilio values

# Chief (Sarpanch) credentials
python setup_chief_credentials.py --show-initial   # one-time passwords
python setup_chief_credentials.py --area "Rampur"  # set your own

# Tests
python -m unittest discover -s tests -v

# Run
streamlit run App.py
# → http://localhost:8501
```

Environment variables (names only): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SERVICE_SID`, `TWILIO_SMS_FROM`, `TWILIO_WHATSAPP_FROM`, `WHATSAPP_NUMBER`, `SV_CHIEF_SESSION_HOURS`, `SV_CHIEF_IDLE_MINUTES`, `SV_CHIEF_MAX_FAILURES`, `SV_CHIEF_LOCKOUT_MINUTES`, `SV_SPLASH_SECONDS`.

---

## 17. Appendix — Sample Record & Output

**Sample plot (Rampur, cell 3-2):**

| Field | Value |
|---|---|
| Khasra / Gata | 310 |
| Village / Mauza | Rampur / रामपुर कटरा (LGD 147610) |
| Tehsil | Nawabganj (00845) |
| Landholder | Demo Landholder 079 |
| Area | 0.286 ha (1.13 bigha) |
| Zone | Rampur West – Gaon Sthal |
| Crop | Non-Agricultural (आबादी / खलिहान) |
| Coordinates | 26.90797, 81.16381 |
| Predicted rain / wind | 4.40 mm/hr · 6.5 km/h |

**Sample advisory (AMBER):** ⚠️ ADVISORY: Localized light-to-moderate rain (1.4 mm/hr) expected over Masauli.

**Sample advisory (GREEN):** ☀️ SAFE WEATHER: Dry and clear conditions across Rampur.

---

*Prepared from the actual codebase, datasets and test runs of this project. Security details: `SECURITY.md` · Change history: `REPORT.md`.*
