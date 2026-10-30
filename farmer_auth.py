"""
farmer_auth.py
--------------
Farmer login/registration (Phone + OTP) and forecast-report delivery.

- OTP login/registration  -> Twilio SMS sends the code, Supabase Postgres stores farmers.
- Forecast report delivery -> Twilio WhatsApp + SMS, sent to the registered number.

Configuration is read from environment variables / .env (see .env.example):
    SUPABASE_URL              e.g. https://abcdefgh.supabase.co
    SUPABASE_SERVICE_ROLE_KEY server-side key (used only on the backend, never exposed)
    TWILIO_ACCOUNT_SID        AC....
    TWILIO_AUTH_TOKEN         ***
    TWILIO_SMS_FROM           your Twilio SMS number, e.g. +911234567890 (needs DLT in India)
    TWILIO_WHATSAPP_FROM      e.g. whatsapp:+14155238886 (sandbox) or your approved sender
"""

import hashlib
import hmac
import os
import secrets
import threading
from datetime import datetime, timedelta

import streamlit as st
import streamlit.runtime.scriptrunner as sr

import security

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_SMS_FROM = os.getenv("TWILIO_SMS_FROM", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
# Twilio Verify service used for SMS OTP (send + check). When set, OTP delivery
# is handled by Verify instead of the local random-code + Messages API path.
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID", "").strip()

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

# Anti-abuse limits (per 10 minutes unless noted). SMS costs money, so the
# send path is capped per phone number and per client.
OTP_SEND_PER_PHONE = (3, 600)
OTP_SEND_PER_CLIENT = (6, 600)
OTP_VERIFY_LOCK = (5, 900)          # failed checks per phone -> 15 min lockout
SESSION_HOURS = 12                  # absolute farmer session lifetime
# Idle timeout: a session that has not been touched logs out on its own, so an
# unattended browser cannot keep a logged-in farmer panel open all day.
SESSION_IDLE_MINUTES = float(os.getenv("SV_FARMER_IDLE_MINUTES", "60"))

# A trial Verify project only messages "verified testers" — the console names the
# exact page, so surface it whenever Twilio refuses the send.
VERIFY_TRIAL_HINT = (
    " | Twilio trial: पहले number को verified tester बनाएँ — Console → Try out Verify → "
    "Send a verification → To (Recipient) में यह नंबर डालकर code verify करें।"
)

# Twilio's WhatsApp sandbox only accepts free-form messages for 24 hours after the
# recipient last messaged the sandbox. Outside that window Twilio answers with
# "ContentSid Required" (or 63007 when the number never joined).
WHATSAPP_SESSION_HINT = (
    " | WhatsApp sandbox का 24 घंटे का सेशन बंद है — Twilio Console → Messaging → "
    "Try it out → WhatsApp Sandbox में दिखा नंबर खोलकर 'join <code>' भेजें, फिर दोबारा कोशिश करें।"
)


def _whatsapp_hint(err_text: str) -> str:
    low = (err_text or "").lower()
    if "contentsid" in low or "63007" in low or "not a valid whatsapp" in low:
        return WHATSAPP_SESSION_HINT
    return ""


def _verify_hint(err_text: str) -> str:
    low = (err_text or "").lower()
    if "verified tester" in low or "try out verify" in low:
        return VERIFY_TRIAL_HINT
    return ""

# session-state keys are prefixed with the Streamlit script-run id so separate
# browser sessions never share a login on this single-process dev server.
def _run_key() -> str:
    try:
        ctx = sr.get_script_run_ctx(suppress_warning=True)
        return ctx.session_id if ctx else "default"
    except Exception:
        return "default"


def _k(name: str) -> str:
    return f"sv_{_run_key()}_{name}"


def _otp_digest(code: str) -> str:
    """Hash the local-fallback OTP so a plaintext code is never held in state."""
    return hashlib.sha256(f"sv-otp::{code}".encode("utf-8")).hexdigest()


def _otp_matches(entered: str, stored_digest: str) -> bool:
    return hmac.compare_digest(_otp_digest(str(entered or "")), str(stored_digest or ""))


def normalize_phone(raw: str) -> str:
    """Return a bare 10-digit Indian mobile number, or '' if invalid."""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 and digits[0] in "6789" else ""


# --------------------------------------------------------------------------- #
# Supabase client (lazy)
# --------------------------------------------------------------------------- #
_supabase = None
_supabase_error = None
_supabase_lock = threading.Lock()


def supabase_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def get_supabase():
    """Return the Supabase client, or (None, reason) when not usable."""
    global _supabase, _supabase_error
    if not supabase_ready():
        return None, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set in .env"
    if _supabase is not None:
        return _supabase, None
    with _supabase_lock:
        if _supabase is not None:
            return _supabase, None
        try:
            from supabase import create_client
            _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
            _supabase_error = None
        except Exception as exc:  # network / bad credentials
            _supabase_error = str(exc)
            _supabase = None
    return _supabase, _supabase_error


def twilio_ready() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)


