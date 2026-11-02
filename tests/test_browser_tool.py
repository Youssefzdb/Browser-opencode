#!/usr/bin/env python3
"""Tests for the Shadowcode browser tool (browser_tool/).

All tests use FakeDriver: no network, no real browser, no Playwright.
Run:  python3 browser_tool/tests/test_browser_tool.py
   or python3 -m unittest discover -s browser_tool/tests -v
"""
import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from browser_tool.driver import FakeDriver
from browser_tool.mcp_server import MCPBrowserServer
from browser_tool.preview import start_preview
from browser_tool.session import BrowserSession, is_secret_field, redact_text


def make_session(tmp):
    return BrowserSession(tmp, driver=FakeDriver())


class TestRedaction(unittest.TestCase):
    def test_secret_hints(self):
        for field in ("password", "passwd", "user_password", "verification_code",
                      "otp", "auth_token", "credit_card", "cvv", "ssn"):
            self.assertTrue(is_secret_field(field), field)
        for field in ("username", "email", "search", "title"):
            self.assertFalse(is_secret_field(field), field)

    def test_redact_keeps_safe_text_and_redacts_secret(self):
        self.assertEqual(redact_text("search", "hello world"), "hello world")
        self.assertEqual(redact_text("password", "hunter2"), "[REDACTED]")
        self.assertEqual(redact_text("verification_code", "123456"), "[REDACTED]")

    def test_agent_type_refuses_secret_field_and_logs_nothing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            out = s.type_text("password", "hunter2")
            self.assertIn("refused", out)
            blob = Path(tmp, ".shadowcode", "browser-tool-watch.jsonl").read_text()
            self.assertNotIn("hunter2", blob)

    def test_user_type_is_logged_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            s.takeover()
            s.user_action("type", field="verification_code", text="987654")
            blob = Path(tmp, ".shadowcode", "browser-tool-watch.jsonl").read_text()
            self.assertNotIn("987654", blob)
            self.assertIn("[REDACTED]", blob)


