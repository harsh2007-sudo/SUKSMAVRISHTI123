"""
security.py — shared security helpers for Suksmavrishti.

Everything here is server-side (this is a Streamlit app: the Python process is
the trusted backend). The module provides:

  * schema-style input validation (Khasra numbers, names, villages, notes)
  * HTML escaping for values that are rendered with unsafe_allow_html=True
  * CSV-formula-injection neutralisation
  * password hashing (PBKDF2-HMAC-SHA256, per-record salt) and verification
  * thread-safe sliding-window rate limiting + lockout for login attempts
  * audit logging (JSON lines, redacted, restricted file permissions)
  * server-side image upload verification (size, magic bytes, real decodable
    image) with re-encoding so a "photo" can never carry an executable payload

No required third-party dependencies, no custom cryptography. (python-dotenv
is used only when it happens to be installed, to read a local .env.)
"""

from __future__ import annotations

import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, Optional, Tuple

# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
MAX_NAME_LEN = 60
MAX_VILLAGE_LEN = 80
MAX_NOTE_LEN = 500
MAX_KHASRA_LEN = 20

MAX_UPLOAD_BYTES = 5 * 1024 * 1024          # 5 MB per photo
MAX_UPLOAD_FILES_PER_BATCH = 1
UPLOAD_RATE_LIMIT = (6, 600)                # 6 uploads / 10 min
FORECAST_RATE_LIMIT = (30, 60)              # 30 lookups / min
REPORT_SEND_RATE_LIMIT = (5, 600)           # 5 SMS/WhatsApp reports / 10 min

# Where the security artefacts (audit log + rate-limit state) live.
#
# SV_DATA_DIR points at a persistent volume — on Render, attach a disk and set
# SV_DATA_DIR=/var/data — so a redeploy that resets the app checkout neither
# loses the audit trail nor clears an active lockout. Unset keeps the old
# behaviour: ./logs next to the code.
def _logs_dir_for(data_dir: str) -> str:
    if data_dir:
        return os.path.join(data_dir, "logs")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


try:                                    # optional: a local .env may set SV_DATA_DIR
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    _load_dotenv = None

_DATA_DIR = os.environ.get("SV_DATA_DIR", "").strip().rstrip("/\\")
_LOGS_DIR = _logs_dir_for(_DATA_DIR)

# Control characters (except tab/newline, which we strip anyway) used to try to
# forge log lines or break markup.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Khasra / Gata numbers in UP Bhulekh look like 142, 21क, 22/1, 1431-1436.
# Devanagari digits/letters are allowed so legitimate Indian records are kept.
_KHASRA_RE = re.compile(r"^[0-9\u0900-\u097F]{1,6}([/-][0-9\u0900-\u097F]{1,4})?$")
_PERSON_RE = re.compile(r"^[A-Za-z\u0900-\u097F][A-Za-z\u0900-\u097F .'\-]{1,59}$")
_VILLAGE_RE = re.compile(r"^[A-Za-z0-9\u0900-\u097F][A-Za-z0-9\u0900-\u097F .,'\-()]{0,79}$")


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def strip_control(value: Any, max_len: int = 400) -> str:
    """Trim, collapse newlines/tabs and strip control characters."""
    text = "" if value is None else str(value)
    text = _CTRL_RE.sub("", text).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:max_len]


def esc(value: Any) -> str:
    """Escape a value for HTML text/attribute context (XSS defence)."""
    return html.escape("" if value is None else str(value), quote=True)


def csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @) in exported CSV."""
    text = strip_control(value, MAX_NOTE_LEN)
    if text[:1] in ("=", "+", "-", "@"):
        text = "'" + text
    return text


def safe_slug(value: Any, max_len: int = 32) -> str:
    """Whitelist a string for use inside a generated filename."""
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_")
    return (text or "field")[:max_len]


def mask_phone(phone: Any) -> str:
    """Show only the last 4 digits in logs / diagnostics."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return ("*" * max(0, len(digits) - 4)) + digits[-4:] if digits else ""


# --------------------------------------------------------------------------- #
# Input validation  (server-side; never trust the widget / browser)
# --------------------------------------------------------------------------- #
def validate_khasra(raw: Any) -> Tuple[bool, str, str]:
    """Return (ok, normalized_value, error_en). Accepts 142, 21क, 22/1, 1431-1436."""
    text = strip_control(raw, MAX_KHASRA_LEN + 1).strip()
    if not text:
        return False, "", "Please enter a valid Khasra number (e.g. 142, 22/1)."
    if len(text) > MAX_KHASRA_LEN:
        return False, "", "That Khasra number is too long."
    if not _KHASRA_RE.match(text):
        return False, "", (
            "Khasra numbers may only contain digits, Devanagari characters and a "
            "single '/' or '-' separator (e.g. 142, 21क, 22/1)."
        )
    return True, text, ""