def sms_configured() -> bool:
    """True when a Twilio from-number for SMS is available."""
    return twilio_ready() and bool(TWILIO_SMS_FROM)


def whatsapp_configured() -> bool:
    """True when a Twilio WhatsApp sender (e.g. sandbox) is available."""
    return twilio_ready() and bool(TWILIO_WHATSAPP_FROM)


def verify_configured() -> bool:
    """True when a Twilio Verify service is set up for the SMS OTP flow."""
    return twilio_ready() and bool(TWILIO_VERIFY_SERVICE_SID)


def otp_ready() -> bool:
    """True when at least one OTP delivery path exists (Verify preferred)."""
    return verify_configured() or sms_configured() or whatsapp_configured()


def _verify_service():
    """Return the Twilio Verify v2 service handle for the configured SID."""
    return _twilio_client().verify.v2.services(TWILIO_VERIFY_SERVICE_SID)


def _twilio_client():
    from twilio.rest import Client
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# --------------------------------------------------------------------------- #
# Farmers table (Supabase)
# --------------------------------------------------------------------------- #
FARMERS_TABLE_SQL = """create table if not exists public.farmers (
    id bigserial primary key,
    phone text unique not null check (phone ~ '^[6-9][0-9]{9}$'),
    name text not null default '' check (char_length(name) <= 60),
    village text not null default '' check (char_length(village) <= 80),
    language text not null default 'English',
    created_at timestamptz not null default now()
);
alter table public.farmers enable row level security;
revoke all on table public.farmers from anon, authenticated;
-- Full least-privilege setup: deploy/supabase_hardening.sql"""


def _upsert_farmer(phone: str, name: str, village: str, language: str):
    """Insert the farmer, or refresh name/village when they update it."""
    client, err = get_supabase()
    if client is None:
        return False, err
    try:
        client.table("farmers").upsert(
            {"phone": phone, "name": name, "village": village, "language": language},
            on_conflict="phone",
        ).execute()
        return True, None
    except Exception as exc:
        msg = str(exc)
        if "relation" in msg and "farmers" in msg:
            return False, (
                "Table 'farmers' does not exist in your Supabase project. "
                "Run the SQL in FARMERS_TABLE_SQL (see .env.example) once in the "
                "Supabase SQL Editor."
            )
        return False, msg


# --------------------------------------------------------------------------- #
# OTP login / registration
# --------------------------------------------------------------------------- #
_verify_ttl_supported = None


def _verify_supports_ttl() -> bool:
    """Older twilio SDKs have no `ttl` argument on verification create."""
    global _verify_ttl_supported
    if _verify_ttl_supported is None:
        try:
            import inspect
            params = inspect.signature(
                _verify_service().verifications.create
            ).parameters
            _verify_ttl_supported = "ttl" in params
        except Exception:
            _verify_ttl_supported = False
    return _verify_ttl_supported


