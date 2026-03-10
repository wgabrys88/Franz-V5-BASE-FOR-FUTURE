import base64
import http.server
import json
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _Cfg:
    panel_host: str = "127.0.0.1"
    panel_port: int = 1236
    vlm_host: str = "127.0.0.1"
    vlm_port: int = 1235
    panel_url: str = "http://127.0.0.1:1236"
    vlm_url: str = "http://127.0.0.1:1235/v1/chat/completions"
    panel_ready_url: str = "http://127.0.0.1:1236/ready"
    panel_events_url: str = "http://127.0.0.1:1236/events"
    panel_completions_url: str = "http://127.0.0.1:1236/v1/chat/completions"
    panel_result_url: str = "http://127.0.0.1:1236/result"
    startup_timeout: float = 10.0
    vlm_response_delay: float = 0.3
    report_path: str = "test_mock_vlm.json"
    log_path: str = "franz-log.jsonl"


CFG = _Cfg()
HERE = Path(__file__).resolve().parent
PANEL_PY = HERE / "panel.py"
PANEL_HTML = HERE / "panel.html"
WIN32_PY = HERE / "win32.py"

_vlm_lock = threading.Lock()
_vlm_requests: list[dict[str, Any]] = []

_sse_lock = threading.Lock()
_sse_events: list[dict[str, Any]] = []

_pump_skip: set[str] = set()
_pump_skip_lock = threading.Lock()

_test_results: list[dict[str, Any]] = []
_interactive_region: str = ""


def _make_png(w: int, h: int, r: int, g: int, b: int) -> bytes:
    raw = bytearray()
    for _ in range(h):
        raw.append(0)
        raw.extend(bytes([r, g, b, 255]) * w)

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 1))
        + chunk(b"IEND", b"")
    )


_PNG_BYTES = _make_png(8, 8, 80, 80, 80)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")
_IMG_URL = f"data:image/png;base64,{_PNG_B64}"

_BLACK64_BYTES = _make_png(64, 64, 0, 0, 0)
_BLACK64_B64 = base64.b64encode(_BLACK64_BYTES).decode("ascii")
_BLACK64_URL = f"data:image/png;base64,{_BLACK64_B64}"


