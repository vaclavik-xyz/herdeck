from __future__ import annotations

import hmac
import io
import json
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .base import DeckDriver, PanelView, TileView

# Panel press maps to this button index (the orchestrator pages on PANEL_INDICES).
_PANEL_PRESS_INDEX = 13


class WebDeck(DeckDriver):
    """A browser-based D200 simulator.

    Renders tiles and the status panel with the SAME code as the real device, so
    the simulator is pixel-faithful, and turns browser clicks into presses. Lets
    you develop the whole app without the physical deck. Bind to a Tailscale IP to
    use it remotely.
    """

    def __init__(
        self,
        slots: int = 13,
        host: str = "127.0.0.1",
        port: int = 8800,
        icon_provider=None,
        icons_dir: str | None = None,
        serve: bool = True,
    ):
        self._slots = slots
        self._callback: Callable[[int], None] | None = None
        self._lock = threading.Lock()
        self._tiles: dict[int, bytes] = {}  # index -> PNG bytes
        self._tile_ver: dict[int, int] = {}  # index -> last-changed version
        self._panel: bytes | None = None
        self._panel_ver = 0
        self._version = 0
        self._press_token = secrets.token_urlsafe(24)
        if icon_provider is None:
            import os
            import tempfile

            from ..icons import DEFAULT_AGENT_SLUGS, IconProvider

            cache = os.path.join(tempfile.gettempdir(), "herdeck-web-icons")
            overrides = os.path.abspath(os.path.expanduser(icons_dir)) if icons_dir else None
            icon_provider = IconProvider(
                cache_dir=cache,
                slug_map=DEFAULT_AGENT_SLUGS,
                overrides_dir=overrides,
            )
        self._icons = icon_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        if serve:
            self._server = ThreadingHTTPServer((host, port), self._handler_class())
            self.host, self.port = self._server.server_address[0], self._server.server_address[1]
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

    # --- DeckDriver interface ---
    def slot_count(self) -> int:
        return self._slots

    @property
    def press_token(self) -> str:
        return self._press_token

    def _bump(self) -> int:
        """Assign the next monotonic version. Call while holding self._lock."""
        self._version += 1
        return self._version

    def render(self, tiles: list[TileView]) -> None:
        new: dict[int, bytes] = {}
        for t in tiles:
            if t.index >= self._slots:
                continue
            new[t.index] = self._icons.render_tile_bytes(t)
        with self._lock:
            for i, png in new.items():
                if self._tiles.get(i) != png:  # bump only changed/new tiles
                    self._tile_ver[i] = self._bump()
            removed = set(self._tile_ver) - set(new)
            for i in removed:  # drop versions of gone tiles
                del self._tile_ver[i]
            if removed:  # a pure removal must still
                self._bump()  # trip the client's version gate
            self._tiles = new

    def render_working(self, tiles: list[TileView]) -> None:
        """Partial re-render of just the given (working) tiles: bumps only their
        versions and leaves every other tile and the panel untouched, so the
        browser refetches just the animating tiles instead of the whole deck."""
        rendered: dict[int, bytes] = {}
        for t in tiles:
            if t.index >= self._slots:
                continue
            rendered[t.index] = self._icons.render_tile_bytes(t)
        with self._lock:
            for i, png in rendered.items():
                if self._tiles.get(i) != png:
                    self._tiles[i] = png
                    self._tile_ver[i] = self._bump()

    def render_panel(self, panel: PanelView) -> None:
        from ..icons import compose_panel

        buf = io.BytesIO()
        compose_panel(panel).convert("RGB").save(buf, "PNG")
        png = buf.getvalue()
        with self._lock:
            if self._panel != png:  # bump only when it changes
                self._panel = png
                self._panel_ver = self._bump()

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback

    def press(self, index: int) -> None:
        """Inject a press (called by the HTTP handler thread; the app marshals).

        Only buttons (0..slots-1) and the two panel cells are valid; anything
        else (e.g. a negative index from a crafted request) is ignored.
        """
        if self._callback is not None and 0 <= index < self._slots + 2:
            self._callback(index)

    def close(self) -> None:
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
            self._server = None
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._thread = None

    # --- state snapshot for the browser ---
    def _state(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "slots": self._slots,
                "has_panel": self._panel is not None,
                "panel": self._panel_ver,
                "tiles": dict(self._tile_ver),
            }

    def _tile_png(self, index: int) -> bytes | None:
        with self._lock:
            return self._tiles.get(index)

    def _panel_png(self) -> bytes | None:
        with self._lock:
            return self._panel

    # --- HTTP ---
    def _handler_class(self):
        deck = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default request logging
                pass

            def _send(self, code, body=b"", ctype="application/octet-stream"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _valid_token(self, token):
                return hmac.compare_digest(token.encode(), deck._press_token.encode())

            def _query_token(self, url):
                return parse_qs(url.query).get("token", [""])[0]

            def _require_token(self, url):
                if self._valid_token(self._query_token(url)):
                    return True
                self._send(403)
                return False

            def do_GET(self):
                url = urlsplit(self.path)
                path = url.path
                if path == "/":
                    token = self._query_token(url)
                    if not self._valid_token(token):
                        self._send(403)
                        return
                    page = _PAGE.replace("__PRESS_TOKEN_JSON__", json.dumps(deck._press_token))
                    self._send(200, page.encode(), "text/html; charset=utf-8")
                elif path == "/state":
                    if not self._require_token(url):
                        return
                    self._send(200, json.dumps(deck._state()).encode(), "application/json")
                elif path == "/panel":
                    if not self._require_token(url):
                        return
                    png = deck._panel_png()
                    self._send(200, png, "image/png") if png else self._send(404)
                elif path.startswith("/tile/"):
                    if not self._require_token(url):
                        return
                    try:
                        png = deck._tile_png(int(path.rsplit("/", 1)[1]))
                    except ValueError:
                        png = None
                    self._send(200, png, "image/png") if png else self._send(404)
                else:
                    self._send(404)

            def do_POST(self):
                path = urlsplit(self.path).path
                if path.startswith("/press/"):
                    token = self.headers.get("X-Herdeck-Token", "")
                    if not self._valid_token(token):
                        self._send(403)
                        return
                    try:
                        deck.press(int(path.rsplit("/", 1)[1]))
                        self._send(204)
                    except ValueError:
                        self._send(400)
                else:
                    self._send(404)

        return Handler


_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Herdeck simulator</title>
<style>
 body{background:#0b0b0d;margin:0;font-family:-apple-system,sans-serif;
   display:flex;align-items:center;justify-content:center;min-height:100vh}
 #deck{background:#2a2a2e;padding:18px;border-radius:18px;
   display:grid;grid-template-columns:repeat(5,min(17vw,150px));gap:10px}
 .cell{width:min(17vw,150px);height:min(17vw,150px);border-radius:8px;background:#111;cursor:pointer;
   overflow:hidden;border:none;padding:0}
 .cell.active{outline:3px solid #5af}
 .cell img{width:100%;height:100%;display:block}
 #panel{grid-column:4 / 6;width:calc(min(17vw,150px)*2 + 10px);height:min(17vw,150px);border-radius:8px;
   overflow:hidden;cursor:pointer;background:#111}
 #panel img{width:100%;height:100%;display:block}
 /* phone portrait: width is the constraint, so shrink the 5-wide deck */
 @media (max-width:560px){
   #deck{grid-template-columns:repeat(5,min(17vw,110px));gap:6px;padding:10px}
   .cell{width:min(17vw,110px);height:min(17vw,110px)}
   #panel{width:calc(min(17vw,110px)*2 + 6px);height:min(17vw,110px)}
 }
 /* phone landscape: HEIGHT is the constraint (3 rows), so size cells by viewport
    height — but also keep the 17vw width cap so a short AND narrow viewport
    (e.g. 320x400, where this rule overrides the portrait one) can't overflow
    sideways. The deck stays within both the short (e.g. 667x375) viewport's
    height and a narrow viewport's width. */
 @media (max-height:430px){
   #deck{grid-template-columns:repeat(5,min(17vw,22vh,110px));gap:6px;padding:10px}
   .cell{width:min(17vw,22vh,110px);height:min(17vw,22vh,110px)}
   #panel{width:calc(min(17vw,22vh,110px)*2 + 6px);height:min(17vw,22vh,110px)}
 }
</style>
<div id=deck></div>
<script>
const deck=document.getElementById('deck');
const pressToken=__PRESS_TOKEN_JSON__;
let cells=[]; const btns=[]; let slotCount=0;
function auth(path){
  return path+(path.includes('?')?'&':'?')+'token='+encodeURIComponent(pressToken);
}
// one press path for clicks and keys: post the press, outline the pressed cell.
async function press(i){
  let r;
  try{
    r=await fetch('/press/'+i,{method:'POST',headers:{'X-Herdeck-Token':pressToken}});
  }catch(e){ return; }
  if(r.status===403) location.reload();
  if(!r.ok) return;
  btns.forEach(b=>b.classList.remove('active'));   // clear any stale outline first
  if(btns[i]) btns[i].classList.add('active');     // panel (no button) leaves none active
}
function addCell(i){
  const b=document.createElement('button');b.className='cell';
  b.onclick=()=>press(i);
  const img=document.createElement('img');b.appendChild(img);
  deck.appendChild(b);cells.push(img);btns.push(b);
}
const panel=document.createElement('div');panel.id='panel';
panel.onclick=()=>press(slotCount);
const pimg=document.createElement('img');panel.appendChild(pimg);
function ensureCells(count){
  if(count===slotCount) return;
  while(btns.length<count) addCell(btns.length);
  while(btns.length>count){
    const i=btns.length-1;
    btns.pop().remove(); cells.pop(); delete tv[i];
  }
  slotCount=count;
  deck.appendChild(panel);
}
// keyboard: 1..9 -> tiles 0..8, 0 -> tile 9; ignore when a modifier is held.
document.addEventListener('keydown',e=>{
  if(e.repeat) return;                                   // don't spam presses on key-hold
  if(e.metaKey||e.ctrlKey||e.altKey||e.shiftKey) return;
  if(e.key>='1'&&e.key<='9') press(e.key.charCodeAt(0)-49);
  else if(e.key==='0') press(9);
});
let lastV=-1; const tv={}; let pv=-1;
async function poll(){
  try{
    const s=await (await fetch(auth('/state'))).json();
    ensureCells(s.slots);
    if(s.version!==lastV){          // cheap gate: nothing changed at all
      lastV=s.version;
      const t=s.tiles||{};
      for(let i=0;i<slotCount;i++){ // refetch only tiles whose version advanced
        const v=t[i];
        if(v===undefined){          // tile gone -> clear the cell
          if(tv[i]!==undefined){ delete tv[i]; cells[i].removeAttribute('src'); }
        } else if(v!==tv[i]){ tv[i]=v; cells[i].src=auth('/tile/'+i+'?v='+v); }
      }
      if(s.has_panel && s.panel!==pv){ pv=s.panel; pimg.src=auth('/panel?v='+pv); }
    }
  }catch(e){}
  setTimeout(poll,300);
}
poll();
</script>
"""