def _send_verify_code(phone: str):
    """Ask Twilio Verify to text an OTP to +91<phone>. Raises when Twilio refuses."""
    params = {"to": f"+91{phone}", "channel": "sms"}
    if _verify_supports_ttl():
        params["ttl"] = OTP_TTL_MINUTES
    try:
        return _verify_service().verifications.create(**params)
    except Exception as exc:
        # Some services pin code_life; retry once without an explicit ttl.
        if "ttl" in str(exc).lower() and "ttl" in params:
            params.pop("ttl")
            return _verify_service().verifications.create(**params)
        raise


def _check_verify_code(phone: str, code: str, language: str):
    """Ask Twilio Verify whether the entered code is right.

    Returns ("approved" | "wrong" | "error", message) — message is only set for "error".
    """
    hi = language == "हिंदी"
    try:
        chk = _verify_service().verification_checks.create(to=f"+91{phone}", code=code)
    except Exception as exc:
        return "error", (f"OTP सत्यापन नहीं हो सका: {exc}{_verify_hint(str(exc))}" if hi
                         else f"Could not verify the OTP: {exc}{_verify_hint(str(exc))}")
    if (getattr(chk, "status", "") or "").lower() == "approved":
        return "approved", ""
    return "wrong", ""


def start_login(phone_raw: str, name: str, language: str):
    """Validate input, enforce send limits, create + send an OTP."""
    hi = language == "हिंदी"
    phone = normalize_phone(phone_raw)
    if not phone:
        return False, ("कृपया 10 अंकों का सही मोबाइल नंबर दर्ज करें।" if hi
                       else "Please enter a valid 10-digit mobile number.")

    ok_name, clean_name, name_err = security.validate_person_name(name)
    if not ok_name:
        return False, ("कृपया 2-60 अक्षरों का सही नाम दर्ज करें।" if hi else name_err)

    if not twilio_ready():
        return False, ("SMS भेजने के लिए TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN "
                       ".env में सेट करें।" if hi else
                       "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set in .env, "
                       "so the OTP cannot be sent.")

    channel = "sms" if sms_configured() else ("whatsapp" if whatsapp_configured() else None)
    via_verify = verify_configured()
    if channel is None and not via_verify:
        return False, (
            "OTP भेजने के लिए कोई चैनल सेट नहीं है — .env में TWILIO_SMS_FROM या "
            "TWILIO_WHATSAPP_FROM भरें।" if hi else
            "No OTP channel configured — set TWILIO_SMS_FROM or "
            "TWILIO_WHATSAPP_FROM in .env."
        )
    if via_verify:
        channel = "sms"  # Verify texts the code; present it as SMS to the user

    # ---- anti-abuse: cap OTP sends per phone number and per client ----
    allowed, retry = security.limiter.hit(f"otp:send:phone:{phone}", *OTP_SEND_PER_PHONE)
    if not allowed:
        security.audit("otp_send_rate_limited", actor=security.mask_phone(phone),
                       outcome="denied", detail={"scope": "phone", "retry_after_s": retry})
        return False, (f"बहुत अधिक OTP अनुरोध। {max(1, retry // 60)} मिनट बाद पुनः प्रयास करें।" if hi
                       else f"Too many OTP requests. Try again in "
                            f"{max(1, retry // 60)} minute(s).")
    allowed, retry = security.limiter.hit(
        f"otp:send:client:{security.client_key()}", *OTP_SEND_PER_CLIENT
    )
    if not allowed:
        security.audit("otp_send_rate_limited", actor=security.mask_phone(phone),
                       outcome="denied", detail={"scope": "client", "retry_after_s": retry})
        return False, ("बहुत अधिक OTP अनुरोध। कृपया थोड़ी देर बाद प्रयास करें।" if hi
                       else "Too many OTP requests. Please try again in a few minutes.")

    # Cryptographically secure local-fallback OTP (Verify generates its own).
    otp = f"{secrets.randbelow(1000000):06d}"
    text = (f"Suksmavrishti login OTP: {otp}. "
            f"Valid for {OTP_TTL_MINUTES} minutes. Do not share it with anyone.")
    try:
        if via_verify:
            _send_verify_code(phone)
        elif channel == "sms":
            _twilio_client().messages.create(
                body=text, from_=TWILIO_SMS_FROM, to=f"+91{phone}"
            )
        else:
            _twilio_client().messages.create(
                body=text, from_=TWILIO_WHATSAPP_FROM, to=f"whatsapp:+91{phone}"
            )
    except Exception as exc:
        # Full provider detail goes to the protected server-side log only.
        if via_verify:
            hint = _verify_hint(str(exc))
        else:
            hint = _whatsapp_hint(str(exc)) if channel == "whatsapp" else ""
        security.audit("otp_send_failed", actor=security.mask_phone(phone),
                       outcome="error", detail={"provider": "twilio", "error": str(exc)})
        return False, (f"OTP नहीं भेजा जा सका। कृपया दोबारा प्रयास करें।{hint}" if hi
                       else f"Could not send the OTP. Please try again.{hint}")

    st.session_state[_k("otp_phone")] = phone
    st.session_state[_k("otp_name")] = clean_name
    st.session_state[_k("otp_code")] = "" if via_verify else _otp_digest(otp)
    st.session_state[_k("otp_via_verify")] = via_verify
    st.session_state[_k("otp_expires")] = datetime.now() + timedelta(minutes=OTP_TTL_MINUTES)
    st.session_state[_k("otp_tries")] = 0
    st.session_state[_k("otp_sent")] = True
    security.audit("otp_sent", actor=security.mask_phone(phone), outcome="info",
                   detail={"channel": channel, "via_verify": via_verify})
    where = ("WhatsApp पर" if channel == "whatsapp" else "SMS पर") \
        if hi else ("WhatsApp" if channel == "whatsapp" else "SMS")
    return True, (f"OTP {where} +91{phone} पर भेज दिया गया है।"
                  if hi else
                  f"OTP sent to +91{phone} via {where}.")