class TestTakeoverQueueAndHandback(unittest.TestCase):
    def test_agent_actions_queue_while_user_drives_and_drain_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            s.takeover()
            self.assertEqual(s.driver_mode, "user")
            queued = s.open("https://example.com/page2")
            self.assertTrue(queued["queued"])
            queued2 = s.click(text="Docs")
            self.assertTrue(queued2["queued"])
            # Queued actions must NOT have executed yet.
            self.assertEqual(s.url, "https://example.com")
            summary = s.handback()
            self.assertEqual(summary["driver"], "agent")
            self.assertEqual(len(summary["queued_executed"]), 2)
            self.assertEqual(summary["queued_executed"][0]["method"], "browser_open")
            self.assertEqual(summary["queued_executed"][1]["method"], "browser_click")
            # Hand-back resumes from the current page and then runs
            # the queue: the click lands on a child of page2.
            self.assertIn("page2", summary["url"])

    def test_handback_without_navigation_resumes_same_page_no_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            driver = s._driver
            s.takeover()
            s.user_action("scroll", direction="down", amount=600)
            summary = s.handback()
            self.assertEqual(summary["url"], "https://example.com")
            self.assertEqual(driver.reload_count, 0)
            self.assertIn("no reload", summary["note"])

    def test_reads_and_screenshots_still_work_while_user_drives(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            s.takeover()
            self.assertIn("example.com", s.read())
            self.assertIn("bytes", s.screenshot())


class TestWatchLog(unittest.TestCase):
    def test_watch_log_format_and_jsonl_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            s.takeover()
            s.user_action("goto", url="https://example.com/about")
            s.user_action("scroll", direction="down")
            summary = s.handback()
            entries = summary["user_actions"]
            self.assertTrue(any(e["action"] == "takeover" and e["driver_transition"] for e in entries))
            self.assertTrue(any(e["action"] == "goto" and e["url_after"] == "https://example.com/about" for e in entries))
            self.assertTrue(any(e["action"] == "handback" and e["driver_transition"] for e in entries))
            for e in entries:
                for key in ("ts", "action", "url_before", "url_after", "driver", "driver_transition"):
                    self.assertIn(key, e)
            lines = Path(tmp, ".shadowcode", "browser-tool-watch.jsonl").read_text().strip().splitlines()
            self.assertEqual(len(lines), len(s.watch_log))
            for line in lines:
                json.loads(line)  # every line is valid JSON

    def test_watch_text_contains_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            s.takeover()
            s.handback()
            self.assertIn("driver transition", s.watch_text())


class TestPreviewServer(unittest.TestCase):
    def test_preview_state_shot_watch_takeover_handback(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            server, url = start_preview(s, start_port=8791)
            try:
                state = json.loads(urllib.request.urlopen(url + "/state.json", timeout=5).read())
                self.assertEqual(state["url"], "https://example.com")
                self.assertEqual(state["driver"], "agent")
                shot = urllib.request.urlopen(url + "/shot.png", timeout=5).read()
                self.assertTrue(shot.startswith(b"\x89PNG"))
                # Takeover via HTTP, then a user control, then hand-back.
                req = urllib.request.Request(url + "/takeover", data=b"{}", method="POST")
                self.assertIn("user driving", urllib.request.urlopen(req, timeout=5).read().decode())
                req = urllib.request.Request(
                    url + "/control", data=json.dumps({"action": "goto", "url": "https://example.com/help"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
                state = json.loads(urllib.request.urlopen(url + "/state.json", timeout=5).read())
                self.assertEqual(state["driver"], "user")
                self.assertEqual(state["url"], "https://example.com/help")
                req = urllib.request.Request(url + "/handback", data=b"{}", method="POST")
                body = urllib.request.urlopen(req, timeout=5).read().decode()
                self.assertIn("no reload", body)
                state = json.loads(urllib.request.urlopen(url + "/state.json", timeout=5).read())
                self.assertEqual(state["driver"], "agent")
                self.assertEqual(state["url"], "https://example.com/help")
            finally:
                server.shutdown(); server.server_close(); server.server_close()

    def test_preview_binds_localhost_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            server, url = start_preview(s, start_port=8793)
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
            finally:
                server.shutdown(); server.server_close()


class TestMCPServer(unittest.TestCase):
    def test_tools_list_includes_required_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = MCPBrowserServer(tmp, fake=True)
            resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            names = [t["name"] for t in resp["result"]["tools"]]
            for required in ("browser_open", "browser_read", "browser_click", "browser_type",
                             "browser_scroll", "browser_navigate", "browser_screenshot",
                             "browser_takeover", "browser_handback", "browser_watch"):
                self.assertIn(required, names)

    def test_mcp_open_takeover_queue_handback_watch_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = MCPBrowserServer(tmp, fake=True)
            srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}}})
            def call(name, args=None):
                resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": name, "arguments": args or {}}})
                self.assertFalse(resp["result"]["isError"], resp)
                return resp["result"]["content"][0]["text"]
            out = call("browser_open", {"url": "https://example.com"})
            self.assertIn("example.com", out)
            self.assertIn("preview:", out)
            call("browser_takeover")
            queued = call("browser_click", {"text": "Docs"})
            self.assertIn("queued", queued)
            handback = call("browser_handback")
            self.assertIn("no reload", handback)
            self.assertIn("queued agent actions executed: 1", handback)
            watch = call("browser_watch")
            self.assertIn("example.com", watch)
            srv.close()

    def test_mcp_unknown_tool_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = MCPBrowserServer(tmp, fake=True)
            resp = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                               "params": {"name": "browser_nope", "arguments": {}}})
            self.assertTrue(resp["result"]["isError"])


class TestAgentControl(unittest.TestCase):
    def test_list_elements_and_read_includes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            elements = s.list_elements()
            self.assertTrue(elements)
            self.assertIn("selector", elements[0])
            self.assertIn("clickable elements", s.read())

    def test_agent_click_xy_and_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            s.open("https://example.com")
            out = s.click_xy(100, 200)
            self.assertIn("clicked", out)
            self.assertIn("click_xy", [c[0] for c in s._driver.calls])
            self.assertIn("now", s.wait(seconds=0.1, text="Content"))

    def test_mcp_new_tools_listed_and_callable(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = MCPBrowserServer(tmp, fake=True)
            resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            names = [t["name"] for t in resp["result"]["tools"]]
            for required in ("browser_list_elements", "browser_click_xy", "browser_wait"):
                self.assertIn(required, names)

            def call(name, args=None):
                resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": name, "arguments": args or {}}})
                self.assertFalse(resp["result"]["isError"], resp)
                return resp["result"]["content"][0]["text"]
            call("browser_open", {"url": "https://example.com"})
            self.assertIn("selector", call("browser_list_elements"))
            self.assertIn("clicked", call("browser_click_xy", {"x": 10, "y": 20}))
            self.assertIn("waited", call("browser_wait", {"seconds": 0.1}))
            srv.close()

    def test_preview_has_fullscreen_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            server, url = start_preview(s, start_port=8795)
            try:
                html = urllib.request.urlopen(url, timeout=5).read().decode()
                self.assertIn("Plein écran", html)
                self.assertIn("requestFullscreen", html)
                self.assertIn("shotwrap", html)
            finally:
                server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