def validate_person_name(raw: Any, required: bool = True) -> Tuple[bool, str, str]:
    text = strip_control(raw, MAX_NAME_LEN + 1)
    if not text:
        return (not required), "", ("Please enter your name." if required else "")
    if len(text) > MAX_NAME_LEN:
        return False, "", f"Name must be at most {MAX_NAME_LEN} characters."
    if not _PERSON_RE.match(text):
        return False, "", (
            "Name may only contain letters, spaces and . ' - characters."
        )
    return True, text, ""


def validate_village(raw: Any) -> Tuple[bool, str, str]:
    """Optional village name; empty is valid."""
    text = strip_control(raw, MAX_VILLAGE_LEN + 1)
    if not text:
        return True, "", ""
    if len(text) > MAX_VILLAGE_LEN:
        return False, "", f"Village name must be at most {MAX_VILLAGE_LEN} characters."
    if not _VILLAGE_RE.match(text):
        return False, "", "Village name contains invalid characters."
    return True, text, ""


def validate_note(raw: Any) -> Tuple[bool, str, str]:
    """Optional free-text note: length-capped, control characters removed."""
    text = strip_control(raw, MAX_NOTE_LEN + 1)
    if len(text) > MAX_NOTE_LEN:
        return False, "", f"Note must be at most {MAX_NOTE_LEN} characters."
    return True, text, ""


def validate_otp(raw: Any) -> Tuple[bool, str]:
    text = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return (len(text) == 6 and text == str(raw or "").strip()), text


# --------------------------------------------------------------------------- #
# Password hashing (PBKDF2-HMAC-SHA256) — no custom cryptography
# --------------------------------------------------------------------------- #
PBKDF2_ITERATIONS = 600_000
PBKDF2_MAX_ITERATIONS = 2_000_000   # refuse absurd values from a tampered file
MIN_PASSWORD_LEN = 12
_DUMMY_HASH = None


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return 'pbkdf2_sha256$iterations$salt_b64$hash_b64'."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    from base64 import b64encode
    return "pbkdf2_sha256${}${}${}".format(
        iterations, b64encode(salt).decode("ascii"), b64encode(derived).decode("ascii")
    )


