from __future__ import annotations

import io
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

    def __init__(self, slots: int = 13, host: str = "127.0.0.1", port: int = 8800,
                 icon_provider=None, serve: bool = True):
        self._slots = slots
        self._callback: Callable[[int], None] | None = None
        self._lock = threading.Lock()
        self._tiles: dict[int, bytes] = {}          # index -> PNG bytes
        self._tile_ver: dict[int, int] = {}         # index -> last-changed version
        self._panel: bytes | None = None
        self._panel_ver = 0
        self._version = 0
        if icon_provider is None:
            import os
            import tempfile

            from ..icons import DEFAULT_AGENT_SLUGS, IconProvider
            cache = os.path.join(tempfile.gettempdir(), "herdeck-web-icons")
            icon_provider = IconProvider(cache_dir=cache,
                                         slug_map=DEFAULT_AGENT_SLUGS,
                                         overrides_dir=None)
        self._icons = icon_provider
        self._server: ThreadingHTTPServer | None = None
        if serve:
            self._server = ThreadingHTTPServer((host, port), self._handler_class())
            self.host, self.port = self._server.server_address[0], self._server.server_address[1]
            threading.Thread(target=self._server.serve_forever, daemon=True).start()

    # --- DeckDriver interface ---
    def slot_count(self) -> int:
        return self._slots

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
                if self._tiles.get(i) != png:          # bump only changed/new tiles
                    self._tile_ver[i] = self._bump()
            removed = set(self._tile_ver) - set(new)
            for i in removed:                          # drop versions of gone tiles
                del self._tile_ver[i]
            if removed:                                # a pure removal must still
                self._bump()                           # trip the client's version gate
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
            if self._panel != png:                     # bump only when it changes
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
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass

    # --- state snapshot for the browser ---
    def _state(self) -> dict:
        with self._lock:
            return {"version": self._version, "slots": self._slots,
                    "has_panel": self._panel is not None,
                    "panel": self._panel_ver,
                    "tiles": dict(self._tile_ver)}

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
            def log_message(self, *a):           # silence default request logging
                pass

            def _send(self, code, body=b"", ctype="application/octet-stream"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/state":
                    self._send(200, json.dumps(deck._state()).encode(),
                               "application/json")
                elif path == "/panel":
                    png = deck._panel_png()
                    self._send(200, png, "image/png") if png else self._send(404)
                elif path.startswith("/tile/"):
                    try:
                        png = deck._tile_png(int(path.rsplit("/", 1)[1]))
                    except ValueError:
                        png = None
                    self._send(200, png, "image/png") if png else self._send(404)
                else:
                    self._send(404)

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/press/"):
                    try:
                        deck.press(int(path.rsplit("/", 1)[1]))
                        self._send(204)
                    except ValueError:
                        self._send(400)
                else:
                    self._send(404)

        return Handler


_PAGE = """<!doctype html><meta charset=utf-8>
<title>Herdeck simulator</title>
<style>
 body{background:#0b0b0d;margin:0;font-family:-apple-system,sans-serif;
   display:flex;align-items:center;justify-content:center;min-height:100vh}
 #deck{background:#2a2a2e;padding:18px;border-radius:18px;
   display:grid;grid-template-columns:repeat(5,110px);gap:10px}
 .cell{width:110px;height:110px;border-radius:8px;background:#111;cursor:pointer;
   overflow:hidden;border:none;padding:0}
 .cell img{width:100%;height:100%;display:block}
 #panel{grid-column:4 / 6;width:230px;height:110px;border-radius:8px;
   overflow:hidden;cursor:pointer;background:#111}
 #panel img{width:100%;height:100%;display:block}
</style>
<div id=deck></div>
<script>
const deck=document.getElementById('deck');
let cells=[];
// 13 buttons fill grid positions 0..12; the panel spans the last two cells.
for(let i=0;i<13;i++){
  const b=document.createElement('button');b.className='cell';
  b.onclick=()=>fetch('/press/'+i,{method:'POST'});
  const img=document.createElement('img');b.appendChild(img);
  deck.appendChild(b);cells.push(img);
}
const panel=document.createElement('div');panel.id='panel';
panel.onclick=()=>fetch('/press/13',{method:'POST'});
const pimg=document.createElement('img');panel.appendChild(pimg);
deck.appendChild(panel);
let lastV=-1; const tv={}; let pv=-1;
async function poll(){
  try{
    const s=await (await fetch('/state')).json();
    if(s.version!==lastV){          // cheap gate: nothing changed at all
      lastV=s.version;
      const t=s.tiles||{};
      for(let i=0;i<13;i++){        // refetch only tiles whose version advanced
        const v=t[i];
        if(v===undefined){          // tile gone -> clear the cell
          if(tv[i]!==undefined){ delete tv[i]; cells[i].removeAttribute('src'); }
        } else if(v!==tv[i]){ tv[i]=v; cells[i].src='/tile/'+i+'?v='+v; }
      }
      if(s.has_panel && s.panel!==pv){ pv=s.panel; pimg.src='/panel?v='+pv; }
    }
  }catch(e){}
  setTimeout(poll,300);
}
poll();
</script>
"""
