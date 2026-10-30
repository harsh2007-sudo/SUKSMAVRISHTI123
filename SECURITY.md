# Suksmavrishti — Security Hardening Report

**Date:** 30 October 2026
**Scope:** the existing project in this folder. No framework change, no redesign, no paid services added.

Everything below was verified in this environment unless it is explicitly listed
under “Requires deployment / provider configuration” or “Not implemented”.

---

## 1. Existing architecture (as found)

| Layer | Reality |
|---|---|
| UI | Streamlit 1.66 single-process app (`App.py`, ~2,900 lines, EN/HI, custom CSS, browser speech via `components.html`) |
| Auth — farmer | Phone + OTP (`farmer_auth.py`): Twilio Verify (preferred) or Twilio SMS/WhatsApp fallback + Supabase Postgres row |
| Auth — chief | Area dropdown + demo ID/password (`chief_auth.py` + `chief_credentials.json`) |
| Data | Local CSVs (`bhulekh_…csv`, `latest_khasra_weather_forecast.csv`, `latest_inference_output.csv`), pickle model used by the offline scripts |
| Advisory | Rule-based Python (`agromet_advisory_engine.py`) — **no LLM/AI API is called at runtime** |
| Uploads | Chief field photos written to `training_uploads/` + CSV feedback log |
| Deployment | Bare `streamlit run` on localhost; no reverse proxy, no TLS, no CI, no git repo in this folder |

Streamlit's Python process *is* the trusted backend: session state is server-side,
so page/role authorization is enforced there, not in the browser.

## 2. Security controls that already existed

- OTP-based farmer login (no password stored for farmers).
- Twilio Verify used server-side; service-role Supabase key only read from the server environment.
- Chief panel required *some* login (`chief_auth.is_logged_in()`), and the chief login is separate from the farmer login.
- The farmers table had RLS *enabled* (no policies).
- Chief upload accepted only png/jpg/jpeg/webp by extension.
- Some HTML escaping existed implicitly (Streamlit markdown) but **not** where `unsafe_allow_html=True` was used.

## 3. Vulnerabilities found

| # | Severity | Issue | Evidence |
|---|---|---|---|
| 1 | **Critical** | Chief passwords stored and committed in plaintext, and **displayed in the UI** ("Show demo ID / password" expander) for all 12 areas | `chief_credentials.json`, `App.py` |
| 2 | **Critical** | Passwords were predictable (`SV2026_<area>`) and also published in `REPORT.md` — anyone could log in as any sarpanch | `REPORT.md`, `chief_auth.py` |
| 3 | **High** | Chief upload page checked only `role == "chief"` and **not** the login session — a visitor could select the Chief role and reach the upload/AI-training page unauthenticated | `App.py` (upload branch) |
| 4 | **High** | Stored/reflected **XSS**: Khasra input, village, landholder, crop, zone and advisory text were interpolated into `unsafe_allow_html` HTML with no encoding; only `.strip()` was applied | `App.py` forecast result panel |
| 5 | **High** | No server-side validation of Khasra numbers, names, villages, notes, or feedback text (length/type/charset) | `App.py`, `farmer_auth.py` |
| 6 | **High** | No rate limiting anywhere: OTP send (paid SMS), OTP verify, chief login brute force, forecast lookups, uploads, outbound reports | whole app |
| 7 | **Medium** | Provider/database exception text returned to the browser (`{exc}` in error messages) and no audit trail of auth decisions | `farmer_auth.py`, `App.py` |
| 8 | **Medium** | Upload trusted the extension and browser MIME; original bytes (including polyglot/HTML payloads and EXIF) were written to disk; no size limit; user text used in filenames | `App.py` upload branch |
| 9 | **Medium** | CSV formula injection via feedback note/village/khasra when the CSV is opened in Excel | upload feedback log |
| 10 | **Medium** | No session expiry/invalidation for farmer or chief sessions; chief sessions lived until the tab closed | both auth modules |
| 11 | **Medium** | Cross-area data exposure: "Recent verifications" showed the last 5 rows of **all** areas to any chief | `App.py` |
| 12 | **Low** | OTP generated with `random` (non-cryptographic) on the local fallback path; OTP compared with `!=` | `farmer_auth.py` |
| 13 | **Low** | Stack traces shown in the browser (`client.showErrorDetails` default) | runtime config |
| 14 | **Performance** | 5 s blocking `time.sleep()` splash on every new session, plus 3 giant images (10.9 MB base64) re-read and re-encoded on **every rerun** | `App.py` |
| 15 | **Trust** | When forecast data was missing the UI **invented** rain/wind/humidity from the cell number, with no source or timestamp | `get_live_prediction_for_cell` |

