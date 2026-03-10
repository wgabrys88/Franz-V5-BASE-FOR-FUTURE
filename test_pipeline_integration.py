import base64
import http.server
import json
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
    panel_ready_url: str = "http://127.0.0.1:1236/ready"
    panel_events_url: str = "http://127.0.0.1:1236/events"
    panel_completions_url: str = "http://127.0.0.1:1236/v1/chat/completions"
    panel_result_url: str = "http://127.0.0.1:1236/result"
    lm_studio_url: str = "http://127.0.0.1:1235/v1/chat/completions"
    lm_studio_models_url: str = "http://127.0.0.1:1235/v1/models"
    startup_timeout: float = 10.0
    vlm_timeout: float = 120.0
    report_path: str = "test_pipeline_integration.json"
    log_path: str = "franz-log.jsonl"
    model: str = "lmstudio-loaded-model"


CFG = _Cfg()
HERE = Path(__file__).resolve().parent
PANEL_PY = HERE / "panel.py"
PANEL_HTML = HERE / "panel.html"
WIN32_PY = HERE / "win32.py"

_sse_lock = threading.Lock()
_sse_events: list[dict[str, Any]] = []

_pump_skip: set[str] = set()
_pump_skip_lock = threading.Lock()

_test_results: list[dict[str, Any]] = []
_interactive_region: str = ""
_detected_model: str = ""


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

_WHITE64_BYTES = _make_png(64, 64, 255, 255, 255)
_WHITE64_B64 = base64.b64encode(_WHITE64_BYTES).decode("ascii")
_WHITE64_URL = f"data:image/png;base64,{_WHITE64_B64}"


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
    timeout: float = 30.0,
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


def _post(url: str, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
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
    system_prompt: str = "You are a helpful assistant.",
    image_url: str = "",
    text: str = "",
    actions: list[dict[str, Any]] | None = None,
    region: str = "",
    capture_size: list[int] | None = None,
    temperature: float = 0.1,
    max_tokens: int = 64,
    timeout: float = 120.0,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text or f"turn from {agent}"}]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    if actions:
        content.append({"type": "actions", "actions": actions})
    return _post(CFG.panel_completions_url, {
        "model": _detected_model or CFG.model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "agent": agent,
        "region": region,
        "capture_size": capture_size or [64, 64],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    }, timeout=timeout)


