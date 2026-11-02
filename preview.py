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
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shadowcode browser preview</title>
<style>html,body{margin:0;padding:0;box-sizing:border-box}*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;padding:8px;overflow-wrap:anywhere}
#controls{position:sticky;top:0;background:#fff;padding:8px 0;z-index:10}
#drv{font-size:1.2em;margin:0 0 4px} #info{margin:4px 0;overflow-wrap:anywhere}
#shotwrap{position:relative;background:#000;line-height:0}
#shotwrap img{width:100%;height:auto;display:block;border:1px solid #888;cursor:crosshair;background:#000}
#cursor{position:absolute;left:0;top:0;width:28px;height:28px;pointer-events:none;display:none;z-index:20;filter:drop-shadow(1px 1px 1px #000)}
#cursor svg{width:28px;height:28px}
#shotwrap:fullscreen{background:#000}
#shotwrap:fullscreen img{width:100vw;height:100vh;object-fit:contain;border:none}
#shotwrap:-webkit-full-screen{background:#000}
#shotwrap:-webkit-full-screen img{width:100vw;height:100vh;object-fit:contain;border:none}
input{margin:2px;max-width:100%} button{margin:2px} #watch{font-size:.9em;overflow-wrap:anywhere;word-break:break-all}
#watch li{margin-bottom:4px} #summary pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body>
<div id="controls">
<h2 id="drv">AGENT DRIVING</h2>
<p id="info"></p>
<p id="queued"></p>
<button onclick="post('/takeover',{})">Take control</button>
<button onclick="post('/handback',{})">Hand back to agent</button>
<button onclick="post('/control',{action:'back'})">Back</button>
<button onclick="post('/control',{action:'forward'})">Forward</button>
<button onclick="post('/control',{action:'scroll',direction:'down',amount:600})">Scroll down</button>
<button onclick="post('/control',{action:'scroll',direction:'up',amount:600})">Scroll up</button>
<button onclick="toggleFullscreen()">⛶ Plein écran</button><br>
<input id="url" placeholder="https://..." style="width:60%"><button onclick="goUrl()">Go</button><br>
<input id="field" placeholder="field label / selector (optional)"><input id="keys" placeholder="type text here"><button onclick="sendKeys()">Send keys</button><br>
<input id="scrollto" placeholder="scroll to: selector or text=..." style="width:40%"><button onclick="scrollToEl()">Scroll to</button>
<input id="clicktext" placeholder="click text: visible link text" style="width:40%"><button onclick="clickText()">Click text</button>
<input id="clickselector" placeholder="click selector: CSS selector" style="width:40%"><button onclick="clickSelector()">Click selector</button>
</div>
<div id="shotwrap"><img id="shot" src="/shot.png" onclick="clickShot(event)" onmousemove="moveCursor(event)" alt="live browser screenshot"><div id="cursor"><svg viewBox="0 0 28 28"><path d="M4 2 L4 22 L10 16 L14 24 L17 22 L13 15 L21 15 Z" fill="#fff" stroke="#000" stroke-width="1.5"/></svg></div></div>
<p>Watch log (what the agent records while you drive):</p><ul id="watch"></ul>
<div id="summary"></div>
<p><small>Hand back to agent resumes from the current page exactly -- no reload, no reset.
Clicking the screenshot forwards the click to the real browser. Agent actions taken
while you drive are queued, not lost, and run on hand-back. Use Plein écran for a
full-screen live preview; Esc exits fullscreen.</small></p>
<script>
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function short(u){u=String(u||'');return u.length>80?u.slice(0,77)+'...':u;}
function shotPos(img,x,y){let rect=img.getBoundingClientRect();let scale=Math.min(rect.width/1280,rect.height/720);let dw=1280*scale,dh=720*scale;return {left:rect.left+(rect.width-dw)/2+x*scale,top:rect.top+(rect.height-dh)/2+y*scale,scale:scale};}
function placeCursor(x,y){let img=document.getElementById('shot');let cur=document.getElementById('cursor');if(x===null||x===undefined||y===null||y===undefined){cur.style.display='none';return;}let p=shotPos(img,x,y);let wrap=document.getElementById('shotwrap').getBoundingClientRect();cur.style.display='block';cur.style.left=(p.left-wrap.left)+'px';cur.style.top=(p.top-wrap.top)+'px';}
function viewportXY(e){let img=e.target;let rect=img.getBoundingClientRect();let scale=Math.min(rect.width/1280,rect.height/720);let dw=1280*scale,dh=720*scale;let ox=rect.left+(rect.width-dw)/2,oy=rect.top+(rect.height-dh)/2;return {x:Math.max(0,Math.min(1280,Math.round((e.clientX-ox)/scale))),y:Math.max(0,Math.min(720,Math.round((e.clientY-oy)/scale)))};}
let lastMousePost=0;
function moveCursor(e){let p=viewportXY(e);placeCursor(p.x,p.y);let now=Date.now();if(now-lastMousePost>500){lastMousePost=now;fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'mouse_move',x:p.x,y:p.y})});}}
async function tick(){let r=await fetch('/state.json');let s=await r.json();
document.getElementById('info').textContent=s.title+' -- '+s.url;
document.getElementById('drv').textContent=s.driver==='user'?'USER DRIVING':'AGENT DRIVING';
document.getElementById('queued').textContent='Queued agent actions: '+(s.queued_agent_actions||0);
placeCursor(s.mouse_x,s.mouse_y);
document.getElementById('watch').innerHTML=(s.watch||[]).map(e=>'<li title="'+esc(e.url_after)+'">'+esc(e.ts)+' '+esc(e.action)+' '+esc(short(e.url_before))+' -&gt; '+esc(short(e.url_after))+' ('+esc(e.driver)+')'+(e.via?' via '+esc(e.via):'')+'</li>').join('');
let img=document.getElementById('shot'); img.src='/shot.png?'+Date.now();}
async function post(path,o){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});let t=await r.text();
if(path==='/handback'){document.getElementById('summary').innerHTML='<pre>'+t.replace(/</g,'&lt;')+'</pre>';}
tick();}
function toggleFullscreen(){let w=document.getElementById('shotwrap');if(document.fullscreenElement){document.exitFullscreen();}else if(w.requestFullscreen){w.requestFullscreen();}else if(w.webkitRequestFullscreen){w.webkitRequestFullscreen();}}
function clickShot(e){let img=e.target;let rect=img.getBoundingClientRect();let scale=Math.min(rect.width/1280,rect.height/720);let dw=1280*scale,dh=720*scale;let ox=rect.left+(rect.width-dw)/2,oy=rect.top+(rect.height-dh)/2;let x=Math.round((e.clientX-ox)/scale),y=Math.round((e.clientY-oy)/scale);x=Math.max(0,Math.min(1280,x));y=Math.max(0,Math.min(720,y));post('/control',{action:'click_xy',x:x,y:y});}
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
                # Any real control from the preview means the user is
                # driving -- except a mouse move, which only tracks
                # the cursor for the overlay and must not flip the
                # driver just because the user hovered the screenshot.
                if data.get("action") != "mouse_move" and session_ref.driver_mode != "user":
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