No hard-coded third-party API keys were found in the code. Real Twilio/Supabase
values live only in `.env` (server-side) and are never rendered or logged.

## 4. Files modified / added

| File | Why |
|---|---|
| `security.py` **(new)** | Validation, HTML/CSV encoding, PBKDF2 hashing, rate limiter/lockout, audit log, image verification, session helpers |
| `chief_auth.py` | Hashed credentials only, one-time credential issuance, migration/rotation, constant-time compare, lockout, expiring sessions, version-based invalidation |
| `setup_chief_credentials.py` **(new)** | Operator CLI to set/rotate/list chief passwords (getpass, policy check, no plaintext storage) |
| `farmer_auth.py` | OTP send/verify limits, name/village validation, hashed local OTP + `secrets`, generic provider errors, audit events, 12 h session expiry, validated report recipients, report-send cap |
| `App.py` | Deny-by-default page guard, chief login guard on the upload page, removal of the credential expander, XSS encoding, input validation, verified/re-encoded uploads, safe filenames, CSV-injection-safe rows, area-scoped feedback, honest data-source labelling, cached/resized images, 1.2 s splash, cached CSV lookups, cheaper mobile CSS |
| `.gitignore` **(new)** | Keeps `.env`, issued passwords, audit logs, uploads, backups out of version control |
| `.streamlit/config.toml` **(new)** | XSRF on, CORS on, 5 MB upload cap, no static file serving, telemetry off, **no stack traces in the browser** |
| `deploy/nginx.conf.example` **(new)** | HTTPS redirect, TLS, HSTS, CSP, nosniff, Referrer-Policy, Permissions-Policy, frame-ancestors, WebSocket proxy |
| `deploy/supabase_hardening.sql` **(new)** | Phone/name/village constraints, RLS deny-by-default, revoke anon/authenticated, backup/rotation notes |
| `requirements.txt` **(new)** | Pinned, auditable runtime dependencies (versions already installed) |
| `tests/test_security.py` **(new)** | 40 unit/regression tests |
| `tests/test_app_rbac.py` **(new)** | 12 headless app tests (authorization + forecast regression) |
| `.env.example`, `REPORT.md`, `chief_credentials.json` | Documented new settings; removed the published password table; rotated credentials to hashes |
| `deploy/https_proxy.py` **(new)** | Stdlib TLS-terminating proxy for dev boxes without nginx: injects CSP/nosniff/frame-options/Referrer-Policy/Permissions-Policy/COOP, adds `Secure; SameSite=Lax` (+`HttpOnly` except `st_xsrf`), strips the upstream `Server` banner, passes WebSocket upgrades through. Auto-generates a self-signed cert into `deploy/certs/`. HSTS is opt-in (`--hsts`) because it would break plain `http://localhost:8501`. |
| `requirements-dev.txt` **(new)** | Pinned `pip-audit` + `bandit` so the security checks are reproducible |
| ~~`.security_backup/`~~ | **Deleted** — it held the old plaintext `SV2026_*` chief passwords. `.chief_credentials_initial.txt` (the one-time plaintext issuance file) is deleted too; rotate with `setup_chief_credentials.py` if a password is ever lost. |

## 5. Security features implemented