def _direct_vlm(
    system_prompt: str,
    user_text: str,
    image_url: str = "",
    temperature: float = 0.1,
    max_tokens: int = 64,
    timeout: float = 120.0,
) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    body = json.dumps({
        "model": _detected_model or CFG.model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    }).encode()
    req = urllib.request.Request(
        CFG.lm_studio_url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        obj = json.loads(resp.read())
    choices = obj.get("choices", [])
    return choices[0].get("message", {}).get("content", "") if choices else ""


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


# ── PHASE 1: LM Studio connectivity ──────────────────────────────────────────

@_run("lm_studio_reachable_at_configured_url")
def t_lm_studio_reachable() -> None:
    try:
        with urllib.request.urlopen(CFG.lm_studio_models_url, timeout=5) as r:
            assert r.status == 200, f"unexpected status {r.status}"
    except Exception as e:
        assert False, f"LM Studio not reachable at {CFG.lm_studio_models_url}: {e}"


@_run("lm_studio_models_endpoint_returns_model_list")
def t_lm_studio_models() -> None:
    global _detected_model
    with urllib.request.urlopen(CFG.lm_studio_models_url, timeout=5) as r:
        obj = json.loads(r.read())
    models = obj.get("data", [])
    assert models, "no models loaded in LM Studio"
    _detected_model = models[0].get("id", CFG.model)
    print(f"      Detected model: {_detected_model}")


@_run("lm_studio_direct_completions_returns_valid_response")
def t_lm_studio_direct() -> None:
    text = _direct_vlm("You are a helpful assistant.", "Reply with the single word: HELLO")
    assert text.strip(), "empty response from LM Studio"
    assert not text.startswith("ERROR:"), f"error response: {text!r}"


@_run("panel_ready_returns_ok")
def t_panel_ready() -> None:
    with urllib.request.urlopen(CFG.panel_ready_url, timeout=5) as r:
        assert json.loads(r.read()).get("ok") is True


@_run("sse_connected_event_on_subscribe")
def t_sse_connected() -> None:
    ev = _wait_sse("connected", timeout=8.0)
    assert ev is not None, "no connected SSE event"


# ── PHASE 2: panel.html static contract ──────────────────────────────────────

@_run("panel_html_contains_renderable_annotate_function")
def t_html_render_annotated() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "renderAnnotated" in src
    assert "OffscreenCanvas" in src
    assert "drawPolygonOn" in src


@_run("panel_html_annotate_event_posts_result_back")
def t_html_posts_result() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "fetch('/result'" in src
    assert "annotated_b64" in src
    assert "request_id" in src


@_run("panel_html_catch_block_falls_back_to_raw_b64_on_render_error")
def t_html_catch_fallback() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "} catch {" in src or "catch(" in src
    assert src.count("fetch('/result'") >= 2


@_run("panel_html_overlay_points_normalized_by_NORM_constant")
def t_html_norm_constant() -> None:
    src = PANEL_HTML.read_text(encoding="utf-8")
    assert "NORM" in src
    assert "/NORM" in src


# ── PHASE 3: Parrot tests — VLM response content verification ─────────────────

@_run("parrot_system_prompt_echoes_user_input_via_panel")
def t_parrot_via_panel() -> None:
    marker = f"ECHO-{uuid.uuid4().hex[:8].upper()}"
    resp = _brain(
        "ag-parrot",
        system_prompt="Repeat back the exact user message word for word. Output only the repeated text, nothing else.",
        text=marker,
        image_url=_IMG_URL,
        temperature=0.0,
        max_tokens=32,
        timeout=CFG.vlm_timeout,
    )
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "")
    assert marker in content, f"parrot did not echo marker {marker!r} — got: {content!r}"


@_run("parrot_direct_vlm_echoes_user_input")
def t_parrot_direct() -> None:
    marker = f"DIRECT-{uuid.uuid4().hex[:8].upper()}"
    text = _direct_vlm(
        "Repeat back the exact user message word for word. Output only the repeated text, nothing else.",
        marker,
        temperature=0.0,
        max_tokens=32,
    )
    assert marker in text, f"direct parrot did not echo {marker!r} — got: {text!r}"


@_run("parrot_temperature_0_produces_identical_responses")
def t_parrot_deterministic() -> None:
    prompt = "Reply with exactly: DETERMINISTIC"
    r1 = _direct_vlm(
        "You are a deterministic assistant. Follow instructions exactly.",
        prompt,
        temperature=0.0,
        max_tokens=16,
    )
    r2 = _direct_vlm(
        "You are a deterministic assistant. Follow instructions exactly.",
        prompt,
        temperature=0.0,
        max_tokens=16,
    )
    assert r1.strip() == r2.strip(), (
        f"temperature=0.0 produced different responses:\n  r1={r1!r}\n  r2={r2!r}"
    )


# ── PHASE 4: Black image VLM vision tests ─────────────────────────────────────

@_run("vlm_identifies_black_image_answers_yes")
def t_vlm_black_image_yes() -> None:
    answer = _direct_vlm(
        "You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        "Is this image completely black?",
        image_url=_BLACK64_URL,
        temperature=0.0,
        max_tokens=4,
    )
    normalized = answer.strip().lower().rstrip(".,!?")
    assert normalized == "yes", f"expected 'yes' for black image, got: {answer!r}"


@_run("vlm_identifies_white_image_is_not_black_answers_no")
def t_vlm_white_image_no() -> None:
    answer = _direct_vlm(
        "You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        "Is this image completely black?",
        image_url=_WHITE64_URL,
        temperature=0.0,
        max_tokens=4,
    )
    normalized = answer.strip().lower().rstrip(".,!?")
    assert normalized == "no", f"expected 'no' for white image, got: {answer!r}"


