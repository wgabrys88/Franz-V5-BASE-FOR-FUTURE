import base64
import http.server
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class _Config:
    host: str = "127.0.0.1"
    port: int = 1236
    vlm_url: str = "http://127.0.0.1:1235/v1/chat/completions"

CFG: _Config = _Config()
PANEL_HTML: Path = Path(__file__).resolve().parent / "panel.html"
WIN32_PATH: Path = Path(__file__).resolve().parent / "win32.py"
HERE: Path = Path(__file__).resolve().parent


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record.msg) if isinstance(record.msg, dict) else super().format(record)


_log_handler = logging.FileHandler(HERE / "franz-log.jsonl", encoding="utf-8")
_log_handler.setFormatter(_JsonFormatter())
_logger = logging.getLogger("franz")
_logger.setLevel(logging.DEBUG)
_logger.addHandler(_log_handler)

_pending: dict[str, dict[str, Any]] = {}
_pending_lock: threading.Lock = threading.Lock()
_sse_lock: threading.Lock = threading.Lock()
_sse_queues: list[Any] = []


def _sse_push(event: str, data: dict[str, Any]) -> None:
    import queue as _q
    chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(chunk)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


def _capture(region: str, w: int = 640, h: int = 640) -> str:
    cmd: list[str] = [sys.executable, str(WIN32_PATH), "capture", "--width", str(w), "--height", str(h)]
    if region:
        cmd.extend(["--region", region])
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return ""
    return base64.b64encode(proc.stdout).decode("ascii")


def _win32(args: list[str], region: str) -> None:
    cmd: list[str] = [sys.executable, str(WIN32_PATH)] + args
    if region and "--region" not in args:
        cmd.extend(["--region", region])
    subprocess.run(cmd, capture_output=True)


def _dispatch_action(act: dict[str, Any], region: str) -> None:
    t: str = act.get("type", "")
    _logger.debug({"event": "action_dispatched", "ts": time.time(), **{k: v for k, v in act.items()}})
    match t:
        case "drag":
            _win32(["drag",
                    "--from_pos", f"{act['x1']},{act['y1']}",
                    "--to_pos",   f"{act['x2']},{act['y2']}"], region)
        case "click":
            _win32(["click", "--pos", f"{act['x']},{act['y']}"], region)
        case "double_click":
            _win32(["double_click", "--pos", f"{act['x']},{act['y']}"], region)
        case "right_click":
            _win32(["right_click", "--pos", f"{act['x']},{act['y']}"], region)
        case "type_text":
            _win32(["type_text", "--text", act.get("text", "")], region)
        case "press_key":
            _win32(["press_key", "--key", act.get("key", "")], region)
        case "hotkey":
            _win32(["hotkey", "--keys", act.get("keys", "")], region)
        case "scroll_up":
            _win32(["scroll_up", "--pos", f"{act['x']},{act['y']}",
                    "--clicks", str(act.get("clicks", 3))], region)
        case "scroll_down":
            _win32(["scroll_down", "--pos", f"{act['x']},{act['y']}",
                    "--clicks", str(act.get("clicks", 3))], region)
        case "cursor_pos":
            _win32(["cursor_pos"], region)


class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def _json(self, code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/":
            body = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/ready":
            self._json(200, {"ok": True})
        elif path == "/events":
            import queue as _q
            q: _q.Queue[bytes | None] = _q.Queue()
            with _sse_lock:
                _sse_queues.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        chunk = q.get(timeout=25)
                    except Exception:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                with _sse_lock:
                    try:
                        _sse_queues.remove(q)
                    except ValueError:
                        pass
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if path == "/v1/chat/completions":
            try:
                req_body: dict[str, Any] = json.loads(body)
            except Exception:
                self._json(400, {"error": "bad json"})
                return

            region: str = req_body.pop("region", "")
            agent: str = req_body.pop("agent", "default")
            capture_size: list[int] = req_body.pop("capture_size", [640, 640])

            actions: list[dict[str, Any]] = []
            overlays: list[dict[str, Any]] = []
            raw_b64: str = ""
            messages: list[Any] = req_body.get("messages", [])

            for msg in messages:
                content = msg.get("content", "")
                if not isinstance(content, list):
                    continue
                stripped: list[dict[str, Any]] = []
                for part in content:
                    if part.get("type") == "actions":
                        for act in part.get("actions", []):
                            (overlays if act.get("type") == "overlay" else actions).append(act)
                    else:
                        stripped.append(part)
                msg["content"] = stripped

            for act in actions:
                _dispatch_action(act, region)

            for msg in messages:
                content = msg.get("content", "")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if part.get("type") == "image_url":
                        url: str = part.get("image_url", {}).get("url", "")
                        if url == "":
                            raw_b64 = _capture(region, capture_size[0], capture_size[1])
                            part["image_url"]["url"] = f"data:image/png;base64,{raw_b64}"
                        else:
                            raw_b64 = url.split(",", 1)[1]

            rid = str(uuid.uuid4())
            event = threading.Event()
            with _pending_lock:
                _pending[rid] = {"event": event, "result": ""}

            t_req = time.time()
            _logger.debug({"event": "vlm_request", "ts": t_req, "model": req_body.get("model", ""), "agent": agent, "overlays": len(overlays)})

            _sse_push("annotate", {
                "request_id": rid,
                "raw_b64": raw_b64,
                "overlays": overlays,
                "model": req_body.get("model", ""),
                "agent": agent,
            })

            event.wait(timeout=30)

            with _pending_lock:
                annotated_b64 = _pending.pop(rid, {}).get("result", raw_b64)

            if annotated_b64 and raw_b64:
                for msg in messages:
                    content = msg.get("content", "")
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if part.get("type") == "image_url":
                            part["image_url"]["url"] = f"data:image/png;base64,{annotated_b64}"

            fwd_body = json.dumps(req_body).encode()
            fwd_req = urllib.request.Request(
                CFG.vlm_url, data=fwd_body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(fwd_req, timeout=360) as resp:
                    resp_bytes = resp.read()
                resp_obj: dict[str, Any] = json.loads(resp_bytes)
                text: str = ""
                choices = resp_obj.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    text = choices[0].get("message", {}).get("content", "")
                _logger.debug({"event": "vlm_response", "ts": time.time(), "duration_ms": round((time.time() - t_req) * 1000), "text": text, "annotated": annotated_b64 != raw_b64})
                _sse_push("vlm_done", {"request_id": rid, "text": text, "annotated_b64": annotated_b64, "agent": agent})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)
            except Exception as exc:
                _logger.debug({"event": "vlm_error", "ts": time.time(), "error": str(exc)})
                _sse_push("vlm_done", {"request_id": rid, "text": f"ERROR: {exc}", "annotated_b64": annotated_b64, "agent": agent})
                self._json(502, {"error": str(exc)})

        elif path == "/result":
            try:
                data: dict[str, Any] = json.loads(body)
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            rid = data.get("request_id", "")
            annotated = data.get("annotated_b64", "")
            with _pending_lock:
                slot = _pending.get(rid)
            if slot:
                slot["result"] = annotated
                slot["event"].set()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "unknown request_id"})

        else:
            self._json(404, {"error": "not found"})


def start(host: str = CFG.host, port: int = CFG.port) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer((host, port), PanelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    start().serve_forever()
