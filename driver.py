"""Driver abstraction for the Shadowcode browser tool.

Original code for this task. Two drivers share one small interface
so the session, preview server and MCP server never care which one
is underneath:

* ``FakeDriver`` -- an in-memory page used by the unit tests and by
  anyone who wants to exercise the tool without a real browser.
  It never touches the network.
* ``PlaywrightDriver`` -- a thin wrapper around a real Playwright
  page. Playwright itself is imported lazily, only when a real
  driver is built, so importing this module (and running the tests)
  never requires Playwright to be installed.

Design note, reusing the existing Shadowcode browser's approach
(``shadowcode_pkg/browser.py``): a real Playwright page must only
ever be touched from one thread. ``BrowserSession`` (session.py)
therefore runs the real driver on a dedicated worker thread and
routes every driver call through it. This module does not start
any thread itself; it only defines the driver objects.

Safeguards kept from the existing browser: no password/payment
field is ever filled by an agent call, no CAPTCHA is solved, and
no login wall is bypassed. Those checks live in the session, not
in the driver, so they apply to every driver.
"""
from __future__ import annotations

import base64
import os
from urllib.parse import urlparse

# A 1x1 transparent PNG, used by FakeDriver screenshots.
_FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720


class FakeDriver:
    """In-memory page for tests and dry runs. No network, no disk."""

    def __init__(self):
        self.url = ""
        self.title = ""
        self.text = ""
        self.calls = []          # [(method, args...)] in call order
        self.reload_count = 0    # hand-back must never increase this
        self._history = []
        self._history_index = -1

    # -- helpers ---------------------------------------------------
    def _title_for(self, url):
        path = urlparse(url).path or "/"
        if "example.com" in url:
            return "Example Domain"
        if path in ("", "/"):
            return "Home"
        return "Page " + path.strip("/").replace("/", " / ") or "Home"

    def _land(self, url):
        if self.url and self._history_index < len(self._history) - 1:
            self._history = self._history[: self._history_index + 1]
        self._history.append(url)
        self._history_index = len(self._history) - 1
        self.url = url
        self.title = self._title_for(url)
        self.text = "Content of %s" % url

    # -- driver interface ------------------------------------------
    def goto(self, url):
        self.calls.append(("goto", url))
        self._land(url)
        return self.url

    def get_url(self):
        return self.url

    def get_title(self):
        return self.title

    def get_text(self):
        return self.text

    def screenshot(self):
        self.calls.append(("screenshot",))
        return _FAKE_PNG

    def click_text(self, text):
        self.calls.append(("click_text", text))
        # A fake click on a link-like text navigates to a child page,
        # mirroring a real link click without any network access.
        slug = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")
        base = self.url.rstrip("/") or "about:blank"
        self._land(base + "/" + (slug or "clicked"))
        return self.url

    def click_selector(self, selector):
        self.calls.append(("click_selector", selector))
        return self.click_text(selector)

    def click_xy(self, x, y):
        self.calls.append(("click_xy", x, y))
        return self.url

    def type_text(self, field, value):
        self.calls.append(("type_text", field, value))
        return "typed"

    def scroll(self, direction="down", amount=600, selector=""):
        self.calls.append(("scroll", direction, amount, selector))
        return "scrolled"

    def go_back(self):
        self.calls.append(("go_back",))
        if self._history_index > 0:
            self._history_index -= 1
            self.url = self._history[self._history_index]
            self.title = self._title_for(self.url)
        return self.url

    def go_forward(self):
        self.calls.append(("go_forward",))
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.url = self._history[self._history_index]
            self.title = self._title_for(self.url)
        return self.url

    def close(self):
        self.calls.append(("close",))
        return None


class PlaywrightDriver:
    """Thin wrapper around a real Playwright page.

    Created only inside the session's driver thread (see session.py).
    ``page`` is a Playwright ``Page`` object; this class only calls
    public Playwright APIs on it.
    """

    def __init__(self, page):
        self._page = page

    def goto(self, url):
        self._page.goto(url, timeout=30000)
        return self._page.url

    def get_url(self):
        return self._page.url

    def get_title(self):
        return self._page.title()

    def get_text(self):
        try:
            return self._page.inner_text("body")[:4000]
        except Exception:
            return ""

    def screenshot(self):
        return self._page.screenshot()

    def click_text(self, text):
        self._page.get_by_text(text, exact=False).first.click(timeout=10000)
        return self._page.url

    def click_selector(self, selector):
        if selector.startswith("text="):
            self._page.get_by_text(selector[5:], exact=False).first.click(timeout=10000)
        else:
            self._page.locator(selector).first.click(timeout=10000)
        return self._page.url

    def click_xy(self, x, y):
        self._page.mouse.click(float(x), float(y))
        return self._page.url

    def type_text(self, field, value):
        # field is a label or selector; try label first, then locator.
        try:
            self._page.get_by_label(field, exact=False).first.fill(value)
        except Exception:
            self._page.locator(field).first.fill(value)
        return "typed"

    def scroll(self, direction="down", amount=600, selector=""):
        if selector:
            if selector.startswith("text="):
                self._page.get_by_text(selector[5:], exact=False).first.scroll_into_view_if_needed(timeout=10000)
            else:
                self._page.locator(selector).first.scroll_into_view_if_needed(timeout=10000)
            return "scrolled"
        direction = str(direction).lower()
        if direction == "top":
            self._page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "up":
            self._page.mouse.wheel(0, -abs(int(amount)))
        else:
            self._page.mouse.wheel(0, abs(int(amount)))
        return "scrolled"

    def go_back(self):
        self._page.go_back()
        return self._page.url

    def go_forward(self):
        self._page.go_forward()
        return self._page.url

    def close(self):
        return None


def start_playwright_driver():
    """Build a real Playwright driver. Call only from the driver thread.

    Returns (driver, cleanup_callable). Raises RuntimeError with an
    actionable message when Playwright is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - needs real Playwright
        raise RuntimeError(
            "Playwright not available: %s. "
            "Install: pip install playwright && playwright install firefox" % exc
        )
    pw = sync_playwright().start()
    engine = os.environ.get("SHADOWCODE_BROWSER_ENGINE", "firefox")
    launcher = getattr(pw, engine)
    browser = launcher.launch()
    context = browser.new_context(
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        ignore_https_errors=True,
    )
    page = context.new_page()
    driver = PlaywrightDriver(page)

    def cleanup():
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    return driver, cleanup