**Authentication & sessions**
- Chief passwords: PBKDF2-HMAC-SHA256, 600 000 rounds, 16-byte random salt, `hmac.compare_digest` verification, dummy-hash path for unknown IDs (no user enumeration by timing).
- No predictable/default chief password: random 18-char password issued once, written to `.chief_credentials_initial.txt` (chmod 600, git-ignored) and only its hash is stored.
- Legacy plaintext entries are rotated automatically on first run (`must_change` flag shown in the panel).
- 5 failed chief logins → 15-minute lockout (configurable), plus a per-attempt KDF cost.
- Sessions: random token, 8 h absolute expiry, 45 min idle timeout, invalidated when credentials are rotated; farmer sessions expire after 12 h and re-validate the phone format.
- OTP: `secrets.randbelow`, stored as SHA-256 digest (never plaintext), compared in constant time, 5 wrong attempts → 15-minute per-phone lockout, 10-minute expiry, 3 sends/10 min per phone and 6/10 min per client.

**Authorization**
- Deny-by-default page table (`welcome/forecast/login/chief_login/chief/upload`) checked on every render; unknown pages bounce to `welcome`.
- Chief pages and the upload page require `role == "chief"` **and** a valid `chief_auth` session. Farmer cannot open Chief pages and vice-versa (tested).
- Feedback rows are attributed to the logged-in chief id; each chief only sees their own last 5 rows.

**Input & output**
- Khasra: `^[0-9\u0900-\u097F]{1,6}([/-][0-9\u0900-\u097F]{1,4})?$`, ≤ 20 chars (supports `21क`, `22/1`, `1431-1436`, keeps multilingual text intact).
- Names ≤ 60, villages ≤ 80, notes ≤ 500 with control-character stripping; OTP must be exactly 6 digits.
- All user/CSV-derived values rendered with `unsafe_allow_html=True` pass through `html.escape` (quote=True).
- CSV export/append uses formula-injection neutralisation (`'` prefix for `= + - @`).
- No SQL is built from user input at all: Supabase client calls use parameterised table operations; `predict_grid.py` only reads local CSVs with pandas.

**API / abuse / errors**
- In-process sliding-window limits: forecast lookups 30/min, uploads 6/10 min, outbound reports 5/10 min, OTP limits above.
- Uploads: 5 MB cap (also enforced by Streamlit), extension + magic-byte + real decode check, decompression-bomb guard, then **re-encoded to JPEG** with a server-generated filename inside `training_uploads/` (path-traversal checked).
- Provider/DB errors are logged server-side and replaced with generic user messages.
- `showErrorDetails = false` so tracebacks never reach the browser.

**Audit logging**
- JSON-lines log at `logs/security_audit.log` (chmod 600, 2 MB rotation, git-ignored) recording: OTP send/rate-limit/verify outcomes, farmer and chief login success/failure/lockout/logout, session rejection, authorization denials, role selection, credential issuance/rotation, uploads accepted/rejected/failed, reports sent/failed, forecast-data failures.
- Automatic redaction of any key containing `pass/otp/token/secret/key/phone/cookie/auth`, control characters stripped (log-forging defence), phone numbers masked to the last 4 digits.

**Data honesty**
- Forecast values now come only from the model output CSVs, with source filename and last-updated time shown, plus “model output, not an official IMD forecast”.
- If no row exists, the UI says data is unavailable and shows **no** numbers (the old fabricated fallback is gone).
- Upload feedback is labelled as an unverified field observation, not proof of a rainfall measurement.

## 6. New dependencies

**None.** `security.py` uses only the standard library (`hashlib`, `hmac`, `secrets`, `html`, `json`, `threading`) and Pillow, which Streamlit already requires. No new runtime package was added; `requirements.txt` only pins what was already installed. `pip-audit` and `bandit` were installed into the dev venv for auditing only.

## 7. Tests executed — actual results

```
python -m unittest discover -s tests -v
Ran 54 tests in 39.8s
OK
```
40 unit tests (`tests/test_security.py`): validation (valid/invalid/boundary/injection payloads), HTML escaping, CSV injection, slug/path traversal, password hashing + tamper rejection + policy, rate limiter window/lockout/memory bound, upload verification (real image re-encoded, fake extension, HTML payload, corrupted PNG, oversize, empty), audit redaction + log-forging, chief store migration (plaintext gone, old password rejected, issued password accepted, wrong area rejected, lockout, expired/idle/rotated session, logout), farmer validation/expiry/OTP digest.

