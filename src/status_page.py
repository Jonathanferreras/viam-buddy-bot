"""
Tiny local status page for the voice router: http://localhost:8765

Serves the Buddy Bot page (web/dist, built with Vite; falls back to the inline page below) plus /state (JSON). Runs in a background thread, bound to
127.0.0.1 only, so nothing outside this computer can reach it. The router hands
it a function that returns its current state; reconfigures just swap that function.
"""

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

# ---- the built React page (web/dist): served when it exists, else the inline PAGE below ----
_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist")
_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2", ".json": "application/json",
         ".map": "application/json"}


def _dist_file(rel: str) -> Optional[str]:
    """Absolute path of a file inside web/dist, or None if it isn't there (or escapes the folder)."""
    full = os.path.normpath(os.path.join(_DIST, rel))
    if not full.startswith(_DIST + os.sep) and full != os.path.join(_DIST, "index.html"):
        return None
    return full if os.path.isfile(full) else None


_state_fn: Optional[Callable[[], Dict[str, Any]]] = None
_action_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
_server: Optional[ThreadingHTTPServer] = None
_lock = threading.Lock()


def set_state_source(fn: Callable[[], Dict[str, Any]]) -> None:
    global _state_fn
    _state_fn = fn


_level = 0.0
_level_ok = False
_snapshot: Optional[bytes] = None   # latest annotated camera image (JPEG)


def set_snapshot(jpeg: bytes) -> None:
    global _snapshot
    _snapshot = jpeg


def push_level(v: float) -> None:
    global _level
    _level = v


def set_level_ok(ok: bool) -> None:
    global _level_ok, _level
    _level_ok = ok
    if not ok:
        _level = 0.0


# ---- live camera feeds: the router pushes frames here, open pages stream them as MJPEG ----
# One entry per camera shown on the page (the table camera, each arm's wrist camera). Functions take
# `which` = the camera's name; None means the first (main) camera.
_viewers_lock = threading.Lock()
_cams: Dict[str, Dict[str, Any]] = {}
_cam_order: List[str] = []


def _cam(which: Optional[str] = None) -> Dict[str, Any]:
    name = which if which is not None else (_cam_order[0] if _cam_order else "")
    c = _cams.get(name)
    if c is None:
        c = _cams[name] = {"cv": threading.Condition(), "status": {}, "viewers": 0,
                           "frame": {"seq": 0, "color": None, "depth": None, "depth_mm": None}}
    return c


def set_live_cams(names: List[str]) -> None:
    """The cameras the page shows, in order; the first is the main one."""
    _cam_order[:] = list(names)
    for n in names:
        _cam(n)


def live_cams() -> List[str]:
    return list(_cam_order)


def want_live(which: Optional[str] = None) -> None:
    """Ask for frames from this camera for the next couple of seconds, even with no page watching."""
    _cam(which)["want_at"] = time.time()


def live_wanted(which: Optional[str] = None) -> bool:
    """True while at least one page is showing this camera (the router only grabs frames then)."""
    c = _cam(which)
    return c["viewers"] > 0 or time.time() - c.get("want_at", 0.0) < 2.5


# ---- recent log lines from the router, for the page's Logs panel ----
_logs: "deque" = None   # set below
_log_seq = 0
_log_lock = threading.Lock()