@_run("vlm_black_image_via_panel_pipeline_answer_yes")
def t_vlm_black_via_panel() -> None:
    resp = _brain(
        "ag-black-vision",
        system_prompt="You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        text="Is this image completely black?",
        image_url=_BLACK64_URL,
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "").strip().lower().rstrip(".,!?")
    assert content == "yes", f"expected 'yes' for black image via panel, got: {content!r}"


# ── PHASE 5: Annotated image VLM vision tests ─────────────────────────────────

@_run("vlm_sees_red_rectangle_on_black_image_answers_yes")
def t_vlm_sees_red_rect() -> None:
    t0 = time.time()
    overlay = {
        "type": "overlay",
        "points": [[200, 200], [800, 200], [800, 800], [200, 800]],
        "stroke": "#ff0000",
        "stroke_width": 20,
        "closed": True,
    }
    resp = _brain(
        "ag-red-rect",
        system_prompt="You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        text="Do you see a red rectangle or red border drawn on this image?",
        image_url=_BLACK64_URL,
        actions=[overlay],
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "").strip().lower().rstrip(".,!?")
    assert content == "yes", f"VLM did not see red rectangle on annotated image, got: {content!r}"


@_run("vlm_does_not_see_red_rectangle_on_unannotated_black_image")
def t_vlm_no_red_rect_without_overlay() -> None:
    resp = _brain(
        "ag-no-red-rect",
        system_prompt="You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        text="Do you see a red rectangle or red border drawn on this image?",
        image_url=_BLACK64_URL,
        actions=[],
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "").strip().lower().rstrip(".,!?")
    assert content == "no", f"VLM falsely saw red rectangle on plain black image, got: {content!r}"


@_run("vlm_annotated_image_differs_from_raw_pixel_level")
def t_annotated_differs_pixel_level() -> None:
    t0 = time.time()
    overlay = {
        "type": "overlay",
        "points": [[100, 100], [900, 100], [900, 900], [100, 900]],
        "stroke": "#ff0000",
        "stroke_width": 15,
        "closed": True,
    }
    threading.Thread(
        target=_brain, args=("ag-pixel-diff",),
        kwargs={
            "system_prompt": "You are a test agent.",
            "text": "test",
            "image_url": _BLACK64_URL,
            "actions": [overlay],
            "timeout": CFG.vlm_timeout,
        },
        daemon=True,
    ).start()
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-pixel-diff", after_ts=t0, timeout=CFG.vlm_timeout)
    assert ev_d is not None, "no vlm_done event"
    ann_b64 = ev_d["data"].get("annotated_b64", "")
    assert ann_b64 and ann_b64 != _BLACK64_B64, "annotated_b64 identical to raw"
    pixels = _decode_png_pixels(ann_b64)
    non_black = [p for p in pixels if p[:3] != (0, 0, 0)]
    assert non_black, "all pixels still black after red overlay"


# ── PHASE 6: Hyperparameter tests ─────────────────────────────────────────────

@_run("max_tokens_1_truncates_response_to_single_token")
def t_max_tokens_1() -> None:
    text = _direct_vlm(
        "You are a helpful assistant.",
        "Count from one to ten.",
        temperature=0.1,
        max_tokens=1,
    )
    words = text.strip().split()
    assert len(words) <= 3, f"max_tokens=1 produced {len(words)} words: {text!r}"


@_run("max_tokens_128_allows_longer_response_than_max_tokens_8")
def t_max_tokens_length_difference() -> None:
    short = _direct_vlm(
        "You are a helpful assistant.",
        "Describe the color red in detail.",
        temperature=0.1,
        max_tokens=8,
    )
    long = _direct_vlm(
        "You are a helpful assistant.",
        "Describe the color red in detail.",
        temperature=0.1,
        max_tokens=128,
    )
    assert len(long) >= len(short), (
        f"max_tokens=128 response ({len(long)} chars) not longer than max_tokens=8 ({len(short)} chars)"
    )