def verify_otp(code: str, village: str, language: str):
    """Check the entered OTP and create the logged-in session. Returns (ok, message)."""
    hi = language == "हिंदी"
    phone = st.session_state.get(_k("otp_phone"), "")
    expected = st.session_state.get(_k("otp_code"), "")
    expires = st.session_state.get(_k("otp_expires"))
    via_verify = bool(st.session_state.get(_k("otp_via_verify")))

    if not phone or (not expected and not via_verify):
        return False, ("सेशन समाप्त हो गया, कृपया दोबारा OTP माँगें।" if hi
                       else "Session expired, please request a new OTP.")

    ok_village, clean_village, _village_err = security.validate_village(village)
    if not ok_village:
        clean_village = ""

    locked_for = security.limiter.is_locked(f"otp:verify:{phone}")
    if locked_for:
        security.audit("otp_verify_locked", actor=security.mask_phone(phone),
                       outcome="denied", detail={"retry_after_s": locked_for})
        return False, ("बहुत अधिक गलत प्रयास। 15 मिनट बाद नया OTP माँगें।" if hi
                       else "Too many wrong attempts. Request a new OTP in 15 minutes.")

    if expires is None or datetime.now() > expires:
        clear_otp()
        return False, ("OTP की समय-सीमा समाप्त हो गई, नया OTP माँगें।" if hi
                       else "The OTP has expired, please request a new one.")

    ok_format, clean_code = security.validate_otp(code)
    if not ok_format:
        return False, ("कृपया 6 अंकों का OTP दर्ज करें।" if hi
                       else "Please enter the 6-digit OTP.")

    tries = int(st.session_state.get(_k("otp_tries"), 0)) + 1
    st.session_state[_k("otp_tries")] = tries
    if tries > OTP_MAX_ATTEMPTS:
        clear_otp()
        security.limiter.fail(f"otp:verify:{phone}", *OTP_VERIFY_LOCK)
        security.audit("otp_verify_failed", actor=security.mask_phone(phone),
                       outcome="denied", detail={"reason": "too_many_session_tries"})
        return False, ("बहुत अधिक गलत प्रयास। नया OTP माँगें।" if hi
                       else "Too many wrong attempts. Please request a new OTP.")

    if not via_verify and not _otp_matches(clean_code, expected):
        remaining = OTP_MAX_ATTEMPTS - tries + 1
        security.limiter.fail(f"otp:verify:{phone}", *OTP_VERIFY_LOCK)
        security.audit("otp_verify_failed", actor=security.mask_phone(phone),
                       outcome="denied", detail={"remaining": remaining})
        return False, (f"गलत OTP। {remaining} प्रयास शेष।" if hi
                       else f"Wrong OTP. {remaining} attempt(s) left.")

    if via_verify:
        state, err = _check_verify_code(phone, clean_code, language)
        if state == "error":
            return False, err
        if state == "wrong":
            remaining = OTP_MAX_ATTEMPTS - tries + 1
            security.limiter.fail(f"otp:verify:{phone}", *OTP_VERIFY_LOCK)
            security.audit("otp_verify_failed", actor=security.mask_phone(phone),
                           outcome="denied", detail={"remaining": remaining})
            return False, (f"गलत OTP। {remaining} प्रयास शेष।" if hi
                           else f"Wrong OTP. {remaining} attempt(s) left.")

    name = st.session_state.get(_k("otp_name"), "")
    ok, err = _upsert_farmer(phone, name, clean_village, language)
    if not ok:
        security.audit("farmer_register_failed", actor=security.mask_phone(phone),
                       outcome="error", detail={"error": err})
        return False, ("रजिस्ट्रेशन अभी सेव नहीं हो सका। कृपया दोबारा प्रयास करें।" if hi
                       else "The registration could not be saved. Please try again.")

    st.session_state[_k("logged_in")] = True
    st.session_state[_k("phone")] = phone
    st.session_state[_k("name")] = name
    st.session_state[_k("village")] = clean_village
    st.session_state[_k("login_at")] = datetime.now()
    st.session_state[_k("last_seen")] = datetime.now()
    st.session_state[_k("session_token")] = security.make_session_token()
    security.limiter.clear(f"otp:verify:{phone}")
    clear_otp()
    security.audit("farmer_login_success", actor=security.mask_phone(phone),
                   outcome="allow", detail={"village": clean_village})
    return True, (f"स्वागत है {name}! आप लॉगिन हो गए हैं।" if hi
                  else f"Welcome {name}! You are now logged in.")