def _png_dims(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


def _decode_png_pixels(b64: str) -> list[tuple[int, int, int, int]]:
    raw = base64.b64decode(b64)
    assert raw[:4] == b"\x89PNG"
    w, h = _png_dims(raw)
    idat = b""
    i = 8
    while i < len(raw):
        length = struct.unpack(">I", raw[i : i + 4])[0]
        chunk_type = raw[i + 4 : i + 8]
        chunk_data = raw[i + 8 : i + 8 + length]
        if chunk_type == b"IDAT":
            idat += chunk_data
        i += 12 + length
    raw_pixels = zlib.decompress(idat)
    pixels: list[tuple[int, int, int, int]] = []
    stride = w * 4 + 1
    for row in range(h):
        base = row * stride + 1
        for col in range(w):
            o = base + col * 4
            pixels.append((raw_pixels[o], raw_pixels[o + 1], raw_pixels[o + 2], raw_pixels[o + 3]))
    return pixels


class _FakeVlm(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            req: dict[str, Any] = json.loads(body)
        except Exception:
            req = {}
        with _vlm_lock:
            _vlm_requests.append({"ts": time.time(), "raw": req})
        delay: float = getattr(self.server, "delay", CFG.vlm_response_delay)
        override: str | None = getattr(self.server, "next_text", None)
        time.sleep(delay)
        text = override if override is not None else f"[mock] step={len(_vlm_requests)}"
        resp = json.dumps({
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", ""),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _start_vlm(delay: float = CFG.vlm_response_delay) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer((CFG.vlm_host, CFG.vlm_port), _FakeVlm)
    srv.delay = delay  # type: ignore[attr-defined]
    srv.next_text = None  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _start_panel() -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, str(PANEL_PY)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(HERE),
    )
    deadline = time.time() + CFG.startup_timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(CFG.panel_ready_url, timeout=1) as r:
                if r.status == 200:
                    return proc
        except Exception:
            pass
        time.sleep(0.15)
    proc.terminate()
    raise RuntimeError("panel did not become ready")


def _open_chrome() -> subprocess.Popen[bytes]:
    for p in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]:
        if Path(p).exists():
            return subprocess.Popen(
                [p, "--new-window", CFG.panel_url + "/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    raise RuntimeError("Chrome not found")


def _sse_reader(stop: threading.Event) -> None:
    try:
        with urllib.request.urlopen(urllib.request.Request(CFG.panel_events_url), timeout=None) as resp:
            buf = b""
            while not stop.is_set():
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                if buf.endswith(b"\n\n"):
                    raw = buf.decode("utf-8", errors="replace").strip()
                    buf = b""
                    ev_type, data_str = "message", ""
                    for line in raw.splitlines():
                        if line.startswith("event:"):
                            ev_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()
                    if data_str:
                        try:
                            parsed = json.loads(data_str)
                        except Exception:
                            parsed = {"raw": data_str}
                        with _sse_lock:
                            _sse_events.append({"event": ev_type, "data": parsed, "ts": time.time()})
    except Exception:
        pass


def _wait_sse(
    ev_type: str,
    pred: Any = None,
    timeout: float = 20.0,
    after_ts: float = 0.0,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    seen = 0
    while time.time() < deadline:
        with _sse_lock:
            current = list(_sse_events)
        for ev in current[seen:]:
            seen += 1
            if ev["ts"] < after_ts:
                continue
            if ev["event"] == ev_type and (pred is None or pred(ev["data"])):
                return ev
        time.sleep(0.04)
    return None


def _post(url: str, payload: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post_result(rid: str, b64: str) -> None:
    try:
        _post(CFG.panel_result_url, {"request_id": rid, "annotated_b64": b64})
    except Exception:
        pass


def _pump(stop: threading.Event) -> None:
    seen: set[str] = set()
    while not stop.is_set():
        with _sse_lock:
            evs = list(_sse_events)
        for ev in evs:
            if ev["event"] == "annotate":
                rid = ev["data"].get("request_id", "")
                if rid and rid not in seen:
                    with _pump_skip_lock:
                        skip = rid in _pump_skip
                    if not skip:
                        seen.add(rid)
                        raw = ev["data"].get("raw_b64", "") or _PNG_B64
                        threading.Thread(target=_post_result, args=(rid, raw), daemon=True).start()
        time.sleep(0.04)


def _brain(
    agent: str,
    model: str = "mock-model",
    image_url: str = "",
    text: str = "",
    actions: list[dict[str, Any]] | None = None,
    region: str = "",
    capture_size: list[int] | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": text or f"turn from {agent}"},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    if actions:
        content.append({"type": "actions", "actions": actions})
    return _post(CFG.panel_completions_url, {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 64,
        "stream": False,
        "agent": agent,
        "region": region,
        "capture_size": capture_size or [64, 64],
        "messages": [
            {"role": "system", "content": "You are a test agent."},
            {"role": "user", "content": content},
        ],
    }, timeout=timeout)


def _win32(*args: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WIN32_PY)] + list(args),
        capture_output=True, text=True, timeout=timeout,
    )


def _cursor_norm(region: str = "") -> tuple[int, int] | None:
    cmd = [sys.executable, str(WIN32_PY), "cursor_pos"]
    if region:
        cmd += ["--region", region]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            x, y = p.stdout.strip().split(",")
            return int(x), int(y)
    except Exception:
        pass
    return None


def _read_log() -> list[dict[str, Any]]:
    p = HERE / CFG.log_path
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _log_size() -> int:
    p = HERE / CFG.log_path
    return p.stat().st_size if p.exists() else 0


def _run(name: str):
    def dec(fn: Any) -> Any:
        def wrapper() -> None:
            t0 = time.time()
            try:
                fn()
                r = {"name": name, "passed": True, "detail": "", "duration_ms": round((time.time() - t0) * 1000, 1)}
            except AssertionError as e:
                r = {"name": name, "passed": False, "detail": str(e), "duration_ms": round((time.time() - t0) * 1000, 1)}
            except Exception as e:
                r = {"name": name, "passed": False, "detail": f"EXCEPTION: {e}", "duration_ms": round((time.time() - t0) * 1000, 1)}
            _test_results.append(r)
            tag = "PASS" if r["passed"] else "FAIL"
            print(f"  [{tag}] {name}" + (f" — {r['detail']}" if r["detail"] else ""))
        wrapper.__test_name__ = name  # type: ignore[attr-defined]
        return wrapper
    return dec


# ── PHASE 1: Panel infrastructure ────────────────────────────────────────────

@_run("panel_ready_returns_ok")
def t_panel_ready() -> None:
    with urllib.request.urlopen(CFG.panel_ready_url, timeout=5) as r:
        assert json.loads(r.read()).get("ok") is True


@_run("panel_serves_html_with_eventsource")
def t_panel_html() -> None:
    with urllib.request.urlopen(CFG.panel_url + "/", timeout=5) as r:
        body = r.read()
    assert b"EventSource" in body and b"/events" in body and b"Franz" in body


@_run("panel_404_on_unknown_path")
def t_panel_404() -> None:
    try:
        urllib.request.urlopen(CFG.panel_url + "/no-such-path", timeout=5)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


@_run("panel_400_on_malformed_json")
def t_panel_400() -> None:
    req = urllib.request.Request(
        CFG.panel_completions_url, data=b"{bad json",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


@_run("sse_connected_event_on_subscribe")
def t_sse_connected() -> None:
    ev = _wait_sse("connected", timeout=8.0)
    assert ev is not None, "no connected SSE event"


@_run("result_404_on_unknown_request_id")
def t_result_404() -> None:
    try:
        _post(CFG.panel_result_url, {"request_id": "nonexistent-xyz", "annotated_b64": ""})
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


@_run("result_400_on_malformed_json")
def t_result_400() -> None:
    req = urllib.request.Request(
        CFG.panel_result_url, data=b"notjson",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


# ── PHASE 2: panel.html static contract verification ─────────────────────────

@_run("panel_html_contains_renderable_annotate_function")
def t_html_render_annotated() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "renderAnnotated" in src, "renderAnnotated function missing"
    assert "OffscreenCanvas" in src, "OffscreenCanvas missing"
    assert "drawPolygonOn" in src, "drawPolygonOn missing"


@_run("panel_html_annotate_event_posts_result_back")
def t_html_posts_result() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "fetch('/result'" in src, "fetch('/result') call missing"
    assert "annotated_b64" in src, "annotated_b64 field missing in fetch body"
    assert "request_id" in src, "request_id field missing in fetch body"


@_run("panel_html_vlm_done_event_updates_pane_text")
def t_html_vlm_done_handler() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "vlm_done" in src, "vlm_done event listener missing"
    assert "p.res.textContent" in src, "pane text update missing"
    assert "annotated_b64" in src, "annotated_b64 in vlm_done handler missing"


@_run("panel_html_multi_agent_pane_creation_logic_present")
def t_html_pane_creation() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "getOrCreatePane" in src, "getOrCreatePane missing"
    assert "gridTemplateColumns" in src, "grid column update missing"
    assert "agentColor" in src, "agentColor assignment missing"
    assert "panes.set" in src, "panes map set missing"


@_run("panel_html_sse_error_clears_live_dot")
def t_html_sse_error_handler() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "es.onerror" in src, "SSE onerror handler missing"
    assert "dot.className" in src, "dot className update missing"


@_run("panel_html_history_chip_appended_on_vlm_done")
def t_html_history_chip() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "chip" in src, "chip element missing"
    assert "p.hist.appendChild" in src, "hist appendChild missing"
    assert "cdur" in src, "duration chip class missing"


@_run("panel_html_catch_block_falls_back_to_raw_b64_on_render_error")
def t_html_catch_fallback() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "} catch {" in src or "catch(" in src, "no catch block in annotate handler"
    assert src.count("fetch('/result'") >= 2, "catch fallback fetch('/result') missing"


@_run("panel_html_overlay_points_normalized_by_NORM_constant")
def t_html_norm_constant() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "NORM" in src, "NORM constant missing"
    assert "/NORM" in src, "NORM division missing in coordinate mapping"


# ── PHASE 3: /result bridge contract ─────────────────────────────────────────

@_run("result_bridge_custom_annotated_b64_forwarded_to_vlm")
def t_result_bridge_custom_b64() -> None:
    t0 = time.time()
    rid_box: list[str] = []
    done = threading.Event()
    resp_box: list[dict[str, Any]] = []

    custom_b64 = base64.b64encode(_make_png(8, 8, 200, 100, 50)).decode("ascii")

    def _watcher() -> None:
        ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-bridge", after_ts=t0)
        if ev:
            rid = ev["data"].get("request_id", "")
            if rid:
                rid_box.append(rid)
                with _pump_skip_lock:
                    _pump_skip.add(rid)

    def _sender() -> None:
        resp_box.append(_brain("ag-bridge", image_url=_IMG_URL))
        done.set()

    wt = threading.Thread(target=_watcher, daemon=True)
    st = threading.Thread(target=_sender, daemon=True)
    wt.start()
    st.start()
    wt.join(timeout=16.0)

    assert rid_box, "no annotate event for ag-bridge"
    rid = rid_box[0]
    time.sleep(0.1)
    assert not done.is_set(), "panel returned before /result posted"

    with _vlm_lock:
        before = len(_vlm_requests)

    _post_result(rid, custom_b64)
    assert done.wait(timeout=15.0), "panel did not unblock after /result"

    with _vlm_lock:
        new_reqs = _vlm_requests[before:]
    assert new_reqs, "no new VLM request after /result"

    vlm_img_b64 = ""
    for msg in new_reqs[-1]["raw"].get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:image/png;base64,"):
                        vlm_img_b64 = url.split(",", 1)[1]

    assert vlm_img_b64 == custom_b64, "VLM did not receive the custom annotated_b64 from /result"
    with _pump_skip_lock:
        _pump_skip.discard(rid)


# ── PHASE 4: 64x64 black PNG pixel-level travel tests ────────────────────────

@_run("black_64x64_png_all_pixels_are_black")
def t_black_png_pixels() -> None:
    pixels = _decode_png_pixels(_BLACK64_B64)
    assert len(pixels) == 64 * 64, f"expected 4096 pixels, got {len(pixels)}"
    non_black = [(i, p) for i, p in enumerate(pixels) if p[:3] != (0, 0, 0)]
    assert not non_black, f"{len(non_black)} pixels are not black in source image"


@_run("black_64x64_travels_untouched_through_panel_no_overlay")
def t_black_travels_untouched() -> None:
    t0 = time.time()
    rid_box: list[str] = []
    done = threading.Event()
    resp_box: list[dict[str, Any]] = []

    def _watcher() -> None:
        ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-black-travel", after_ts=t0)
        if ev:
            rid = ev["data"].get("request_id", "")
            raw = ev["data"].get("raw_b64", "")
            if rid:
                rid_box.append(rid)
                with _pump_skip_lock:
                    _pump_skip.add(rid)
                _post_result(rid, raw)

    def _sender() -> None:
        resp_box.append(_brain("ag-black-travel", image_url=_BLACK64_URL))
        done.set()

    wt = threading.Thread(target=_watcher, daemon=True)
    st = threading.Thread(target=_sender, daemon=True)
    wt.start()
    st.start()
    wt.join(timeout=16.0)
    assert done.wait(timeout=20.0), "panel did not complete"

    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-black-travel", after_ts=t0)
    assert ev_d is not None, "no vlm_done event"
    ann_b64 = ev_d["data"].get("annotated_b64", "")
    assert ann_b64, "annotated_b64 empty"

    pixels = _decode_png_pixels(ann_b64)
    non_black = [p for p in pixels if p[:3] != (0, 0, 0)]
    assert not non_black, f"{len(non_black)} pixels changed — image was modified without overlay"

    with _pump_skip_lock:
        _pump_skip.discard(rid_box[0] if rid_box else "")


@_run("black_64x64_with_red_overlay_has_non_black_pixels_in_annotated")
def t_black_with_overlay_has_red_pixels() -> None:
    t0 = time.time()
    overlay = {
        "type": "overlay",
        "points": [[100, 100], [900, 100], [900, 900], [100, 900]],
        "stroke": "#ff0000",
        "stroke_width": 20,
        "closed": True,
    }
    threading.Thread(
        target=_brain, args=("ag-black-overlay",),
        kwargs={"image_url": _BLACK64_URL, "actions": [overlay]},
        daemon=True,
    ).start()
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-black-overlay", after_ts=t0)
    assert ev_d is not None, "no vlm_done event"
    ann_b64 = ev_d["data"].get("annotated_b64", "")
    assert ann_b64, "annotated_b64 empty"
    assert ann_b64 != _BLACK64_B64, "annotated_b64 identical to raw — overlay not rendered"
    pixels = _decode_png_pixels(ann_b64)
    non_black = [p for p in pixels if p[:3] != (0, 0, 0)]
    assert non_black, "all pixels still black after red overlay — OffscreenCanvas did not render"


@_run("black_64x64_overlay_pixel_count_proportional_to_stroke_width")
def t_overlay_pixel_count_scales_with_stroke() -> None:
    t0_thin = time.time()
    thin_overlay = {
        "type": "overlay",
        "points": [[500, 0], [500, 1000]],
        "stroke": "#ff0000",
        "stroke_width": 2,
        "closed": False,
    }
    threading.Thread(
        target=_brain, args=("ag-thin-stroke",),
        kwargs={"image_url": _BLACK64_URL, "actions": [thin_overlay]},
        daemon=True,
    ).start()
    ev_thin = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-thin-stroke", after_ts=t0_thin)
    assert ev_thin is not None, "no vlm_done for thin stroke"
    thin_b64 = ev_thin["data"].get("annotated_b64", "")

    t0_thick = time.time()
    thick_overlay = {
        "type": "overlay",
        "points": [[500, 0], [500, 1000]],
        "stroke": "#ff0000",
        "stroke_width": 30,
        "closed": False,
    }
    threading.Thread(
        target=_brain, args=("ag-thick-stroke",),
        kwargs={"image_url": _BLACK64_URL, "actions": [thick_overlay]},
        daemon=True,
    ).start()
    ev_thick = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-thick-stroke", after_ts=t0_thick)
    assert ev_thick is not None, "no vlm_done for thick stroke"
    thick_b64 = ev_thick["data"].get("annotated_b64", "")

    thin_non_black = len([p for p in _decode_png_pixels(thin_b64) if p[:3] != (0, 0, 0)])
    thick_non_black = len([p for p in _decode_png_pixels(thick_b64) if p[:3] != (0, 0, 0)])
    assert thick_non_black > thin_non_black, (
        f"thick stroke ({thick_non_black} px) not more than thin ({thin_non_black} px)"
    )


@_run("black_64x64_fill_overlay_covers_majority_of_pixels")
def t_fill_overlay_covers_majority() -> None:
    t0 = time.time()
    fill_overlay = {
        "type": "overlay",
        "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
        "fill": "rgba(255,0,0,0.9)",
        "stroke": "#ff0000",
        "stroke_width": 1,
        "closed": True,
    }
    threading.Thread(
        target=_brain, args=("ag-fill-overlay",),
        kwargs={"image_url": _BLACK64_URL, "actions": [fill_overlay]},
        daemon=True,
    ).start()
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-fill-overlay", after_ts=t0)
    assert ev_d is not None, "no vlm_done"
    ann_b64 = ev_d["data"].get("annotated_b64", "")
    assert ann_b64, "annotated_b64 empty"
    pixels = _decode_png_pixels(ann_b64)
    non_black = len([p for p in pixels if p[:3] != (0, 0, 0)])
    total = len(pixels)
    assert non_black > total * 0.5, f"fill overlay only changed {non_black}/{total} pixels"


@_run("black_64x64_raw_b64_in_annotate_event_matches_sent_image")
def t_black_raw_b64_in_annotate() -> None:
    t0 = time.time()
    threading.Thread(
        target=_brain, args=("ag-black-raw",),
        kwargs={"image_url": _BLACK64_URL},
        daemon=True,
    ).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-black-raw", after_ts=t0)
    assert ev is not None, "no annotate event"
    assert ev["data"].get("raw_b64") == _BLACK64_B64, "raw_b64 in annotate event does not match sent image"


# ── PHASE 5: Single-agent flow ────────────────────────────────────────────────

@_run("single_agent_annotate_sse_event_fired")
def t_single_annotate() -> None:
    t0 = time.time()
    threading.Thread(target=_brain, args=("ag-single",), kwargs={"image_url": _IMG_URL}, daemon=True).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-single", after_ts=t0)
    assert ev is not None, "no annotate event"
    assert ev["data"].get("request_id"), "missing request_id"


@_run("single_agent_vlm_done_sse_event_fired")
def t_single_vlm_done() -> None:
    ev = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-single", timeout=25.0)
    assert ev is not None, "no vlm_done event"
    text = ev["data"].get("text", "")
    assert text and not text.startswith("ERROR:"), f"bad text: {text!r}"


@_run("single_agent_http_response_valid_openai_shape")
def t_single_http() -> None:
    resp = _brain("ag-http", image_url=_IMG_URL)
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "")
    assert content and not content.startswith("ERROR:"), f"bad content: {content!r}"


@_run("panel_strips_agent_region_capture_size_from_vlm_request")
def t_strips_custom_fields() -> None:
    with _vlm_lock:
        reqs = list(_vlm_requests)
    assert reqs, "no VLM requests recorded"
    for req in reqs:
        raw = req["raw"]
        assert "agent" not in raw, "agent leaked to VLM"
        assert "region" not in raw, "region leaked to VLM"
        assert "capture_size" not in raw, "capture_size leaked to VLM"


@_run("panel_strips_actions_parts_from_vlm_messages")
def t_strips_actions() -> None:
    with _vlm_lock:
        reqs = list(_vlm_requests)
    assert reqs
    for req in reqs:
        for msg in req["raw"].get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    assert part.get("type") != "actions", "actions part leaked to VLM"


@_run("panel_result_post_unblocks_vlm_forward")
def t_result_unblocks() -> None:
    t0 = time.time()
    rid_box: list[str] = []
    done = threading.Event()

    def _watcher() -> None:
        ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-unblock", after_ts=t0)
        if ev:
            rid = ev["data"].get("request_id", "")
            if rid:
                rid_box.append(rid)
                with _pump_skip_lock:
                    _pump_skip.add(rid)

    def _sender() -> None:
        _brain("ag-unblock", image_url=_IMG_URL)
        done.set()

    wt = threading.Thread(target=_watcher, daemon=True)
    st = threading.Thread(target=_sender, daemon=True)
    wt.start()
    st.start()
    wt.join(timeout=16.0)
    assert rid_box, "no request_id captured"
    rid = rid_box[0]
    time.sleep(0.1)
    assert not done.is_set(), "panel returned before /result posted"
    _post_result(rid, _PNG_B64)
    assert done.wait(timeout=15.0), "panel did not unblock after /result"
    with _pump_skip_lock:
        _pump_skip.discard(rid)


@_run("panel_result_timeout_falls_back_to_raw_b64")
def t_result_timeout_fallback() -> None:
    t0 = time.time()
    rid_box: list[str] = []
    done = threading.Event()
    resp_box: list[dict[str, Any]] = []

    def _watcher() -> None:
        ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-timeout", after_ts=t0)
        if ev:
            rid = ev["data"].get("request_id", "")
            if rid:
                rid_box.append(rid)
                with _pump_skip_lock:
                    _pump_skip.add(rid)

    def _sender() -> None:
        resp_box.append(_brain("ag-timeout", image_url=_IMG_URL, timeout=90.0))
        done.set()

    wt = threading.Thread(target=_watcher, daemon=True)
    st = threading.Thread(target=_sender, daemon=True)
    wt.start()
    st.start()
    wt.join(timeout=16.0)
    assert rid_box, "no annotate event for ag-timeout"
    assert done.wait(timeout=45.0), "panel never returned after 30s timeout"
    assert resp_box, "no response captured"
    assert resp_box[0].get("choices"), "no choices in fallback response"
    with _pump_skip_lock:
        _pump_skip.discard(rid_box[0])


# ── PHASE 6: Image pipeline ───────────────────────────────────────────────────

@_run("empty_image_url_triggers_screen_capture")
def t_empty_url_captures() -> None:
    t0 = time.time()
    threading.Thread(target=_brain, args=("ag-capture",), kwargs={"image_url": ""}, daemon=True).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-capture", after_ts=t0)
    assert ev is not None, "no annotate event"
    raw_b64 = ev["data"].get("raw_b64", "")
    assert raw_b64, "raw_b64 empty — capture did not happen"
    assert base64.b64decode(raw_b64)[:4] == b"\x89PNG", "captured data is not a PNG"


@_run("data_url_image_extracted_correctly_as_raw_b64")
def t_data_url_extracted() -> None:
    t0 = time.time()
    threading.Thread(target=_brain, args=("ag-dataurl",), kwargs={"image_url": _IMG_URL}, daemon=True).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-dataurl", after_ts=t0)
    assert ev is not None, "no annotate event"
    assert ev["data"].get("raw_b64") == _PNG_B64, "raw_b64 does not match sent image"


@_run("capture_with_region_produces_valid_png_at_requested_size")
def t_capture_region_size() -> None:
    proc = subprocess.run(
        [sys.executable, str(WIN32_PY), "capture", "--region", "100,100,900,900",
         "--width", "64", "--height", "64"],
        capture_output=True, timeout=10,
    )
    assert proc.returncode == 0 and proc.stdout[:4] == b"\x89PNG"
    w, h = _png_dims(proc.stdout)
    assert w == 64 and h == 64, f"wrong size {w}x{h}"


@_run("capture_full_screen_produces_valid_png")
def t_capture_full() -> None:
    proc = subprocess.run(
        [sys.executable, str(WIN32_PY), "capture", "--width", "128", "--height", "128"],
        capture_output=True, timeout=10,
    )
    assert proc.returncode == 0 and proc.stdout[:4] == b"\x89PNG"
    assert len(proc.stdout) > 200


@_run("capture_dimensions_match_requested_width_height")
def t_capture_dims() -> None:
    proc = subprocess.run(
        [sys.executable, str(WIN32_PY), "capture", "--width", "320", "--height", "240"],
        capture_output=True, timeout=10,
    )
    assert proc.returncode == 0
    w, h = _png_dims(proc.stdout)
    assert w == 320 and h == 240, f"got {w}x{h}"


@_run("two_different_regions_produce_different_images")
def t_two_regions_differ() -> None:
    p1 = subprocess.run(
        [sys.executable, str(WIN32_PY), "capture", "--region", "0,0,200,200",
         "--width", "64", "--height", "64"],
        capture_output=True, timeout=10,
    )
    p2 = subprocess.run(
        [sys.executable, str(WIN32_PY), "capture", "--region", "800,800,1000,1000",
         "--width", "64", "--height", "64"],
        capture_output=True, timeout=10,
    )
    assert p1.returncode == 0 and p2.returncode == 0
    assert p1.stdout != p2.stdout, "two different screen regions produced identical images"


# ── PHASE 7: Annotation layer ─────────────────────────────────────────────────

@_run("overlay_annotated_b64_differs_from_raw_b64")
def t_overlay_differs() -> None:
    t0 = time.time()
    overlay = {"type": "overlay", "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
               "stroke": "#ff4455", "stroke_width": 8, "closed": True}
    threading.Thread(
        target=_brain, args=("ag-overlay",),
        kwargs={"image_url": _IMG_URL, "actions": [overlay]}, daemon=True,
    ).start()
    ev_a = _wait_sse("annotate", lambda d: d.get("agent") == "ag-overlay", after_ts=t0)
    assert ev_a is not None, "no annotate event"
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-overlay", after_ts=t0)
    assert ev_d is not None, "no vlm_done event"
    raw = ev_a["data"].get("raw_b64", "")
    annotated = ev_d["data"].get("annotated_b64", "")
    assert annotated, "annotated_b64 empty"
    assert annotated != raw, "annotated_b64 identical to raw — overlay not rendered"


@_run("overlay_annotated_png_has_same_dimensions_as_raw")
def t_overlay_same_dims() -> None:
    ev_a = _wait_sse("annotate", lambda d: d.get("agent") == "ag-overlay", timeout=5.0)
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-overlay", timeout=5.0)
    assert ev_a and ev_d, "overlay events not found"
    rw, rh = _png_dims(base64.b64decode(ev_a["data"].get("raw_b64", "")))
    aw, ah = _png_dims(base64.b64decode(ev_d["data"].get("annotated_b64", "")))
    assert (rw, rh) == (aw, ah), f"raw {rw}x{rh} != annotated {aw}x{ah}"


@_run("multiple_overlays_all_forwarded_to_browser")
def t_multi_overlay_count() -> None:
    t0 = time.time()
    overlays = [
        {"type": "overlay", "points": [[100, 100], [400, 400]], "stroke": "#4a9eff", "stroke_width": 3, "closed": False},
        {"type": "overlay", "points": [[600, 600], [900, 900]], "stroke": "#3ecf8e", "stroke_width": 3, "closed": False},
        {"type": "overlay", "points": [[500, 0], [500, 1000]], "stroke": "#f0a000", "stroke_width": 2, "closed": False},
    ]
    threading.Thread(
        target=_brain, args=("ag-multi-ov",),
        kwargs={"image_url": _IMG_URL, "actions": overlays}, daemon=True,
    ).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-multi-ov", after_ts=t0)
    assert ev is not None, "no annotate event"
    assert len(ev["data"].get("overlays", [])) == 3, f"expected 3 overlays, got {len(ev['data'].get('overlays', []))}"


@_run("empty_overlays_list_annotated_b64_equals_raw_b64")
def t_no_overlay_passthrough() -> None:
    t0 = time.time()
    threading.Thread(
        target=_brain, args=("ag-noov",),
        kwargs={"image_url": _IMG_URL, "actions": []}, daemon=True,
    ).start()
    ev_a = _wait_sse("annotate", lambda d: d.get("agent") == "ag-noov", after_ts=t0)
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-noov", after_ts=t0)
    assert ev_a and ev_d, "missing events"
    assert ev_d["data"].get("annotated_b64") == ev_a["data"].get("raw_b64"), \
        "annotated_b64 differs from raw_b64 with no overlays"


@_run("overlay_vlm_receives_annotated_image_not_raw")
def t_overlay_vlm_gets_annotated() -> None:
    t0 = time.time()
    overlay = {"type": "overlay", "points": [[200, 200], [800, 200], [800, 800], [200, 800]],
               "stroke": "#c084fc", "stroke_width": 6, "closed": True}
    with _vlm_lock:
        before = len(_vlm_requests)
    threading.Thread(
        target=_brain, args=("ag-vlm-ann",),
        kwargs={"image_url": _IMG_URL, "actions": [overlay]}, daemon=True,
    ).start()
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-vlm-ann", after_ts=t0)
    assert ev_d is not None, "no vlm_done"
    with _vlm_lock:
        new_reqs = _vlm_requests[before:]
    assert new_reqs, "no new VLM request"
    vlm_img_b64 = ""
    for msg in new_reqs[-1]["raw"].get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:image/png;base64,"):
                        vlm_img_b64 = url.split(",", 1)[1]
    assert vlm_img_b64 == ev_d["data"].get("annotated_b64", ""), "VLM received raw instead of annotated"


@_run("action_dispatched_before_capture_ordering")
def t_action_before_capture() -> None:
    log_before = _read_log()
    t0 = time.time()
    threading.Thread(
        target=_brain, args=("ag-order",),
        kwargs={"image_url": "", "actions": [{"type": "cursor_pos"}]}, daemon=True,
    ).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-order", after_ts=t0)
    assert ev is not None, "no annotate event"
    log_after = _read_log()
    types = [e.get("event") for e in log_after[len(log_before):]]
    assert "action_dispatched" in types, "action_dispatched not logged"
    assert "vlm_request" in types, "vlm_request not logged"
    ai = next(i for i, t in enumerate(types) if t == "action_dispatched")
    vi = next(i for i, t in enumerate(types) if t == "vlm_request")
    assert ai < vi, f"action_dispatched (idx {ai}) must precede vlm_request (idx {vi})"


# ── PHASE 8: Swarm ────────────────────────────────────────────────────────────

@_run("swarm_8_agents_all_receive_annotate_events")
def t_swarm_annotate() -> None:
    agents = [f"swarm-{i}" for i in range(8)]
    t0 = time.time()
    for ag in agents:
        threading.Thread(target=_brain, args=(ag,), kwargs={"image_url": _IMG_URL}, daemon=True).start()
    found: set[str] = set()
    deadline = time.time() + 30.0
    while time.time() < deadline and len(found) < len(agents):
        with _sse_lock:
            evs = list(_sse_events)
        for ev in evs:
            if ev["event"] == "annotate" and ev["ts"] >= t0:
                ag = ev["data"].get("agent", "")
                if ag in agents:
                    found.add(ag)
        time.sleep(0.04)
    missing = set(agents) - found
    assert not missing, f"missing annotate events for: {missing}"


@_run("swarm_8_agents_all_receive_vlm_done_events")
def t_swarm_vlm_done() -> None:
    agents = {f"swarm-{i}" for i in range(8)}
    found: set[str] = set()
    deadline = time.time() + 40.0
    while time.time() < deadline and len(found) < len(agents):
        with _sse_lock:
            evs = list(_sse_events)
        for ev in evs:
            if ev["event"] == "vlm_done":
                ag = ev["data"].get("agent", "")
                if ag in agents:
                    found.add(ag)
        time.sleep(0.04)
    assert not (agents - found), f"missing vlm_done for: {agents - found}"


@_run("swarm_8_agents_all_get_http_responses")
def t_swarm_http_responses() -> None:
    agents = [f"swarm-resp-{i}" for i in range(8)]
    results: dict[str, str] = {}
    lock = threading.Lock()

    def send(ag: str) -> None:
        resp = _brain(ag, image_url=_IMG_URL)
        choices = resp.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        with lock:
            results[ag] = content

    threads = [threading.Thread(target=send, args=(ag,), daemon=True) for ag in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40.0)
    assert len(results) == len(agents), f"only {len(results)}/{len(agents)} responded"
    for ag, content in results.items():
        assert content and not content.startswith("ERROR:"), f"{ag}: {content!r}"


@_run("swarm_no_response_cross_contamination")
def t_swarm_no_cross_contamination() -> None:
    agents = [f"xcheck-{i}" for i in range(5)]
    results: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def send(ag: str) -> None:
        resp = _brain(ag, image_url=_IMG_URL)
        with lock:
            results[ag] = resp

    threads = [threading.Thread(target=send, args=(ag,), daemon=True) for ag in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40.0)
    assert len(results) == len(agents)
    ids: set[str] = set()
    for ag, resp in results.items():
        rid = resp.get("id", "")
        assert rid not in ids, f"duplicate response id {rid}"
        ids.add(rid)


@_run("swarm_same_agent_overlapping_requests_both_complete")
def t_swarm_same_agent_overlap() -> None:
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def send(turn: int) -> None:
        resp = _brain("ag-overlap", image_url=_IMG_URL, text=f"overlap turn {turn}")
        with lock:
            results.append(resp)

    t1 = threading.Thread(target=send, args=(1,), daemon=True)
    t2 = threading.Thread(target=send, args=(2,), daemon=True)
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=40.0)
    t2.join(timeout=40.0)
    assert len(results) == 2, f"expected 2 responses, got {len(results)}"
    for r in results:
        assert r.get("choices"), "missing choices"


@_run("swarm_multi_turn_sequential_same_agent")
def t_swarm_multi_turn() -> None:
    for turn in range(4):
        resp = _brain("ag-multiturn", image_url=_IMG_URL, text=f"turn {turn}")
        choices = resp.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        assert content and not content.startswith("ERROR:"), f"turn {turn} failed: {content!r}"


@_run("swarm_sse_broadcast_reaches_second_listener")
def t_swarm_broadcast() -> None:
    second: list[dict[str, Any]] = []
    stop = threading.Event()

    def listen() -> None:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(CFG.panel_events_url), timeout=None
            ) as resp:
                buf = b""
                while not stop.is_set():
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        raw = buf.decode("utf-8", errors="replace").strip()
                        buf = b""
                        ev_type, data_str = "message", ""
                        for line in raw.splitlines():
                            if line.startswith("event:"):
                                ev_type = line[6:].strip()
                            elif line.startswith("data:"):
                                data_str = line[5:].strip()
                        if data_str:
                            try:
                                second.append({"event": ev_type, "data": json.loads(data_str)})
                            except Exception:
                                pass
        except Exception:
            pass

    lt = threading.Thread(target=listen, daemon=True)
    lt.start()
    time.sleep(0.3)
    _brain("ag-broadcast", image_url=_IMG_URL)
    stop.set()
    lt.join(timeout=3.0)
    assert any(
        e["event"] == "annotate" and e["data"].get("agent") == "ag-broadcast"
        for e in second
    ), "second SSE listener did not receive annotate event"


# ── PHASE 9: Agent isolation ──────────────────────────────────────────────────

@_run("two_agents_different_regions_get_different_raw_b64")
def t_two_agents_different_regions() -> None:
    import ctypes as _ct
    t0 = time.time()
    results: dict[str, str] = {}
    lock = threading.Lock()

    def send(ag: str, region: str) -> None:
        ev_inner = threading.Event()

        def watch() -> None:
            ev = _wait_sse("annotate", lambda d: d.get("agent") == ag, after_ts=t0)
            if ev:
                with lock:
                    results[ag] = ev["data"].get("raw_b64", "")
            ev_inner.set()

        threading.Thread(target=watch, daemon=True).start()
        _brain(ag, image_url="", region=region, capture_size=[64, 64])
        ev_inner.wait(timeout=20.0)

    ta = threading.Thread(target=send, args=("ag-region-a", "0,0,300,300"), daemon=True)
    tb = threading.Thread(target=send, args=("ag-region-b", "700,700,1000,1000"), daemon=True)
    ta.start()
    tb.start()
    ta.join(timeout=25.0)
    tb.join(timeout=25.0)
    assert "ag-region-a" in results and "ag-region-b" in results, "missing region captures"
    assert results["ag-region-a"] != results["ag-region-b"], "region isolation broken"


@_run("agent_with_text_only_no_image_url_still_completes")
def t_text_only_agent() -> None:
    payload = {
        "model": "mock-model", "max_tokens": 32, "stream": False,
        "agent": "ag-textonly", "region": "", "capture_size": [32, 32],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "text only query"}]}],
    }
    resp = _post(CFG.panel_completions_url, payload)
    choices = resp.get("choices", [])
    assert choices, "no choices for text-only agent"
    content = choices[0].get("message", {}).get("content", "")
    assert content and not content.startswith("ERROR:"), f"text-only agent failed: {content!r}"