def _parse_hash(stored: str) -> Optional[Tuple[int, bytes, bytes]]:
    try:
        algo, iterations, salt_b64, hash_b64 = str(stored).split("$", 3)
        if algo != "pbkdf2_sha256":
            return None
        rounds = int(iterations)
        if not (10_000 <= rounds <= PBKDF2_MAX_ITERATIONS):
            return None
        from base64 import b64decode
        return rounds, b64decode(salt_b64), b64decode(hash_b64)
    except Exception:
        return None


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Always does the KDF work (no quick exit)."""
    parsed = _parse_hash(stored)
    if parsed is None:
        # Burn comparable time on a dummy hash so a missing/invalid record is
        # not distinguishable from a wrong password.
        global _DUMMY_HASH
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("suksmavrishti-dummy-password")
        parsed_dummy = _parse_hash(_DUMMY_HASH)
        if parsed_dummy:
            rounds, salt, _ = parsed_dummy
            hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, rounds)
        return False
    rounds, salt, expected = parsed
    derived = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, rounds)
    return hmac.compare_digest(derived, expected)


def password_problem(password: str) -> str:
    """Return an error message, or '' when the password is acceptable."""
    pwd = str(password or "")
    if len(pwd) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if pwd.lower() in {"password", "changeme", "suksmavrishti", "123456789012"}:
        return "That password is too common."
    classes = sum([
        any(c.islower() for c in pwd),
        any(c.isupper() for c in pwd),
        any(c.isdigit() for c in pwd),
        any(not c.isalnum() for c in pwd),
    ])
    if classes < 3:
        return "Use upper case, lower case, digits and a symbol (at least 3 of the 4)."
    return ""


def generate_password(length: int = 18) -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%*-_"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if not password_problem(pwd):
            return pwd


# --------------------------------------------------------------------------- #
# Rate limiting / lockout (in-process; single Streamlit server)
# --------------------------------------------------------------------------- #
class AttemptLimiter:
    """Sliding-window rate limiter and failure lockout.

    Thread-safe. Keys are pruned so untrusted input cannot grow memory without
    bound. With `persist=True` the state is mirrored to an owner-only JSON file
    so a process restart cannot clear an active lockout (the module-level
    `limiter` uses it; throw-away instances stay in-memory). For a
    multi-instance deployment move this to Redis (the interface is a single
    class).
    """

    SAVE_INTERVAL = 2.0          # seconds between routine flushes
    PERSIST_WINDOW = 900.0       # only keep stamps inside the longest window
    MAX_PERSIST_KEYS = 2000      # bound on the state file

    def __init__(self, max_keys: int = 20_000, persist: bool = False,
                 state_file: Optional[str] = None) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._locked: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._max_keys = max_keys
        self._state_file = (
            state_file or os.path.join(_LOGS_DIR, "rate_limiter_state.json")
        ) if persist else None
        self._last_save = 0.0
        if self._state_file:
            self._load_state()

    # -- restart-proof persistence ------------------------------------------
    def _load_state(self) -> None:
        """Restore counters and lockouts written by a previous run (best effort)."""
        try:
            with open(self._state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        try:
            for key, until in (data.get("locked") or {}).items():
                until = float(until)
                if until > now:
                    self._locked[str(key)[:200]] = until
            for key, stamps in (data.get("hits") or {}).items():
                row = [float(t) for t in stamps if 0 < now - float(t) < self.PERSIST_WINDOW]
                if row:
                    self._hits[str(key)[:200]] = deque(row[-64:])
        except Exception:
            self._hits.clear()
            self._locked.clear()

    def _save_state(self, force: bool = False) -> None:
        """Persist atomically with owner-only permissions (throttled)."""
        if not self._state_file:
            return
        now = time.time()
        if not force and now - self._last_save < self.SAVE_INTERVAL:
            return
        self._last_save = now
        try:
            with self._lock:
                for key in list(self._locked):
                    if self._locked[key] <= now:
                        self._locked.pop(key, None)
                hits: Dict[str, list] = {}
                for i, (key, stamps) in enumerate(self._hits.items()):
                    if i >= self.MAX_PERSIST_KEYS:
                        break
                    recent = [t for t in list(stamps)[-64:]
                              if 0 < now - t < self.PERSIST_WINDOW]
                    if recent:
                        hits[key] = recent
                payload = json.dumps({"v": 1, "saved_at": now,
                                      "locked": dict(self._locked), "hits": hits})
            tmp = f"{self._state_file}.{os.getpid()}.tmp"
            os.makedirs(os.path.dirname(self._state_file) or ".", exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_file)
        except Exception:
            pass  # persistence is best effort; never break a request

    # -- internal ------------------------------------------------------------
    def _prune(self, key: str, window: float, now: float) -> None:
        q = self._hits.get(key)
        if not q:
            return
        while q and now - q[0] > window:
            q.popleft()
        if not q:
            self._hits.pop(key, None)

    def _shrink(self, now: float) -> None:
        if len(self._hits) <= self._max_keys and len(self._locked) <= self._max_keys:
            return
        for key, until in list(self._locked.items()):
            if until <= now:
                self._locked.pop(key, None)
        while len(self._hits) > self._max_keys // 2:
            self._hits.pop(next(iter(self._hits)), None)

    # -- public --------------------------------------------------------------
    def hit(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        """Count one event. Returns (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            self._shrink(now)
            lock_until = self._locked.get(key, 0.0)
            if lock_until > now:
                return False, int(lock_until - now) + 1
            self._prune(key, window_seconds, now)
            q = self._hits[key]
            if len(q) >= limit:
                retry = int(window_seconds - (now - q[0])) + 1
                return False, max(retry, 1)
            q.append(now)
            self._save_state()
            return True, 0

    def is_locked(self, key: str) -> int:
        now = time.time()
        with self._lock:
            until = self._locked.get(key, 0.0)
            if until <= now:
                self._locked.pop(key, None)
                return 0
            return int(until - now) + 1

    def fail(self, key: str, threshold: int = 5, window_seconds: int = 900,
             lockout_seconds: int = 900) -> int:
        """Record a failed attempt. Returns lockout seconds remaining (0 = not locked)."""
        now = time.time()
        with self._lock:
            self._shrink(now)
            self._prune(key, window_seconds, now)
            q = self._hits[key]
            q.append(now)
            if len(q) >= threshold:
                self._locked[key] = now + lockout_seconds
                self._hits.pop(key, None)
                self._save_state(force=True)   # a lockout must survive a restart
                return lockout_seconds
            self._save_state()
            return 0

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
            self._locked.pop(key, None)
            self._save_state(force=True)

    def reset_all(self) -> None:
        """Forget everything, including the persisted state (tests / admin)."""
        with self._lock:
            self._hits.clear()
            self._locked.clear()
            if self._state_file:
                try:
                    os.remove(self._state_file)
                except OSError:
                    pass


# The process-wide limiter persists to logs/rate_limiter_state.json so an
# active lockout cannot be cleared by restarting the server.
limiter = AttemptLimiter(persist=True)


def client_key() -> str:
    """Best-effort client identity for rate limiting (IP when available).

    Set SV_TRUST_PROXY=1 when the app sits behind a reverse proxy you control
    (nginx, deploy/https_proxy.py): the proxy-set X-Forwarded-For is then used,
    so every visitor keeps their own bucket instead of sharing the proxy's IP.
    The header is parsed as an IP address, so untrusted input cannot forge keys.
    """
    if os.environ.get("SV_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes"}:
        try:
            import ipaddress
            import streamlit as st
            raw = st.context.headers or {}
            lookup = {str(k).lower(): v for k, v in dict(raw).items()}
            fwd = str(lookup.get("x-forwarded-for") or "").split(",")[0].strip()
            if fwd:
                ipaddress.ip_address(fwd)          # raises on junk
                return f"ip:{fwd}"
        except Exception:
            pass
    try:
        import streamlit as st
        ip = getattr(st.context, "ip_address", None)
        if ip:
            return f"ip:{ip}"
    except Exception:
        pass
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        ctx = get_script_run_ctx(suppress_warning=True)
        if ctx is not None:
            return f"sid:{ctx.session_id}"
    except Exception:
        pass
    return "sid:anonymous"


# --------------------------------------------------------------------------- #
# Audit logging
# --------------------------------------------------------------------------- #
AUDIT_DIR = _LOGS_DIR
AUDIT_FILE = os.path.join(AUDIT_DIR, "security_audit.log")
AUDIT_MAX_BYTES = 2 * 1024 * 1024
_audit_lock = threading.Lock()
_REDACT_KEYS = ("pass", "otp", "token", "secret", "key", "phone", "cookie", "auth")


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(tag in str(k).lower() for tag in _REDACT_KEYS):
                out[k] = "[redacted]"
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_scrub(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        return _CTRL_RE.sub("", value)[:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return strip_control(value, 200)


def audit(event: str, actor: str = "", outcome: str = "info",
          detail: Optional[Dict[str, Any]] = None) -> None:
    """Append one redacted JSON line to the local security audit log."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": strip_control(event, 60),
        "actor": strip_control(actor, 80),
        "outcome": strip_control(outcome, 20),
        "detail": _scrub(detail or {}),
    }
    try:
        with _audit_lock:
            os.makedirs(AUDIT_DIR, exist_ok=True)
            try:
                os.chmod(AUDIT_DIR, 0o700)
            except Exception:
                pass
            if os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) > AUDIT_MAX_BYTES:
                rotated = AUDIT_FILE + ".1"
                os.replace(AUDIT_FILE, rotated)
                try:
                    os.chmod(rotated, 0o600)
                except Exception:
                    pass
            # Created owner-only from the first byte, then fsynced so a crash
            # cannot lose the security trail it just recorded.
            fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
    except Exception:
        # Logging must never break a request.
        pass


def read_audit_tail(limit: int = 50) -> Iterable[Dict[str, Any]]:
    """Admin helper: newest-first tail of the audit log."""
    try:
        with open(AUDIT_FILE, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        return [json.loads(line) for line in reversed(lines) if line.strip()]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# File upload verification
# --------------------------------------------------------------------------- #
_IMAGE_MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}


def validate_image_upload(filename: str, data: bytes,
                          max_bytes: int = MAX_UPLOAD_BYTES) -> Tuple[Optional[bytes], str]:
    """Verify and sanitise an uploaded field photo.

    Returns (jpeg_bytes, "") on success or (None, error_message). The image is
    decoded and re-encoded as JPEG so polyglot/HTML payloads and EXIF metadata
    never reach the training folder.
    """
    if not data:
        return None, "The uploaded file is empty."
    if len(data) > max_bytes:
        return None, f"Photo is too large (max {max_bytes // (1024 * 1024)} MB)."

    ext = os.path.splitext(str(filename or ""))[1].lower().lstrip(".")
    if ext not in _IMAGE_MAGIC:
        return None, "Only JPG, PNG or WEBP photos are allowed."

    if not any(data.startswith(magic) for magic in _IMAGE_MAGIC[ext]):
        return None, "The file content does not match its extension."
    if ext == "webp" and not (len(data) > 12 and data[8:12] == b"WEBP"):
        return None, "The file content does not match its extension."

    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 24_000_000   # decompression-bomb guard
        with Image.open(io.BytesIO(data)) as probe:
            fmt = (probe.format or "").upper()
            probe.verify()
        if fmt not in {"PNG", "JPEG", "WEBP"}:
            return None, "Only real photo files are allowed."
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((1600, 1600))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue(), ""
    except Exception:
        return None, "That file is not a valid image."


def inside_directory(path: str, directory: str) -> bool:
    """True when `path` resolves inside `directory` (path-traversal guard)."""
    try:
        base = os.path.realpath(directory)
        target = os.path.realpath(path)
        return os.path.commonpath([base, target]) == base
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #
def now_ts() -> float:
    return time.time()


def make_session_token() -> str:
    return secrets.token_urlsafe(32)