def clear_otp():
    for key in ("otp_phone", "otp_name", "otp_code", "otp_expires", "otp_tries",
                "otp_sent", "otp_via_verify"):
        st.session_state.pop(_k(key), None)



def resend_otp(language: str):
    """Invalidate the current code and send a fresh OTP to the same farmer."""
    phone = st.session_state.get(_k("otp_phone"), "")
    name = st.session_state.get(_k("otp_name"), "")
    clear_otp()
    return start_login(phone, name or "Farmer", language)


def otp_pending() -> bool:
    """True while an OTP has been sent and is awaiting verification."""
    return bool(st.session_state.get(_k("otp_sent")))


def pending_phone() -> str:
    return st.session_state.get(_k("otp_phone"), "")


def set_flash(msg: str):
    st.session_state[_k("flash")] = msg


def pop_flash():
    return st.session_state.pop(_k("flash"), None)


def _reject_session(reason: str) -> None:
    """End a session server-side and record why (audit trail for farmers too)."""
    phone = st.session_state.get(_k("phone"), "")
    security.audit("farmer_session_rejected", actor=security.mask_phone(phone),
                   outcome="denied", detail={"reason": reason})
    logout()


def is_logged_in() -> bool:
    """True only while the server-side session is valid (expiry + idle)."""
    if not st.session_state.get(_k("logged_in")):
        return False
    now = datetime.now()
    login_at = st.session_state.get(_k("login_at"))
    if login_at is None or (now - login_at) > timedelta(hours=SESSION_HOURS):
        _reject_session("expired")
        return False
    last_seen = st.session_state.get(_k("last_seen"))
    if last_seen is None:
        st.session_state[_k("last_seen")] = now          # start counting now
    elif (now - last_seen) > timedelta(minutes=SESSION_IDLE_MINUTES):
        _reject_session("idle")
        return False
    else:
        st.session_state[_k("last_seen")] = now          # keep the session warm
    phone = normalize_phone(st.session_state.get(_k("phone"), ""))
    if not phone:
        logout()
        return False
    return True


