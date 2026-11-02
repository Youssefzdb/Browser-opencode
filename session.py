"""BrowserSession: driver ownership, takeover queue, watch-and-record.

Original code for this task. This is the heart of the browser tool
and the part that makes it Muse-parity:

* One driver thread owns the real browser page (see driver.py).
  In synchronous mode (tests, FakeDriver) calls run directly, so
  tests stay deterministic and never need a thread.
* Agent actions while the user is driving do NOT execute. They are
  queued, in order, and only run when the user hands back -- this
  is the one deliberate difference from the older Shadowcode
  browser, which answered agent calls with a "paused" message and
  dropped them. Queuing means an agent task started before a
  takeover is not lost, it is deferred.
* Every action -- agent or user -- is recorded in a JSONL watch
  log, with timestamp, action type, selector/text where safe,
  URL before and after, driver, and driver-transition flag.
  Hand-back reads that log back as context, so the agent knows
  exactly what the user did, without any reload.
* Secrets are never logged. A typed value whose field looks like
  a password, code, token, card or similar field is stored as
  "[REDACTED]" and, for agent calls, the fill is refused outright
  (same safeguard as the older browser).

Hand-back never reloads: it only flips the driver back to
"agent", drains the queued agent actions in order against the
current page, and returns a summary built from the watch log
entries recorded since takeover.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REDACTED = "[REDACTED]"

# Substrings that mark a field as secret-looking. Checked against
# the field name / selector / label, lower-cased.
SECRET_HINTS = (
    "password", "passwd", "pwd", "passcode", "otp", "verification",
    "auth", "token", "secret", "pin", "card", "cvv", "cvc", "ssn",
    "credit", "payment", "code",
)

# "code" is deliberately broad: a verification code, a recovery
# code and a one-time code must never appear in a log. The cost is
# that a field literally named "code" (e.g. a promo code) is also
# redacted -- that is the safe side to err on.


def is_secret_field(field):
    """True when a field name/selector/label looks secret-like."""
    text = str(field or "").lower()
    return any(hint in text for hint in SECRET_HINTS)


def redact_text(field, value):
    """Value to store in a log entry: redacted when secret-looking."""
    if is_secret_field(field):
        return REDACTED
    text = str(value or "")
    return text[:200]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class BrowserSession:
    """A browser session: one driver, one driver mode, one watch log.

    Parameters:
      workdir: workspace directory; the JSONL watch log lives under
        <workdir>/.shadowcode/browser-tool-watch.jsonl, nowhere else.
      driver_factory: callable returning (driver, cleanup) -- used
        in worker-thread mode. Ignored when ``driver`` is given.
      driver: a driver object (FakeDriver in tests). When given,
        the session runs in synchronous mode unless ``use_thread``
        is explicitly True.
      use_thread: force worker-thread mode even with a given driver
        (only useful for thread-safety tests of the queue).
    """

    def __init__(self, workdir, driver_factory=None, driver=None, use_thread=False):
        self.workdir = Path(workdir)
        self.watch_path = self.workdir / ".shadowcode" / "browser-tool-watch.jsonl"
        self.driver_mode = "agent"  # "agent" | "user"
        self.url = ""
        self.title = ""
        self._shot = None
        self.watch_log = []          # in-memory entries, capped
        self._queued_agent = []      # [(method, args)] while user drives
        self._queued_results = []    # results of drained queue, last handback
        self._takeover_index = None  # watch_log index at takeover
        self._last_handback = None   # summary dict from the last handback
        self._lock = threading.Lock()
        self._driver_factory = driver_factory
        self._driver = driver
        self._cleanup = None
        self._use_thread = bool(use_thread or driver_factory is not None)
        self._job_q = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._started = False
        self.preview_port = None
        self.preview_url = ""

    # -- lifecycle ---------------------------------------------------
    def start(self):
        if self._started:
            return True
        if self._use_thread:
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()
            # Wait until the worker has built its driver (or failed).
            deadline = time.time() + 30
            while self._driver is None and not self._stop.is_set() and time.time() < deadline:
                time.sleep(0.05)
            if self._driver is None:
                raise RuntimeError("browser driver thread did not start")
        elif self._driver is None and self._driver_factory is not None:
            self._driver, self._cleanup = self._driver_factory()
        self._started = True
        return True

    def close(self):
        if self._use_thread and self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=5)
            self._thread = None
        if self._cleanup is not None:
            try:
                self._cleanup()
            except Exception:
                pass
            self._cleanup = None
        self._started = False

    def _worker_loop(self):
        # This thread, and only this thread, builds and touches the
        # real Playwright driver. FakeDriver objects are also routed
        # here in thread mode, which keeps semantics identical.
        try:
            if self._driver is None and self._driver_factory is not None:
                self._driver, self._cleanup = self._driver_factory()
            elif self._driver is None:
                from .driver import FakeDriver
                self._driver = FakeDriver()
        except Exception:
            self._stop.set()
            return
        while not self._stop.is_set():
            try:
                fn, result_q = self._job_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result_q.put(("ok", fn(self._driver)))
            except Exception as exc:  # never kill the worker on one bad call
                result_q.put(("error", str(exc)))

    def _run(self, fn):
        """Run fn(driver) on the owning thread (or directly in sync mode)."""
        self.start()
        if not self._use_thread:
            return fn(self._driver)
        result_q = queue.Queue()
        self._job_q.put((fn, result_q))
        try:
            status, value = result_q.get(timeout=40)
        except queue.Empty:
            raise RuntimeError("browser command timed out")
        if status == "error":
            raise RuntimeError(value)
        return value

    # -- watch log ---------------------------------------------------
    def _record(self, action, url_before, url_after, selector="", text="",
                driver_transition=False, via=""):
        entry = {
            "ts": _now_iso(),
            "action": action,
            "selector": selector or "",
            "text": text or "",
            "url_before": url_before or "",
            "url_after": url_after or "",
            "driver": self.driver_mode,
            "driver_transition": bool(driver_transition),
            "via": via or "",
        }
        with self._lock:
            self.watch_log.append(entry)
            self.watch_log = self.watch_log[-1000:]
        try:
            self.watch_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.watch_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # a log-write failure must never break browsing
        return entry

    def _refresh_state(self):
        try:
            self.url = self._run(lambda d: d.get_url()) or self.url
            self.title = self._run(lambda d: d.get_title()) or self.title
        except Exception:
            pass

    # -- state / watch ------------------------------------------------
    def state(self):
        """Current state, as the agent sees it. No reload, ever."""
        self._refresh_state()
        with self._lock:
            watch_tail = list(self.watch_log[-10:])
            queued = len(self._queued_agent)
        return {
            "url": self.url,
            "title": self.title,
            "driver": self.driver_mode,
            "has_screenshot": self._shot is not None,
            "watch": watch_tail,
            "watch_count": len(self.watch_log),
            "watch_file": str(self.watch_path),
            "queued_agent_actions": queued,
            "preview_url": self.preview_url,
        }

    def watch(self, limit=None):
        """Full (or tail) watch log, plus current page. No reload."""
        self._refresh_state()
        with self._lock:
            entries = list(self.watch_log)
        if limit:
            entries = entries[-int(limit):]
        return {
            "url": self.url,
            "title": self.title,
            "driver": self.driver_mode,
            "entries": entries,
            "count": len(self.watch_log),
        }

    def watch_text(self, limit=None):
        data = self.watch(limit=limit)
        lines = [
            "current: %s -- %s (driver: %s)" % (data["title"], data["url"], data["driver"]),
            "watch: %d entries" % data["count"],
        ]
        for e in data["entries"]:
            trans = " [driver transition]" if e.get("driver_transition") else ""
            sel = (" selector=%r" % e["selector"]) if e.get("selector") else ""
            txt = (" text=%r" % e["text"]) if e.get("text") else ""
            lines.append(
                "  %s %s %s -> %s (%s)%s%s%s" % (
                    e["ts"], e["action"], e["url_before"], e["url_after"],
                    e["driver"], trans, sel, txt)
            )
        return "\n".join(lines)

    # -- agent actions (queued while the user drives) -----------------
    def _agent_or_queue(self, method, args, fn):
        """Run now if the agent drives, else queue for handback."""
        if self.driver_mode == "user":
            with self._lock:
                self._queued_agent.append((method, args))
            return {
                "queued": True,
                "message": (
                    "browser is user-driven right now; action queued "
                    "and will run on hand-back, in order"),
                "method": method,
                "queued_count": len(self._queued_agent),
            }
        return fn()

    def open(self, url):
        def run():
            before = self.url
            self._run(lambda d: d.goto(url))
            self._refresh_state()
            self._record("open", before, self.url, via="agent open")
            return "opened %s title=%r" % (self.url, self.title)
        return self._agent_or_queue("browser_open", {"url": url}, run)

    def read(self):
        # Reads are safe while the user drives: they never change the page.
        self._refresh_state()
        try:
            text = self._run(lambda d: d.get_text()) or ""
        except Exception:
            text = ""
        return "title: %s\nurl: %s\n%s" % (self.title, self.url, text[:4000])

    def click(self, text="", selector=""):
        def run():
            before = self.url
            if selector:
                self._run(lambda d: d.click_selector(selector))
                via = "agent click (%s)" % selector
                sel = selector
            else:
                self._run(lambda d: d.click_text(text))
                via = "agent click (%s)" % text
                sel = ""
            self._refresh_state()
            self._record("click", before, self.url, selector=sel,
                         text=text if not selector else "", via=via)
            return "clicked, now %s title=%r" % (self.url, self.title)
        return self._agent_or_queue("browser_click", {"text": text, "selector": selector}, run)

    def type_text(self, field, value):
        if is_secret_field(field):
            return "refused: password/payment/code fields are not filled by the agent"
        def run():
            before = self.url
            self._run(lambda d: d.type_text(field, value))
            self._refresh_state()
            self._record("type", before, self.url, selector=field,
                         text=redact_text(field, value), via="agent type")
            return "typed into %r" % field
        return self._agent_or_queue("browser_type", {"field": field, "value": value}, run)

    def scroll(self, direction="down", amount=600, selector=""):
        def run():
            before = self.url
            self._run(lambda d: d.scroll(direction=direction, amount=int(amount), selector=selector))
            self._refresh_state()
            self._record("scroll", before, self.url, selector=selector,
                         via="agent scroll %s" % (selector or direction))
            return "scrolled, now %s" % self.url
        return self._agent_or_queue("browser_scroll", {"direction": direction, "amount": amount, "selector": selector}, run)

    def navigate(self, direction="back"):
        def run():
            before = self.url
            if direction == "back":
                self._run(lambda d: d.go_back())
            else:
                self._run(lambda d: d.go_forward())
            self._refresh_state()
            self._record("navigate", before, self.url, via="agent navigate %s" % direction)
            return "now %s" % self.url
        return self._agent_or_queue("browser_navigate", {"direction": direction}, run)

    def screenshot(self, name=""):
        # Screenshot is a read: it runs even while the user drives,
        # so the preview stays live during takeover.
        try:
            shot = self._run(lambda d: d.screenshot())
        except Exception as exc:
            return "tool error: %s" % exc
        self._shot = shot
        if name:
            out = self.workdir / "screenshots" / name
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(shot)
                return "screenshot saved: %s (%d bytes)" % (out, len(shot))
            except Exception as exc:
                return "screenshot captured (%d bytes) but not saved: %s" % (len(shot), exc)
        return "screenshot captured (%d bytes); preview: %s/shot.png" % (
            len(shot), self.preview_url)

    def screenshot_bytes(self):
        try:
            shot = self._run(lambda d: d.screenshot())
        except Exception:
            shot = None
        if shot:
            self._shot = shot
        return self._shot

    # -- user actions (from the preview page) -------------------------
    def user_action(self, action, **kwargs):
        """Execute a user-driven action immediately and record it.

        This is the preview's control path: the user is driving, so
        these actions are never queued. Secrets are redacted in the
        log even though the user typed them themselves -- the log
        is read back by the agent later, and must not carry them.
        """
        before = self.url
        if action == "click_xy":
            self._run(lambda d: d.click_xy(kwargs.get("x", 0), kwargs.get("y", 0)))
            via, sel, txt = "preview click (%s,%s)" % (kwargs.get("x"), kwargs.get("y")), "", ""
        elif action == "click_text":
            text = str(kwargs.get("text", ""))
            self._run(lambda d: d.click_text(text))
            via, sel, txt = "preview click (%s)" % text, "", text
        elif action == "click_selector":
            sel = str(kwargs.get("selector", ""))
            self._run(lambda d: d.click_selector(sel))
            via, txt = "preview click (%s)" % sel, ""
        elif action == "type":
            field = str(kwargs.get("field", ""))
            value = str(kwargs.get("text", ""))
            self._run(lambda d: d.type_text(field, value))
            via, sel, txt = "preview type", field, redact_text(field, value)
        elif action == "key":
            # Keys are sent as a type of an empty field with the key name.
            self._run(lambda d: d.type_text("", kwargs.get("key", "Enter")))
            via, sel, txt = "preview key", "", str(kwargs.get("key", ""))
        elif action == "goto":
            url = str(kwargs.get("url", ""))
            self._run(lambda d: d.goto(url))
            via, sel, txt = "preview goto", "", url
        elif action in ("back", "forward"):
            if action == "back":
                self._run(lambda d: d.go_back())
            else:
                self._run(lambda d: d.go_forward())
            via, sel, txt = "preview %s" % action, "", ""
        elif action == "scroll":
            direction = str(kwargs.get("direction", "down"))
            amount = int(kwargs.get("amount", 600))
            self._run(lambda d: d.scroll(direction=direction, amount=amount))
            via, sel, txt = "preview scroll %s" % direction, "", ""
        elif action == "scroll_to":
            sel = str(kwargs.get("selector", ""))
            self._run(lambda d: d.scroll(selector=sel))
            via, txt = "preview scroll to %s" % sel, ""
        else:
            return "unknown user action: %s" % action
        self._refresh_state()
        self._record(action, before, self.url, selector=sel, text=txt, via=via)
        return "user action %s done, now %s" % (action, self.url)

    # -- takeover / hand-back -----------------------------------------
    def takeover(self):
        if self.driver_mode == "user":
            return "already user-driven"
        before = self.url
        self.driver_mode = "user"
        with self._lock:
            self._takeover_index = len(self.watch_log)
        self._record("takeover", before, self.url, driver_transition=True,
                     via="takeover")
        return "user driving now; agent actions will queue until hand-back"

    def handback(self):
        """Resume from the exact current page. No reload, no reset.

        Drains queued agent actions in order, then returns a summary
        built from the watch entries recorded since takeover, so the
        agent (or an MCP client) can continue the original task
        knowing what the user did.
        """
        if self.driver_mode != "user":
            # Hand-back with nothing to hand back is still useful:
            # it reports the current page, without reloading it.
            self._refresh_state()
            return {
                "resumed": True,
                "url": self.url,
                "title": self.title,
                "driver": "agent",
                "user_actions": [],
                "queued_executed": [],
                "note": "was not user-driven; resumed from current page, no reload",
            }
        before = self.url
        self.driver_mode = "agent"
        self._record("handback", before, self.url, driver_transition=True,
                     via="handback")
        with self._lock:
            start = self._takeover_index if self._takeover_index is not None else 0
            user_actions = list(self.watch_log[start:])
            queued = list(self._queued_agent)
            self._queued_agent = []
        # Drain the queue in order, against the CURRENT page.
        results = []
        for method, args in queued:
            try:
                if method == "browser_open":
                    out = self.open(args.get("url", ""))
                elif method == "browser_click":
                    out = self.click(text=args.get("text", ""), selector=args.get("selector", ""))
                elif method == "browser_type":
                    out = self.type_text(args.get("field", ""), args.get("value", ""))
                elif method == "browser_scroll":
                    out = self.scroll(direction=args.get("direction", "down"),
                                      amount=args.get("amount", 600),
                                      selector=args.get("selector", ""))
                elif method == "browser_navigate":
                    out = self.navigate(args.get("direction", "back"))
                else:
                    out = "unknown queued method: %s (skipped)" % method
            except Exception as exc:
                out = "queued %s failed: %s" % (method, exc)
            results.append({"method": method, "result": out})
        self._refresh_state()
        summary = {
            "resumed": True,
            "url": self.url,
            "title": self.title,
            "driver": "agent",
            "user_actions": user_actions,
            "queued_executed": results,
            "note": "resumed from current page exactly, no reload",
        }
        self._last_handback = summary
        return summary

    def handback_summary_text(self):
        summary = self._last_handback or self.handback()
        lines = [
            "resumed from current page (no reload): %s -- %s" % (summary["title"], summary["url"]),
            "user actions since takeover: %d" % len(summary["user_actions"]),
        ]
        for e in summary["user_actions"]:
            lines.append("  %s %s %s -> %s" % (e["ts"], e["action"], e["url_before"], e["url_after"]))
        lines.append("queued agent actions executed: %d" % len(summary["queued_executed"]))
        for r in summary["queued_executed"]:
            lines.append("  %s -> %s" % (r["method"], r["result"]))
        return "\n".join(lines)
