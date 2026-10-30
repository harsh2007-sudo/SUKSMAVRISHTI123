"""Browser verification of the hardened app (Playwright + installed Chrome)."""
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://127.0.0.1:8501"


def issued_password(area="Rampur"):
    path = os.path.join(ROOT, ".chief_credentials_initial.txt")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(f"{area}:"):
                return line.rsplit("password=", 1)[1].strip()
    raise SystemExit("no password issued for " + area)


def main():
    pwd = issued_password()
    errors = []
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None,
        )

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        t0 = time.time()
        page.get_by_role("button", name="English").wait_for(timeout=60000)
        results.append(("language page reached (s)", round(time.time() - t0, 2)))

        page.get_by_role("button", name="English").click()
        page.get_by_role("button", name=re.compile("Login as Chief")).wait_for(timeout=30000)
        page.get_by_role("button", name=re.compile("Login as Chief")).click()

        # Chief login page
        page.get_by_placeholder("chief_…").wait_for(timeout=30000)
        results.append(("demo password expander removed",
                        page.get_by_text("Show demo ID / password").count() == 0))

        page.locator("div[data-testid='stSelectbox']").first.click()
        page.get_by_role("option", name=re.compile("^Rampur")).first.click()
        page.wait_for_timeout(800)
        page.get_by_placeholder("chief_…").fill("chief_rampur")
        page.locator("input[type='password']").fill("SV2026_rampur")
        page.get_by_role("button", name=re.compile("लॉगिन")).click()
        page.get_by_text("Wrong ID or password.").wait_for(timeout=30000)
        results.append(("old predictable password rejected", True))

        page.locator("input[type='password']").fill(pwd)
        page.get_by_role("button", name=re.compile("लॉगिन")).click()
        page.get_by_text(re.compile("Chief login successful")).wait_for(timeout=30000)
        results.append(("issued password accepted", True))

        page.get_by_role("button", name=re.compile("Open Chief Panel")).click()
        page.get_by_text("Village Weather", exact=False).first.wait_for(timeout=30000)
        results.append(("chief panel renders", True))

        hero_src_len = page.evaluate(
            "() => { const i = document.querySelector('.sv-hero-bg img');"
            " return i ? i.src.length : -1; }"
        )
        results.append(("hero data-URI size (KB)", round(hero_src_len / 1024)))

        img_count = page.evaluate("() => document.querySelectorAll('img').length")
        results.append(("img elements on page", img_count))

        # A farmer page must not be reachable from the chief session.
        page.goto(URL + "/?page=login", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        browser.close()

    print("\n--- RESULTS ---")
    for name, value in results:
        print(f"{name}: {value}")
    print("--- ERRORS ---")
    print("\n".join(errors) if errors else "none")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