def current_farmer():
    if not is_logged_in():
        return None
    return {
        "phone": st.session_state.get(_k("phone"), ""),
        "name": st.session_state.get(_k("name"), ""),
        "village": st.session_state.get(_k("village"), ""),
    }


def logout():
    phone = st.session_state.get(_k("phone"), "")
    for key in ("logged_in", "phone", "name", "village", "login_at",
                "last_seen", "session_token"):
        st.session_state.pop(_k(key), None)
    clear_otp()
    if phone:
        security.audit("farmer_logout", actor=security.mask_phone(phone), outcome="info")


# --------------------------------------------------------------------------- #
# Report delivery: WhatsApp + SMS
# --------------------------------------------------------------------------- #
def _report_allowed() -> tuple[bool, int]:
    """Cost control: cap outbound reports per client."""
    return security.limiter.hit(f"report:client:{security.client_key()}",
                                *security.REPORT_SEND_RATE_LIMIT)


def send_whatsapp_report(phone: str, body: str) -> tuple[bool, str]:
    if not twilio_ready():
        return False, "Twilio credentials are not configured."
    if not TWILIO_WHATSAPP_FROM:
        return False, "TWILIO_WHATSAPP_FROM is not set in .env."
    safe_phone = normalize_phone(phone)
    if not safe_phone:
        return False, "Invalid recipient number."
    allowed, _retry = _report_allowed()
    if not allowed:
        return False, "Report limit reached. Please try again later."
    try:
        _twilio_client().messages.create(
            body=body[:1500],
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:+91{safe_phone}",
        )
        security.audit("report_sent", actor=security.mask_phone(safe_phone),
                       outcome="info", detail={"channel": "whatsapp"})
        return True, ""
    except Exception as exc:
        security.audit("report_send_failed", actor=security.mask_phone(safe_phone),
                       outcome="error", detail={"channel": "whatsapp", "error": str(exc)})
        return False, "WhatsApp delivery failed." + _whatsapp_hint(str(exc))


def send_sms_report(phone: str, body: str) -> tuple[bool, str]:
    if not twilio_ready():
        return False, "Twilio credentials are not configured."
    if not TWILIO_SMS_FROM:
        return False, "TWILIO_SMS_FROM is not set in .env."
    safe_phone = normalize_phone(phone)
    if not safe_phone:
        return False, "Invalid recipient number."
    allowed, _retry = _report_allowed()
    if not allowed:
        return False, "Report limit reached. Please try again later."
    try:
        _twilio_client().messages.create(
            body=body[:1500], from_=TWILIO_SMS_FROM, to=f"+91{safe_phone}"
        )
        security.audit("report_sent", actor=security.mask_phone(safe_phone),
                       outcome="info", detail={"channel": "sms"})
        return True, ""
    except Exception as exc:
        security.audit("report_send_failed", actor=security.mask_phone(safe_phone),
                       outcome="error", detail={"channel": "sms", "error": str(exc)})
        return False, "SMS delivery failed."