class _LogBuffer(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _log_seq
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        with _log_lock:
            _log_seq += 1
            _logs.append({"seq": _log_seq, "t": record.created, "level": record.levelname.lower(), "msg": msg})


from collections import deque  # noqa: E402
_logs = deque(maxlen=500)
_log_handler = _LogBuffer(level=logging.INFO)


def capture_logs(logger: logging.Logger) -> None:
    """Copy this logger's lines into the buffer the page's Logs panel reads (once per logger)."""
    if _log_handler not in logger.handlers:
        logger.addHandler(_log_handler)


def logs_after(seq: int) -> Dict[str, Any]:
    with _log_lock:
        return {"seq": _log_seq, "logs": [r for r in _logs if r["seq"] > seq]}


_last_state_at = 0.0   # when a page last polled /state


def page_open(within: float = 3.0) -> bool:
    """True while a status page is open (it polls /state several times a second)."""
    return time.time() - _last_state_at < within


def set_live_frame(color: Optional[bytes], depth: Optional[bytes], depth_mm, which: Optional[str] = None) -> None:
    c = _cam(which)
    with c["cv"]:
        f = c["frame"]
        f.update(seq=f["seq"] + 1, color=color, depth=depth, depth_mm=depth_mm)
        c["cv"].notify_all()


def set_live_status(which: Optional[str] = None, **kw) -> None:
    _cam(which)["status"].update(kw)


def live_status(which: Optional[str] = None) -> Dict[str, Any]:
    c = _cam(which)
    return {**c["status"], "viewers": c["viewers"]}


_rec_dir: Optional[str] = None   # where recordings are saved (served at /recordings/<file>)


def set_recordings_dir(path: Optional[str]) -> None:
    global _rec_dir
    _rec_dir = path


def set_action_handler(fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
    global _action_fn
    _action_fn = fn


def start(port: int, logger) -> None:
    """Start the server once per process; later calls are no-ops."""
    global _server
    with _lock:
        if _server is not None:
            return
        try:
            _server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
            _server.daemon_threads = True  # open level streams must not block shutdown
        except OSError as e:
            logger.warning(f"status page not started on port {port}: {e}")
            return
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        logger.info(f"voice-router status page: http://localhost:{port}")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep viam logs quiet
        pass

    def do_GET(self):
        if self.path.startswith("/logs"):
            try:
                after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._send(200, "application/json", json.dumps(logs_after(after)))
        elif self.path.startswith("/state"):
            global _last_state_at
            _last_state_at = time.time()
            try:
                body = json.dumps(_state_fn() if _state_fn else {"phase": "starting"}, default=str)
            except Exception as e:  # never let the page crash the router
                body = json.dumps({"phase": "error", "last_error": str(e)})
            self._send(200, "application/json", body)
        elif self.path.startswith("/levels"):
            self._stream_levels()
        elif self.path.startswith("/live/stream"):
            q = parse_qs(urlparse(self.path).query)
            kind, cam = q.get("kind", ["depth"])[0], q.get("cam", [None])[0]
            if cam is not None and cam not in _cam_order:
                self._send(404, "text/plain", "no such camera")
                return
            self._stream_live("color" if kind == "color" else "depth", cam)
        elif self.path.startswith("/live/frame"):
            self._live_frame()
        elif self.path.startswith("/live/depth_at"):
            self._send(200, "application/json", json.dumps({"mm": _depth_at(self.path)}))
        elif self.path.startswith("/snapshot.jpg"):
            if _snapshot:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(_snapshot)))
                self.end_headers()
                self.wfile.write(_snapshot)
            else:
                self._send(404, "text/plain", "no snapshot yet")
        elif self.path.startswith("/recordings/"):
            self._recording(urlparse(self.path).path[len("/recordings/"):])
        elif self.path in ("/", "/index.html"):
            built = _dist_file("index.html")
            if built:
                self._send_file(built, "text/html; charset=utf-8")
            else:
                self._send(200, "text/html; charset=utf-8", PAGE)
        elif self.path.startswith("/assets/") or os.path.splitext(urlparse(self.path).path)[1].lower() in _MIME:
            # the built page's files: hashed bundles under /assets/, plus root files like /viam-wordmark.png
            rel = urlparse(self.path).path.lstrip("/")
            built = _dist_file(rel)
            if built:
                self._send_file(built, _MIME.get(os.path.splitext(built)[1].lower(), "application/octet-stream"), cache=rel.startswith("assets/"))
            else:
                self._send(404, "text/plain", "not found")
        else:
            self._send(404, "text/plain", "not found")

    def do_POST(self):
        # Only the page's own fetch() can send JSON here; a plain cross-site form post can't,
        # since browsers preflight application/json and we never answer the preflight.
        if self.path != "/action" or not self.headers.get("Content-Type", "").startswith("application/json"):
            self._send(404, "text/plain", "not found")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
            if _action_fn is None:
                raise RuntimeError("router not ready")
            self._send(200, "application/json", json.dumps({"ok": True, "result": _action_fn(body)}, default=str))
        except Exception as e:
            self._send(200, "application/json", json.dumps({"ok": False, "error": str(e)}))

    def _stream_levels(self):
        """Server-sent events: the mic loudness (0..1) every 50 ms, or 'off' if there's no level feed."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                msg = f"data: {_level:.3f}\n\n" if _level_ok else "data: off\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # browser closed the page

    def _live_frame(self):
        """The latest JPEG of one camera (?cam=&kind=color|depth). With ?have=<seq> it answers 204 if
        there's nothing newer, so a page can poll cheaply. Each request is brief: unlike MJPEG streams,
        any number of cameras fit in the browser's per-site connection limit."""
        q = parse_qs(urlparse(self.path).query)
        cam = q.get("cam", [None])[0]
        if cam is not None and cam not in _cam_order:
            self._send(404, "text/plain", "no such camera")
            return
        kind = "color" if q.get("kind", ["depth"])[0] == "color" else "depth"
        c = _cam(cam)
        c["want_at"] = time.time()
        try:
            have = int(q.get("have", ["-1"])[0])
        except ValueError:
            have = -1
        f = c["frame"]
        seq, jpg = f["seq"], f[kind]
        if jpg is None or seq == have:
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Seq", str(seq))
        self.send_header("Content-Length", str(len(jpg)))
        self.end_headers()
        self.wfile.write(jpg)

    def _stream_live(self, kind: str, cam: Optional[str] = None):
        """MJPEG (multipart/x-mixed-replace): an <img> shows each frame as it arrives."""
        c = _cam(cam)
        cv, frame = c["cv"], c["frame"]
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with _viewers_lock:
            c["viewers"] += 1
        last = -1
        try:
            self.wfile.write(b"--frame\r\n")
            while True:
                with cv:
                    if frame["seq"] == last:
                        cv.wait(timeout=2.0)
                    seq, jpg = frame["seq"], frame[kind]
                if jpg is None:
                    continue
                # On a stall the last frame is re-sent every 2 s, which also notices closed pages.
                self.wfile.write(b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n--frame\r\n")
                self.wfile.flush()
                last = seq
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # page closed or switched views
        finally:
            with _viewers_lock:
                c["viewers"] -= 1

    def _recording(self, name: str):
        """A saved clip, with Range support so the browser can seek (Safari needs it to play at all)."""
        import re
        from urllib.parse import unquote
        name = unquote(name)
        if not _rec_dir or not re.fullmatch(r"(?:[\w-]+/)?[\w][\w.-]*\.mp4", name):
            self._send(404, "text/plain", "not found")
            return
        path = os.path.join(_rec_dir, name)
        if not os.path.isfile(path):
            self._send(404, "text/plain", "not found")
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_file(self, path: str, ctype: str, cache: bool = False):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # Vite names assets by content hash, so they can be cached hard; index.html must not be.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if cache else "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send(self, code: int, ctype: str, body: str):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _depth_at(path: str) -> Optional[int]:
    """Distance (mm) at a spot of the latest depth frame; x and y are 0..1 across the picture."""
    q = parse_qs(urlparse(path).query)
    cam = q.get("cam", [None])[0]
    if cam is not None and cam not in _cam_order:
        return None
    d = _cam(cam)["frame"]["depth_mm"]
    try:
        fx, fy = float(q["x"][0]), float(q["y"][0])
    except (KeyError, ValueError):
        return None
    if d is None or not (0 <= fx <= 1 and 0 <= fy <= 1):
        return None
    h, w = d.shape
    u, v = min(w - 1, int(fx * w)), min(h - 1, int(fy * h))
    win = d[max(0, v - 3):v + 4, max(0, u - 3):u + 4]
    vals = win[win > 0]
    return int(np.median(vals)) if vals.size else None


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Hey Viam</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAADx0lEQVR42u2by07rMBCGx04XPEArRC+w5CkAUYl7JfY8CQgkQFwKiMchCxQKAcFTsKQIaARrEE3+szmOXE5o7DRJdZpY8qqRZ/5v5p/GqDDSW6D/Y7E4H/xfREfSyEdcfKgGNsLClfTyDIkP1MYzJD5QI6eML56x6v+jlWdQfA+E3AJZB8Ay2v55B+QAcgA5gBxADkAXAIDA7bouAaD7+3vinJNhGMQYGzi5QqFARES7u7sEgLrdbmgOtm1HeiVU2v1Wt9sFABweHoKIUCgUoHP2z20YBogIMzMz+Pr6guu68DwvMLb47Pn5GbVaTTeW+sO/JQAAnuf5EFZWVnpE6G7OOTjnKBaLaLfbvsjf4n5/fwMA6vV6lHh6HfBbInIlOp0OyuUyGGPgnGslxBjzu+fi4qKnu4KWEL+3txe189QfPj8/D01IfNZqtfyEGGPKMYSAra2tHoH9YlmWFSmWNgAigmVZylXZ39/XqoqwzNzcnN/aYb5/fX3FxMREpG7TBsAYQ7lcRqfTged5Sr5cWFhQmgecczDGUCqV8PT0pHz+4uLiQPOGokzm5eVlv9JhFXp5ecH4+HjfCkX1vW6HxWIBEezo6EjZo5eXl309Ks7c3NxUPvPq6soHF8H30QHIQW9ubpSrtbOzE1gt0VWzs7NwXTct30cHIPu1UqkozwPP8zA/P98jWpxTLBa1fL+0tDSo7wcDIAdfW1sLbVtRuXa7jVKp5FdOdINpmsqddHBwEMub5sAA5CSOj4+VvWuaJogIY2Nj2t/3rVYrLt/HA0BOxrZt5Spub2+DiFCv15W/79/e3uL0fTwAZB9Xq1U4jhPqYyFoY2MDj4+Pyu/5g94vEgOgOw+CRIZ1TFw3zMQAyMk1m81QCJ7n+TvM99fX10n4Pn4AjDEYhgHOOe7u7kLnQb8l3yorlUoSvo8fgJgHRIRarQbHceC6bt/r8xB9nwwAOdlGo6E9D1L0fXIA5KRPT0+1IAjL2LadtO+TBRBlHsi+r1arSfs+WQDyPJicnMT7+3vfeSD7fnV1NQ3fJw9AFrG+vt737wdCfLPZTMv36QCQxZydnQXOA2GN29tb/4KUgu/TAyDmgWEYeHh46BEtfO84Ttq+Tw+APA+mpqbw8fHhzwPRDY1GI23fpwsgaB58fn4CAE5OTobh+/QBBN0XLMsalu+HA0B43DAMmKaJ6enpHosMY+c/ksp/H5ADyAFkHgDLsH6Wd0AO4G8rZLH9f3YAy5r43AIBAFiWqv9bB7CsiFcRi1EVrjoD2CiLjyJw5P59/g/74CXK81cGmgAAAABJRU5ErkJggg==">
<style>
  :root {
    --bg:#F2F2F7; --surface:#FFFFFF; --fill:rgba(118,118,128,.12); --fill2:rgba(118,118,128,.2);
    --thumb:#FFFFFF; --ink:#1D1D1F; --ink2:#6E6E73; --sep:rgba(60,60,67,.14);
    --on-c:#FFFFFF; --bar:rgba(242,242,247,.72);
    --idle:#1D1D1F; --hear:#0071E3; --think:#C93400; --run:#248A3D; --bad:#D70015;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#000000; --surface:#1C1C1E; --fill:rgba(118,118,128,.24); --fill2:rgba(118,118,128,.36);
            --thumb:#636366; --ink:#F5F5F7; --ink2:#98989D; --sep:rgba(84,84,88,.55);
            --on-c:#000000; --bar:rgba(0,0,0,.72);
            --idle:#F5F5F7; --hear:#0A84FF; --think:#FF9F0A; --run:#30D158; --bad:#FF453A; }
  }
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--ink); letter-spacing:-.01em; -webkit-font-smoothing:antialiased;
         font:15px/1.47 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif; }
  button, select { font:inherit; color:inherit; letter-spacing:inherit; }
  :focus-visible { outline:3px solid color-mix(in srgb, var(--hear) 65%, transparent); outline-offset:2px; }

  /* frosted top bar */
  .bar { position:sticky; top:0; z-index:10; background:var(--bar); border-bottom:1px solid var(--sep);
         -webkit-backdrop-filter:saturate(180%) blur(20px); backdrop-filter:saturate(180%) blur(20px); }
  .bar-in { max-width:1600px; margin:0 auto; padding:0 24px; height:52px;
            display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .brand { display:flex; align-items:center; gap:12px; font-size:17px; font-weight:600; letter-spacing:-.022em; }
  /* Viam wordmark: a transparent mask tinted with the text color, so it follows light/dark */
  .wordmark { flex:none; width:calc(17px * 5.064); height:17px; background:var(--ink);
              -webkit-mask:url("data:image/png;base64,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") center / contain no-repeat;
              mask:url("data:image/png;base64,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") center / contain no-repeat; }
  .bdiv { width:1px; height:18px; background:var(--sep); }
  .prod { font-weight:600; color:var(--ink2); }
  @media (max-width:420px) { .bdiv, .prod { display:none; } }
  .bar-r { display:flex; align-items:center; gap:10px; }
  .logbtn { display:inline-flex; align-items:center; gap:6px; min-height:30px; padding:4px 12px; font-size:13px; }
  .logbtn svg { width:15px; height:15px; }
  .logbtn[aria-expanded=true], .logbtn[aria-expanded=true]:hover { background:var(--ink); color:var(--surface); }
  .logbtn .n { min-width:18px; padding:0 5px; border-radius:9px; background:var(--bad); color:#fff; font-size:11px; line-height:18px; }
  .logs { position:fixed; top:53px; right:0; bottom:0; z-index:20; width:min(480px, 100vw); display:flex; flex-direction:column;
          background:var(--surface); border-left:1px solid var(--sep); box-shadow:-16px 0 40px -20px rgba(0,0,0,.35); }
  .logs-hd { display:flex; align-items:center; gap:12px; padding:10px 10px 10px 16px; border-bottom:1px solid var(--sep); }
  .logs-hd h2 { flex:1; font-size:17px; }
  .logs-f { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--ink2); cursor:pointer; }
  .logx { width:34px; min-height:34px; padding:0; display:grid; place-items:center; }
  .logx svg { width:16px; height:16px; }
  .loglist { flex:1; overflow-y:auto; margin:0; padding:6px 0; list-style:none;
             font:12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; letter-spacing:0; }
  .loglist li { display:grid; grid-template-columns:auto 42px minmax(0,1fr); gap:8px; padding:3px 16px; }
  .loglist .lt { color:var(--ink2); font-variant-numeric:tabular-nums; }
  .loglist .lv { font-size:10px; font-weight:700; text-transform:uppercase; padding-top:1px; color:var(--ink2); }
  .loglist .warning .lv { color:var(--think); }
  .loglist .error .lv, .loglist .critical .lv { color:var(--bad); }
  .loglist .error, .loglist .critical { background:color-mix(in srgb, var(--bad) 8%, transparent); }
  .loglist .lm { white-space:pre-wrap; overflow-wrap:anywhere; }
  .cmdbody { flex:1; overflow-y:auto; padding:14px 16px 24px; }
  .cmdbody .lead { margin:0 0 4px; color:var(--ink2); font-size:14px; line-height:1.45; }
  .cmdbody h3 { margin:18px 0 8px; font-size:12px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; color:var(--ink2); }
  .cmdbody .chips { display:flex; flex-wrap:wrap; gap:6px; }
  .cmdbody .chip { padding:4px 11px; border-radius:999px; background:var(--fill); font-size:13px; }
  .cmdbody .chip.seen { background:color-mix(in srgb, var(--hear) 12%, var(--surface)); color:var(--hear); font-weight:600; }
  .cmdlist { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:12px; }
  .cmdlist b { display:block; margin-bottom:5px; font-size:14px; font-weight:600; }
  .loglist .empty { display:block; padding:16px; color:var(--ink2); font-family:-apple-system, system-ui, sans-serif; font-size:14px; }
  .live { display:flex; align-items:center; gap:7px; padding:5px 12px; border-radius:999px; background:var(--fill);
          font-size:13px; font-weight:600; color:var(--ink2); }
  .live .dot { width:8px; height:8px; border-radius:50%; background:var(--run); animation:blink 2s ease-in-out infinite; }
  .live.down .dot { background:var(--bad); animation:none; }
  @keyframes blink { 0%,100% { opacity:1 } 50% { opacity:.3 } }
  /* recording pill: red while a command is being filmed, then a link to the saved clip */
  .recpill { display:inline-flex; align-items:center; gap:7px; min-height:30px; padding:4px 12px; border-radius:999px;
             background:var(--fill); color:var(--ink2); font-size:13px; font-weight:600; text-decoration:none;
             font-variant-numeric:tabular-nums; white-space:nowrap; }
  .recpill[hidden] { display:none; }
  .recpill .rd { width:8px; height:8px; border-radius:50%; background:currentColor; flex:none; }
  .recpill.on { background:color-mix(in srgb, var(--bad) 14%, var(--surface)); color:var(--bad); }
  .recpill.on .rd { animation:blink 1.2s ease-in-out infinite; }
  .recpill .rs { font-weight:500; opacity:.8; }
  .recpill svg { width:13px; height:13px; flex:none; }
  a.recpill:hover { background:var(--fill2); color:var(--ink); }
  @media (max-width:560px) { .recpill .rs { display:none; } }

  /* one screen: mic and arms on the left, the camera on the right. On a screen big enough the page
     never scrolls (the panels shrink to fit); on a small one everything stacks and scrolls. */
  main { max-width:1600px; margin:0 auto; padding:16px 20px 20px; }
  .conn { margin:0 0 12px; padding:10px 16px; border-radius:14px; font-weight:500; color:var(--bad);
          background:color-mix(in srgb, var(--bad) 10%, var(--surface)); }
  .conn:empty { display:none; }
  .dash { display:grid; grid-template-columns:minmax(300px, 380px) minmax(0,1fr); gap:20px; align-items:start; }
  .left { display:flex; flex-direction:column; gap:20px; min-width:0; }
  .camsec { min-width:0; }
  @media (min-width:900px) and (min-height:620px) {
    body { height:100vh; height:100dvh; overflow:hidden; display:flex; flex-direction:column; }
    main { flex:1; min-height:0; width:100%; display:flex; flex-direction:column; }
    .dash { flex:1; min-height:0; align-items:stretch; }
    .left { min-height:0; }
    .arms { flex:1; min-height:0; display:flex; flex-direction:column; }
    .armlist { flex:1; min-height:0; }
    .arm { flex:1; min-height:0; }
    .arm-cam { flex:1; min-height:56px; }
    .camsec, .cams, .cam { min-height:0; display:flex; flex-direction:column; }
    .cams, .cam { flex:1; }
    .media { flex:1; min-height:120px; }
    .views { position:absolute; inset:0; container-type:size; }
    .view { flex:none; width:min(calc(50cqw - 1px), calc(100cqh * var(--ar))); }
    .views[data-view=color] .view, .views[data-view=depth] .view { width:min(100cqw, calc(100cqh * var(--ar))); }
    .objgrid { height:136px; min-height:0 !important; overflow-y:auto; align-content:start; }
  }
  @media not ((min-width:900px) and (min-height:620px)) {
    .dash { grid-template-columns:minmax(0,1fr); }
    .arm-cam { aspect-ratio:16/9; }
  }
  @media (max-width:560px) { main { padding:12px 12px 32px; } .bar-in { padding:0 16px; }
                             .views { flex-direction:column; } .view { width:100%; } }

  /* section headings */
  .sh { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:0 4px 10px; min-height:32px; }
  h2 { margin:0; font-size:19px; line-height:1.2; font-weight:700; letter-spacing:-.022em; }
  h3 { margin:0 16px 8px; font-size:15px; font-weight:600; }
  .aux { min-width:0; color:var(--ink2); font-size:13px; text-align:right;
         overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .foot { margin:8px 16px 0; max-width:68ch; color:var(--ink2); font-size:13px; line-height:1.45; }
  .msg { margin:8px 16px 0; font-size:13px; font-weight:500; color:var(--ink2); }
  .msg:empty { display:none; }
  .msg.ok { color:var(--run); } .msg.no { color:var(--bad); }
  .st { color:var(--ink2); font-size:13px; } .st.set { color:var(--run); }

  /* controls */
  button { border:0; border-radius:999px; padding:8px 16px; min-height:36px; cursor:pointer;
           font-size:14px; font-weight:600; background:var(--fill); color:var(--ink); transition:background .15s, opacity .15s; }
  button:hover { background:var(--fill2); }
  button.primary { background:var(--ink); color:var(--surface); }
  button.primary:hover { background:var(--ink); opacity:.82; }
  button:disabled { opacity:.4; cursor:default; }
  select { appearance:none; -webkit-appearance:none; border:0; border-radius:999px; min-height:36px; cursor:pointer;
           padding:8px 32px 8px 14px; font-size:14px; font-weight:600; background-color:var(--fill);
           background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%238E8E93' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
           background-repeat:no-repeat; background-position:right 13px center; }
  .btns { display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }

  /* the voice panel: the orb is the one loud thing on the page */
  .hero { position:relative; background:var(--surface); border-radius:22px; padding:16px 20px 14px;
          display:grid; grid-template-columns:104px minmax(0,1fr); column-gap:16px; align-items:center;
          grid-template-areas:"stage title" "stage sub" "steps steps"; }
  .hero > .stage { grid-area:stage; } .hero > h1 { grid-area:title; } .hero > .sub { grid-area:sub; }
  .hero > .steps { grid-area:steps; }
  .hero.flash { animation:flash 1.1s ease-out; }
  @keyframes flash { 0%   { box-shadow:0 0 0 0 transparent }
                     25%  { box-shadow:0 0 0 3px var(--hear), 0 0 48px 2px color-mix(in srgb, var(--hear) 45%, transparent) }
                     100% { box-shadow:0 0 0 0 transparent } }
  .stage { --c:var(--idle); position:relative; width:104px; height:104px; display:grid; place-items:center; }
  .orb { position:relative; z-index:1; width:64px; height:64px; border-radius:50%; background:var(--c); color:var(--on-c);
         display:grid; place-items:center; box-shadow:0 10px 22px -10px color-mix(in srgb, var(--c) 70%, transparent);
         transition:background .35s, box-shadow .35s; }
  .ring { position:absolute; width:64px; height:64px; border-radius:50%; border:1.5px solid var(--c); opacity:0; pointer-events:none; }
  .arc { position:absolute; width:80px; height:80px; border-radius:50%; border:2.5px solid transparent;
         border-top-color:var(--c); border-right-color:var(--c); opacity:0; }
  @keyframes sonar   { 0% { transform:scale(1); opacity:.5 } 100% { transform:scale(1.62); opacity:0 } }
  @keyframes breathe { 0%,100% { transform:scale(.96) } 50% { transform:scale(1) } }
  @keyframes spin    { to { transform:rotate(360deg) } }
  @keyframes eq      { 0%,100% { transform:scaleY(.25) } 50% { transform:scaleY(1) } }
  .waiting .ring { animation:sonar 3s cubic-bezier(.2,.6,.3,1) infinite; }
  .waiting .ring:nth-child(2) { animation-delay:1s; }
  .waiting .ring:nth-child(3) { animation-delay:2s; }
  .waiting .orb { animation:breathe 3s ease-in-out infinite; }
  .hearing .ring { animation:sonar 1.1s ease-out infinite; }
  .hearing .ring:nth-child(2) { animation-delay:.37s; }
  .hearing .ring:nth-child(3) { animation-delay:.74s; }
  .hearing .orb { transform:scale(1.06); }
  .processing .arc, .starting .arc, .offline .arc { opacity:1; animation:spin .9s linear infinite; }
  .running .ring { animation:sonar 1.6s ease-out infinite; }
  .running .ring:nth-child(2) { animation-delay:.8s; }
  .running .orb { animation:breathe 1.6s ease-in-out infinite; }
  .aura { position:absolute; width:64px; height:64px; border-radius:50%; background:var(--c);
          opacity:calc(var(--lv, 0) * .5); scale:calc(1 + var(--lv, 0) * .8); filter:blur(7px);
          transition:background .3s; pointer-events:none; }
  .waiting .aura, .starting .aura { background:var(--hear); opacity:calc(var(--lv, 0) * .65); }
  /* the orb doubles as the mic switch */
  .stage { cursor:pointer; border-radius:50%; -webkit-tap-highlight-color:transparent; }
  .stage:focus-visible { outline-offset:-6px; }
  .stage:hover .orb { filter:brightness(1.08); }
  .stage:active .orb { scale:.95; }
  .muted .orb { background:var(--fill2); color:var(--ink2); box-shadow:none; }
  .micbadge { position:absolute; z-index:2; left:50%; top:50%; translate:14px 14px; width:24px; height:24px;
              border-radius:50%; display:grid; place-items:center; background:var(--surface); color:var(--ink2);
              box-shadow:0 0 0 1.5px var(--sep), 0 1px 3px rgba(0,0,0,.12);
              opacity:0; scale:.6; transition:opacity .2s, scale .2s; pointer-events:none; }
  .micbadge svg { width:14px; height:14px; }
  .stage.off:not(.muted):not(.starting):not(.offline):not(.running):not(.processing) .micbadge { opacity:1; scale:1; }
  /* working with the mic off: the orb shows a crossed-out mic instead of sound bars */
  .stage.off.running .bars, .stage.off.processing .bars { display:none; }
  .stage.off.running .micoff, .stage.off.processing .micoff { display:block; }
  /* mic off while an arm works: the circle stays grey (its color always means the mic); the green
     rings pulsing around it show that something is happening */
  .stage.off.running .orb, .stage.off.processing .orb { background:var(--fill2); color:var(--ink2); box-shadow:none; }
  .stage.off.running .aura, .stage.off.processing .aura { opacity:0 !important; }
  .muted .aura { opacity:0 !important; }
  .muted.lvl .orb { scale:1; }
  .mictag { display:inline-flex; align-items:center; gap:6px; margin-right:6px; padding:1px 8px; border-radius:999px;
            font-size:12px; font-weight:600; vertical-align:1px; background:var(--fill); color:var(--ink2); }
  .mictag.on { background:color-mix(in srgb, var(--run) 16%, var(--surface)); color:var(--run); }
  .mictag i { width:6px; height:6px; border-radius:50%; background:currentColor; }
  .mictag.on i { animation:blink 1.4s ease-in-out infinite; }
  .stage.lvl .orb { scale:calc(1 + var(--lv, 0) * .12); }
  .stage.lvl .bars i { animation:none; transform:scaleY(var(--b, .2)); transition:transform .06s linear; }
  .ic { display:none; width:26px; height:26px; }
  .bars { display:none; gap:3.5px; align-items:center; height:26px; }
  .bars i { width:4px; height:100%; border-radius:2px; background:var(--on-c); animation:eq .9s ease-in-out infinite; }
  .bars i:nth-child(2) { animation-duration:.7s;  animation-delay:-.2s; }
  .bars i:nth-child(3) { animation-duration:1.1s; animation-delay:-.5s; }
  .bars i:nth-child(4) { animation-duration:.8s;  animation-delay:-.3s; }
  .bars i:nth-child(5) { animation-duration:1s;   animation-delay:-.7s; }
  .stage[data-icon=micoff] .micoff,
  .stage[data-icon=mic] .mic, .stage[data-icon=check] .check,
  .stage[data-icon=stop] .stop, .stage[data-icon=alert] .alert { display:block; }
  .stage[data-icon=bars] .bars { display:flex; }
  /* title and subtitle keep a fixed footprint (2 lines each) so state changes don't push the page around */
  h1 { margin:0; align-self:end; font-size:20px; line-height:1.2; font-weight:700; letter-spacing:-.022em; text-wrap:balance;
       height:2.4em; display:flex; align-items:flex-end; overflow:hidden; }
  .sub { align-self:start; margin-top:3px; height:2.8em; font-size:13.5px; line-height:1.4; color:var(--ink2);
         display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }

  /* Hey Viam -> Command -> Action */
  .steps { display:flex; align-items:flex-start; justify-content:center; margin:12px 0 0; padding-top:12px;
           border-top:1px solid var(--sep); }
  .step { flex:1; min-width:0; display:flex; flex-direction:column; align-items:center; gap:5px; }
  .step .dot { width:24px; height:24px; border-radius:50%; display:grid; place-items:center; font-size:12px; font-weight:700;
               background:var(--fill); color:var(--ink2); transition:background .3s, color .3s; }
  .step .lbl { font-size:13px; font-weight:600; }
  .step .val { max-width:100%; min-height:1.4em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
               font-size:13px; color:var(--ink2); }
  .link { flex:0 0 28px; align-self:flex-start; margin-top:11px; height:2px; border-radius:1px; background:var(--fill2); transition:background .3s; }
  .step.on .dot { color:var(--on-c); }
  .step.on.s-wake .dot { background:var(--hear); }  .link.on.l1 { background:var(--hear); }
  .step.on.s-cmd .dot  { background:var(--run); }   .link.on.l2 { background:var(--run); }
  .step.on.s-act .dot  { background:var(--run); }
  .step.bad .dot  { background:var(--bad); color:var(--on-c); }
  .step.warn .dot { background:var(--think); color:var(--on-c); }
  .step.active .dot { animation:breathe 1.2s ease-in-out infinite; }
  .step.pop .dot { animation:pop .6s cubic-bezier(.2,1.6,.4,1); }
  @keyframes pop { 0% { transform:scale(.6) } 60% { transform:scale(1.25) } 100% { transform:scale(1) } }

  /* live camera */
  .media { position:relative; overflow:hidden; border-radius:18px; background:#000; }
  @media (prefers-color-scheme: dark) { .media { box-shadow:0 0 0 1px var(--sep); } }
  /* each picture keeps the camera's shape (--ar), so the boxes drawn over it line up */
  .views { --ar:1.3333; display:flex; justify-content:center; align-items:center; gap:2px; }
  .views[data-view=color] [data-kind=depth], .views[data-view=depth] [data-kind=color] { display:none; }
  .view { position:relative; margin:0; flex:1 1 0; min-width:0; aspect-ratio:var(--ar); background:#0a0a0a;
          overflow:hidden; cursor:crosshair; }
  .view img { display:block; width:100%; height:100%; object-fit:contain; }
  .pill { position:absolute; display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px;
          background:rgba(0,0,0,.45); color:#fff; font-size:12px; font-weight:600; pointer-events:none;
          -webkit-backdrop-filter:blur(12px) saturate(160%); backdrop-filter:blur(12px) saturate(160%); }
  .view figcaption { top:12px; left:12px; }
  .view .wait { position:absolute; inset:0; display:grid; place-items:center; padding:20px; text-align:center;
                color:#c7c7cc; font-size:14px; pointer-events:none; }
  .livebadge { top:12px; right:12px; }
  .livebadge i { width:7px; height:7px; border-radius:50%; background:#8E8E93; }
  .livebadge.on i { background:#30D158; animation:blink 2s ease-in-out infinite; }
  .livebadge.bad i { background:#FF453A; }
  .livebadge .fps { font-weight:500; opacity:.75; }
  .livebadge .fps:empty { display:none; }
  .probe { position:absolute; width:0; height:0; pointer-events:none; }
  .probe::before, .probe::after { content:""; position:absolute; background:#fff; box-shadow:0 0 0 1px rgba(0,0,0,.5); }
  .probe::before { left:-9px; top:-1px; width:18px; height:2px; }
  .probe::after  { left:-1px; top:-9px; width:2px; height:18px; }
  .probe b { position:absolute; left:10px; top:8px; padding:3px 8px; border-radius:8px; white-space:nowrap;
             background:rgba(0,0,0,.72); color:#fff; font-size:13px; font-variant-numeric:tabular-nums; }
  .probe.flip b { left:auto; right:10px; }
  .camrow { display:flex; align-items:center; gap:12px; margin:10px 4px 0; min-height:34px; }
  .camrow .msg { flex:1; min-width:0; margin:0; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .camrow .msg:empty { display:block; }
  .camhd .seg button { padding:5px 14px; }
  .legend { display:flex; align-items:center; gap:8px; color:var(--ink2); font-size:13px; font-variant-numeric:tabular-nums; }
  .ramp { width:112px; height:8px; border-radius:4px;
          background:linear-gradient(90deg,#7a0403,#e4460a,#fbb938,#a4fc3c,#1ae4b6,#4686fb,#30123b); }
  .seg { display:inline-flex; padding:2px; border-radius:10px; background:var(--fill); }
  .seg button { min-height:30px; padding:5px 16px; border-radius:8px; background:transparent; font-weight:500; }
  .seg button:hover { background:transparent; color:var(--ink); }
  .seg button[aria-pressed=true] { background:var(--thumb); font-weight:600;
                                   box-shadow:0 1px 3px rgba(0,0,0,.12), 0 1px 1px rgba(0,0,0,.04); }

  /* things on the table */
  .ov { position:absolute; inset:0; pointer-events:none; }
  .box { position:absolute; border:1.5px dashed rgba(255,255,255,.6); border-radius:6px; pointer-events:auto; cursor:pointer;
         transition:border-color .15s, background .15s; }
  .box.hov { border-style:solid; border-color:#fff; }
  .box.sel { border:3px solid #0A84FF; background:rgba(10,132,255,.18); z-index:1;
             box-shadow:0 0 0 1px rgba(0,0,0,.35), 0 0 24px rgba(10,132,255,.55); }
  .box b { display:none; position:absolute; left:-3px; bottom:calc(100% + 6px); padding:3px 8px; border-radius:7px;
           white-space:nowrap; background:#0A84FF; color:#fff; font-size:12px; font-weight:600; }
  .box.sel b { display:block; }
  .box.low b { bottom:auto; top:calc(100% + 6px); }
  .objs { margin-top:12px; }
  .objs .sh { margin:0 4px 8px; }
  .objs h3 { margin:0; font-size:17px; font-weight:600; letter-spacing:-.02em; }
  /* compact 5-column grid of fixed-size tiles */
  .objgrid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:8px; transition:min-height .3s; }
  .objgrid .empty { grid-column:1 / -1; height:64px; display:grid; place-items:center; padding:0 16px; text-align:center;
                    border-radius:14px; background:var(--surface); color:var(--ink2); font-size:14px; }
  .tile { position:relative; display:flex; flex-direction:column; justify-content:center; gap:3px; min-width:0; height:64px;
          min-height:0; padding:8px 10px; border-radius:14px; background:var(--surface); text-align:left; font-weight:400;
          transition:opacity .4s, box-shadow .15s, background .15s; }
  .tile:hover { background:var(--surface); box-shadow:inset 0 0 0 1.5px var(--fill2); }
  .tile[aria-pressed=true] { background:color-mix(in srgb, var(--hear) 10%, var(--surface)); box-shadow:inset 0 0 0 2px var(--hear); }
  .tile .top { display:flex; align-items:center; gap:6px; min-width:0; }
  .tile .sw { flex:none; width:12px; height:12px; border-radius:4px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.14); }
  .tile .nm { min-width:0; font-size:13px; font-weight:600; letter-spacing:-.015em;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tile .st { padding-left:18px; font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
              font-variant-numeric:tabular-nums; }
  .tile.gone { opacity:.38; }                      /* not seen for a moment: fading, not yet removed */
  .tile.fresh { animation:tilein .35s ease-out; }
  @keyframes tilein { from { opacity:0; transform:scale(.94); } to { opacity:1; transform:none; } }
  @media (prefers-reduced-motion: reduce) { .tile.fresh { animation:none; } }


  /* arms: one card each, with a small live picture from the arm's own camera */
  .armlist { display:flex; flex-direction:column; gap:12px; }
  .arm { display:flex; flex-direction:column; gap:8px; padding:8px 8px 10px; background:var(--surface); border-radius:16px;
         min-width:0; box-shadow:inset 0 0 0 2px transparent; transition:box-shadow .3s; }
  .arm.busy { box-shadow:inset 0 0 0 2px color-mix(in srgb, var(--run) 60%, transparent); }
  .arm.ext  { box-shadow:inset 0 0 0 2px color-mix(in srgb, var(--think) 60%, transparent); }
  .arm-cam { position:relative; border-radius:10px; overflow:hidden; background:#0a0a0a; }
  .arm-cam img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; opacity:0; transition:opacity .3s; }
  .arm-cam.has img { opacity:1; }
  .arm-cam .nocam { position:absolute; inset:0; display:grid; place-items:center; color:#8E8E93; font-size:13px; }
  .arm-cam.has .nocam { display:none; }
  .arm-cam .nm { position:absolute; left:10px; bottom:8px; color:#fff; font-size:15px; font-weight:600; letter-spacing:-.015em;
                 text-shadow:0 1px 3px rgba(0,0,0,.6); }
  .arm-cam .nm::first-letter { text-transform:uppercase; }
  .arm-cam .apill { position:absolute; top:8px; right:8px; }
  .arm-cam::after { content:""; position:absolute; inset:auto 0 0; height:44px; pointer-events:none;
                    background:linear-gradient(transparent, rgba(0,0,0,.45)); }
  .arm-row { display:flex; align-items:center; gap:8px; min-height:30px; padding:0 4px; }
  .arm-do { flex:1; min-width:0; font-size:14px; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .arm-do.quiet { color:var(--ink2); font-weight:400; }
  .arm-row button { flex:none; min-height:28px; padding:3px 12px; font-size:13px; }
  .arm-btns { display:flex; gap:6px; flex:none; }
  .arm-btns button { display:inline-flex; align-items:center; gap:5px; }
  .arm-btns svg { width:13px; height:13px; }
  .arm-btns .release.on { background:var(--ink); color:var(--surface); }
  .stopall { display:inline-flex; align-items:center; gap:6px; min-height:32px; padding:5px 14px; font-size:13px;
             background:var(--bad); color:#fff; }
  .stopall:hover { background:var(--bad); filter:brightness(1.08); }
  .stopall:active { scale:.97; }
  .stopall svg { width:12px; height:12px; }
  .stopall.sent { filter:saturate(.4); }
  .joints { display:flex; justify-content:space-between; gap:4px; padding:0 4px; font-size:12px; color:var(--ink2);
            font-variant-numeric:tabular-nums; white-space:nowrap; }
  .joints b { color:var(--ink); font-weight:500; margin-left:3px; }
  .apill { display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:999px; white-space:nowrap;
           font-size:12px; font-weight:600; background:rgba(255,255,255,.92); color:#3A3A3C; }
  .apill i { width:7px; height:7px; border-radius:50%; background:currentColor; }
  .apill.run  { color:#fff; background:#248A3D; }
  .apill.ext  { color:#fff; background:#C93400; }
  .apill.bad  { color:#fff; background:#D70015; }
  .apill.run i, .apill.ext i { animation:blink 1s ease-in-out infinite; }

  /* click-to-grab bar over the camera */
  .grabbar { position:absolute; left:50%; bottom:12px; transform:translateX(-50%); z-index:2;
             display:flex; align-items:center; gap:10px; width:max-content; max-width:calc(100% - 24px);
             padding:6px 6px 6px 12px; border-radius:999px; background:rgba(0,0,0,.66); color:#fff;
             -webkit-backdrop-filter:blur(14px) saturate(160%); backdrop-filter:blur(14px) saturate(160%); }
  .grabbar.nogo { padding:8px 16px 8px 12px; }
  .gbsw { flex:none; width:16px; height:16px; border-radius:5px; box-shadow:inset 0 0 0 1px rgba(255,255,255,.35); }
  .gbtxt { min-width:0; display:flex; flex-direction:column; font-size:13px; line-height:1.3; }
  .gbtxt b { font-weight:600; }
  .gbtxt span { color:rgba(255,255,255,.78); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .grabbar .gbgo { flex:none; background:#fff; color:#000; }
  .grabbar .gbgo:hover { background:#fff; opacity:.85; }
  .grabbar .gbgo:disabled { opacity:.45; }

  @media (prefers-reduced-motion: reduce) {  @media (prefers-reduced-motion: reduce) {
    .ring, .orb, .arc, .bars i, .live .dot, .livebadge i, .step .dot, .hero, .apill i { animation:none !important; }
    .aura, .stage.lvl .orb { scale:1 !important; }
  }
</style></head>
<body>
<header class="bar"><div class="bar-in">
  <div class="brand"><span class="wordmark" role="img" aria-label="Viam"></span><span class="bdiv" aria-hidden="true"></span><span class="prod">Hey Viam</span></div>
  <div class="bar-r">
    <a id="recpill" class="recpill" hidden target="_blank" rel="noopener" role="status"></a>
    <button id="cmdbtn" class="logbtn" aria-expanded="false" aria-controls="cmdpanel" title="What you can say (C)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>Commands</button>
    <button id="logbtn" class="logbtn" aria-expanded="false" aria-controls="logs" title="Show the router's log (L)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h10"/></svg>Logs<span id="logn" class="n" hidden></span></button>
    <div id="live" class="live down" role="status"><span class="dot"></span><span id="livetxt">Connecting</span></div>
  </div>
</div></header>
<main>
  <div id="conn" class="conn" role="alert"></div>
  <div class="dash">
    <div class="left">
      <section class="hero" aria-label="Voice control">
        <div id="stage" class="stage starting" data-icon="mic" role="switch" tabindex="0" aria-checked="false"
             aria-label="Microphone" title="Turn the microphone on or off (M)">
          <span class="ring"></span><span class="ring"></span><span class="ring"></span>
          <span class="arc"></span>
          <span class="aura"></span>
          <div class="orb">
            <svg class="ic mic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v4"/><path d="M8 21h8"/></svg>
            <div class="bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
            <svg class="ic check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
            <svg class="ic stop" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
            <svg class="ic alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M12 6v8"/><path d="M12 18h.01"/></svg>
            <svg class="ic micoff" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 9.4V5a3 3 0 0 0-5.7-1.3"/><path d="M9 9v3a3 3 0 0 0 4.9 2.3"/><path d="M19 10a7 7 0 0 1-1.1 3.8M5 10a7 7 0 0 0 11.4 5.4"/><path d="M12 17v4"/><path d="M8 21h8"/><path d="M3 3l18 18"/></svg>
          </div>
          <span class="micbadge" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 9.4V5a3 3 0 0 0-5.7-1.3"/><path d="M9 9v3a3 3 0 0 0 4.9 2.3"/><path d="M19 10a7 7 0 0 1-1.1 3.8M5 10a7 7 0 0 0 11.4 5.4"/><path d="M12 17v4"/><path d="M3 3l18 18"/></svg></span>
        </div>
        <h1 id="title">Connecting…</h1>
        <div id="sub" class="sub"></div>
        <div class="steps" aria-live="polite">
          <div id="s1" class="step s-wake"><div class="dot">1</div><div class="lbl">Hey Viam</div><div class="val"></div></div>
          <div id="l1" class="link l1"></div>
          <div id="s2" class="step s-cmd"><div class="dot">2</div><div class="lbl">Command</div><div class="val"></div></div>
          <div id="l2" class="link l2"></div>
          <div id="s3" class="step s-act"><div class="dot">3</div><div class="lbl">Action</div><div class="val"></div></div>
        </div>
      </section>

      <section id="armsec" class="arms" aria-label="Arms" hidden>
        <div class="sh"><h2>Arms</h2>
          <button id="stopall" class="stopall" title="Halt every arm and gripper right now (Esc)">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>Stop all</button></div>
        <div id="arms" class="armlist"></div>
      </section>
    </div>

    <section id="camwrap" class="camsec" aria-label="Camera" hidden>
      <div class="sh camhd"><h2>Camera</h2><div id="camseg" class="seg" role="group" aria-label="Choose a camera"></div></div>
      <div id="cams" class="cams"></div>
    </section>
  </div>

  <template id="camtpl">
  <div class="cam">
    <div class="media">
      <div data-r="views" class="views" data-view="both">
        <figure class="view" data-kind="color">
          <img data-r="livecolor" alt="">
          <div class="ov" aria-hidden="true"></div><figcaption class="pill">Color</figcaption><div class="wait"></div><span class="probe" hidden><b></b></span>
        </figure>
        <figure class="view" data-kind="depth">
          <img data-r="livedepth" alt="">
          <div class="ov" aria-hidden="true"></div><figcaption class="pill">Depth</figcaption><div class="wait"></div><span class="probe" hidden><b></b></span>
        </figure>
      </div>
      <div data-r="livebadge" class="livebadge pill"><i></i><span data-r="livestate">Starting</span><span data-r="livefps" class="fps"></span></div>
      <div data-r="grabbar" class="grabbar" role="region" aria-label="Selected object" hidden>
        <span data-r="gbsw" class="gbsw"></span>
        <span class="gbtxt"><b data-r="gbname"></b><span data-r="gbwhy"></span></span>
        <button data-r="gbgo" class="gbgo" hidden>Grab</button>
      </div>
    </div>
    <div class="camrow">
      <div class="legend" aria-label="Depth color scale"><span data-r="lgnear">near</span><i class="ramp"></i><span data-r="lgfar">far</span></div>
      <div data-r="grabmsg" class="msg" role="status"></div>
      <div data-r="viewseg" class="seg" role="group" aria-label="Picture">
        <button data-view="both" aria-pressed="true">Both</button>
        <button data-view="color" aria-pressed="false">Color</button>
        <button data-view="depth" aria-pressed="false">Depth</button>
      </div>
    </div>
    <div class="objs">
      <div class="sh"><h3>On the table</h3><span data-r="objcount" class="aux"></span></div>
      <div data-r="objs" class="objgrid" role="list"><div class="empty">Waiting for the camera…</div></div>
    </div>
  </div>
  </template>
</main>
<aside id="cmdpanel" class="logs" aria-label="What you can say" hidden>
  <div class="logs-hd"><h2>What you can say</h2>
    <button id="cmdclose" class="logx" aria-label="Close the commands">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg></button>
  </div>
  <div id="cmdbody" class="cmdbody"></div>
</aside>
<aside id="logs" class="logs" aria-label="Logs" hidden>
  <div class="logs-hd"><h2>Logs</h2>
    <label class="logs-f"><input type="checkbox" id="logprob"> Problems only</label>
    <button id="logclose" class="logx" aria-label="Close the logs">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg></button>
  </div>
  <ol id="loglist" class="loglist" aria-live="off"></ol>
</aside>
<script>
const $ = id => document.getElementById(id);
const PHASES = {
  starting:   { c:"--idle",  i:"mic",   t:"Starting up…",            s:"Loading the speech model" },
  waiting:    { c:"--idle",  i:"mic",   t:"Listening for “Hey Viam”", s:"Say the wake word, then your command" },
  hearing:    { c:"--hear",  i:"bars",  t:"I’m listening…",           s:"Heard “Hey Viam” — say your command" },
  processing: { c:"--think", i:"bars",  t:"Understanding…",           s:"" },
  running:    { c:"--run",   i:"bars",  t:"Doing it",                 s:"Say “Hey Viam, stop” to halt" },
  done:       { c:"--run",   i:"check", t:"Done",                     s:"" },
  stopped:    { c:"--think", i:"stop",  t:"Stopped",                  s:"Arms halted where they are" },
  nomatch:    { c:"--bad",   i:"alert", t:"Didn’t catch a command",   s:"Try again: “Hey Viam”, then the command" },
  notfound:   { c:"--bad",   i:"alert", t:"Couldn’t find it",          s:"Check the block is on the table and in the camera’s view" },
  error:      { c:"--bad",   i:"alert", t:"Something went wrong",     s:"" },
  offline:    { c:"--bad",   i:"mic",   t:"Microphone not connected", s:"Retrying…" },
  muted:      { c:"--idle",  i:"micoff", t:"Mic is off",               s:"Tap the mic, then say a command" },
};
let lastPhase = "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[ch]));
const hhmm = ts => new Date(ts * 1000).toLocaleTimeString([], {hour:"numeric", minute:"2-digit", second:"2-digit"});
let lastCmds = "";

async function tick() {
  try {
    const r = await fetch("/state", {cache:"no-store"});
    const st = await r.json();
    $("conn").textContent = "";
    $("live").className = "live"; $("livetxt").textContent = "Live";
    if (routerDown) { routerDown = false; restartStreams(); }   // router came back: reopen the camera
    render(st);
  } catch (e) {
    $("conn").textContent = "Can’t reach the voice router — is viam-server running?";
    $("live").className = "live down"; $("livetxt").textContent = "Offline";
    routerDown = true;
  }
}

// ---- mic switch: tap the orb (or press M) to start/stop listening ----
let micPending = null;                   // optimistic value while the click is on its way
const micMode = st => (st.mic && st.mic.mode) || "wake_word";
const micOn = st => micPending !== null ? micPending : !!(st.mic && st.mic.on);
const TOGGLE_TEXT = {
  waiting: { t: "Listening", s: "Say a command, like “wave arm eighteen”" },
  hearing: { t: "I’m listening…", s: "Go ahead — I’ll act when you pause" },
  running: { s: "Say “stop” to halt" },
};
function micText(st, phase, key, dflt) {
  if (micMode(st) !== "toggle") return dflt;
  if (phase === "running" && !micOn(st) && key === "s")
    return st.mic && st.mic.stop_only ? "Mic’s off — it still hears “stop” while the arm moves"
                                      : "Tap the mic and say “stop” to halt it";
  return (TOGGLE_TEXT[phase] && TOGGLE_TEXT[phase][key]) || dflt;
}
let lastMicKey = "";
function renderMic(st, phase) {
  const stage = $("stage"), toggle = micMode(st) === "toggle", on = micOn(st);
  const key = [toggle, on, phase].join();
  if (key === lastMicKey) return;
  lastMicKey = key;
  stage.classList.toggle("off", toggle && !on);
  stage.setAttribute("role", toggle ? "switch" : "img");
  stage.tabIndex = toggle ? 0 : -1;
  stage.setAttribute("aria-checked", String(toggle && on));
  stage.setAttribute("aria-label", toggle ? (on ? "Microphone on — tap to turn off" : "Microphone off — tap to talk") : "Voice status");
  stage.title = toggle ? (on ? "Turn the microphone off (M)" : "Turn the microphone on (M)") : "";
  stage.style.cursor = toggle ? "pointer" : "default";
  document.querySelector("#s1 .lbl").textContent = toggle ? "You spoke" : "Hey Viam";
}
let lastState = null;
async function toggleMic() {
  if (!lastState || micMode(lastState) !== "toggle") return;
  const want = !micOn(lastState);
  micPending = want; lastMicKey = ""; lastPhase = "";
  render({ ...lastState, phase: want ? "waiting" : "muted" });   // instant feedback
  try {
    const r = await fetch("/action", {method:"POST", headers:{"Content-Type":"application/json"},
                                      body: JSON.stringify({action: "mic", on: want})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);
  } catch (e) {
    $("conn").textContent = "Couldn’t switch the microphone: " + e.message;
    setTimeout(() => { if ($("conn").textContent.startsWith("Couldn’t switch")) $("conn").textContent = ""; }, 4000);
  } finally { setTimeout(() => { micPending = null; lastMicKey = ""; }, 400); }
}
$("stage").addEventListener("click", toggleMic);
$("stage").addEventListener("keydown", ev => { if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); toggleMic(); } });
document.addEventListener("keydown", ev => {
  if ((ev.key === "m" || ev.key === "M") && !ev.metaKey && !ev.ctrlKey && !ev.altKey
      && !/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) toggleMic();
});

const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
let recKey = "";
function renderRec(recs) {
  // every camera being filmed (a voice command's camera, and cameras that film whenever an arm moves)
  const el = $("recpill");
  let html = "", href = null, cls = "recpill", title = "";
  const active = recs.filter(r => r.recording || r.state === "saving");
  if (active.length) {
    const secs = Math.max(...active.map(r => r.secs || 0));
    const saving = active.every(r => r.state === "saving");
    const still = active.every(r => r.still_for != null && r.still_for >= 1);
    const left = Math.max(...active.map(r => r.still_for != null ? Math.max(0, Math.ceil(r.idle_s - r.still_for)) : 0));
    const who = active.length === 1 ? (active[0].label || active[0].camera) : `${active.length} cameras`;
    const sub = [who, saving ? "saving" : still ? `stops in ${left}s` : ""].filter(Boolean).join(" · ");
    html = `<span class="rd"></span>REC ${mmss(secs)} <span class="rs">· ${esc(sub)}</span>`;
    cls += " on";
    title = "Recording " + active.map(r => r.label || r.camera).join(", ") +
            ` — each ends ${active[0].idle_s}s after the arms stop moving`;
  } else {
    const last = recs.map(r => ({ ...r.last, cam: r.label || r.camera }))
                     .filter(l => l.url && /\.mp4$/.test(l.url))
                     .sort((a, b) => String(b.started || "").localeCompare(String(a.started || "")))[0];
    if (last) {
      href = "/recordings/" + last.url.split("/").map(encodeURIComponent).join("/");
      html = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>Last clip <span class="rs">${mmss(last.seconds || 0)} · ${esc(last.cam)}</span>`;
      title = `Play ${last.url} (in the project's recordings folder)`;
    }
  }
  const key = cls + html + href;
  if (key === recKey) return;
  recKey = key;
  el.hidden = !html;
  el.className = cls; el.innerHTML = html; el.title = title;
  if (href) el.setAttribute("href", href); else el.removeAttribute("href");
}
function render(st) {
  lastState = st;
  if (micPending !== null && ["waiting", "muted"].includes(st.phase))     // a tap is on its way: trust it
    st = { ...st, phase: micPending ? "waiting" : "muted" };
  const phase = PHASES[st.phase] ? st.phase : "starting";
  const p = PHASES[phase];
  if (phase !== lastPhase) {  // only touch classes on change so animations don't restart
    lastPhase = phase;
    const stage = $("stage");
    stage.className = "stage " + phase;
    stage.dataset.icon = p.i;
    stage.style.setProperty("--c", `var(${p.c})`);
  }
  renderMic(st, phase);
  renderRec(st.recs || (st.rec ? [st.rec] : []));
  const title = ["running","notfound"].includes(st.phase) && st.current ? st.current
              : st.phase === "done" && st.current ? st.current : micText(st, phase, "t", p.t);
  if ($("title").textContent !== title) $("title").textContent = title;
  const sub = st.phase === "error" ? (st.last_error || "") : micText(st, phase, "s", p.s);
  const subHtml = micMode(st) === "toggle" && !["muted","starting","offline"].includes(phase)
      ? `<span class="mictag ${micOn(st) ? "on" : ""}"><i></i>${micOn(st) ? "Mic on" : "Mic off"}</span>${esc(sub)}` : esc(sub);
  if ($("sub").innerHTML !== subHtml) $("sub").innerHTML = subHtml;
  armsBusy = st.phase === "running";
  renderArms(st.arms || [], st.cams || []);
  renderCams(st);
  renderCommands(st);
  renderSteps(st);

}

// ---- Hey Viam -> Command -> Action tracker ----
// The router bumps counters when the wake word fires and when a command is understood; we light the
// steps on each bump and hold them so they're visible even if everything happens within a second.
const flow = { seen:null, wakeAt:0, cmdAt:0, cmd:null, lastEvent:0 };
const MIN_GAP = 700;   // ms between "Hey Viam" and "Command" lighting up, so both are noticeable
const RESET_AFTER = 7000;

function pop(id) { const el = $(id); el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop"); }
function flash() { const h = document.querySelector(".hero"); h.classList.remove("flash"); void h.offsetWidth; h.classList.add("flash"); }

function renderSteps(st) {
  const ev = st.events || {}, now = Date.now();
  if (flow.seen === null) { flow.seen = { wake: ev.wake_seq, cmd: ev.cmd_seq }; }  // first load: no replay
  if (ev.wake_seq !== flow.seen.wake) {
    flow.seen.wake = ev.wake_seq; flow.wakeAt = now; flow.cmd = null; flow.cmdAt = 0; flow.lastEvent = now;
    flash(); pop("s1");
  }
  if (ev.cmd_seq !== flow.seen.cmd) {
    flow.seen.cmd = ev.cmd_seq; flow.lastEvent = now;
    flow.cmd = { text: ev.cmd_text, matched: ev.cmd_matched, label: ev.cmd_label, typed: ev.cmd_typed };
    if (ev.cmd_typed) flow.wakeAt = 0;                                  // typed via simulate: no wake word
    flow.cmdAt = Math.max(now, flow.wakeAt ? flow.wakeAt + MIN_GAP : now);
  }
  const busy = ["hearing","processing","running"].includes(st.phase);
  if (!busy && flow.lastEvent && now - flow.lastEvent > RESET_AFTER) { flow.wakeAt = 0; flow.cmd = null; flow.cmdAt = 0; flow.lastEvent = 0; }

  // step 1: wake word
  const s1 = $("s1"), wakeOn = !!flow.wakeAt || st.phase === "hearing";
  s1.classList.toggle("on", wakeOn);
  s1.classList.toggle("active", wakeOn && !flow.cmd);
  s1.querySelector(".val").textContent = wakeOn ? (micMode(st) === "toggle" ? "Heard you" : "Heard")
                                               : (flow.cmd && flow.cmd.typed ? "Typed" : "");

  // step 2: command (held back until MIN_GAP after the wake word)
  const s2 = $("s2"), showCmd = flow.cmd && now >= flow.cmdAt;
  if (showCmd && !s2.dataset.shown) { s2.dataset.shown = "1"; pop("s2"); }
  if (!showCmd) delete s2.dataset.shown;
  s2.classList.toggle("on", !!(showCmd && flow.cmd.matched));
  s2.classList.toggle("bad", !!(showCmd && !flow.cmd.matched));
  s2.classList.toggle("active", !showCmd && wakeOn);
  s2.querySelector(".val").textContent = showCmd
      ? (flow.cmd.text ? `“${flow.cmd.text}”` : "didn’t catch it")
      : (wakeOn ? "listening…" : "");
  $("l1").classList.toggle("on", !!showCmd);

  // step 3: action
  const s3 = $("s3"), act = showCmd && flow.cmd.matched;
  // remember how the action ended, so the result stays put after the circle goes back to waiting
  if (act && ["running","error","stopped","done","notfound"].includes(st.phase)) flow.cmd.outcome = st.phase;
  const ph = act ? (flow.cmd.outcome || "running") : null;
  const bad = ph === "error" || ph === "notfound";
  s3.className = "step s-act" + (!act ? "" : ph === "running" ? " on active" : bad ? " bad"
               : ph === "stopped" ? " warn" : " on");
  s3.querySelector(".dot").textContent = !act ? "3" : ph === "running" ? "3" : bad ? "!" : ph === "stopped" ? "■" : "✓";
  s3.querySelector(".val").textContent = !act ? "" : ph === "running" ? (st.current || flow.cmd.label || "")
      : ph === "notfound" ? "not found" : ph === "error" ? "failed" : ph === "stopped" ? "stopped" : "done";
  $("l2").classList.toggle("on", !!act);
}

// ---- arms: one card per arm with a small live picture from its own camera ----
// Cards are built once per arm and then updated in place. Each card fetches its arm camera's color
// frames itself (no boxes or detection on these), one at a time, about twice a second.
let armsBusy = false;
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : "";
const nap = ms => new Promise(r => setTimeout(r, ms));
async function previewLoop(c) {
  const box = c.querySelector(".arm-cam"), img = box.querySelector("img");
  let seq = -1, url = null, cam = null;
  while (c.isConnected) {
    if (c.dataset.cam !== cam) { cam = c.dataset.cam; seq = -1; box.classList.remove("has"); }
    if (!cam || document.hidden) { await nap(600); continue; }
    try {
      const r = await fetch(`/live/frame?cam=${encodeURIComponent(cam)}&kind=color&have=${seq}`, {cache:"no-store"});
      if (r.status === 200) {
        seq = +(r.headers.get("X-Seq") || -1);
        const u = URL.createObjectURL(await r.blob()), prev = url;
        url = u; img.src = u; box.classList.add("has");
        if (prev) setTimeout(() => URL.revokeObjectURL(prev), 1000);
      }
    } catch (e) { await nap(1500); }
    await nap(450);
  }
  if (url) URL.revokeObjectURL(url);
}
function renderArms(arms, cams) {
  $("armsec").hidden = !arms.length;
  const wrap = $("arms");
  const cards = new Map([...wrap.querySelectorAll("article[data-arm]")].map(c => [c.dataset.arm, c]));
  const keep = new Set();
  let prev = null;
  for (const a of arms) {
    const k = a.resource || a.name; keep.add(k);
    let c = cards.get(k);
    const fresh = !c;
    if (!c) {
      c = document.createElement("article"); c.className = "arm"; c.dataset.arm = k;
      c.innerHTML = `<div class="arm-cam"><img alt=""><span class="nocam">No camera</span>
            <span class="nm"></span><span class="apill"><i></i><span class="pt"></span></span></div>
          <div class="arm-row"><span class="arm-do"></span><span class="arm-btns">
            <button class="reset" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>Reset</button>
            <button class="release" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 4v6l-3 3"/><path d="M16 4v6l3 3"/><path d="M12 14v6"/></svg>Release</button>
          </span></div>
          <div class="joints" aria-label="Joint angles"></div>`;
      c.querySelector(".release").addEventListener("click", ev => armAction(ev, "release", "let go"));
      c.querySelector(".reset").addEventListener("click", ev => armAction(ev, "reset", "reset"));
    }
    const cam = cams.find(x => x.arm && x.arm === a.name);
    c.dataset.cam = cam && !cam.error ? cam.name : "";
    const nocam = !cam ? "No camera" : cam.error ? "Camera not connected" : "Starting the camera…";
    const nc = c.querySelector(".nocam"); if (nc.textContent !== nocam) nc.textContent = nocam;
    c.querySelector(".arm-cam img").alt = `Live view from ${a.name}’s camera`;

    let cls = "", pill, line, quiet = false;
    if (!a.ok)          { cls = "off";  pill = ["bad", "Offline"]; line = a.error || "No reading"; quiet = true; }
    else if (a.action)  { cls = "busy"; pill = ["run", a.moving ? "Moving" : "Working"]; line = a.action; }
    else if (a.moving)  { cls = "ext";  pill = ["ext", "Moving"]; line = "Moving — not from a voice command"; }
    else                { pill = ["", "Idle"]; line = a.holding ? `Holding the ${a.holding}` : "Ready"; quiet = !a.holding; }
    const want = ["arm", cls].filter(Boolean).join(" ");
    if (c.className !== want) c.className = want;
    c.setAttribute("aria-label", a.name);
    const set = (sel, v) => { const el = c.querySelector(sel); if (el.textContent !== v) el.textContent = v; };
    set(".nm", a.name);
    const ap = c.querySelector(".apill"), apc = ("apill " + pill[0]).trim();
    if (ap.className !== apc) ap.className = apc;
    set(".pt", pill[1]);
    const ad = c.querySelector(".arm-do"), adc = "arm-do" + (quiet ? " quiet" : "");
    if (ad.className !== adc) ad.className = adc;
    set(".arm-do", line); if (ad.title !== line) ad.title = line;
    const busyNow = armsBusy || armBtnBusy || !a.ok;
    const rs = c.querySelector(".reset");
    rs.disabled = busyNow || !a.can_reset;
    rs.title = !a.can_reset ? `No starting position saved for ${a.name} yet`
             : `Send ${a.name} back to its starting position`;
    const rl = c.querySelector(".release");
    rl.hidden = !a.has_gripper;
    rl.disabled = busyNow;
    rl.classList.toggle("on", !!a.holding);
    rl.title = a.holding ? `Open ${a.name}’s gripper and let go of the ${a.holding}` : `Open ${a.name}’s gripper`;
    const j = c.querySelector(".joints");
    j.hidden = !(a.ok && a.joints);
    if (a.ok && a.joints) {
      if (j.children.length !== a.joints.length)
        j.innerHTML = a.joints.map((_, i) => `<span>J${i + 1}<b></b></span>`).join("");
      a.joints.forEach((v, i) => { const b = j.children[i].querySelector("b"), t = `${Math.round(v)}°`;
                                   if (b.textContent !== t) b.textContent = t; });
    }
    const after = prev ? prev.nextSibling : wrap.firstChild;
    if (c !== after) wrap.insertBefore(c, after);
    prev = c;
    if (fresh) previewLoop(c);
  }
  cards.forEach((c, k) => { if (!keep.has(k)) c.remove(); });
}
let armBtnBusy = false;
async function post(body) {
  const r = await fetch("/action", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!j.ok) throw new Error(j.error);
  return j;
}
async function armAction(ev, action, verb) {
  const b = ev.currentTarget, card = b.closest("article[data-arm]"), line = card.querySelector(".arm-do");
  armBtnBusy = true; card.querySelectorAll(".arm-btns button").forEach(x => x.disabled = true);
  try { await post({action, arm: card.dataset.arm}); }
  catch (e) { line.textContent = `Couldn’t ${verb}: ` + e.message; line.title = line.textContent; }
  finally { armBtnBusy = false; }
}
async function stopAll() {
  const b = $("stopall");
  b.classList.add("sent");
  try { await post({action: "stop"}); }
  catch (e) { $("conn").textContent = "Couldn’t stop: " + e.message;
              setTimeout(() => { if ($("conn").textContent.startsWith("Couldn’t stop")) $("conn").textContent = ""; }, 5000); }
  finally { setTimeout(() => b.classList.remove("sent"), 300); }
}
$("stopall").addEventListener("click", stopAll);
document.addEventListener("keydown", ev => {   // Esc halts the arms (unless it's closing a panel)
  if (ev.key !== "Escape" || ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey) return;
  if (!$("logs").hidden || !$("cmdpanel").hidden) return;
  stopAll();
}, true);   // capture: checked before a panel's own Esc handler closes it

// ---- live sound level: the router streams mic loudness; we smooth it and drive the orb ----
let lvTarget = 0, lv = 0, lvLive = false;
const es = new EventSource("/levels");   // reconnects on its own if the router restarts
es.onmessage = e => {
  if (e.data === "off") { lvLive = false; lvTarget = 0; }
  else { lvLive = true; lvTarget = Math.max(0, Math.min(1, parseFloat(e.data) || 0)); }
};
es.onerror = () => { lvLive = false; lvTarget = 0; };
const BAR_SHAPE = [.55, .85, 1, .8, .6];
const bars = Array.from(document.querySelectorAll(".bars i"));
function frame(t) {
  lv += (lvTarget - lv) * (lvTarget > lv ? .4 : .12);   // quick attack, gentle release
  const stage = $("stage");
  const live = lvLive && (!lastState || micMode(lastState) !== "toggle" || micOn(lastState));
  stage.classList.toggle("lvl", live);
  stage.style.setProperty("--lv", live ? lv.toFixed(3) : "0");
  if (live) bars.forEach((b, i) => {
    const wobble = .75 + .5 * Math.abs(Math.sin(t / 110 + i * 1.7));
    b.style.setProperty("--b", Math.max(.18, Math.min(1, (.15 + lv * 1.0) * BAR_SHAPE[i] * wobble)).toFixed(3));
  });
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---- live cameras: one panel per camera (the table camera and each arm's wrist camera) ----
// Each panel shows its camera's color and depth pictures (two MJPEG streams, opened only while the panel
// is on screen), reads the distance under the pointer, lists what it sees on the table, and lets you send
// an arm to grab something.
let routerDown = false, grabBusy = false, camOrder = null;
const CAMS = new Map();   // camera name -> panel
const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
const fmtMM = mm => mm == null ? "no reading" : mm < 1000 ? `${Math.round(mm / 10)} cm` : `${(mm / 1000).toFixed(2)} m`;

// The detector runs ~5x a second and is noisy: the same cup can come back with a new id, flip between
// "White object" and "Gray object", or drop out for a frame. So instead of drawing its raw list we keep
// *tracks*: each detection is matched to the nearest existing track, a track only appears once it's been
// seen twice, shows its most common recent label, and fades for a moment before it's removed. Things with a
// recognizable name ("White cup", "Red block") are pinned to the top left, generic "… object"s come after;
// within each group tiles stay in the order things first showed up, left to right.
const SHOW_AFTER_HITS = 2, STALE_MS = 700, KEEP_MS = 1800, MATCH_PX = 40, LABEL_WINDOW = 8;
const fmtCm = mm => mm == null ? "?" : mm < 1000 ? `${Math.max(1, Math.round(mm / 10))} cm` : `${(mm / 1000).toFixed(1)} m`;
function shortSize(o) {
  const [l, w, h] = (o.size_mm || [0, 0, 0]).map(x => Math.max(1, Math.round(x / 10)));
  return o.tall ? `${w}×${h} cm` : `${l}×${w} cm`;
}
const fmtDist = mm => mm == null ? "" : mm < 1000 ? `${Math.round(mm / 10)} cm` : `${(mm / 1000).toFixed(1)} m`;
function longSize(o) {
  const [l, w, h] = (o.size_mm || [0, 0, 0]).map(x => Math.max(1, Math.round(x / 10)));
  return o.tall ? `About ${w} cm wide, ${h} cm tall` : `About ${l} × ${w} cm`;
}
function mode(arr) {                       // most frequent value, ties go to the latest
  const n = new Map(); let best = arr[arr.length - 1], bn = 0;
  for (const v of arr) { const c = (n.get(v) || 0) + 1; n.set(v, c); if (c >= bn) { bn = c; best = v; } }
  return best;
}
const isSeen = (t, now) => now - t.lastSeen <= STALE_MS;

function restartStreams() { CAMS.forEach(p => p.restart()); }
document.addEventListener("visibilitychange", () => CAMS.forEach(p => p.setStreams()));
document.addEventListener("keydown", ev => { if (ev.key === "Escape") CAMS.forEach(p => p.clearSel()); });

let camSel = null;
try { camSel = localStorage.getItem("cam"); } catch (e) {}
const shortCam = c => (c.label || c.name).replace(/\s+camera$/i, "");
function showCam() {
  CAMS.forEach((p, n) => { const on = n === camSel; if (p.sec.hidden === on) p.sec.hidden = !on; if (!on) p.clearSel(); });
  $("camseg").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.cam === camSel)));
}
$("camseg").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-cam]");
  if (!b || b.dataset.cam === camSel) return;
  camSel = b.dataset.cam;
  try { localStorage.setItem("cam", camSel); } catch (e) {}
  showCam();
});

function renderCams(st) {
  const cams = st.cams || (st.live ? [{ ...st.live, name: st.live.camera }] : []);
  const order = JSON.stringify(cams.map(c => [c.name, c.label || ""]));
  if (order !== camOrder) {   // first load, or the camera list changed in the config: rebuild the panels
    camOrder = order;
    CAMS.forEach(p => p.destroy());
    CAMS.clear();
    $("cams").innerHTML = "";
    for (const c of cams) { const p = makeCam(c); p.sec.hidden = true; CAMS.set(c.name, p); $("cams").appendChild(p.sec); }
    $("camseg").innerHTML = cams.map(c => `<button data-cam="${esc(c.name)}" aria-pressed="false">${esc(shortCam(c))}</button>`).join("");
    $("camseg").hidden = cams.length < 2;
    if (!cams.some(c => c.name === camSel)) camSel = cams.length ? cams[0].name : null;
    showCam();
  }
  $("camwrap").hidden = !cams.length;
  for (const c of cams) CAMS.get(c.name).render(c, st.commands || []);
}

function makeCam(info) {
  const name = info.name, title = info.label || "Camera", enc = encodeURIComponent(name);
  const sec = $("camtpl").content.firstElementChild.cloneNode(true);
  const R = n => sec.querySelector(`[data-r="${n}"]`);
  const imgs = { color: R("livecolor"), depth: R("livedepth") };
  sec.setAttribute("aria-label", title);
  imgs.color.alt = `Live color view from ${title}`;
  imgs.depth.alt = `Live depth view from ${title}: warm colors are closer, cool colors are farther away`;
  R("objs").setAttribute("aria-label", `Things ${title} sees on the table`);

  // ---- the two pictures ----
  // Frames are fetched one at a time (not as MJPEG streams that each hold a connection open): with
  // several cameras, streams would use up the browser's six connections to this page and stall it.
  let avail = false, liveOk = false, onScreen = false, view = "both", lastSize = "", dead = false, looping = false;
  try { view = localStorage.getItem("liveView:" + name) || localStorage.getItem("liveView") || "both"; } catch (e) {}
  const seqs = { color: -1, depth: -1 }, urls = { color: null, depth: null };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const watching = () => avail && onScreen && !document.hidden && !dead;
  const wanted = kind => watching() && (view === "both" || view === kind);
  async function fetchFrame(kind) {
    const r = await fetch(`/live/frame?cam=${enc}&kind=${kind}&have=${seqs[kind]}`, {cache: "no-store"});
    if (r.status !== 200) return false;
    seqs[kind] = +(r.headers.get("X-Seq") || -1);
    const url = URL.createObjectURL(await r.blob()), prev = urls[kind];
    urls[kind] = url;
    imgs[kind].src = url;
    if (prev) setTimeout(() => URL.revokeObjectURL(prev), 1000);
    return true;
  }
  async function frameLoop() {
    if (looping) return;
    looping = true;
    try {
      while (watching()) {
        const kinds = ["color", "depth"].filter(wanted);
        if (!kinds.length) break;
        const t0 = performance.now();
        try { await Promise.all(kinds.map(fetchFrame)); }
        catch (e) { await sleep(1500); }          // router restarting: try again shortly
        await sleep(Math.max(40, 120 - (performance.now() - t0)));
      }
    } finally { looping = false; }
  }
  function setStreams() { if (watching()) frameLoop(); }
  function restart() { seqs.color = seqs.depth = -1; setStreams(); }
  // only stream (and so only make the router pull frames) while the panel is on screen
  const io = new IntersectionObserver(es => { onScreen = es[es.length - 1].isIntersecting; setStreams(); },
                                      { rootMargin: "200px 0px" });
  io.observe(R("views"));

  function applyView() {
    R("views").dataset.view = view;
    R("viewseg").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", b.dataset.view === view));
    setStreams();
  }
  R("viewseg").addEventListener("click", ev => {
    const b = ev.target.closest("button[data-view]");
    if (!b) return;
    view = b.dataset.view;
    try { localStorage.setItem("liveView:" + name, view); } catch (e) {}
    applyView();
  });

  function renderLive(lv) {
    if (!avail) { avail = true; setStreams(); }
    const cam = lv.camera || name;
    liveOk = !!lv.ok;
    const streaming = lv.ok && !lv.paused && !lv.error;
    R("livebadge").className = "livebadge pill" + (lv.error ? " bad" : streaming ? " on" : "");
    R("livestate").textContent = lv.error ? "Not connected" : !lv.ok ? "Starting" : lv.paused ? "Paused" : "Live";
    R("livefps").textContent = streaming ? `${lv.fps} fps` : "";
    const iv = lv.fps > 0 ? 1000 / lv.fps : 0;
    staleMs = Math.max(STALE_MS, 1.6 * iv); keepMs = Math.max(KEEP_MS, 3.5 * iv);
    const waitTxt = lv.error ? `Can’t get pictures from ${cam}: ${lv.error}` : lv.ok ? "" : "Starting the camera…";
    sec.querySelectorAll(".view .wait").forEach(w => { w.textContent = waitTxt; w.hidden = !waitTxt; });
    if (lv.range_mm) { R("lgnear").textContent = fmtMM(lv.range_mm[0]); R("lgfar").textContent = fmtMM(lv.range_mm[1]); }
    if (lv.has_color === false && view !== "depth") { view = "depth"; applyView(); }
    R("viewseg").hidden = lv.has_color === false;
    const sz = JSON.stringify(lv.size || null);
    if (sz !== lastSize && lv.size) {
      lastSize = sz;
      R("views").style.setProperty("--ar", (lv.size[0] / lv.size[1]).toFixed(4));
    }
  }

  // ---- hover readout: same spot marked on both pictures (color and depth are aligned) ----
  let probeAt = null, probeBusy = false;
  async function readProbe() {
    if (!probeAt || probeBusy) return;
    probeBusy = true;
    const at = probeAt;
    try {
      const r = await fetch(`/live/depth_at?cam=${enc}&x=${at.x.toFixed(4)}&y=${at.y.toFixed(4)}`, {cache:"no-store"});
      const j = await r.json();
      if (probeAt) sec.querySelectorAll(".probe b").forEach(b => b.textContent = fmtMM(j.mm));
    } catch (e) {} finally { probeBusy = false; }
  }
  R("views").addEventListener("pointermove", ev => {
    const fig = ev.target.closest(".view");
    if (!fig || !liveOk) return;
    const img = fig.querySelector("img"), r = img.getBoundingClientRect();
    const nw = img.naturalWidth || 4, nh = img.naturalHeight || 3;   // the picture's box inside object-fit:contain
    const s = Math.min(r.width / nw, r.height / nh), w = nw * s, h = nh * s;
    const x = (ev.clientX - r.left - (r.width - w) / 2) / w, y = (ev.clientY - r.top - (r.height - h) / 2) / h;
    if (x < 0 || x > 1 || y < 0 || y > 1) { hideProbe(); return; }
    probeAt = {x, y};
    sec.querySelectorAll(".view").forEach(f => {
      const p = f.querySelector(".probe"), fi = f.querySelector("img").getBoundingClientRect();
      p.style.left = ((fi.width - w) / 2 + x * w) + "px"; p.style.top = ((fi.height - h) / 2 + y * h) + "px";
      p.classList.toggle("flip", x > .7); p.hidden = false;
    });
    readProbe();
  });
  function hideProbe() { probeAt = null; sec.querySelectorAll(".probe").forEach(p => p.hidden = true); }
  R("views").addEventListener("pointerleave", hideProbe);
  const probeTimer = setInterval(() => { if (probeAt) readProbe(); }, 300);   // keep the number fresh while hovering still

  // ---- things on the table: tracks, tiles and boxes ----
  const TR = { list: [], seq: 0, lastFrame: null };
  // "not seen" / "gone" timeouts stretch with the camera's frame rate: a wrist camera may only manage
  // a frame every couple of seconds, and things shouldn't fade out between its frames
  let staleMs = STALE_MS, keepMs = KEEP_MS;
  const seen_ = (t, now) => now - t.lastSeen <= staleMs;
  let selKey = null, hovKey = null, objSize = [640, 480], gridRows = 1, gridRowsUntil = 0;
  function updateTracks(objs, now) {
    const frame = JSON.stringify(objs.map(o => [o.id, o.uv]));
    if (frame === TR.lastFrame) return;      // same detections as last poll: nothing new to learn
    TR.lastFrame = frame;
    const free = new Set(TR.list);
    // nearest-first matching so two neighbours don't steal each other's track
    const pairs = [];
    for (const o of objs) for (const t of free) {
      const d = Math.hypot(t.obj.uv[0] - o.uv[0], t.obj.uv[1] - o.uv[1]) + (t.obj.color === o.color ? 0 : 10);
      if (d <= MATCH_PX) pairs.push([d, o, t]);
    }
    pairs.sort((a, b) => a[0] - b[0]);
    const used = new Set();
    for (const [, o, t] of pairs) {
      if (used.has(o) || !free.has(t)) continue;
      used.add(o); free.delete(t);
      t.obj = o; t.lastSeen = now; t.hits++;
      t.labels.push([o.label, o.swatch || ""]); if (t.labels.length > LABEL_WINDOW) t.labels.shift();
    }
    for (const o of objs) if (!used.has(o)) {
      TR.list.push({ key: ++TR.seq, obj: o, firstSeen: now, lastSeen: now, hits: 1, labels: [[o.label, o.swatch || ""]],
                     order: o.uv[0] });
    }
    TR.list = TR.list.filter(t => now - t.lastSeen <= keepMs);
  }
  const named = t => !/\bobject$/i.test(mode(t.labels.map(x => x[0])) || "");
  const shownTracks = () => TR.list.filter(t => t.hits >= SHOW_AFTER_HITS)
                                   .sort((a, b) => (named(b) - named(a)) || a.order - b.order || a.key - b.key);
  const trackByKey = k => TR.list.find(t => t.key === k) || null;

  function renderObjects(lv, commands) {
    const grid = R("objs"), now = Date.now();
    if (lv && lv.size) objSize = lv.size;
    updateTracks((lv && lv.ok && !lv.error && lv.objects) || [], now);
    const tracks = shownTracks();
    if (selKey !== null && !trackByKey(selKey)) selKey = null;   // it left the table
    if (hovKey !== null && !trackByKey(hovKey)) hovKey = null;
    const seenN = tracks.filter(t => seen_(t, now)).length;
    const nm = lv && lv.naming, cnt = seenN ? `${seenN} ${seenN === 1 ? "item" : "items"}` : "";
    const nmTxt = !nm || nm.state === "ready" ? "" : nm.state === "error" ? "Naming unavailable" : "Setting up names…";
    const ocText = [cnt, nmTxt].filter(Boolean).join(" · ");
    if (R("objcount").textContent !== ocText) R("objcount").textContent = ocText;
    R("objcount").title = nm && nm.detail ? nm.detail : "";

    const empty = !lv ? "" : lv.error ? "The camera isn’t connected."
                : lv.detect_error ? `Can’t check the table: ${lv.detect_error}`
                : !lv.ok || lv.table == null ? "Waiting for the camera…"
                : lv.table === false ? "Can’t find the table surface. Point the camera at the table."
                : !tracks.length ? "Nothing on the table right now." : "";

    // hold the grid's height for a few seconds when it would shrink, so one missing tile doesn't
    // collapse a row and yank the page up
    const cols = getComputedStyle(grid).gridTemplateColumns.split(" ").length || 5;
    const rows = Math.max(1, Math.ceil((empty ? 1 : tracks.length) / cols));
    if (rows >= gridRows) { gridRows = rows; gridRowsUntil = now + 4000; }
    else if (now > gridRowsUntil) { gridRows = rows; }
    grid.style.minHeight = `${gridRows * 64 + (gridRows - 1) * 8}px`;

    let em = grid.querySelector(".empty");
    if (empty) {
      if (!em) { em = document.createElement("div"); em.className = "empty"; grid.prepend(em); }
      if (em.textContent !== empty) em.textContent = empty;
    } else if (em) em.remove();

    // keyed update: tiles (and keyboard focus) survive every refresh; they're only added/removed when a
    // track really appears or leaves
    const tiles = new Map([...grid.querySelectorAll(".tile")].map(b => [b.dataset.key, b]));
    const keep = new Set();
    let prev = em && empty ? em : null;
    for (const t of empty ? [] : tracks) {
      const k = String(t.key); keep.add(k);
      let b = tiles.get(k);
      if (!b) {
        b = document.createElement("button");
        b.className = "tile fresh"; b.dataset.key = k; b.setAttribute("role", "listitem");
        b.innerHTML = `<span class="top"><span class="sw"></span><span class="nm"></span></span><span class="st"></span>`;
        b.addEventListener("animationend", () => b.classList.remove("fresh"), { once: true });
      }
      const o = t.obj, [label, swatch] = mode(t.labels.map(x => x.join("\u0000"))).split("\u0000");
      const phrase = o.grab && o.grab.arm && !/ object$/i.test(label) ? `grab the ${label.toLowerCase()}` : "";
      const voice = !!phrase;
      const seen = seen_(t, now);
      const setText = (sel, v) => { const el = b.querySelector(sel); if (el.textContent !== v) el.textContent = v; };
      setText(".nm", label);
      setText(".st", [shortSize(o), fmtDist(o.dist_mm)].filter(Boolean).join(" · "));
      b.querySelector(".sw").style.background = swatch || "var(--fill2)";
      b.classList.toggle("gone", !seen);
      b.setAttribute("aria-pressed", String(t.key === selKey));
      const tip = `${label}. ${longSize(o)}, ${fmtCm(o.dist_mm)} from the camera.` + (voice ? ` Say “Hey Viam, ${phrase}”.` : "")
                + (seen ? "" : " (Not seen for a moment.)");
      if (b.title !== tip) { b.title = tip; b.setAttribute("aria-label", tip); }
      const after = prev ? prev.nextSibling : grid.firstChild;
      if (b !== after) grid.insertBefore(b, after);
      prev = b;
    }
    tiles.forEach((b, k) => { if (!keep.has(k)) b.remove(); });
    drawBoxes();
  }

  function drawBoxes() {
    const [W, H] = objSize, now = Date.now();
    const html = shownTracks().filter(t => seen_(t, now)).map(t => {
      const o = t.obj, label = mode(t.labels.map(x => x[0]));
      const [x0, y0, x1, y1] = o.bbox, pad = 4;
      const l = Math.max(0, x0 - pad) / W * 100, tp = Math.max(0, y0 - pad) / H * 100;
      const w = (Math.min(W, x1 + pad) - Math.max(0, x0 - pad)) / W * 100, h = (Math.min(H, y1 + pad) - Math.max(0, y0 - pad)) / H * 100;
      const cls = "box" + (t.key === selKey ? " sel" : "") + (t.key === hovKey ? " hov" : "") + (y0 < H * .12 ? " low" : "");
      return `<div class="${cls}" data-key="${t.key}" style="left:${l.toFixed(2)}%;top:${tp.toFixed(2)}%;width:${w.toFixed(2)}%;height:${h.toFixed(2)}%"><b>${esc(label)}</b></div>`;
    }).join("");
    sec.querySelectorAll(".view .ov").forEach(ov => { if (ov.innerHTML !== html) ov.innerHTML = html; });
  }

  function pickObj(key) {
    selKey = selKey === key ? null : key;
    sec.querySelectorAll(".tile").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.key === selKey)));
    drawBoxes();
    updateGrabBar();
  }
  function clearSel() { if (selKey !== null) pickObj(selKey); }
  R("objs").addEventListener("click", ev => { const b = ev.target.closest(".tile"); if (b) pickObj(+b.dataset.key); });
  R("views").addEventListener("click", ev => { const b = ev.target.closest(".box"); if (b) pickObj(+b.dataset.key); });
  R("objs").addEventListener("pointerover", ev => {
    const b = ev.target.closest(".tile"); const k = b ? +b.dataset.key : null;
    if (k !== hovKey) { hovKey = k; drawBoxes(); }
  });
  R("objs").addEventListener("pointerleave", () => { hovKey = null; drawBoxes(); });

  // ---- click a thing, then "Grab with arm N" ----
  // The table camera sends the closest calibrated arm; a wrist camera sends the arm it's mounted on.
  function updateGrabBar() {
    const t = selKey === null ? null : trackByKey(selKey);
    const o = t ? t.obj : null;
    const bar = R("grabbar");
    bar.hidden = !o;
    if (!o) return;
    const visible = seen_(t, Date.now());
    const [label, swatch] = mode(t.labels.map(x => x.join("\u0000"))).split("\u0000");
    R("gbsw").style.background = swatch || "var(--fill2)";
    if (R("gbname").textContent !== label) R("gbname").textContent = label;
    const g = o.grab, go = R("gbgo"), can = !!(g && g.arm);
    go.hidden = !can;
    bar.classList.toggle("nogo", !can);
    const why = !g ? "Grabbing isn’t set up for this camera" : g.why ? `Can’t grab: ${g.why}`
              : !visible ? "Not in view right now" : armsBusy ? "Wait — an arm is busy"
              : g.via === "wrist" ? `Seen by ${cap(g.arm)}’s own camera` : `${cap(g.arm)} is closest`;
    if (R("gbwhy").textContent !== why) R("gbwhy").textContent = why;
    if (can) { const txt = `Grab with ${cap(g.arm)}`; if (go.textContent !== txt) go.textContent = txt; }
    go.disabled = grabBusy || armsBusy || !visible;
  }
  R("gbgo").addEventListener("click", async () => {
    const t = selKey === null ? null : trackByKey(selKey);
    if (!t || grabBusy) return;
    grabBusy = true; CAMS.forEach(p => p.updateGrabBar());
    const m = R("grabmsg"); m.className = "msg"; m.textContent = "Sending the arm…";
    try {
      const r = await fetch("/action", {method:"POST", headers:{"Content-Type":"application/json"},
                                        body: JSON.stringify({action:"grab_object", id: t.obj.id, cam: name})});
      const j = await r.json();
      if (!j.ok) throw new Error(j.error);
      m.className = "msg ok"; m.textContent = `${j.result.started}. Say “Hey Viam, stop” to halt it.`;
    } catch (e) {
      m.className = "msg no"; m.textContent = "Couldn’t grab it: " + e.message;
    } finally { grabBusy = false; CAMS.forEach(p => p.updateGrabBar()); }
  });

  function render(lv, commands) { renderLive(lv); renderObjects(lv, commands); updateGrabBar(); }
  function destroy() {
    dead = true; io.disconnect(); clearInterval(probeTimer);
    imgs.color.src = BLANK; imgs.depth.src = BLANK;
    Object.values(urls).forEach(u => u && URL.revokeObjectURL(u));
  }
  applyView();
  return { sec, render, setStreams, restart, clearSel, updateGrabBar, destroy };
}

// ---- commands drawer: what you can say, including "grab the …" for things the picking arm sees now ----
let cmdOpen = false, lastCmdKey = "";
const EXAMPLES = ["pick up the white cup", "get the red ball", "grab the yellow one", "drop it", "put it down",
                  "go back to the start", "reset arm 17", "wave arm 18", "stop"];
function renderCommands(st) {
  if (!cmdOpen || !st) return;
  const cam = (st.cams || []).find(c => c.arm && (c.objects || []).some(o => o.grab && o.grab.arm));
  const seen = cam ? [...new Set(cam.objects.filter(o => o.grab && o.grab.arm && !/ object$/i.test(o.label))
                                            .map(o => o.label.toLowerCase()))].slice(0, 10) : [];
  const key = JSON.stringify([st.command_list, seen, micMode(st)]);
  if (key === lastCmdKey) return;
  lastCmdKey = key;
  const chip = (p, cls = "") => `<span class="chip ${cls}">${esc(p)}</span>`;
  $("cmdbody").innerHTML =
    `<p class="lead">${micMode(st) === "toggle" ? "Tap the mic, then say it." : "Say “Hey Viam”, then the command."}
       Exact words aren’t needed: say what you want, like the examples below.</p>` +
    (seen.length ? `<h3>${esc(cap(cam.arm))} can see</h3><div class="chips">${seen.map(l => chip("grab the " + l, "seen")).join("")}</div>` : "") +
    `<h3>Also understood</h3><div class="chips">${EXAMPLES.map(e => chip(e)).join("")}</div>` +
    `<h3>Set phrases</h3><ul class="cmdlist">${(st.command_list || []).map(c =>
       `<li><b>${esc(c.label)}</b><div class="chips">${c.phrases.map(p => chip(p)).join("")}</div></li>`).join("")}</ul>`;
}
function setCmdsOpen(open) {
  cmdOpen = open;
  $("cmdpanel").hidden = !open;
  $("cmdbtn").setAttribute("aria-expanded", String(open));
  if (open && LOG.open) setLogsOpen(false);
  lastCmdKey = "";
  renderCommands(lastState);
}
$("cmdbtn").addEventListener("click", () => setCmdsOpen(!cmdOpen));
$("cmdclose").addEventListener("click", () => { setCmdsOpen(false); $("cmdbtn").focus(); });
document.addEventListener("keydown", ev => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
  if (ev.key === "c" || ev.key === "C") setCmdsOpen(!cmdOpen);
  else if (ev.key === "Escape" && cmdOpen) setCmdsOpen(false);
});

// ---- logs drawer: the router's recent log lines, opened from the top bar (or press L) ----
// Polled once a second while open; while closed, a red count on the button says new errors came in.
const LOG = { open: false, seq: 0, rows: [], problems: false, unseen: 0, timer: null };
try { LOG.open = localStorage.getItem("logsOpen") === "1"; LOG.problems = localStorage.getItem("logsProblems") === "1"; } catch (e) {}
const isProblem = r => r.level === "warning" || r.level === "error" || r.level === "critical";
async function pollLogs() {
  try {
    const r = await fetch(`/logs?after=${LOG.seq}`, {cache: "no-store"});
    const j = await r.json();
    if (j.seq < LOG.seq) { LOG.seq = 0; LOG.rows = []; return pollLogs(); }   // the router restarted
    if (j.logs.length) {
      LOG.rows.push(...j.logs); LOG.rows = LOG.rows.slice(-500);
      if (!LOG.open && LOG.seq) LOG.unseen += j.logs.filter(x => x.level === "error" || x.level === "critical").length;
    }
    LOG.seq = j.seq;
    if (LOG.open && (j.logs.length || !$("loglist").children.length)) drawLogs();
    const n = $("logn"); n.hidden = !LOG.unseen; n.textContent = LOG.unseen > 99 ? "99+" : String(LOG.unseen);
  } catch (e) {}
}
function drawLogs() {
  const list = $("loglist");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const rows = LOG.rows.filter(r => !LOG.problems || isProblem(r));
  list.innerHTML = rows.length
    ? rows.map(r => `<li class="${esc(r.level)}"><span class="lt">${hhmm(r.t)}</span><span class="lv">${r.level === "warning" ? "warn" : esc(r.level)}</span><span class="lm">${esc(r.msg)}</span></li>`).join("")
    : `<li class="empty">${LOG.problems ? "No warnings or errors." : "Nothing logged yet."}</li>`;
  if (atBottom) list.scrollTop = list.scrollHeight;
}
function setLogsOpen(open) {
  LOG.open = open;
  $("logs").hidden = !open;
  $("logbtn").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("logsOpen", open ? "1" : "0"); } catch (e) {}
  if (open && cmdOpen) setCmdsOpen(false);
  if (open) { LOG.unseen = 0; drawLogs(); const l = $("loglist"); l.scrollTop = l.scrollHeight; }
  pollLogs();
}
$("logbtn").addEventListener("click", () => setLogsOpen(!LOG.open));
$("logclose").addEventListener("click", () => { setLogsOpen(false); $("logbtn").focus(); });
$("logprob").checked = LOG.problems;
$("logprob").addEventListener("change", ev => {
  LOG.problems = ev.target.checked;
  try { localStorage.setItem("logsProblems", LOG.problems ? "1" : "0"); } catch (e) {}
  drawLogs();
});
document.addEventListener("keydown", ev => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
  if (ev.key === "l" || ev.key === "L") setLogsOpen(!LOG.open);
  else if (ev.key === "Escape" && LOG.open) setLogsOpen(false);
});
setLogsOpen(LOG.open);
setInterval(pollLogs, 1000);

tick(); setInterval(tick, 250);
</script></body></html>
"""
