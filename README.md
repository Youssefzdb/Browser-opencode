# Shadowcode browser tool — Muse-parity browser for OpenCode (MCP)

Original, clean-room code. No Muse, Claude Code or OpenCode source,
prompts, schemas or binaries were copied; the only external API used
is public Playwright. This tool reuses the *ideas* of the existing
Shadowcode browser (`shadowcode_pkg/browser.py` — one driver thread,
localhost preview, takeover, watch log, hand-back without reload)
and adds, as a standalone tool, the pieces that browser did not have:
agent actions **queued** (not dropped) during takeover, a full
**JSONL action log** (not just URL/title changes), a richer live
preview, and a **stdio MCP server** so OpenCode can connect to it.

## What it does

1. **Live localhost preview.** Open the preview URL in your own
   browser. It shows, refreshed about every second: current
   URL/title, driver (`AGENT DRIVING` / `USER DRIVING`), queued
   agent actions, a live screenshot (click it to forward the click
   to the real browser), and the watch-log tail. It also has a URL
   bar (Go), Back/Forward, Scroll down/up, a type box, a scroll-to
   box, a click-text box and a click-selector box. A
   server-sent-events stream is available at `/events` for
   live-stream consumers; polling `/state.json` + `/shot.png`
   gives the same data.
2. **True user takeover.** Click **Take control** in the preview
   (or call `browser_takeover`). Agent actions then **pause and
   queue** — they do not execute, and they are not lost. The
   browser stays open. Your clicks, typing, scrolling and
   navigation in the preview are forwarded to the real browser
   through `/control`.
3. **Watch-and-record while you drive.** The agent does not stop:
   it keeps watching and records *every* action in a JSONL log
   (`<workdir>/.shadowcode/browser-tool-watch.jsonl`), one JSON
   object per line: `ts`, `action`, `selector`, `text`,
   `url_before`, `url_after`, `driver`, `driver_transition`,
   `via`. This covers clicks, typing, scrolling, navigation,
   gotos and driver transitions — not just URL/title changes.
   **Secrets are never logged:** a typed value whose field looks
   like a password, code, token, card or similar field is stored
   as `[REDACTED]`; agent fills of such fields are refused
   outright.
4. **Hand-back.** Click **Hand back to agent** (or call
   `browser_handback`). The driver flips back to `agent`, queued
   agent actions run **in order against the current page**, and
   the hand-back summary lists the user actions recorded since
   takeover (redacted) plus the queued results. There is **no
   reload, no reset, no restart** — the agent continues the
   original task from the exact page the user left it on.

## Layout

```
browser_tool/
  __init__.py     public surface (BrowserSession, drivers, preview)
  driver.py       FakeDriver (tests, no network) + PlaywrightDriver
  session.py      BrowserSession: driver thread, queue, watch log,
                  takeover/hand-back, secret redaction
  preview.py      localhost-only preview HTTP server (127.0.0.1)
  mcp_server.py   stdio JSON-RPC MCP server (the OpenCode entry point)
  tests/
    test_browser_tool.py   14 unit tests, FakeDriver only
  README.md       this file
```

## Start it

Real browser (needs Playwright, installed by the parent, not here):

```bash
cd ~/workspace/shadowcode
pip install playwright && playwright install firefox
python3 browser_tool/mcp_server.py --workdir test-workspace
# stderr prints: Live preview: http://localhost:8766
```

Dry run / tests (no real browser, no network):

```bash
python3 browser_tool/mcp_server.py --fake --workdir /tmp/bt-demo
python3 browser_tool/tests/test_browser_tool.py   # 14 tests
```

Port: `8766` by default, next free port above it if taken, or
`SHADOWCODE_BROWSER_TOOL_PORT` / `--port`. The server binds to
`127.0.0.1` only — never expose it publicly, and never put
credentials in code or logs.

## Connect OpenCode to it (MCP, stdio)

Add a server entry pointing at this script, with an absolute path
(the example below uses the path of this workspace; change it to
yours). OpenCode's exact config file name and key layout vary by
version — adapt the snippet to the `mcp` section of your OpenCode
config (`opencode.json` in the usual layout):

```json
{
  "mcp": {
    "shadowcode-browser": {
      "type": "local",
      "command": ["python3", "/home/hatch/workspace/shadowcode/browser_tool/mcp_server.py"],
      "args": ["--workdir", "/home/hatch/workspace/shadowcode/test-workspace"],
      "environment": {
        "SHADOWCODE_BROWSER_TOOL_PORT": "8766"
      }
    }
  }
}
```