12 app tests (`tests/test_app_rbac.py`, Streamlit `AppTest`): farmer→chief denied, farmer→upload denied, chief→farmer-login denied, chief panel/upload without session→`chief_login`, unknown page→`welcome`, valid chief session allowed, expired session rejected, credentials/metadata never rendered, forecast flow still renders results, malicious Khasra rejected.

**Browser check** (Playwright + Chrome, headless, `127.0.0.1:8501`):
```
language page reached: 3.0 s (includes 1.2 s splash)
demo password expander removed: True
old predictable password rejected: True
issued password accepted: True
chief panel renders: True
hero data-URI size: 325 KB
console/page errors: none
```

**Static / dependency checks**
```
pip-audit -r requirements.txt   -> No known vulnerabilities found
bandit (App.py, security.py, auth modules, pipeline) -> 0 High, 0 Medium, 11 Low
```
The remaining Low findings are deliberate `try/except: pass` guards around browser-API/audit/chmod best-effort calls (B110). The one `random` OTP finding (B311) was fixed to use `secrets`.

**Performance (measured)**

| Metric | Before | After |
|---|---|---|
| Images embedded per page load (base64) | 10 898 KB | 744 KB (**−93.2 %**) |
| Hero background data-URI | 3 080 KB | 325 KB |
| Image processing per rerun | 209 ms (every rerun) | 1.4 ms (cached) |
| Splash screen blocking sleep | 5.0 s | 1.2 s (`SV_SPLASH_SECONDS`) |
| Forecast CSV parsing | every click | cached per file mtime |
| Feedback CSV parsing | every rerun | cached, 10 s |
| Blurred glass panel on phones | `backdrop-filter` | disabled ≤ 820 px |

Also removed: the fabricated weather fallback, and per-rerun `os.listdir`/`read_csv` work on the chief panel.

## 8. Fixes and retest status

| Vuln | Fix | Retest |
|---|---|---|
| 1, 2 plaintext/known chief passwords | hashed store, rotation, UI expander removed | migration + login tests, browser check ✅ |
| 3 unauthenticated upload page | page guard + chief-session check | `test_upload_without_login_redirects_to_chief_login`, `test_farmer_cannot_open_upload_page` ✅ |
| 4 XSS | HTML escaping everywhere `unsafe_allow_html` is used + strict Khasra/name validation | `test_html_escape`, `test_malicious_khasra_is_rejected_in_the_ui` ✅ |
| 5 input validation | server-side validators on all fields | validation tests ✅ |
| 6 rate limits / brute force | limiter + lockout on OTP, chief login, forecast, uploads, reports | limiter, lockout, OTP tests ✅ |
| 7 error leakage / no audit | generic errors + redacted audit log | audit redaction tests ✅ |
| 8 upload abuse | magic-byte + decode + re-encode + safe name + size cap | 6 upload tests ✅ |
| 9 CSV injection | `csv_safe` on every written field | `test_csv_injection_neutralised` ✅ |
| 10 session lifetime | expiry + idle timeout + rotation invalidation | 4 session tests ✅ |
| 11 cross-area feedback | filtered by chief id | code path exercised in `test_chief_with_valid_session_can_open_panel` ✅ |
| 12 weak OTP RNG/compare | `secrets` + digest + `compare_digest` | OTP digest test, bandit re-run ✅ |
| 13 stack traces | `client.showErrorDetails = false` | config present; UI behaviour verified indirectly by AppTest (no exceptions rendered) |
| 14 lag | cached images, 1.2 s splash, cached CSVs, mobile CSS | measured table above + browser run ✅ |

## 9. Status by category

### ✅ Implemented and tested
Chief auth hashing/migration/lockout/sessions · farmer session idle timeout (60 min, configurable) + rejection audit · page-level RBAC · upload authorization · XSS encoding · input validation · rate limits that **survive a restart** (file-backed, `logs/rate_limiter_state.json`, 0600) · proxy-aware client keys (`SV_TRUST_PROXY=1` uses `X-Forwarded-For`) · audit logging (redacted, fsynced, owner-only from the first byte) · upload content verification and re-encoding · CSV-injection protection · truthful weather-data labelling · HTTPS + security headers on dev via `deploy/https_proxy.py` (verified with `curl -D-`: 200 + full header set; WebSocket upgrade 101; POST relayed) · performance work · **54 automated tests** · `pip-audit` (0 CVEs) · `bandit` (0 High / 0 Medium).