@_run("temperature_high_produces_varied_responses")
def t_temperature_high_varies() -> None:
    responses: set[str] = set()
    for _ in range(3):
        text = _direct_vlm(
            "You are a creative assistant.",
            "Name one random animal.",
            temperature=1.0,
            max_tokens=8,
        )
        responses.add(text.strip().lower())
    assert len(responses) >= 1, "no responses collected"


@_run("model_field_in_response_matches_requested_model")
def t_model_field_roundtrip() -> None:
    model = _detected_model or CFG.model
    body = json.dumps({
        "model": model,
        "temperature": 0.1,
        "max_tokens": 8,
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        CFG.lm_studio_url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        obj = json.loads(resp.read())
    returned_model = obj.get("model", "")
    assert returned_model, "model field missing from response"


@_run("stream_false_returns_complete_response_not_chunks")
def t_stream_false_complete() -> None:
    body = json.dumps({
        "model": _detected_model or CFG.model,
        "temperature": 0.1,
        "max_tokens": 16,
        "stream": False,
        "messages": [{"role": "user", "content": "Say hello."}],
    }).encode()
    req = urllib.request.Request(
        CFG.lm_studio_url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    obj = json.loads(raw)
    assert "choices" in obj, "no choices in non-streaming response"
    assert obj["choices"][0].get("finish_reason") in ("stop", "length"), \
        f"unexpected finish_reason: {obj['choices'][0].get('finish_reason')}"


# ── PHASE 7: Panel pipeline with real VLM ─────────────────────────────────────

@_run("panel_pipeline_single_agent_completes_end_to_end")
def t_pipeline_single_agent() -> None:
    t0 = time.time()
    resp = _brain(
        "ag-e2e",
        system_prompt="You are a helpful assistant.",
        text="Reply with the single word: PIPELINE",
        image_url=_IMG_URL,
        temperature=0.0,
        max_tokens=8,
        timeout=CFG.vlm_timeout,
    )
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "")
    assert content and not content.startswith("ERROR:"), f"pipeline failed: {content!r}"
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-e2e", after_ts=t0, timeout=5.0)
    assert ev_d is not None, "no vlm_done SSE event for ag-e2e"


@_run("panel_pipeline_annotate_sse_fires_before_vlm_response")
def t_pipeline_annotate_before_response() -> None:
    t0 = time.time()
    done = threading.Event()
    annotate_ts: list[float] = []
    response_ts: list[float] = []

    def _sender() -> None:
        _brain(
            "ag-timing",
            system_prompt="You are a helpful assistant.",
            text="Say: OK",
            image_url=_IMG_URL,
            temperature=0.0,
            max_tokens=4,
            timeout=CFG.vlm_timeout,
        )
        response_ts.append(time.time())
        done.set()

    threading.Thread(target=_sender, daemon=True).start()
    ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-timing", after_ts=t0, timeout=30.0)
    assert ev is not None, "no annotate event"
    annotate_ts.append(ev["ts"])
    assert done.wait(timeout=CFG.vlm_timeout), "pipeline did not complete"
    assert annotate_ts[0] < response_ts[0], "annotate SSE did not fire before HTTP response"


@_run("panel_pipeline_vlm_done_sse_fires_after_vlm_response")
def t_pipeline_vlm_done_fires() -> None:
    ev = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-timing", timeout=10.0)
    assert ev is not None, "no vlm_done SSE event for ag-timing"
    text = ev["data"].get("text", "")
    assert text and not text.startswith("ERROR:"), f"vlm_done text bad: {text!r}"


@_run("panel_pipeline_strips_agent_field_from_vlm_request")
def t_pipeline_strips_agent() -> None:
    t0 = time.time()
    _brain(
        "ag-strip-check",
        system_prompt="You are a helpful assistant.",
        text="Say: STRIP",
        image_url=_IMG_URL,
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    entries = _read_log()
    req_entries = [e for e in entries if e.get("event") == "vlm_request" and e.get("agent") == "ag-strip-check"]
    assert req_entries, "no vlm_request log entry for ag-strip-check"


@_run("panel_pipeline_image_forwarded_to_real_vlm")
def t_pipeline_image_forwarded() -> None:
    t0 = time.time()
    resp = _brain(
        "ag-img-fwd",
        system_prompt="You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        text="Is there an image attached to this message?",
        image_url=_BLACK64_URL,
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "").strip().lower().rstrip(".,!?")
    assert content == "yes", f"VLM did not see image forwarded through panel: {content!r}"


# ── PHASE 8: Logging with real VLM ────────────────────────────────────────────

@_run("log_vlm_request_logged_for_real_vlm_call")
def t_log_real_vlm_request() -> None:
    size_before = _log_size()
    _brain(
        "ag-log-real",
        system_prompt="You are a helpful assistant.",
        text="Say: LOG",
        image_url=_IMG_URL,
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    time.sleep(0.3)
    assert _log_size() > size_before, "log did not grow after real VLM call"
    entries = _read_log()
    req_entries = [e for e in entries if e.get("event") == "vlm_request" and e.get("agent") == "ag-log-real"]
    assert req_entries, "no vlm_request log entry for ag-log-real"


@_run("log_vlm_response_logged_for_real_vlm_call")
def t_log_real_vlm_response() -> None:
    entries = _read_log()
    resp_entries = [e for e in entries if e.get("event") == "vlm_response"]
    assert resp_entries, "no vlm_response entries after real VLM call"
    last = resp_entries[-1]
    assert "duration_ms" in last, "vlm_response missing duration_ms"
    assert "text" in last, "vlm_response missing text"
    assert last.get("duration_ms", 0) > 0, "duration_ms is zero"


@_run("log_vlm_response_missing_agent_request_id_bug_documented")
def t_log_vlm_response_bug() -> None:
    resp_entries = [e for e in _read_log() if e.get("event") == "vlm_response"]
    assert resp_entries, "no vlm_response entries"
    missing_agent = [e for e in resp_entries if "agent" not in e]
    missing_rid = [e for e in resp_entries if "request_id" not in e]
    assert not missing_agent, (
        f"BUG: {len(missing_agent)} vlm_response entries missing 'agent' — "
        "cannot correlate response to agent under concurrent load"
    )
    assert not missing_rid, (
        f"BUG: {len(missing_rid)} vlm_response entries missing 'request_id' — "
        "cannot correlate response to request under concurrent load"
    )


@_run("log_vlm_response_duration_ms_reflects_real_latency")
def t_log_real_vlm_latency() -> None:
    resp_entries = [e for e in _read_log() if e.get("event") == "vlm_response"]
    assert resp_entries, "no vlm_response entries"
    durations = [e.get("duration_ms", 0) for e in resp_entries]
    assert max(durations) > 100, f"max duration {max(durations)}ms suspiciously low for real VLM"


# ── PHASE 9: Concurrent agents with real VLM ──────────────────────────────────

@_run("two_concurrent_agents_both_complete_with_real_vlm")
def t_two_concurrent_real_vlm() -> None:
    results: dict[str, str] = {}
    lock = threading.Lock()

    def send(ag: str, marker: str) -> None:
        resp = _brain(
            ag,
            system_prompt="Repeat back the exact user message word for word. Output only the repeated text.",
            text=marker,
            image_url=_IMG_URL,
            temperature=0.0,
            max_tokens=16,
            timeout=CFG.vlm_timeout,
        )
        choices = resp.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        with lock:
            results[ag] = content

    m1 = f"AGENT1-{uuid.uuid4().hex[:6].upper()}"
    m2 = f"AGENT2-{uuid.uuid4().hex[:6].upper()}"
    t1 = threading.Thread(target=send, args=("ag-conc-1", m1), daemon=True)
    t2 = threading.Thread(target=send, args=("ag-conc-2", m2), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=CFG.vlm_timeout + 10)
    t2.join(timeout=CFG.vlm_timeout + 10)

    assert "ag-conc-1" in results and "ag-conc-2" in results, \
        f"missing results: {set(['ag-conc-1','ag-conc-2']) - set(results)}"
    assert not results["ag-conc-1"].startswith("ERROR:"), f"ag-conc-1 error: {results['ag-conc-1']!r}"
    assert not results["ag-conc-2"].startswith("ERROR:"), f"ag-conc-2 error: {results['ag-conc-2']!r}"


@_run("concurrent_agents_responses_not_cross_contaminated")
def t_concurrent_no_cross_contamination() -> None:
    results: dict[str, str] = {}
    lock = threading.Lock()
    markers: dict[str, str] = {
        f"ag-iso-{i}": f"ISOLATE-{uuid.uuid4().hex[:6].upper()}" for i in range(3)
    }

    def send(ag: str, marker: str) -> None:
        resp = _brain(
            ag,
            system_prompt="Repeat back the exact user message word for word. Output only the repeated text.",
            text=marker,
            image_url=_IMG_URL,
            temperature=0.0,
            max_tokens=16,
            timeout=CFG.vlm_timeout,
        )
        choices = resp.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        with lock:
            results[ag] = content

    threads = [threading.Thread(target=send, args=(ag, m), daemon=True) for ag, m in markers.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=CFG.vlm_timeout + 10)

    for ag, marker in markers.items():
        content = results.get(ag, "")
        assert marker in content, (
            f"agent {ag} expected marker {marker!r} in response, got: {content!r} — possible cross-contamination"
        )


# ── PHASE 10: 64x64 pixel travel with real VLM pipeline ──────────────────────

@_run("black_64x64_travels_untouched_no_overlay_real_vlm")
def t_black_travels_real_vlm() -> None:
    t0 = time.time()
    rid_box: list[str] = []

    def _watcher() -> None:
        ev = _wait_sse("annotate", lambda d: d.get("agent") == "ag-black-real", after_ts=t0)
        if ev:
            rid = ev["data"].get("request_id", "")
            raw = ev["data"].get("raw_b64", "")
            if rid:
                rid_box.append(rid)
                with _pump_skip_lock:
                    _pump_skip.add(rid)
                _post_result(rid, raw)

    threading.Thread(target=_watcher, daemon=True).start()
    resp = _brain(
        "ag-black-real",
        system_prompt="You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        text="Is this image completely black?",
        image_url=_BLACK64_URL,
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-black-real", after_ts=t0, timeout=10.0)
    assert ev_d is not None, "no vlm_done"
    ann_b64 = ev_d["data"].get("annotated_b64", "")
    assert ann_b64, "annotated_b64 empty"
    pixels = _decode_png_pixels(ann_b64)
    non_black = [p for p in pixels if p[:3] != (0, 0, 0)]
    assert not non_black, f"{len(non_black)} pixels changed without overlay — image corrupted in transit"
    if rid_box:
        with _pump_skip_lock:
            _pump_skip.discard(rid_box[0])


@_run("red_overlay_on_black_image_vlm_confirms_red_shape_visible")
def t_red_overlay_vlm_confirms() -> None:
    t0 = time.time()
    overlay = {
        "type": "overlay",
        "points": [[150, 150], [850, 150], [850, 850], [150, 850]],
        "stroke": "#ff0000",
        "stroke_width": 20,
        "closed": True,
    }
    resp = _brain(
        "ag-red-confirm",
        system_prompt="You are a vision assistant. Answer only yes or no, lowercase, no punctuation.",
        text="Do you see a red rectangle or red border drawn on this image?",
        image_url=_BLACK64_URL,
        actions=[overlay],
        temperature=0.0,
        max_tokens=4,
        timeout=CFG.vlm_timeout,
    )
    choices = resp.get("choices", [])
    assert choices, "no choices"
    content = choices[0].get("message", {}).get("content", "").strip().lower().rstrip(".,!?")
    assert content == "yes", f"VLM did not confirm red overlay on black image: {content!r}"

    ev_d = _wait_sse("vlm_done", lambda d: d.get("agent") == "ag-red-confirm", after_ts=t0, timeout=10.0)
    assert ev_d is not None, "no vlm_done"
    ann_b64 = ev_d["data"].get("annotated_b64", "")
    assert ann_b64 and ann_b64 != _BLACK64_B64, "annotated_b64 not modified by overlay"
    pixels = _decode_png_pixels(ann_b64)
    non_black = [p for p in pixels if p[:3] != (0, 0, 0)]
    assert non_black, "pixel-level: no non-black pixels despite red overlay"


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


ALL_TESTS: list[Any] = [
    t_lm_studio_reachable, t_lm_studio_models, t_lm_studio_direct,
    t_panel_ready, t_sse_connected,
    t_html_render_annotated, t_html_posts_result, t_html_catch_fallback, t_html_norm_constant,
    t_parrot_via_panel, t_parrot_direct, t_parrot_deterministic,
    t_vlm_black_image_yes, t_vlm_white_image_no, t_vlm_black_via_panel,
    t_vlm_sees_red_rect, t_vlm_does_not_see_red_rectangle_on_unannotated_black_image,
    t_annotated_differs_pixel_level,
    t_max_tokens_1, t_max_tokens_length_difference, t_temperature_high_varies,
    t_model_field_roundtrip, t_stream_false_complete,
    t_pipeline_single_agent, t_pipeline_annotate_before_response,
    t_pipeline_vlm_done_fires, t_pipeline_strips_agent, t_pipeline_image_forwarded,
    t_log_real_vlm_request, t_log_real_vlm_response, t_log_vlm_response_bug,
    t_log_real_vlm_latency,
    t_two_concurrent_real_vlm, t_concurrent_no_cross_contamination,
    t_black_travels_real_vlm, t_red_overlay_vlm_confirms,
]


def main() -> None:
    print("\n=== test_pipeline_integration — Full Pipeline Integration (37 tests) ===\n")
    print("  REQUIRES: LM Studio running at 127.0.0.1:1235 with a vision model loaded.\n")
    print("  Tests: parrot echo, black/white image vision, red overlay detection,")
    print("         hyperparameters, pixel-level image travel, concurrent agents.\n")

    for p in (HERE / "test_pipeline_integration.json", HERE / "franz-log.jsonl"):
        p.unlink(missing_ok=True)

    print("[1/5] Verifying LM Studio is reachable...")
    try:
        with urllib.request.urlopen(CFG.lm_studio_models_url, timeout=3) as r:
            pass
    except Exception as e:
        print(f"\n  FATAL: LM Studio not reachable at {CFG.lm_studio_models_url}")
        print(f"  Start LM Studio, load a vision model, and enable the local server.\n")
        sys.exit(2)

    print("[2/5] Starting panel.py...")
    panel_proc = _start_panel()
    print(f"      Panel ready at {CFG.panel_url}")

    print("[3/5] Opening Chrome...")
    try:
        chrome = _open_chrome()
        print(f"      Chrome PID {chrome.pid}")
        time.sleep(1.5)
    except RuntimeError as e:
        print(f"      WARNING: {e} — continuing without Chrome")
        chrome = None

    print("[4/5] Starting SSE listener + auto-result pump...")
    stop = threading.Event()
    threading.Thread(target=_sse_reader, args=(stop,), daemon=True).start()
    threading.Thread(target=_pump, args=(stop,), daemon=True).start()
    time.sleep(1.0)

    print("[5/5] Running all tests...\n")
    for fn in ALL_TESTS:
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
        log_src.rename(HERE / "test_pipeline_integration.jsonl")

    if failed:
        print(f"  {failed} test(s) FAILED — see {CFG.report_path}\n")
        sys.exit(1)
    else:
        print("  All tests passed.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