If your OpenCode version uses `command` + `args` as separate
fields instead of a single command list, split them the same way:
command `python3`, args as above. After connecting, OpenCode sees
these tools: `browser_open`, `browser_read`, `browser_click`,
`browser_list_elements`, `browser_click_xy`, `browser_wait`,
`browser_type`, `browser_scroll`, `browser_navigate`,
`browser_screenshot`, `browser_takeover`, `browser_handback`,
`browser_watch`.

### Live preview: full screen

The preview page is responsive (phone and desktop). Click
**⛶ Plein écran** above the screenshot for a full-screen live
preview (Esc exits). Clicking the screenshot in full screen still
forwards the click to the real browser, with coordinates corrected
for the full-screen scaling. Long URLs in the watch log are
truncated on screen (full URL in the tooltip) so they no longer
break the page layout.

## Takeover / hand-back flow (for the OpenCode user)

1. Agent calls `browser_open` with a URL. Open the printed preview
   URL (default `http://localhost:8766`) in your own browser.
2. Click **Take control** in the preview. The driver badge flips to
   `USER DRIVING`; agent calls now queue instead of executing.
3. Drive in the preview: click the screenshot, use the URL bar,
   type, scroll, click text/selectors. Every move is recorded in
   the JSONL watch log (secrets redacted).
4. Click **Hand back to agent**. The agent resumes from the exact
   current page, runs its queued actions in order, and reads the
   recorded user actions as context (via `browser_handback` /
   `browser_watch`) before continuing.
5. The agent never reloads on hand-back. If it ever reports a
   different page than the preview shows, that is a bug — the
   watch log is the evidence.

## For AI agents: how to use this tool (read this first)

You are an agent, not a human browsing. Use these tools exactly as
described. Do not invent other browser tools, do not drive the
browser with shell commands, and do not reload a page to "reset" it.

### 1. Start

1. Call `browser_open` with the URL the user asked for, or with the
   URL needed for the task. That is the only way to start a session.
2. Tell the user, in one short sentence, the live preview URL
   printed on stderr (default `http://localhost:8766`). The user
   opens that URL in their own browser to watch you.
3. Before every action, know the current state: call `browser_read`
   or `browser_watch`. Never act on a page you have not read.

### 2. The tools, and when to use each one

- `browser_open(url)` — open a new page. Use it only to start, or
  when the task explicitly asks for a different site. It is not a
  refresh button.
- `browser_read()` — read the current URL, title and page text.
  It now also lists clickable elements (first 30). Use it after
  every navigation, click and hand-back, before you decide the
  next step.
- `browser_list_elements()` — list visible links, buttons and
  inputs with a selector per element. **Call this before clicking,
  instead of guessing text or selectors from page text alone.**
  This is the fix for weak control: the agent was blind before,
  it now sees what it can actually click. Always runs, even while
  the user drives.
- `browser_click(text_or_selector)` — click a visible link/button
  by its text, or an element by CSS selector, preferably a selector
  taken from `browser_list_elements`. Click one thing, then read
  again. Do not click the same thing twice because a page was
  slow.
- `browser_click_xy(x, y)` — click by screenshot coordinates
  (1280x720 viewport). Use it ONLY after a screenshot, with
  coordinates from that screenshot, and only when no selector from
  `browser_list_elements` fits.
- `browser_wait(seconds, text)` — wait up to 5 seconds, optionally
  until given text appears. Use it after a navigation or a click,
  before reading again, instead of re-clicking because a page was
  slow.
- `browser_type(field, text)` — type into a labelled field. Never
  type into a password, payment, code, token or secret field: the
  tool refuses those fields, and that refusal is final. Never ask
  the user to paste a password or code into the browser for you to
  use.
- `browser_scroll(direction, amount, selector)` — scroll down/up,
  or scroll to a selector/text. Use it to bring content into view
  before clicking it.
- `browser_navigate(direction)` — go back or forward in history.
  Use it only when the task needs history, not as a way to undo a
  user action.
- `browser_screenshot()` — take a screenshot of the current page.
  Use it to check layout, not as a substitute for reading text.
- `browser_watch()` — read the recorded action log and the current
  page. Use it after a takeover, after a hand-back, and whenever
  you are unsure what changed. It never reloads the page.
- `browser_takeover()` / `browser_handback()` — see sections 3
  and 4. Only the user, or a user request, triggers a takeover.

### 3. When the user takes control

The preview badge flips to `USER DRIVING`. From that moment:

- STOP sending clicks, typing, scrolling, navigation and opens.
  The session queues agent actions instead of executing them, so
  sending more actions only builds a queue the user did not ask for.
- You may still call `browser_read`, `browser_watch` and
  `browser_screenshot`. Those always run, even while the user drives.