@_run("agent_vlm_request_contains_messages_field")
def t_vlm_has_messages() -> None:
    with _vlm_lock:
        reqs = list(_vlm_requests)
    assert reqs, "no VLM requests"
    assert reqs[-1]["raw"].get("messages"), "forwarded request missing messages"


@_run("agent_image_url_in_vlm_request_has_data_prefix")
def t_vlm_image_has_prefix() -> None:
    with _vlm_lock:
        reqs = list(_vlm_requests)
    assert reqs
    for req in reqs:
        for msg in req["raw"].get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url:
                            assert url.startswith("data:image/png;base64,"), \
                                f"image_url missing data prefix: {url[:40]!r}"


# ── PHASE 10: Action dispatch ─────────────────────────────────────────────────

@_run("action_click_moves_cursor_to_target_norm_pos")
def t_action_click() -> None:
    import ctypes as _ct
    _ct.windll.user32.SetCursorPos(0, 0)
    time.sleep(0.05)
    proc = subprocess.run(
        [sys.executable, str(WIN32_PY), "click", "--pos", "500,500"],
        capture_output=True, timeout=10,
    )
    assert proc.returncode == 0
    time.sleep(0.1)
    pt = _ct.wintypes.POINT()
    _ct.windll.user32.GetCursorPos(_ct.byref(pt))
    sw = _ct.windll.user32.GetSystemMetrics(0)
    sh = _ct.windll.user32.GetSystemMetrics(1)
    assert abs(pt.x - sw // 2) < 15, f"cursor x={pt.x} expected ~{sw//2}"
    assert abs(pt.y - sh // 2) < 15, f"cursor y={pt.y} expected ~{sh//2}"


@_run("action_cursor_pos_returns_normalized_0_to_1000")
def t_action_cursor_pos() -> None:
    pos = _cursor_norm()
    assert pos is not None, "cursor_pos returned nothing"
    nx, ny = pos
    assert 0 <= nx <= 1000 and 0 <= ny <= 1000, f"out of range: {nx},{ny}"


@_run("action_cursor_pos_changes_after_setcursorpos")
def t_action_cursor_changes() -> None:
    import ctypes as _ct
    _ct.windll.user32.SetCursorPos(50, 50)
    time.sleep(0.05)
    p1 = _cursor_norm()
    _ct.windll.user32.SetCursorPos(900, 700)
    time.sleep(0.05)
    p2 = _cursor_norm()
    assert p1 is not None and p2 is not None
    assert p1 != p2, f"cursor_pos did not change: {p1} == {p2}"


@_run("action_cursor_pos_within_region_is_normalized_to_region")
def t_action_cursor_region_norm() -> None:
    import ctypes as _ct
    _ct.windll.user32.SetCursorPos(
        _ct.windll.user32.GetSystemMetrics(0) // 2,
        _ct.windll.user32.GetSystemMetrics(1) // 2,
    )
    time.sleep(0.05)
    pos = _cursor_norm("0,0,1000,1000")
    assert pos is not None
    nx, ny = pos
    assert 450 <= nx <= 550 and 450 <= ny <= 550, f"center cursor not near 500,500: {nx},{ny}"


@_run("action_dispatch_logged_in_franz_log")
def t_action_dispatch_logged() -> None:
    size_before = _log_size()
    t0 = time.time()
    threading.Thread(
        target=_brain, args=("ag-actlog",),
        kwargs={"image_url": _IMG_URL, "actions": [{"type": "cursor_pos"}]},
        daemon=True,
    ).start()
    _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-actlog", after_ts=t0)
    time.sleep(0.2)
    assert _log_size() > size_before, "log did not grow"
    assert any(e.get("event") == "action_dispatched" for e in _read_log()), \
        "no action_dispatched in log"


@_run("action_type_text_dispatched_and_logged")
def t_action_type_text_logged() -> None:
    t0 = time.time()
    threading.Thread(
        target=_brain, args=("ag-typetext",),
        kwargs={"image_url": _IMG_URL, "actions": [{"type": "type_text", "text": "SWARM"}]},
        daemon=True,
    ).start()
    _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-typetext", after_ts=t0)
    time.sleep(0.2)
    typed = [e for e in _read_log() if e.get("event") == "action_dispatched" and e.get("type") == "type_text"]
    assert typed, "no type_text action_dispatched in log"
    assert any(e.get("text") == "SWARM" for e in typed), "type_text log entry missing text field"


@_run("action_drag_dispatched_and_logged")
def t_action_drag_logged() -> None:
    t0 = time.time()
    threading.Thread(
        target=_brain, args=("ag-drag",),
        kwargs={"image_url": _IMG_URL, "actions": [{"type": "drag", "x1": 100, "y1": 100, "x2": 900, "y2": 900}]},
        daemon=True,
    ).start()
    _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-drag", after_ts=t0)
    time.sleep(0.2)
    drags = [e for e in _read_log() if e.get("event") == "action_dispatched" and e.get("type") == "drag"]
    assert drags, "no drag action_dispatched in log"


# ── PHASE 11: Logging integrity ───────────────────────────────────────────────

@_run("log_every_line_is_valid_json")
def t_log_valid_json() -> None:
    p = HERE / CFG.log_path
    assert p.exists(), f"{CFG.log_path} does not exist"
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except Exception as e:
            assert False, f"line {i+1} is not valid JSON: {e}"


@_run("log_every_entry_has_event_and_ts_fields")
def t_log_fields() -> None:
    entries = _read_log()
    assert entries, "log is empty"
    for e in entries:
        assert "event" in e, f"entry missing event: {e}"
        assert "ts" in e, f"entry missing ts: {e}"


@_run("log_timestamps_monotonically_non_decreasing")
def t_log_monotonic() -> None:
    ts_list = [e["ts"] for e in _read_log() if "ts" in e]
    assert ts_list
    for i in range(1, len(ts_list)):
        assert ts_list[i] >= ts_list[i-1] - 0.001, \
            f"timestamp went backwards at index {i}: {ts_list[i-1]} -> {ts_list[i]}"


@_run("log_vlm_request_entries_have_agent_model_overlays")
def t_log_vlm_request_fields() -> None:
    req_entries = [e for e in _read_log() if e.get("event") == "vlm_request"]
    assert req_entries, "no vlm_request entries"
    for e in req_entries:
        assert "agent" in e, f"vlm_request missing agent: {e}"
        assert "model" in e, f"vlm_request missing model: {e}"
        assert "overlays" in e, f"vlm_request missing overlays: {e}"


@_run("log_vlm_response_entries_present_with_duration_and_text")
def t_log_vlm_response() -> None:
    resp_entries = [e for e in _read_log() if e.get("event") == "vlm_response"]
    assert resp_entries, "no vlm_response entries"
    for e in resp_entries:
        assert "duration_ms" in e, f"vlm_response missing duration_ms: {e}"
        assert "text" in e, f"vlm_response missing text: {e}"


@_run("log_vlm_response_missing_agent_and_request_id_bug_documented")
def t_log_vlm_response_missing_fields_bug() -> None:
    resp_entries = [e for e in _read_log() if e.get("event") == "vlm_response"]
    assert resp_entries, "no vlm_response entries"
    missing_agent = [e for e in resp_entries if "agent" not in e]
    missing_rid = [e for e in resp_entries if "request_id" not in e]
    assert not missing_agent, (
        f"BUG CONFIRMED: {len(missing_agent)} vlm_response entries missing 'agent' field — "
        "cannot correlate response to agent under concurrent load"
    )
    assert not missing_rid, (
        f"BUG CONFIRMED: {len(missing_rid)} vlm_response entries missing 'request_id' field — "
        "cannot correlate response to request under concurrent load"
    )


@_run("log_request_count_matches_vlm_receive_count")
def t_log_request_count() -> None:
    entries = _read_log()
    log_req_count = sum(1 for e in entries if e.get("event") == "vlm_request")
    log_resp_count = sum(1 for e in entries if e.get("event") == "vlm_response")
    with _vlm_lock:
        actual_count = len(_vlm_requests)
    assert log_req_count >= actual_count, \
        f"log has {log_req_count} vlm_request but fake VLM received {actual_count}"
    assert log_resp_count >= actual_count, \
        f"log has {log_resp_count} vlm_response but {actual_count} requests were made"


@_run("log_concurrent_writes_no_interleaved_lines")
def t_log_no_interleaving() -> None:
    agents = [f"log-conc-{i}" for i in range(6)]
    threads = [
        threading.Thread(target=_brain, args=(ag,), kwargs={"image_url": _IMG_URL}, daemon=True)
        for ag in agents
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)
    time.sleep(0.3)
    for i, line in enumerate((HERE / CFG.log_path).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except Exception:
            assert False, f"interleaved/corrupt log line {i+1}: {line[:80]!r}"


@_run("log_action_dispatched_entries_have_type_field")
def t_log_action_type_field() -> None:
    act_entries = [e for e in _read_log() if e.get("event") == "action_dispatched"]
    assert act_entries, "no action_dispatched entries"
    for e in act_entries:
        assert "type" in e, f"action_dispatched missing type: {e}"


@_run("log_file_size_only_grows_never_truncated")
def t_log_grows() -> None:
    size_before = _log_size()
    _brain("ag-logsize", image_url=_IMG_URL)
    time.sleep(0.2)
    size_after = _log_size()
    assert size_after > size_before, f"log did not grow: before={size_before} after={size_after}"


# ── PHASE 12: win32.py direct ─────────────────────────────────────────────────

@_run("win32_unknown_command_exits_1")
def t_win32_unknown_cmd() -> None:
    p = _win32("no-such-command")
    assert p.returncode == 1, f"expected exit 1, got {p.returncode}"


@_run("win32_capture_valid_png_stdout")
def t_win32_capture() -> None:
    p = subprocess.run(
        [sys.executable, str(WIN32_PY), "capture", "--width", "32", "--height", "32"],
        capture_output=True, timeout=10,
    )
    assert p.returncode == 0 and p.stdout[:4] == b"\x89PNG"


@_run("win32_click_at_norm_0_0_lands_at_screen_top_left")
def t_win32_click_topleft() -> None:
    import ctypes as _ct
    _ct.windll.user32.SetCursorPos(500, 500)
    time.sleep(0.05)
    p = _win32("click", "--pos", "0,0")
    assert p.returncode == 0
    time.sleep(0.1)
    pt = _ct.wintypes.POINT()
    _ct.windll.user32.GetCursorPos(_ct.byref(pt))
    assert pt.x < 20 and pt.y < 20, f"cursor at {pt.x},{pt.y} not near top-left"


@_run("win32_click_at_norm_1000_1000_lands_at_screen_bottom_right")
def t_win32_click_bottomright() -> None:
    import ctypes as _ct
    sw = _ct.windll.user32.GetSystemMetrics(0)
    sh = _ct.windll.user32.GetSystemMetrics(1)
    _ct.windll.user32.SetCursorPos(0, 0)
    time.sleep(0.05)
    p = _win32("click", "--pos", "1000,1000")
    assert p.returncode == 0
    time.sleep(0.1)
    pt = _ct.wintypes.POINT()
    _ct.windll.user32.GetCursorPos(_ct.byref(pt))
    assert abs(pt.x - (sw - 1)) < 15 and abs(pt.y - (sh - 1)) < 15, \
        f"cursor at {pt.x},{pt.y} not near bottom-right {sw-1},{sh-1}"


@_run("win32_drag_ends_cursor_at_destination")
def t_win32_drag_dest() -> None:
    import ctypes as _ct
    _ct.windll.user32.SetCursorPos(0, 0)
    time.sleep(0.05)
    p = _win32("drag", "--from_pos", "100,100", "--to_pos", "800,800")
    assert p.returncode == 0
    time.sleep(0.15)
    pos = _cursor_norm()
    assert pos is not None
    nx, ny = pos
    assert 750 <= nx <= 1000 and 750 <= ny <= 1000, f"drag dest cursor at {nx},{ny}"


@_run("win32_scroll_up_and_down_return_0")
def t_win32_scroll() -> None:
    pu = _win32("scroll_up", "--pos", "500,500", "--clicks", "2")
    pd = _win32("scroll_down", "--pos", "500,500", "--clicks", "2")
    assert pu.returncode == 0 and pd.returncode == 0


@_run("win32_right_click_returns_0")
def t_win32_right_click() -> None:
    import ctypes as _ct
    p = _win32("right_click", "--pos", "500,500")
    assert p.returncode == 0
    time.sleep(0.1)
    _ct.windll.user32.keybd_event(0x1B, 0, 0, None)
    time.sleep(0.05)
    _ct.windll.user32.keybd_event(0x1B, 0, 2, None)


@_run("win32_double_click_returns_0")
def t_win32_double_click() -> None:
    p = _win32("double_click", "--pos", "500,500")
    assert p.returncode == 0


@_run("win32_hotkey_ctrl_z_returns_0")
def t_win32_hotkey() -> None:
    p = _win32("hotkey", "--keys", "ctrl+z")
    assert p.returncode == 0


@_run("win32_press_key_escape_returns_0")
def t_win32_press_key() -> None:
    p = _win32("press_key", "--key", "escape")
    assert p.returncode == 0


@_run("win32_select_region_esc_returns_exit_code_2")
def t_win32_select_region_esc() -> None:
    print()
    print("  >>> INTERACTIVE: selector overlay will open — press ESC immediately.")
    time.sleep(0.5)
    p = subprocess.run(
        [sys.executable, str(WIN32_PY), "select_region"],
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 2, f"expected exit 2 on ESC, got {p.returncode}"
    assert p.stdout.strip() == "", f"expected empty stdout on cancel, got: {p.stdout!r}"


@_run("win32_select_region_valid_drag_returns_4_part_coords")
def t_win32_select_region_drag() -> None:
    global _interactive_region
    print()
    print("  >>> INTERACTIVE: drag to select any region on screen, then release.")
    time.sleep(0.3)
    p = subprocess.run(
        [sys.executable, str(WIN32_PY), "select_region"],
        capture_output=True, text=True, timeout=60,
    )
    if p.returncode == 2:
        assert False, "user cancelled — drag a region instead of pressing ESC"
    region = p.stdout.strip()
    assert region, "empty stdout after successful drag"
    parts = region.split(",")
    assert len(parts) == 4, f"expected 4 parts, got: {region!r}"
    x1, y1, x2, y2 = map(int, parts)
    assert x2 > x1 and y2 > y1, f"invalid region: {region}"
    assert all(0 <= v <= 1000 for v in (x1, y1, x2, y2)), f"coords out of 0-1000 range: {region}"
    assert (x2 - x1) >= 20 and (y2 - y1) >= 20, f"region too small: {region}"
    _interactive_region = region
    print(f"      Selected region: {region}")


@_run("win32_capture_selected_region_produces_valid_png")
def t_win32_capture_selected_region() -> None:
    region = _interactive_region or "100,100,900,900"
    p = subprocess.run(
        [sys.executable, str(WIN32_PY), "capture", "--region", region,
         "--width", "128", "--height", "128"],
        capture_output=True, timeout=10,
    )
    assert p.returncode == 0 and p.stdout[:4] == b"\x89PNG"
    w, h = _png_dims(p.stdout)
    assert w == 128 and h == 128, f"wrong size {w}x{h}"


@_run("win32_cursor_pos_within_selected_region_normalized")
def t_win32_cursor_in_region() -> None:
    import ctypes as _ct
    region = _interactive_region or "0,0,1000,1000"
    x1, y1, x2, y2 = map(int, region.split(","))
    sw = _ct.windll.user32.GetSystemMetrics(0)
    sh = _ct.windll.user32.GetSystemMetrics(1)
    _ct.windll.user32.SetCursorPos((x1 + x2) * sw // 2000, (y1 + y2) * sh // 2000)
    time.sleep(0.05)
    pos = _cursor_norm(region)
    assert pos is not None
    nx, ny = pos
    assert 400 <= nx <= 600 and 400 <= ny <= 600, f"center cursor not near 500,500: {nx},{ny}"


@_run("brain_request_with_selected_region_captures_correct_area")
def t_brain_with_region() -> None:
    region = _interactive_region or "100,100,900,900"
    t0 = time.time()
    threading.Thread(
        target=_brain, args=("ag-region-cap",),
        kwargs={"image_url": "", "region": region, "capture_size": [128, 128]},
        daemon=True,
    ).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-region-cap", after_ts=t0)
    assert ev is not None, "no annotate event for region capture"
    raw_b64 = ev["data"].get("raw_b64", "")
    assert raw_b64, "raw_b64 empty"
    png = base64.b64decode(raw_b64)
    assert png[:4] == b"\x89PNG"
    w, h = _png_dims(png)
    assert w == 128 and h == 128, f"wrong capture size {w}x{h}"


# ── PHASE 13: VLM-down ────────────────────────────────────────────────────────

@_run("panel_returns_502_when_vlm_unreachable")
def t_vlm_down_502() -> None:
    try:
        _brain("ag-502", image_url=_IMG_URL, timeout=45.0)
        assert False, "expected 502 but got success"
    except urllib.error.HTTPError as e:
        assert e.code == 502, f"expected 502 got {e.code}"
    except Exception as e:
        assert False, f"expected HTTPError 502, got: {type(e).__name__}: {e}"


@_run("panel_logs_vlm_error_event_on_502")
def t_vlm_down_logged() -> None:
    assert any(e.get("event") == "vlm_error" for e in _read_log()), \
        "no vlm_error entry in log after VLM-down test"


@_run("panel_sse_vlm_done_with_error_text_on_502")
def t_vlm_down_sse_error() -> None:
    ev = _wait_sse(
        "vlm_done",
        lambda d: d.get("agent") == "ag-502" and d.get("text", "").startswith("ERROR:"),
        timeout=5.0,
    )
    assert ev is not None, "no vlm_done error SSE event for ag-502"


# ── Report + main ─────────────────────────────────────────────────────────────

def _emit_report() -> None:
    passed = [r for r in _test_results if r["passed"]]
    failed = [r for r in _test_results if not r["passed"]]
    report = {
        "summary": {
            "total": len(_test_results),
            "passed": len(passed),
            "failed": len(failed),
            "pass_rate": round(len(passed) / max(len(_test_results), 1) * 100, 1),
        },
        "results": _test_results,
    }
    out = HERE / CFG.report_path
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n  Report written to {out}")


PHASES_1_TO_12: list[Any] = [
    t_panel_ready, t_panel_html, t_panel_404, t_panel_400,
    t_sse_connected, t_result_404, t_result_400,
    t_html_render_annotated, t_html_posts_result, t_html_vlm_done_handler,
    t_html_pane_creation, t_html_sse_error_handler, t_html_history_chip,
    t_html_catch_fallback, t_html_norm_constant,
    t_result_bridge_custom_b64,
    t_black_png_pixels, t_black_travels_untouched, t_black_with_overlay_has_red_pixels,
    t_overlay_pixel_count_scales_with_stroke, t_fill_overlay_covers_majority,
    t_black_raw_b64_in_annotate,
    t_single_annotate, t_single_vlm_done, t_single_http,
    t_strips_custom_fields, t_strips_actions,
    t_result_unblocks, t_result_timeout_fallback,
    t_empty_url_captures, t_data_url_extracted,
    t_capture_region_size, t_capture_full, t_capture_dims, t_two_regions_differ,
    t_overlay_differs, t_overlay_same_dims, t_multi_overlay_count,
    t_no_overlay_passthrough, t_overlay_vlm_gets_annotated, t_action_before_capture,
    t_swarm_annotate, t_swarm_vlm_done, t_swarm_http_responses,
    t_swarm_no_cross_contamination, t_swarm_same_agent_overlap,
    t_swarm_multi_turn, t_swarm_broadcast,
    t_two_agents_different_regions, t_text_only_agent,
    t_vlm_has_messages, t_vlm_image_has_prefix,
    t_action_click, t_action_cursor_pos, t_action_cursor_changes,
    t_action_cursor_region_norm, t_action_dispatch_logged,
    t_action_type_text_logged, t_action_drag_logged,
    t_log_valid_json, t_log_fields, t_log_monotonic,
    t_log_vlm_request_fields, t_log_vlm_response,
    t_log_vlm_response_missing_fields_bug,
    t_log_request_count, t_log_no_interleaving, t_log_action_type_field, t_log_grows,
    t_win32_unknown_cmd, t_win32_capture,
    t_win32_click_topleft, t_win32_click_bottomright,
    t_win32_drag_dest, t_win32_scroll, t_win32_right_click,
    t_win32_double_click, t_win32_hotkey, t_win32_press_key,
    t_win32_select_region_esc, t_win32_select_region_drag,
    t_win32_capture_selected_region, t_win32_cursor_in_region,
    t_brain_with_region,
]

PHASE_13_VLM_DOWN: list[Any] = [
    t_vlm_down_502, t_vlm_down_logged, t_vlm_down_sse_error,
]


def main() -> None:
    print("\n=== test_mock_vlm — Mock VLM Tests (91 tests) ===\n")
    print("  Includes: panel.html contract, 64x64 pixel-level image travel, /result bridge.\n")
    print("  NOTE: interactive prompts will appear for region selection and ESC test.\n")

    for p in (HERE / "test_mock_vlm.json", HERE / "franz-log.jsonl"):
        p.unlink(missing_ok=True)

    print("[1/6] Starting fake VLM...")
    vlm_srv = _start_vlm()
    print(f"      VLM on {CFG.vlm_host}:{CFG.vlm_port}")

    print("[2/6] Starting panel.py...")
    panel_proc = _start_panel()
    print(f"      Panel ready at {CFG.panel_url}")

    print("[3/6] Opening Chrome...")
    try:
        chrome = _open_chrome()
        print(f"      Chrome PID {chrome.pid}")
        time.sleep(1.5)
    except RuntimeError as e:
        print(f"      WARNING: {e} — continuing without Chrome")
        chrome = None

    print("[4/6] Starting SSE listener + auto-result pump...")
    stop = threading.Event()
    threading.Thread(target=_sse_reader, args=(stop,), daemon=True).start()
    threading.Thread(target=_pump, args=(stop,), daemon=True).start()
    time.sleep(1.0)

    print("[5/6] Running phases 1–12...\n")
    for fn in PHASES_1_TO_12:
        fn()

    print("\n[6/6] Running phase 13 — VLM-down (stopping fake VLM)...")
    vlm_srv.shutdown()
    vlm_srv.server_close()
    time.sleep(1.5)
    for fn in PHASE_13_VLM_DOWN:
        fn()

    stop.set()
    panel_proc.terminate()
    if chrome is not None:
        try:
            chrome.terminate()
        except Exception:
            pass

    passed = sum(1 for r in _test_results if r["passed"])
    failed = sum(1 for r in _test_results if not r["passed"])
    total = len(_test_results)

    print(f"\n{'='*48}")
    print(f"  TOTAL: {total}  PASSED: {passed}  FAILED: {failed}")
    print(f"  Swarm readiness: {round(passed/max(total,1)*100,1)}%")
    print(f"{'='*48}\n")

    _emit_report()

    log_src = HERE / "franz-log.jsonl"
    if log_src.exists():
        log_src.rename(HERE / "test_mock_vlm.jsonl")

    if failed:
        print(f"  {failed} test(s) FAILED — see {CFG.report_path}\n")
        sys.exit(1)
    else:
        print("  All tests passed.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
