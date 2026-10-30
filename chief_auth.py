"""
chief_auth.py — Sarpanch (Chief) authentication for Suksmavrishti.

Security model
--------------
* Passwords are NEVER stored or logged in plaintext: only PBKDF2-HMAC-SHA256
  hashes with a per-record random salt (`pbkdf2_sha256$600000$salt$hash`).
* There are no predictable default passwords. `ensure_credentials()` issues a
  cryptographically random password for every area that has none, writes it
  ONCE to `.chief_credentials_initial.txt` (chmod 600, git-ignored) and stores
  only the hash. Rotate with `setup_chief_credentials.py`.
* Login is rate-limited and locks out after repeated failures; the error
  message never reveals whether the area, ID or password was wrong.
* Sessions carry a random token, an absolute expiry and an idle timeout, and
  are invalidated when credentials are rotated.

The role/authorisation checks live in the UI layer (App.py) and always combine
`role == "chief"` with `chief_auth.is_logged_in()` — editing browser state or
the URL cannot grant Chief access because session state is server-side and
this module is the only source of truth for `is_logged_in()`.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import security

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_paths(data_dir: str = "") -> "tuple[str, str]":
    """(credential store, one-time password file) for a data directory.

    An empty `data_dir` keeps both files next to the code (dev default). Set
    SV_DATA_DIR=/var/data to keep them on a persistent volume instead, so a
    redeploy that resets the app checkout does not silently reissue every chief
    password. Returns plain paths; nothing is read or written here.
    """
    if not data_dir:
        return (os.path.join(APP_DIR, "chief_credentials.json"),
                os.path.join(APP_DIR, ".chief_credentials_initial.txt"))
    return (os.path.join(data_dir, "chief_credentials.json"),
            os.path.join(data_dir, ".chief_credentials_initial.txt"))


CRED_FILE, INITIAL_PASSWORDS_FILE = _resolve_paths(
    os.environ.get("SV_DATA_DIR", "").strip().rstrip("/\\")
)

SESSION_KEY = "sv_chief_session"
STORE_VERSION = 2

SESSION_HOURS = float(os.getenv("SV_CHIEF_SESSION_HOURS", "8"))
IDLE_MINUTES = float(os.getenv("SV_CHIEF_IDLE_MINUTES", "45"))
MAX_FAILURES = int(os.getenv("SV_CHIEF_MAX_FAILURES", "5"))
LOCKOUT_MINUTES = int(os.getenv("SV_CHIEF_LOCKOUT_MINUTES", "15"))

# Fallback area list (village / mauza covered by the forecast grid) used when
# the Bhulekh CSV is not available.
DEFAULT_AREAS = [
    ("Kalyani Basin", "कल्याणी कछार"),
    ("Deokhaliya", "देवखलिया"),
    ("Masauli", "मसौली"),
    ("Chhatauni", "छतौनी"),
    ("Rasauli", "रसौली"),
    ("Satrikh", "सतरिख देहात"),
    ("Rampur", "रामपुर कटरा"),
    ("Fatehpur", "फतेहपुर देहात"),
    ("Safedabad", "सफेदाबाद"),
    ("Nawabganj", "नवाबगंज (पल्हरी / कस्बा)"),
    ("Bado Sarai", "बदोसराय"),
    ("Zaidpur", "जैदपुर देहात"),
]

_store_cache = {"mtime": None, "data": None}
_store_lock = threading.RLock()


def _slug(text: str) -> str:
    """Area name -> url-style slug used in the chief ID."""
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return s or "area"


def _default_area_objs():
    return [{"en": e, "hi": h} for e, h in DEFAULT_AREAS]


def areas_from_khasra(df_khasra=None):
    """Return [{"en": ..., "hi": ...}] for every area used by the forecast."""
    if df_khasra is not None:
        en_col = "village_mauza_en" if "village_mauza_en" in df_khasra.columns else None
        if en_col is None and "village_mauza" in df_khasra.columns:
            en_col = "village_mauza"
        if en_col:
            hi_col = (
                "village_mauza_hi"
                if "village_mauza_hi" in df_khasra.columns
                else en_col
            )
            hi_map = {}
            try:
                for e, h in zip(df_khasra[en_col], df_khasra[hi_col]):
                    if e != e:  # NaN guard
                        continue
                    e = str(e).strip()
                    if e and e not in hi_map and isinstance(h, str) and h.strip():
                        hi_map[e] = h.strip()
                out = []
                for e in df_khasra[en_col].dropna().unique():
                    e = str(e).strip()
                    if e and not any(o["en"] == e for o in out):
                        out.append({"en": e, "hi": hi_map.get(e, e)})
                if out:
                    return out
            except Exception:
                pass
    return _default_area_objs()


# --------------------------------------------------------------------------- #
# Credential store (hashes only)
# --------------------------------------------------------------------------- #
def _read_store() -> dict:
    """Read the store once per file change (avoids JSON I/O on every rerun)."""
    with _store_lock:
        try:
            mtime = os.path.getmtime(CRED_FILE)
        except OSError:
            mtime = None
        if mtime is None:
            # First boot on a fresh data volume: adopt the checkout copy so the
            # existing logins keep working instead of being reissued.
            mtime = _adopt_store(CRED_FILE, os.path.join(APP_DIR, "chief_credentials.json"))
        if _store_cache["data"] is not None and _store_cache["mtime"] == mtime:
            return _store_cache["data"]
        data = {}
        if mtime is not None:
            try:
                with open(CRED_FILE, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        _store_cache["mtime"] = mtime
        _store_cache["data"] = data
        return data


def _adopt_store(target: str, seed: str) -> "float | None":
    """Copy the checkout's credential store onto a fresh data volume (once).

    Returns the new file's mtime, or None when there was nothing to adopt
    (same path, seed missing, or target already present).
    """
    if target == seed or os.path.exists(target) or not os.path.exists(seed):
        return None
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(seed, "rb") as fh:
            data = fh.read()
        tmp = target + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
        security.audit("chief_store_adopted", outcome="info",
                       detail={"from": "checkout", "bytes": len(data)})
        return os.path.getmtime(target)
    except Exception as exc:
        security.audit("chief_store_adopt_failed", outcome="error",
                       detail={"error": str(exc)})
        return None


def _write_store(data: dict) -> None:
    """Atomic write with owner-only permissions."""
    with _store_lock:
        os.makedirs(os.path.dirname(CRED_FILE) or ".", exist_ok=True)
        tmp = CRED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CRED_FILE)
        try:
            os.chmod(CRED_FILE, 0o600)
        except Exception:
            pass
        try:
            _store_cache["mtime"] = os.path.getmtime(CRED_FILE)
        except OSError:
            _store_cache["mtime"] = None
        _store_cache["data"] = data


def _append_initial_passwords(issued: dict) -> None:
    """Write freshly generated passwords exactly once, for the operator only."""
    if not issued:
        return
    try:
        os.makedirs(os.path.dirname(INITIAL_PASSWORDS_FILE) or ".", exist_ok=True)
        new_file = not os.path.exists(INITIAL_PASSWORDS_FILE)
        with open(INITIAL_PASSWORDS_FILE, "a", encoding="utf-8") as fh:
            if new_file:
                fh.write(
                    "# Suksmavrishti - one-time chief credentials.\n"
                    "# READ these, then delete this file. Only password hashes are kept in\n"
                    "# chief_credentials.json. Change any password with:\n"
                    "#   python setup_chief_credentials.py --area \"Rampur\"\n"
                )
            fh.write(f"\n# issued {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
            for area, credentials in issued.items():
                fh.write(
                    f"{area}: id={credentials['id']} password={credentials['password']}\n"
                )
        try:
            os.chmod(INITIAL_PASSWORDS_FILE, 0o600)
        except Exception:
            pass
    except Exception as exc:  # never block login on this
        security.audit("chief_credentials_file_error", outcome="error",
                       detail={"error": str(exc)})


def _version(store: dict) -> str:
    meta = store.get("_meta") if isinstance(store.get("_meta"), dict) else {}
    return str(meta.get("credentials_version", ""))


def _bump_version(store: dict) -> None:
    meta = store.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        store["_meta"] = meta
    meta["version"] = STORE_VERSION
    meta["algo"] = "pbkdf2_sha256"
    meta["iterations"] = security.PBKDF2_ITERATIONS
    meta["credentials_version"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_credentials(areas=None) -> dict:
    """Create/repair one credential record per area. Returns the store.

    Legacy plaintext entries (and known demo passwords from the old build) are
    rotated automatically: the exposed value is discarded and a fresh random
    password is issued. No predictable password is ever kept.
    """
    if areas is None:
        areas = _default_area_objs()
    store = dict(_read_store())
    changed = False
    issued = {}

    if store.get("_meta") is None:
        store["_meta"] = {}
        changed = True

    for area in areas:
        name = str(area["en"] if isinstance(area, dict) else area).strip()
        if not name:
            continue
        entry = store.get(name)
        valid = (
            isinstance(entry, dict)
            and entry.get("id")
            and str(entry.get("password_hash", "")).startswith("pbkdf2_sha256$")
            and "password" not in entry
        )
        if valid:
            continue

        uid = f"chief_{_slug(name)}"
        password = security.generate_password()
        store[name] = {
            "id": uid,
            "password_hash": security.hash_password(password),
            "must_change": True,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        issued[name] = {"id": uid, "password": password}
        changed = True
        security.audit(
            "chief_credential_issued",
            actor=uid,
            outcome="rotated" if isinstance(entry, dict) else "created",
            detail={"area": name, "reason": "legacy_or_missing"},
        )

    if changed:
        _bump_version(store)
        try:
            _write_store(store)
        except Exception as exc:
            security.audit("chief_credentials_write_failed", outcome="error",
                           detail={"error": str(exc)})
        _append_initial_passwords(issued)

    return store


def needs_password_change() -> bool:
    try:
        session = st.session_state.get(SESSION_KEY) or {}
    except Exception:
        return False
    return bool(session.get("must_change"))


def credentials_hint(is_hi: bool = False) -> str:
    """Operator hint shown on the login page (never exposes a secret)."""
    return (
        "पासवर्ड भूल गए? `python setup_chief_credentials.py --area \"<क्षेत्र>\"` चलाकर नया पासवर्ड सेट करें।"
        if is_hi else
        "Forgot the password? Set a new one with "
        "`python setup_chief_credentials.py --area \"<area>\"` on the server."
    )


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def _is_hi() -> bool:
    try:
        return st.session_state.get("language", "English") != "English"
    except Exception:
        return False


def _limit_key(area: str, user_id: str) -> str:
    return f"chief|{security.safe_slug(area)}|{security.safe_slug(user_id, 40)}|{security.client_key()}"


def login(area, user_id, password, areas=None):
    """Verify credentials -> (ok, message). Generic errors, rate-limited."""
    store = ensure_credentials(areas)
    area = str(area or "").strip()
    uid = str(user_id or "").strip()
    pwd = str(password or "")

    hi = _is_hi()
    key = _limit_key(area, uid)

    locked_for = security.limiter.is_locked(key)
    if locked_for:
        security.audit("chief_login_locked", actor=uid, outcome="denied",
                       detail={"area": area, "retry_after_s": locked_for})
        minutes = max(1, locked_for // 60)
        return False, (
            f"बहुत अधिक गलत प्रयास। {minutes} मिनट बाद पुनः प्रयास करें।" if hi
            else f"Too many failed attempts. Try again in {minutes} minute(s)."
        )

    entry = store.get(area)
    if not isinstance(entry, dict):
        # Same generic message as a wrong password (no area enumeration).
        wait = security.limiter.fail(key, MAX_FAILURES, LOCKOUT_MINUTES * 60,
                                     LOCKOUT_MINUTES * 60)
        security.audit("chief_login_failed", actor=uid, outcome="denied",
                       detail={"area": area, "reason": "unknown_area", "locked": bool(wait)})
        return False, ("गलत ID या पासवर्ड।" if hi else "Wrong ID or password.")

    if not uid or not pwd:
        return False, ("ID और पासवर्ड दोनों भरें।" if hi else "Please enter both ID and password.")

    stored_id = str(entry.get("id", ""))
    stored_hash = str(entry.get("password_hash", ""))

    # Verify both factors in constant time; the hash is computed either way so
    # response time does not reveal which part was wrong.
    id_ok = hmac.compare_digest(uid.encode("utf-8"), stored_id.encode("utf-8"))
    pwd_ok = security.verify_password(pwd, stored_hash)

    if id_ok and pwd_ok:
        security.limiter.clear(key)
        st.session_state[SESSION_KEY] = {
            "area": area,
            "id": stored_id,
            "token": security.make_session_token(),
            "cred_version": _version(store),
            "issued_at": security.now_ts(),
            "last_seen": security.now_ts(),
            "must_change": bool(entry.get("must_change")),
        }
        security.audit("chief_login_success", actor=stored_id, outcome="allow",
                       detail={"area": area})
        return True, (f"सरपंच लॉगिन सफल — {area}" if hi
                      else f"Chief login successful — {area}")

    wait = security.limiter.fail(key, MAX_FAILURES, LOCKOUT_MINUTES * 60,
                                 LOCKOUT_MINUTES * 60)
    security.audit("chief_login_failed", actor=uid, outcome="denied",
                   detail={"area": area, "reason": "bad_credentials", "locked": bool(wait)})
    if wait:
        return False, (
            f"बहुत अधिक गलत प्रयास। {LOCKOUT_MINUTES} मिनट बाद पुनः प्रयास करें।" if hi
            else f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minute(s)."
        )
    return False, ("गलत ID या पासवर्ड।" if hi else "Wrong ID or password.")


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def _session_invalid_reason(session) -> str:
    if not isinstance(session, dict) or not session.get("id"):
        return "missing"
    now = security.now_ts()
    issued = float(session.get("issued_at", 0) or 0)
    seen = float(session.get("last_seen", 0) or 0)
    if not issued or now - issued > SESSION_HOURS * 3600:
        return "expired"
    if not seen or now - seen > IDLE_MINUTES * 60:
        return "idle"
    try:
        current_version = _version(_read_store())
    except Exception:
        current_version = session.get("cred_version", "")
    if session.get("cred_version") and current_version and session["cred_version"] != current_version:
        return "rotated"
    return ""


def is_logged_in() -> bool:
    """Server-side session validity: role-independent, used by every guard."""
    try:
        session = st.session_state.get(SESSION_KEY)
    except Exception:
        return False
    reason = _session_invalid_reason(session)
    if reason:
        if reason != "missing" and isinstance(session, dict):
            security.audit("chief_session_rejected", actor=session.get("id", ""),
                           outcome="denied", detail={"reason": reason})
            st.session_state.pop(SESSION_KEY, None)
        return False
    session["last_seen"] = security.now_ts()
    return True


def current_chief() -> dict:
    if not is_logged_in():
        return {}
    session = st.session_state.get(SESSION_KEY) or {}
    return {
        "area": session.get("area", ""),
        "id": session.get("id", ""),
        "must_change": bool(session.get("must_change")),
    }


def session_expiry_message(is_hi: bool = False) -> str:
    return ("सेशन समाप्त हो गया — कृपया दोबारा लॉगिन करें।" if is_hi
            else "Your session expired — please log in again.")


def logout() -> None:
    session = None
    try:
        session = st.session_state.get(SESSION_KEY)
    except Exception:
        pass
    if isinstance(session, dict) and session.get("id"):
        security.audit("chief_logout", actor=session.get("id", ""), outcome="info",
                       detail={"area": session.get("area", "")})
    try:
        st.session_state.pop(SESSION_KEY, None)
    except Exception:
        pass