- Keep watching. Every user move is recorded in the JSONL watch
  log: `ts`, `action`, `selector`, `text`, `url_before`,
  `url_after`, `driver`, `driver_transition`, `via`. Secret-looking
  values appear as `[REDACTED]` — never try to recover them, and
  never repeat a redacted value.
- Do not narrate every user click. Wait for the user to hand back,
  unless the user asks you a question while driving.
- Do not close, reload or restart the browser while the user drives.

### 4. When the user hands back

1. Call `browser_handback()`. It flips the driver back to AGENT,
   with no reload, no reset and no restart.
2. Read the hand-back summary and then `browser_watch()`. The
   summary lists the user actions since takeover (redacted) and
   the results of queued agent actions, in order.
3. Continue from the CURRENT page the user left you on, not from
   the page you were on before takeover. Do not navigate back to
   the old page, do not re-do steps the user already did, and do
   not re-click a button the user already clicked.
4. If a queued action failed because the page changed, say so
   plainly and choose the next step from the current page, not
   from the old plan.
5. Tell the user, in one sentence, where you are now and what you
   will do next.

### 5. Example workflow

1. `browser_open("https://example.com")` → tell the user the
   preview URL.
2. `browser_list_elements()` → find the link you need and its
   selector (do not guess it from text alone).
3. `browser_click(selector)` → `browser_wait(2)` →
   `browser_read()` again.
4. User clicks Take control → you stop actions, you watch.
5. User drives to a new page and clicks Hand back to agent.
6. `browser_handback()` → read the summary and `browser_watch()`.
7. Continue from the new page. Never reload the old page.

### 6. Hard rules

- Never fill a password, payment, verification-code, token or
  secret field, and never solve a CAPTCHA or bypass a login wall
  or a site block. If a tool refuses, stop that step and report
  the refusal exactly.
- Never expose the preview outside localhost (`127.0.0.1`). The
  preview port is 8766 by default, or `SHADOWCODE_BROWSER_TOOL_PORT`.
- Never put credentials in code, logs or the watch file.
- If the preview, the watch log and the page disagree, trust the
  watch log and the current page, and say that they disagreed.
- If you ever report a different page than the preview shows,
  that is a bug in your reasoning — re-read with `browser_watch()`
  before you act again.

### 7. Standalone repo layout (this repository)

In this repository the tool files are at the repo root, not under
`browser_tool/`:

```bash
python3 mcp_server.py --workdir <your-workdir>
# stderr prints: Live preview: http://localhost:8766
```

OpenCode MCP config for a standalone clone (replace
`<clone-path>` with the absolute path where you cloned this repo):

```json
{
  "mcp": {
    "shadowcode-browser": {
      "type": "local",
      "command": ["python3", "<clone-path>/mcp_server.py"],
      "args": ["--workdir", "<clone-path>/test-workspace"],
      "environment": {
        "SHADOWCODE_BROWSER_TOOL_PORT": "8766"
      }
    }
  }
}
```

Inside a Shadowcode checkout the same server lives at
`browser_tool/mcp_server.py` — the tools, the preview and the
takeover/hand-back rules are identical.

## Safeguards

* Localhost only (`127.0.0.1`), no public exposure.
* No sign-in, no password/payment fill by the agent, no CAPTCHA
  solving, no bypass of a block or login wall.
* No credentials in code, logs or the watch file; secret-looking
  typed values are `[REDACTED]`.
* Watch log and screenshots stay inside the working directory
  (`.shadowcode/`, `screenshots/`), never in the home directory.

## What was verified here, and what was NOT

Verified in this task, with FakeDriver only (no live browser, no
real websites, as required for this subagent task):

* 14/14 unit tests pass (`browser_tool/tests/test_browser_tool.py`).
* MCP stdio smoke run with `--fake`: initialize, tools/list (all
  10 required tools), open, takeover, queued click, hand-back
  (queued click executed, landing on the queued page), watch —
  all correct, and the JSONL watch file has the expected entries.
* Preview HTTP endpoints verified by tests: `/state.json`,
  `/shot.png` (valid PNG), `/watch`, takeover/hand-back/control
  over HTTP, localhost-only binding.
* Existing Shadowcode suite still passes: 47/47
  (`python3 tests/run_tests.py`).

**NOT verified by this task, left for the parent agent:**
live Playwright verification on example.com / httpbin.org (or any
real site) — real-browser open, preview takeover, user driving,
watch log, hand-back from the exact page, and queued-action
drain against a real page. Playwright is not installed in this
environment's system Python, and this task was forbidden from
live website navigation, so that step is explicitly outstanding.
