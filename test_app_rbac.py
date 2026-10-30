"""End-to-end authorization tests for the Streamlit app (AppTest).

These run App.py headlessly and assert the server-side page guards:
  * a Farmer session cannot reach Chief pages,
  * a Chief page needs a valid Chief session,
  * unknown pages fall back to the welcome page (deny by default),
  * no Chief password is ever rendered in the browser.

Run from the project root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(ROOT, "App.py")


def valid_chief_session(area="Rampur", chief_id="chief_rampur"):
    now = time.time()
    return {
        "area": area,
        "id": chief_id,
        "token": "test-token",
        "issued_at": now,
        "last_seen": now,
        "must_change": False,
    }


def run_app(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["splash_done"] = True
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    return at


class TestPageAuthorization(unittest.TestCase):
    def assert_no_exception(self, at):
        self.assertEqual(
            list(at.exception), [],
            f"app raised: {[e.value for e in at.exception]}",
        )

    def test_farmer_cannot_open_chief_panel(self):
        at = run_app(lang_chosen=True, role="farmer", current_page="chief",
                     language="English")
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "welcome")

    def test_farmer_cannot_open_upload_page(self):
        at = run_app(lang_chosen=True, role="farmer", current_page="upload",
                     language="English")
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "welcome")

    def test_chief_cannot_open_farmer_login(self):
        at = run_app(lang_chosen=True, role="chief", current_page="login",
                     language="English")
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "welcome")

    def test_chief_panel_without_login_redirects_to_chief_login(self):
        at = run_app(lang_chosen=True, role="chief", current_page="chief",
                     language="English")
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "chief_login")

    def test_upload_without_login_redirects_to_chief_login(self):
        at = run_app(lang_chosen=True, role="chief", current_page="upload",
                     language="English")
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "chief_login")

    def test_unknown_page_is_denied(self):
        at = run_app(lang_chosen=True, role="chief", current_page="../../admin",
                     language="English")
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "welcome")

    def test_chief_with_valid_session_can_open_upload(self):
        at = run_app(lang_chosen=True, role="chief", current_page="upload",
                     language="English", sv_chief_session=valid_chief_session())
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "upload")

    def test_chief_with_valid_session_can_open_panel(self):
        at = run_app(lang_chosen=True, role="chief", current_page="chief",
                     language="English", sv_chief_session=valid_chief_session())
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "chief")

    def test_expired_chief_session_is_rejected_by_the_page_guard(self):
        session = valid_chief_session()
        session["issued_at"] = time.time() - 20 * 3600
        at = run_app(lang_chosen=True, role="chief", current_page="chief",
                     language="English", sv_chief_session=session)
        self.assert_no_exception(at)
        self.assertEqual(at.session_state["current_page"], "chief_login")

    def test_forecast_flow_still_works_for_logged_in_chief(self):
        at = run_app(lang_chosen=True, role="chief", current_page="forecast",
                     language="English", sv_chief_session=valid_chief_session())
        self.assert_no_exception(at)
        self.assertTrue(at.text_input, "khasra input missing")
        at.text_input[0].set_value("21")
        clicked = False
        for button in at.button:
            if "Tell Weather Forecast" in str(button.label):
                button.click()
                clicked = True
                break
        self.assertTrue(clicked, "forecast button missing")
        at.run()
        self.assert_no_exception(at)
        metrics = [str(getattr(m, "label", "")) for m in at.metric]
        self.assertTrue(
            any("Rainfall" in label for label in metrics),
            f"forecast result did not render (metrics={metrics})",
        )

    def test_malicious_khasra_is_rejected_in_the_ui(self):
        at = run_app(lang_chosen=True, role="chief", current_page="forecast",
                     language="English", sv_chief_session=valid_chief_session())
        at.text_input[0].set_value('<img src=x onerror="alert(1)">')
        for button in at.button:
            if "Tell Weather Forecast" in str(button.label):
                button.click()
                break
        at.run()
        self.assert_no_exception(at)
        warnings = " ".join(str(w.value) for w in at.warning)
        self.assertIn("Khasra", warnings)
        self.assertNotIn("onerror", " ".join(str(m.value) for m in at.markdown))

    def test_credentials_are_never_rendered(self):
        at = run_app(lang_chosen=True, role="chief", current_page="chief",
                     language="English", sv_chief_session=valid_chief_session())
        texts = []
        for attr in ("markdown", "caption", "success", "info", "warning", "text"):
            try:
                for element in getattr(at, attr):
                    texts.append(str(getattr(element, "value", "")))
            except Exception:
                continue
        blob = " ".join(texts)
        self.assertNotIn("SV2026_", blob)
        self.assertNotIn("password=", blob)
        self.assertNotIn("password_hash", blob)
        self.assertNotIn("<script>", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
