"""Shadowcode browser tool: Muse-parity live browser as an MCP server.

Public surface:
  BrowserSession  -- session.py (driver thread, takeover queue, watch log)
  FakeDriver      -- driver.py  (in-memory driver for tests)
  start_preview   -- preview.py (localhost-only live preview server)
  MCPBrowserServer -- mcp_server.py (stdio JSON-RPC server for OpenCode)
"""
from .driver import FakeDriver, PlaywrightDriver, start_playwright_driver
from .session import BrowserSession, is_secret_field, redact_text
from .preview import start_preview

__all__ = [
    "BrowserSession", "FakeDriver", "PlaywrightDriver",
    "start_playwright_driver", "start_preview",
    "is_secret_field", "redact_text",
]