### ✅ Implemented, not fully exercised
- **Farmer OTP end-to-end with Twilio**: sending a real SMS would message a real phone, so the live Twilio send/verify path was not executed here. Validation, expiry, digest comparison, lockout and error handling are unit-tested; the UI path is unchanged from the working build.
- **Upload write path**: verification/re-encoding and filename logic are unit-tested; the full browser submit was not re-run after the change (it would add a real photo row).
- **Audit-log alerting**: the log exists and is protected; no external alerting/monitoring service is connected.

### ⚠️ Requires deployment / provider configuration (cannot be done from this folder)
1. **HTTPS/HSTS + security headers** — Streamlit cannot set custom response headers, so they come from a proxy. On this dev box they are now **enforced** by `python deploy/https_proxy.py` (https://127.0.0.1:8443 → 127.0.0.1:8501): CSP, nosniff, X-Frame-Options, Referrer-Policy, Permissions-Policy, COOP and hardened cookies are all present in `curl -D-`. For production, apply `deploy/nginx.conf.example` (or the platform equivalent) and enable HSTS there — the local proxy leaves HSTS off deliberately, because a localhost HSTS entry would force `http://localhost:8501` to https and break the direct dev URL. The app itself should stay bound to `127.0.0.1` while the proxy is in front of it.
2. **Supabase** — run `deploy/supabase_hardening.sql` in the SQL editor; confirm RLS with no policies and that anon/authenticated have no grants.
3. **Rotate exposed credentials** — `.env` holds real Twilio and Supabase service-role values. No git repo exists here, so there is no evidence they were committed, but **if this folder was ever zipped, shared, screenshotted or uploaded, rotate the Twilio Auth Token and the Supabase service-role key**. Deleting a secret from a file does not invalidate a copy that already leaked.
4. **Chief passwords** — `.chief_credentials_initial.txt` has been deleted (passwords were issued and handed over; only PBKDF2 hashes remain in `chief_credentials.json`). If a password is lost, set a new one with `setup_chief_credentials.py --area "<area>"`.
5. **DLT / paid Twilio sender** for production SMS in India.
6. **Malware scanning** for uploads — not available here; re-encoding removes script content, but an AV scanner (e.g. ClamAV) should be added on the upload host.
7. **Distributed rate limiting** — counters and lockouts are now mirrored to a local state file, so a *restart* no longer clears them. Two or more instances would still keep separate books: if you scale out, move the single-class interface to Redis.

### ❌ Not applicable / not implemented
- **AI/LLM prompt-injection defences** — the app calls no LLM. The advisory is deterministic rule-based Python, so there is no prompt, no tool access and nothing to inject. If an LLM is added later, system/user separation, output validation and least-privilege tools must be designed then.
- **OWASP ZAP active scanning** — not run (needs an authorised target and a running reverse-proxy deployment). The checks above are unit/AppTest/browser-level.
- **Penetration test / certification** — none performed, none claimed.

## 10. Deployment steps and environment variables

```bash
# 1. Dependencies
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Linux/macOS: .venv/bin/pip

# 2. Secrets (never commit .env)
cp .env.example .env        # then fill in real values

# 3. Database hardening (Supabase SQL editor)
#    paste deploy/supabase_hardening.sql and run it

# 4. Chief credentials
python setup_chief_credentials.py --show-initial    # read once, then delete the file
python setup_chief_credentials.py --area "Rampur"   # or set your own

# 5. Tests
python -m unittest discover -s tests -v

# 6. Run (bind to localhost, put nginx/TLS in front)
streamlit run App.py --server.address 127.0.0.1 --server.port 8501

# 6b. Render / any host whose checkout is wiped on every deploy:
#     attach a Persistent Disk (example mount: /var/data) and set
#       SV_DATA_DIR=/var/data     -> chief_credentials.json + logs/ live there,
#                                    so redeploys keep logins/audit/lockouts
#       SV_TRUST_PROXY=1           -> rate limits key on the real client IP
#     Start command (Render assigns the port):
#       streamlit run App.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
#     Render terminates TLS at its edge, so deploy/https_proxy.py is NOT used
#     there — it exists for local dev boxes without nginx. First boot on an
#     empty volume adopts the checkout's credential store automatically.

# 7. Verify the proxy actually sets the headers
curl -sD- -o /dev/null https://your-domain/ | grep -Ei "strict-transport|content-security|x-content-type|referrer|permissions"
```

Environment variables (names only):

| Variable | Purpose |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | farmer table (server-side only) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio API |
| `TWILIO_VERIFY_SERVICE_SID` | OTP service (preferred) |
| `TWILIO_SMS_FROM`, `TWILIO_WHATSAPP_FROM` | fallback senders (leave WhatsApp empty if unused) |
| `WHATSAPP_NUMBER` | public “chat with us” number |
| `SV_CHIEF_SESSION_HOURS`, `SV_CHIEF_IDLE_MINUTES`, `SV_CHIEF_MAX_FAILURES`, `SV_CHIEF_LOCKOUT_MINUTES` | chief session/abuse tuning |
| `SV_FARMER_IDLE_MINUTES` | farmer idle logout (default 60) |
| `SV_DATA_DIR` | persistent volume for `chief_credentials.json` + `logs/` (audit log, rate-limit state); unset = app folder |
| `SV_TRUST_PROXY` | `1` when behind a proxy you control: rate limits use `X-Forwarded-For` |
| `SV_SPLASH_SECONDS` | splash duration (default 1.2) |

**File permissions note (Windows):** the code calls `chmod 600` on
`chief_credentials.json`, the one-time passwords file and the audit log, but Git
Bash on Windows does not enforce POSIX modes (`ls -l` still shows `rw-r--r--`).
On Windows, restrict them with NTFS ACLs, e.g.
`icacls chief_credentials.json /inheritance:r /grant:r "%USERNAME%:R"` and run the
app under a dedicated service account. On Linux/macOS the chmod calls do apply.

## 11. Priority list for work that cannot be finished here

1. **Deploy behind HTTPS with the provided headers** and confirm HSTS only after TLS works.
2. **Rotate Twilio + Supabase keys** if the folder was ever shared.
3. **Run `deploy/supabase_hardening.sql`** and verify RLS/grants.
4. **Live-test the OTP flow** with a verified Twilio tester number.
5. **Hand out and delete** the one-time chief passwords.
6. **Add malware scanning** to the upload directory.
7. **Move rate limiting to Redis** if you scale beyond one process.
8. **Add CI** (GitHub Actions: `pip-audit`, `bandit`, `unittest`) — the commands are already in this report.
9. **Add a real review workflow** for chief submissions (who approved what) before they are used for training.

## 12. Hackathon security checklist

| Check | Status |
|---|---|
| Chief panel requires login, not just a role toggle | ✅ tested |
| Farmer cannot reach Chief pages; Chief cannot reach farmer login | ✅ tested |
| Passwords hashed (PBKDF2, salted) and never displayed | ✅ tested |
| Old predictable demo passwords rejected | ✅ tested |
| Login brute force limited / locked out | ✅ tested |
| Session expiry + logout | ✅ tested |
| All form input validated server-side | ✅ tested |
| XSS payloads rejected/escaped | ✅ tested |
| Uploads: type + content + size verified, re-encoded, safe names | ✅ tested |
| Upload page requires a Chief session | ✅ tested |
| Secrets not in code, not in the browser, not in logs | ✅ verified |
| Rate limits on OTP, login, forecast, upload, reports | ✅ tested |
| Audit log of auth/authz/upload events (redacted) | ✅ verified |
| Honest weather data source + “unavailable” instead of fake numbers | ✅ verified |
| Security headers / HTTPS | ⚠️ config provided, needs the proxy |
| Dependency audit clean | ✅ `pip-audit` |
| Static analysis | ✅ `bandit`: 0 High, 0 Medium |
| Page speed | ✅ 10.9 MB → 744 KB images, 5 s → 1.2 s splash |
