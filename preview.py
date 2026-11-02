"""Localhost-only live preview server for the browser tool.

Original code for this task. The user opens the preview URL in
their own browser and sees, in near real time:

* current URL and title, and the driver (AGENT DRIVING / USER DRIVING)
* a live screenshot, refreshed by polling ``/shot.png`` and by
  the optional ``/events`` server-sent-events stream
* a URL bar (Go), Back/Forward, scroll controls, a type box,
  a scroll-to box, a click-text box and a click-selector box
* Take control / Hand back to agent buttons
* the watch log tail, so the user sees what the agent records

Every user action in the preview is forwarded to the real
browser through ``BrowserSession.user_action`` -- the preview
never edits a page itself, and it never reloads the page on
hand-back (hand-back only flips the driver and drains the
queued agent actions; see session.py).

Security: the server binds to 127.0.0.1 only, never to 0.0.0.0.
It serves no credentials and logs no typed values beyond what
the session's redacted watch log already stores.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Shadowcode browser preview</title>
<style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:1em auto}
#drv{font-size:1.2em} img{max-width:100%;border:1px solid #888;cursor:crosshair}
input{margin:2px} button{margin:2px} #watch{font-size:.9em}</style></head><body>
<h2 id="drv">AGENT DRIVING</h2>
<p id="info"></p>
<p id="queued"></p>
<button onclick="post('/takeover',{})">Take control</button>
<button onclick="post('/handback',{})">Hand back to agent</button>
<button onclick="post('/control',{action:'back'})">Back</button>
<button onclick="post('/control',{action:'forward'})">Forward</button>
<button onclick="post('/control',{action:'scroll',direction:'down',amount:600})">Scroll down</button>
<button onclick="post('/control',{action:'scroll',direction:'up',amount:600})">Scroll up</button><br>
<input id="url" placeholder="https://..." style="width:60%"><button onclick="goUrl()">Go</button><br>
<input id="field" placeholder="field label / selector (optional)"><input id="keys" placeholder="type text here"><button onclick="sendKeys()">Send keys</button><br>
<input id="scrollto" placeholder="scroll to: selector or text=..." style="width:40%"><button onclick="scrollToEl()">Scroll to</button>
<input id="clicktext" placeholder="click text: visible link text" style="width:40%"><button onclick="clickText()">Click text</button>
<input id="clickselector" placeholder="click selector: CSS selector" style="width:40%"><button onclick="clickSelector()">Click selector</button><br>
<img id="shot" src="/shot.png" onclick="clickShot(event)" alt="live browser screenshot">
<p>Watch log (what the agent records while you drive):</p><ul id="watch"></ul>
<div id="summary"></div>
<p><small>Hand back to agent resumes from the current page exactly -- no reload, no reset.
Clicking the screenshot forwards the click to the real browser. Agent actions taken
while you drive are queued, not lost, and run on hand-back.</small></p>
<script>
async function tick(){let r=await fetch('/state.json');let s=await r.json();
document.getElementById('info').textContent=s.title+' -- '+s.url;
document.getElementById('drv').textContent=s.driver==='user'?'USER DRIVING':'AGENT DRIVING';
document.getElementById('queued').textContent='Queued agent actions: '+(s.queued_agent_actions||0);
document.getElementById('watch').innerHTML=(s.watch||[]).map(e=>'<li>'+e.ts+' '+e.action+' '+e.url_before+' -&gt; '+e.url_after+' ('+e.driver+')'+(e.via?' via '+e.via:'')+'</li>').join('');
let img=document.getElementById('shot'); img.src='/shot.png?'+Date.now();}
async function post(path,o){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});let t=await r.text();
if(path==='/handback'){document.getElementById('summary').innerHTML='<pre>'+t.replace(/</g,'&lt;')+'</pre>';}
tick();}
function clickShot(e){let img=e.target;let x=Math.round(e.offsetX/img.clientWidth*1280);let y=Math.round(e.offsetY/img.clientHeight*720);post('/control',{action:'click_xy',x:x,y:y});}
function sendKeys(){post('/control',{action:'type',field:document.getElementById('field').value,text:document.getElementById('keys').value});}
function scrollToEl(){post('/control',{action:'scroll_to',selector:document.getElementById('scrollto').value});}
function clickText(){post('/control',{action:'click_text',text:document.getElementById('clicktext').value});}
function clickSelector(){post('/control',{action:'click_selector',selector:document.getElementById('clickselector').value});}
function goUrl(){post('/control',{action:'goto',url:document.getElementById('url').value});}
setInterval(tick,800); tick();
</script></body></html>
"""


def start_preview(session, start_port=8766):
    """Start the preview server for a session. Returns (server, url).

    Binds to 127.0.0.1 only, trying start_port and the next 20 ports.
    The caller owns shutdown (server.shutdown()).
    """
    session_ref = session

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, ctype, body):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(n) if n else b"{}"
                return json.loads(raw or b"{}")
            except Exception:
                return {}

        def do_GET(self):
            if self.path == "/state.json":
                self._send(200, "application/json", json.dumps(session_ref.state()))
            elif self.path.startswith("/shot.png"):
                shot = session_ref.screenshot_bytes()
                if shot:
                    self._send(200, "image/png", shot)
                else:
                    self._send(404, "text/plain", "no screenshot yet")
            elif self.path == "/watch":
                self._send(200, "application/json", json.dumps(session_ref.watch()))
            elif self.path == "/summary":
                summary = session_ref._last_handback or {}
                self._send(200, "application/json", json.dumps(summary))
            elif self.path == "/events":
                # Server-sent events: a state snapshot every second.
                # Clients that only poll /state.json get the same data;
                # this stream exists for live-stream consumers.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for _ in range(3600):
                        payload = json.dumps(session_ref.state())
                        self.wfile.write(("data: %s\n\n" % payload).encode())
                        self.wfile.flush()
                        time.sleep(1.0)
                except Exception:
                    pass
            else:
                self._send(200, "text/html", PAGE_HTML)

        def do_POST(self):
            if self.path == "/takeover":
                self._send(200, "text/plain", str(session_ref.takeover()))
            elif self.path == "/handback":
                summary = session_ref.handback()
                self._send(200, "text/plain", session_ref.handback_summary_text()
                           if isinstance(summary, dict) else str(summary))
            elif self.path == "/control":
                data = self._read_json()
                # Any control from the preview means the user is driving.
                if session_ref.driver_mode != "user":
                    session_ref.takeover()
                action = data.get("action", "")
                result = session_ref.user_action(action, **{
                    k: v for k, v in data.items() if k != "action"})
                self._send(200, "text/plain", str(result))
            else:
                self._send(404, "text/plain", "not found")

    for port in range(start_port, start_port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:  # pragma: no cover - no free port in range
        raise RuntimeError("no free localhost port for preview")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://localhost:%d" % port
    session_ref.preview_port = port
    session_ref.preview_url = url
    return server, url
