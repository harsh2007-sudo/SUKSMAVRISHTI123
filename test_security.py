"""Security regression tests for Suksmavrishti (stdlib unittest).

Run from the project root:
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import security  # noqa: E402


class TestInputValidation(unittest.TestCase):
    def test_valid_khasra_numbers(self):
        for value in ("142", "21", "22/1", "1431-1436", "21क"):
            ok, cleaned, err = security.validate_khasra(value)
            self.assertTrue(ok, f"{value} should be valid ({err})")
            self.assertEqual(cleaned, value)

    def test_invalid_khasra_numbers(self):
        for value in ("", "   ", "../../etc/passwd", "'; DROP TABLE farmers; --",
                      "142 OR 1=1", "<script>alert(1)</script>", "1" * 30):
            ok, _cleaned, err = security.validate_khasra(value)
            self.assertFalse(ok, f"{value!r} must be rejected")
            self.assertTrue(err)

    def test_control_characters_stripped(self):
        ok, cleaned, _ = security.validate_khasra("\x00142\x07")
        self.assertTrue(ok)
        self.assertEqual(cleaned, "142")
        # A tab injection does not silently become "14 2"; it is rejected.
        self.assertFalse(security.validate_khasra("14\t2")[0])

    def test_names(self):
        for good in ("Ram Kumar", "राम कुमार", "O'Brien", "A. P. Singh"):
            ok, _clean, err = security.validate_person_name(good)
            self.assertTrue(ok, f"{good} rejected: {err}")
        for bad in ("<script>x</script>", "a", "x" * 61, "Ram<script>"):
            ok, _clean, _err = security.validate_person_name(bad)
            self.assertFalse(ok, f"{bad!r} must be rejected")

    def test_village_and_note_length_caps(self):
        self.assertTrue(security.validate_village("")[0])
        self.assertTrue(security.validate_village("Rampur Katra")[0])
        self.assertFalse(security.validate_village("<img onerror=x>")[0])
        self.assertFalse(security.validate_note("x" * 501)[0])
        self.assertTrue(security.validate_note("x" * 500)[0])

    def test_otp_format(self):
        self.assertTrue(security.validate_otp("123456")[0])
        self.assertFalse(security.validate_otp("12345")[0])
        self.assertFalse(security.validate_otp("12345a")[0])


class TestOutputEncoding(unittest.TestCase):
    def test_html_escape(self):
        payload = '<img src=x onerror="alert(1)">'
        escaped = security.esc(payload)
        self.assertNotIn("<img", escaped)
        self.assertIn("&lt;img", escaped)
        self.assertNotIn('"', escaped)

    def test_csv_injection_neutralised(self):
        for payload in ("=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(A1)"):
            self.assertTrue(security.csv_safe(payload).startswith("'"))
        self.assertEqual(security.csv_safe("normal note"), "normal note")

    def test_safe_slug(self):
        self.assertEqual(security.safe_slug("../../etc/passwd"), "etc_passwd")
        self.assertEqual(security.safe_slug(""), "field")
        self.assertNotIn("/", security.safe_slug("Rampur/Katra"))

    def test_inside_directory(self):
        base = os.path.join(ROOT, "training_uploads")
        self.assertTrue(security.inside_directory(os.path.join(base, "a.jpg"), base))
        self.assertFalse(security.inside_directory(os.path.join(base, "..", "App.py"), base))


class TestPasswordHashing(unittest.TestCase):
    def test_hash_is_salted_and_verifiable(self):
        pwd = "CorrectHorse9!battery"
        h1 = security.hash_password(pwd)
        h2 = security.hash_password(pwd)
        self.assertNotEqual(h1, h2, "each hash must use a new salt")
        self.assertTrue(h1.startswith("pbkdf2_sha256$"))
        self.assertNotIn(pwd, h1)
        self.assertTrue(security.verify_password(pwd, h1))
        self.assertFalse(security.verify_password(pwd + "x", h1))

    def test_tampered_or_missing_hash_is_rejected(self):
        for bad in ("", "plaintext-password", "pbkdf2_sha256$999$aaaa$bbbb",
                    "pbkdf2_sha256$999999999$aaaa$bbbb", "md5$1$a$b"):
            self.assertFalse(security.verify_password("anything", bad))

    def test_password_policy(self):
        self.assertTrue(security.password_problem("short"))
        self.assertTrue(security.password_problem("alllowercaseletters"))
        self.assertEqual(security.password_problem("CorrectHorse9!battery"), "")
        generated = security.generate_password()
        self.assertEqual(security.password_problem(generated), "")


class TestRateLimiter(unittest.TestCase):
    def test_window_limit(self):
        limiter = security.AttemptLimiter()
        results = [limiter.hit("k", 2, 60)[0] for _ in range(3)]
        self.assertEqual(results, [True, True, False])

    def test_lockout_after_failures(self):
        limiter = security.AttemptLimiter()
        self.assertEqual(limiter.fail("k", 5, 900, 900), 0)
        for _ in range(4):
            remaining = limiter.fail("k", 5, 900, 900)
        self.assertGreater(remaining, 0)
        self.assertGreater(limiter.is_locked("k"), 0)
        self.assertFalse(limiter.hit("k", 100, 900)[0])

    def test_clear(self):
        limiter = security.AttemptLimiter()
        for _ in range(5):
            limiter.fail("k", 5, 900, 900)
        limiter.clear("k")
        self.assertEqual(limiter.is_locked("k"), 0)

    def test_memory_is_bounded(self):
        limiter = security.AttemptLimiter(max_keys=10)
        for i in range(100):
            limiter.hit(f"key-{i}", 5, 60)
        self.assertLessEqual(len(limiter._hits), 10)

    def test_lockout_survives_process_restart(self):
        """A persisted lockout must still hold after the server restarts."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            first = security.AttemptLimiter(persist=True, state_file=state)
            for _ in range(5):
                first.fail("chief|rampur", 5, 900, 900)
            self.assertGreater(first.is_locked("chief|rampur"), 0)

            # Brand-new instance == a process that just restarted.
            second = security.AttemptLimiter(persist=True, state_file=state)
            self.assertGreater(second.is_locked("chief|rampur"), 0)
            self.assertFalse(second.hit("chief|rampur", 100, 900)[0])

            # Unlocking is persisted as well.
            second.clear("chief|rampur")
            third = security.AttemptLimiter(persist=True, state_file=state)
            self.assertEqual(third.is_locked("chief|rampur"), 0)

    def test_persistence_is_off_by_default(self):
        """Throw-away limiters never touch the shared state file."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "state.json")
            limiter = security.AttemptLimiter(state_file=probe)
            for _ in range(5):
                limiter.fail("chief|rampur", 5, 900, 900)
            self.assertFalse(os.path.exists(probe))


class TestConfigurableDataDir(unittest.TestCase):
    """SV_DATA_DIR keeps credentials + logs on a persistent volume."""

    def test_logs_dir_follows_data_dir(self):
        self.assertEqual(security._logs_dir_for("/var/data"),
                         os.path.join("/var/data", "logs"))
        app_dir = os.path.dirname(os.path.abspath(security.__file__))
        self.assertEqual(security._logs_dir_for(""), os.path.join(app_dir, "logs"))

    def test_credential_paths_follow_data_dir(self):
        import chief_auth
        cred, initial = chief_auth._resolve_paths(os.path.join("var", "data"))
        self.assertEqual(cred, os.path.join("var", "data", "chief_credentials.json"))
        self.assertEqual(initial,
                         os.path.join("var", "data", ".chief_credentials_initial.txt"))

    def test_credential_paths_default_to_app_folder(self):
        import chief_auth
        cred, initial = chief_auth._resolve_paths("")
        self.assertEqual(cred, os.path.join(chief_auth.APP_DIR, "chief_credentials.json"))
        self.assertEqual(initial,
                         os.path.join(chief_auth.APP_DIR, ".chief_credentials_initial.txt"))

    def test_fresh_volume_adopts_checkout_store_once(self):
        """A new disk inherits the existing hashes instead of reissuing them."""
        import tempfile
        import chief_auth
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "checkout", "chief_credentials.json")
            os.makedirs(os.path.dirname(seed))
            payload = b'{"Rampur": {"id": "chief_rampur", "password_hash": "x"}}'
            with open(seed, "wb") as fh:
                fh.write(payload)
            target = os.path.join(tmp, "volume", "chief_credentials.json")

            adopted = chief_auth._adopt_store(target, seed)
            self.assertIsNotNone(adopted)
            with open(target, "rb") as fh:
                self.assertEqual(fh.read(), payload)

            # Idempotent: never overwrites what the volume already has.
            with open(target, "wb") as fh:
                fh.write(b"volume-owned")
            self.assertIsNone(chief_auth._adopt_store(target, seed))
            with open(target, "rb") as fh:
                self.assertEqual(fh.read(), b"volume-owned")


class TestUploads(unittest.TestCase):
    def _png_bytes(self):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, format="PNG")
        return buf.getvalue()

    def test_real_image_is_reencoded(self):
        data, err = security.validate_image_upload("field.png", self._png_bytes())
        self.assertIsNotNone(data, err)
        self.assertEqual(data[:3], b"\xff\xd8\xff", "stored bytes must be JPEG")

    def test_fake_extension_is_rejected(self):
        data, err = security.validate_image_upload("shell.php", self._png_bytes())
        self.assertIsNone(data)
        self.assertIn("JPG", err)

    def test_html_payload_with_png_extension(self):
        data, err = security.validate_image_upload(
            "x.png", b"<html><script>alert(1)</script></html>"
        )
        self.assertIsNone(data)

    def test_png_magic_with_garbage_body(self):
        data, _err = security.validate_image_upload("x.png", b"\x89PNG\r\n\x1a\n" + b"garbage")
        self.assertIsNone(data)

    def test_oversize_rejected(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"0" * 200
        data, err = security.validate_image_upload("big.png", payload, max_bytes=100)
        self.assertIsNone(data)
        self.assertIn("too large", err)

    def test_empty_rejected(self):
        self.assertIsNone(security.validate_image_upload("x.png", b"")[0])


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = security.AUDIT_DIR
        self._orig_file = security.AUDIT_FILE
        security.AUDIT_DIR = self.tmp
        security.AUDIT_FILE = os.path.join(self.tmp, "audit.log")

    def tearDown(self):
        security.AUDIT_DIR = self._orig_dir
        security.AUDIT_FILE = self._orig_file

    def test_secrets_are_redacted(self):
        security.audit("login_failed", actor="chief_rampur", outcome="denied",
                       detail={"password": "SuperSecret123!", "otp": "123456",
                               "phone": "9876543210", "wrong_password": "x",
                               "reason": "bad_credentials"})
        with open(security.AUDIT_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("SuperSecret123!", raw)
        self.assertNotIn("123456", raw)
        self.assertNotIn("9876543210", raw)
        self.assertIn("bad_credentials", raw)

    def test_log_forging_is_prevented(self):
        security.audit("event", actor="a\n{\"event\": \"fake_success\"}", outcome="info")
        with open(security.AUDIT_FILE, encoding="utf-8") as fh:
            lines = [line for line in fh.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "event")
        self.assertNotIn("\n", record["actor"])


class TestChiefAuth(unittest.TestCase):
    """Exercises the real credential store against a temporary copy."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        import chief_auth
        cls.chief_auth = chief_auth
        cls._orig_cred = chief_auth.CRED_FILE
        cls._orig_initial = chief_auth.INITIAL_PASSWORDS_FILE
        chief_auth.CRED_FILE = os.path.join(cls.tmp, "chief_credentials.json")
        chief_auth.INITIAL_PASSWORDS_FILE = os.path.join(cls.tmp, "initial.txt")
        # Legacy plaintext store, exactly like the shipped demo build.
        with open(chief_auth.CRED_FILE, "w", encoding="utf-8") as fh:
            json.dump({"Rampur": {"id": "chief_rampur", "password": "SV2026_rampur"}}, fh)
        chief_auth._store_cache = {"mtime": None, "data": None}
        security.limiter.reset_all()
        st = sys.modules.get("streamlit")
        if st is not None:
            st.session_state.clear()
        cls.store = chief_auth.ensure_credentials(
            [{"en": "Rampur", "hi": "रामपुर"}]
        )
        cls.initial_text = ""
        if os.path.exists(chief_auth.INITIAL_PASSWORDS_FILE):
            with open(chief_auth.INITIAL_PASSWORDS_FILE, encoding="utf-8") as fh:
                cls.initial_text = fh.read()

    @classmethod
    def tearDownClass(cls):
        cls.chief_auth.CRED_FILE = cls._orig_cred
        cls.chief_auth.INITIAL_PASSWORDS_FILE = cls._orig_initial
        cls.chief_auth._store_cache = {"mtime": None, "data": None}

    def setUp(self):
        security.limiter.reset_all()
        import streamlit as st
        st.session_state.clear()

    def _issued_password(self, area="Rampur"):
        for line in self.initial_text.splitlines():
            if line.startswith(f"{area}:"):
                return line.rsplit("password=", 1)[1].strip()
        self.fail(f"no issued password for {area}")

    def test_plaintext_password_is_gone(self):
        entry = self.store["Rampur"]
        self.assertNotIn("password", entry)
        self.assertTrue(entry["password_hash"].startswith("pbkdf2_sha256$"))
        with open(self.chief_auth.CRED_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("SV2026_rampur", raw)

    def test_old_predictable_password_no_longer_works(self):
        ok, _msg = self.chief_auth.login("Rampur", "chief_rampur", "SV2026_rampur")
        self.assertFalse(ok)

    def test_login_with_issued_password_works(self):
        ok, msg = self.chief_auth.login(
            "Rampur", "chief_rampur", self._issued_password()
        )
        self.assertTrue(ok, msg)
        self.assertTrue(self.chief_auth.is_logged_in())
        self.assertEqual(self.chief_auth.current_chief()["area"], "Rampur")

    def test_wrong_area_cannot_log_in(self):
        ok, _msg = self.chief_auth.login("Zaidpur", "chief_rampur", self._issued_password())
        self.assertFalse(ok)

    def test_lockout_after_repeated_failures(self):
        for _ in range(self.chief_auth.MAX_FAILURES):
            ok, _ = self.chief_auth.login("Rampur", "chief_rampur", "wrong-password")
            self.assertFalse(ok)
        ok, msg = self.chief_auth.login("Rampur", "chief_rampur", self._issued_password())
        self.assertFalse(ok)
        self.assertIn("Too many", msg)

    def test_expired_session_is_rejected(self):
        import streamlit as st
        self.chief_auth.login("Rampur", "chief_rampur", self._issued_password())
        session = st.session_state[self.chief_auth.SESSION_KEY]
        session["issued_at"] = time.time() - (self.chief_auth.SESSION_HOURS + 1) * 3600
        self.assertFalse(self.chief_auth.is_logged_in())

    def test_idle_session_is_rejected(self):
        import streamlit as st
        self.chief_auth.login("Rampur", "chief_rampur", self._issued_password())
        session = st.session_state[self.chief_auth.SESSION_KEY]
        session["last_seen"] = time.time() - (self.chief_auth.IDLE_MINUTES + 1) * 60
        self.assertFalse(self.chief_auth.is_logged_in())

    def test_credential_rotation_invalidates_session(self):
        import streamlit as st
        self.chief_auth.login("Rampur", "chief_rampur", self._issued_password())
        st.session_state[self.chief_auth.SESSION_KEY]["cred_version"] = "stale-version"
        self.assertFalse(self.chief_auth.is_logged_in())

    def test_logout_clears_session(self):
        self.chief_auth.login("Rampur", "chief_rampur", self._issued_password())
        self.chief_auth.logout()
        self.assertFalse(self.chief_auth.is_logged_in())

    def test_no_role_claim_in_session_can_be_forged(self):
        """A chief session for one area can never carry another area's id."""
        self.chief_auth.login("Rampur", "chief_rampur", self._issued_password())
        chief = self.chief_auth.current_chief()
        self.assertEqual(chief["id"], "chief_rampur")
        self.assertEqual(chief["area"], "Rampur")


class TestFarmerAuthValidation(unittest.TestCase):
    def setUp(self):
        import streamlit as st
        st.session_state.clear()
        import farmer_auth
        self.farmer_auth = farmer_auth

    def test_bad_phone_rejected_before_any_send(self):
        ok, msg = self.farmer_auth.start_login("12345", "Ram Kumar", "English")
        self.assertFalse(ok)
        self.assertIn("mobile", msg.lower())

    def test_bad_name_rejected(self):
        ok, msg = self.farmer_auth.start_login("9876543210", "<script>", "English")
        self.assertFalse(ok)
        self.assertIn("Name", msg)

    def test_phone_normalisation(self):
        self.assertEqual(self.farmer_auth.normalize_phone("+91 98765 43210"), "9876543210")
        self.assertEqual(self.farmer_auth.normalize_phone("09876543210"), "9876543210")
        self.assertEqual(self.farmer_auth.normalize_phone("12345"), "")

    def test_expired_farmer_session_is_rejected(self):
        import streamlit as st
        from datetime import datetime, timedelta
        f = self.farmer_auth
        st.session_state[f._k("logged_in")] = True
        st.session_state[f._k("phone")] = "9876543210"
        st.session_state[f._k("login_at")] = datetime.now() - timedelta(hours=f.SESSION_HOURS + 1)
        self.assertFalse(f.is_logged_in())
        self.assertIsNone(f.current_farmer())

    def test_otp_is_not_stored_in_plaintext(self):
        from datetime import datetime, timedelta
        import streamlit as st
        f = self.farmer_auth
        st.session_state[f._k("otp_phone")] = "9876543210"
        st.session_state[f._k("otp_code")] = f._otp_digest("123456")
        st.session_state[f._k("otp_via_verify")] = False
        st.session_state[f._k("otp_expires")] = datetime.now() + timedelta(minutes=5)
        st.session_state[f._k("otp_tries")] = 0
        self.assertNotEqual(st.session_state[f._k("otp_code")], "123456")
        self.assertTrue(f._otp_matches("123456", st.session_state[f._k("otp_code")]))
        self.assertFalse(f._otp_matches("654321", st.session_state[f._k("otp_code")]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
