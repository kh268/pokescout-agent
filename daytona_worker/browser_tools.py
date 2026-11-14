"""Small, single-context Playwright browser adapter for the Daytona worker."""

import os
from pathlib import Path
from urllib.parse import urlparse

class BrowserTools:
    def __init__(self, profile_dir: str | None = None, display: str | None = None):
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        self.context = None
        self.page = None
        profile = Path(profile_dir or os.environ.get("CARD_BROWSER_PROFILE", "~/card-browser-profile")).expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        browser_env = {**os.environ, "DISPLAY": display or os.environ.get("DISPLAY", ":0")}
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,
                env=browser_env,
                viewport={"width": 1280, "height": 720},
                timeout=30000,
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(15000)
        except Exception:
            self.playwright.stop()
            raise

    def open_page(self, url: str) -> dict:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("open_page requires an HTTP or HTTPS URL")
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return self.read_page()

    def smoke_test(self) -> dict:
        self.page.goto("data:text/html,<title>Browser Ready</title><h1>Browser Ready</h1>")
        return self.read_page()

    def read_page(self) -> dict:
        text = self.page.locator("body").inner_text(timeout=15000)[:8000]
        actions = self.page.locator("a, button, input, textarea, select").evaluate_all(
            """elements => elements.slice(0, 40).map(el => ({
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role'),
                name: el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || '',
                href: el.getAttribute('href')
            })).filter(item => item.name || item.href)"""
        )
        return {"url": self.page.url, "title": self.page.title(), "text": text, "actions": actions}

    def click(self, role: str, name: str) -> dict:
        self.page.get_by_role(role, name=name, exact=True).click()
        return self.read_page()

    def fill(self, label: str, value: str) -> dict:
        self.page.get_by_label(label, exact=True).fill(value)
        return self.read_page()

    def screenshot(self) -> dict:
        path = "/tmp/card-browser-screenshot.png"
        self.page.screenshot(path=path, full_page=False)
        return {"url": self.page.url, "path": path}

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        self.playwright.stop()
