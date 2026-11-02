#!/usr/bin/env python3
"""MCP server (stdio, JSON-RPC 2.0) for the Shadowcode browser tool.

Original code for this task. This is what OpenCode connects to:
a local stdio process, no network server, no credentials in code
or logs. The server speaks newline-delimited JSON-RPC:

  initialize -> notifications/initialized -> tools/list -> tools/call

Tools exposed (at least the set the task requires, plus a selector
variant of click, which OpenCode clients commonly need):

  browser_open       {url}
  browser_read       {}
  browser_click      {text} | {selector}
  browser_type       {field, text}
  browser_scroll     {direction, amount, selector}
  browser_navigate   {direction: "back" | "forward"}
  browser_screenshot {name}
  browser_takeover   {}
  browser_handback   {}
  browser_watch      {limit}

Behaviour notes:
* The browser (and its localhost preview) starts lazily on the
  first browser_open, so merely listing tools never launches a
  browser. The preview URL is printed to stderr on start, and is
  also returned by browser_open and browser_watch.
* While the user drives (after browser_takeover, or from the
  preview page), agent tools do not execute: they are queued and
  run in order on browser_handback. browser_read, browser_watch
  and browser_screenshot are reads and always run. browser_handback
  returns the hand-back summary: current page, user actions since
  takeover (secrets redacted), and queued actions executed.
* No sign-in, password/payment fill, CAPTCHA solving or login-wall
  bypass is offered. browser_type refuses secret-looking fields.

Usage:
  python3 browser_tool/mcp_server.py [--workdir DIR] [--port PORT]
  SHADOWCODE_BROWSER_TOOL_PORT=8766 python3 browser_tool/mcp_server.py

OpenCode config is documented in browser_tool/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running as a plain script (python3 browser_tool/mcp_server.py)
# as well as a module (python3 -m browser_tool.mcp_server).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from browser_tool.driver import FakeDriver, start_playwright_driver
    from browser_tool.preview import start_preview
    from browser_tool.session import BrowserSession
except ImportError:  # pragma: no cover - package import path
    from driver import FakeDriver, start_playwright_driver  # type: ignore
    from preview import start_preview  # type: ignore
    from session import BrowserSession  # type: ignore

TOOLS = [
    {"name": "browser_open", "description": "Open a URL in the agent browser (starts browser + live preview on first use).",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "browser_read", "description": "Read the current page title, URL and text. Never reloads.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_click", "description": "Click visible text, or a CSS selector when 'selector' is given.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "selector": {"type": "string"}}}},
    {"name": "browser_type", "description": "Type text into a field (label or selector). Secret-looking fields are refused.",
     "inputSchema": {"type": "object", "properties": {"field": {"type": "string"}, "text": {"type": "string"}}, "required": ["text"]}},
    {"name": "browser_scroll", "description": "Scroll down/up/top/bottom, or scroll a selector into view.",
     "inputSchema": {"type": "object", "properties": {"direction": {"type": "string"}, "amount": {"type": "integer"}, "selector": {"type": "string"}}}},
    {"name": "browser_navigate", "description": "Go back or forward in browser history.",
     "inputSchema": {"type": "object", "properties": {"direction": {"type": "string"}}}},
    {"name": "browser_screenshot", "description": "Capture the current page screenshot (also visible at the preview URL).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "browser_takeover", "description": "Hand control to the user: agent actions queue until hand-back.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_handback", "description": "Hand control back to the agent from the exact current page (no reload), draining queued actions and summarising user actions.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_watch", "description": "Read the recorded action log (user + agent actions, secrets redacted) and current page. Never reloads.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
]


class MCPBrowserServer:
    def __init__(self, workdir, port=8766, fake=False):
        self.workdir = Path(workdir)
        self.port = port
        self._preview_server = None
        if fake:
            self.session = BrowserSession(workdir, driver=FakeDriver())
        else:
            self.session = BrowserSession(workdir, driver_factory=start_playwright_driver)

    def _ensure_preview(self):
        if self._preview_server is None:
            self._preview_server, url = start_preview(self.session, start_port=self.port)
            print("Live preview: %s" % url, file=sys.stderr, flush=True)
        return self.session.preview_url

    def call_tool(self, name, args):
        args = args or {}
        s = self.session
        if name == "browser_open":
            url = args.get("url", "")
            if not url:
                raise ValueError("browser_open needs url")
            self._ensure_preview()
            out = s.open(url)
            if isinstance(out, dict):
                return "%s\npreview: %s" % (out.get("message", ""), s.preview_url)
            return "%s\npreview: %s" % (out, s.preview_url)
        if name == "browser_read":
            return s.read()
        if name == "browser_click":
            out = s.click(text=args.get("text", ""), selector=args.get("selector", ""))
            return out if isinstance(out, str) else json.dumps(out)
        if name == "browser_type":
            out = s.type_text(args.get("field", ""), args.get("text", ""))
            return out if isinstance(out, str) else json.dumps(out)
        if name == "browser_scroll":
            out = s.scroll(direction=args.get("direction", "down"),
                           amount=args.get("amount", 600),
                           selector=args.get("selector", ""))
            return out if isinstance(out, str) else json.dumps(out)
        if name == "browser_navigate":
            out = s.navigate(args.get("direction", "back"))
            return out if isinstance(out, str) else json.dumps(out)
        if name == "browser_screenshot":
            return s.screenshot(name=args.get("name", ""))
        if name == "browser_takeover":
            return s.takeover()
        if name == "browser_handback":
            # Hand-back summary text is the most useful single reply
            # for an agent client continuing the original task.
            s.handback()
            return s.handback_summary_text()
        if name == "browser_watch":
            return s.watch_text(limit=args.get("limit"))
        raise ValueError("unknown tool: %s" % name)

    def handle(self, msg):
        """Handle one JSON-RPC message, return a response dict or None."""
        method = msg.get("method", "")
        rid = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shadowcode-browser", "version": "0.1.0"},
            }}
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = msg.get("params", {}) or {}
            try:
                text = self.call_tool(params.get("name", ""), params.get("arguments", {}))
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": text}], "isError": False}}
            except Exception as exc:
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": "tool error: %s" % exc}], "isError": True}}
        if rid is not None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "method not found: %s" % method}}
        return None

    def serve_forever(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            try:
                resp = self.handle(msg)
            except Exception as exc:
                resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32603, "message": str(exc)}}
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass
        if self._preview_server is not None:
            try:
                self._preview_server.shutdown()
                self._preview_server.server_close()
            except Exception:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shadowcode browser MCP server (stdio)")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("SHADOWCODE_BROWSER_TOOL_PORT", "8766")))
    parser.add_argument("--fake", action="store_true",
                        help="use the in-memory FakeDriver (tests / dry runs, no real browser)")
    args = parser.parse_args(argv)
    server = MCPBrowserServer(args.workdir, port=args.port, fake=args.fake)
    try:
        server.serve_forever()
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
