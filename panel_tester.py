import copy
import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class _Cfg:
    endpoint: str = "http://127.0.0.1:1236/v1/chat/completions"
    model: str = "qwen3.5-0.8b"
    timeout: int = 120

_CFG = _Cfg()
_WIN32: Path = Path(__file__).resolve().parent / "win32.py"

_region: str = ""
_cap_w: int = 640
_cap_h: int = 640

_SYS_PARROT = "Repeat the user message exactly as-is."
_SYS_OBSERVE = "Describe what you see in one sentence."
_SYS_SHAPES = "List all colored shapes and lines you see."

def _img() -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": ""}}

def _acts(*actions: dict[str, Any]) -> dict[str, Any]:
    return {"type": "actions", "actions": list(actions)}

def _ov_line(x1: int, y1: int, x2: int, y2: int, color: str) -> dict[str, Any]:
    return {"type": "overlay", "points": [[x1, y1], [x2, y2]], "stroke": color, "stroke_width": 2, "closed": False}

def _ov_rect(x1: int, y1: int, x2: int, y2: int, color: str, fill: str | None = None) -> dict[str, Any]:
    ov: dict[str, Any] = {"type": "overlay", "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]], "stroke": color, "stroke_width": 2, "closed": True}
    if fill:
        ov["fill"] = fill
    return ov

def _ov_cross(x: int, y: int, r: int, color: str) -> list[dict[str, Any]]:
    return [
        {"type": "overlay", "points": [[x - r, y], [x + r, y]], "stroke": color, "stroke_width": 2, "closed": False},
        {"type": "overlay", "points": [[x, y - r], [x, y + r]], "stroke": color, "stroke_width": 2, "closed": False},
    ]

def _msg(sys_prompt: str, *parts: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "system", "content": sys_prompt}, {"role": "user", "content": list(parts)}]

def _text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}

_TESTS: list[dict[str, Any]] = [
    {
        "id": "T01", "needs_prep": False,
        "name": "Parrot click center",
        "desc": "Clicks 500,500 on canvas. Crosshair overlay. VLM echoes user message — confirms full round-trip.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "parrot",
            "messages": _msg(_SYS_PARROT,
                _text("CLICK_CENTER_TEST"),
                _img(),
                _acts({"type": "click", "x": 500, "y": 500}, *_ov_cross(500, 500, 20, "#4a9eff")),
            ),
        },
    },
    {
        "id": "T02", "needs_prep": False,
        "name": "No-image text only",
        "desc": "No image_url. Pure text. Verifies panel handles missing image — SSE annotate fires with empty raw_b64.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "parrot",
            "messages": _msg(_SYS_PARROT, _text("NO_IMAGE_TEST")),
        },
    },
    {
        "id": "T03", "needs_prep": False,
        "name": "Screenshot only",
        "desc": "image_url present, no actions, no overlays. Verifies capture pipeline and annotate/result cycle.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "observer",
            "messages": _msg(_SYS_OBSERVE, _img()),
        },
    },
    {
        "id": "T04", "needs_prep": False,
        "name": "Right-click center",
        "desc": "right_click 500,500. Orange square overlay. Context menu should appear on canvas.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "clicker",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "right_click", "x": 500, "y": 500}, _ov_rect(480, 480, 520, 520, "#f0a000")),
            ),
        },
    },
    {
        "id": "T05", "needs_prep": False,
        "name": "Double-click center",
        "desc": "double_click 500,500. Blue square overlay. Verifies double-click timing.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "clicker",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "double_click", "x": 500, "y": 500}, _ov_rect(485, 485, 515, 515, "#4a9eff")),
            ),
        },
    },
    {
        "id": "T06", "needs_prep": False,
        "name": "Drag diagonal TL to BR",
        "desc": "drag 50,50 -> 950,950. Red diagonal line overlay. Should draw a visible stroke across canvas.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "dragger",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "drag", "x1": 50, "y1": 50, "x2": 950, "y2": 950}, _ov_line(50, 50, 950, 950, "#ff4455")),
            ),
        },
    },
    {
        "id": "T07", "needs_prep": False,
        "name": "Drag diagonal TR to BL",
        "desc": "drag 950,50 -> 50,950. Blue diagonal line overlay. Second stroke crossing the first.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "dragger",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "drag", "x1": 950, "y1": 50, "x2": 50, "y2": 950}, _ov_line(950, 50, 50, 950, "#4a9eff")),
            ),
        },
    },
    {
        "id": "T08", "needs_prep": False,
        "name": "Drag horizontal top",
        "desc": "drag 50,50 -> 950,50. Green horizontal line overlay at top edge.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "dragger",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "drag", "x1": 50, "y1": 50, "x2": 950, "y2": 50}, _ov_line(50, 50, 950, 50, "#3ecf8e")),
            ),
        },
    },
    {
        "id": "T09", "needs_prep": False,
        "name": "Drag vertical left",
        "desc": "drag 50,50 -> 50,950. Orange vertical line overlay at left edge.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "dragger",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "drag", "x1": 50, "y1": 50, "x2": 50, "y2": 950}, _ov_line(50, 50, 50, 950, "#f0a000")),
            ),
        },
    },
    {
        "id": "T10", "needs_prep": False,
        "name": "Click corners x4",
        "desc": "Clicks all 4 corners (10,10 / 990,10 / 10,990 / 990,990). Red corner-box overlays. Tests edge coordinate mapping.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "clicker",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts(
                    {"type": "click", "x": 10, "y": 10},
                    {"type": "click", "x": 990, "y": 10},
                    {"type": "click", "x": 10, "y": 990},
                    {"type": "click", "x": 990, "y": 990},
                    _ov_rect(5, 5, 25, 25, "#ff4455"),
                    _ov_rect(975, 5, 995, 25, "#ff4455"),
                    _ov_rect(5, 975, 25, 995, "#ff4455"),
                    _ov_rect(975, 975, 995, 995, "#ff4455"),
                ),
            ),
        },
    },
    {
        "id": "T11", "needs_prep": True,
        "name": "Type text",
        "desc": "type_text 'Hello Franz'. Prepare Notepad (or click on canvas text tool first). Verifies keyboard dispatch.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "keyboard",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "type_text", "text": "Hello Franz"}),
            ),
        },
    },
    {
        "id": "T12", "needs_prep": True,
        "name": "Press key Escape",
        "desc": "press_key 'escape'. Useful to dismiss any open dialog. Verifies single key press.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "keyboard",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "press_key", "key": "escape"}),
            ),
        },
    },
    {
        "id": "T13", "needs_prep": True,
        "name": "Hotkey Ctrl+Z (undo)",
        "desc": "hotkey 'ctrl+z'. Should undo the last paint stroke. Verifies multi-key hotkey dispatch.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "keyboard",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts({"type": "hotkey", "keys": "ctrl+z"}),
            ),
        },
    },
    {
        "id": "T14", "needs_prep": False,
        "name": "Scroll up center",
        "desc": "scroll_up 500,500 clicks=5. Arrow-up overlay. Canvas should scroll up if scrollable.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "scroller",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts(
                    {"type": "scroll_up", "x": 500, "y": 500, "clicks": 5},
                    {"type": "overlay", "points": [[500, 530], [490, 510], [510, 510]], "stroke": "#3ecf8e", "stroke_width": 2, "closed": True},
                ),
            ),
        },
    },
    {
        "id": "T15", "needs_prep": False,
        "name": "Scroll down center",
        "desc": "scroll_down 500,500 clicks=5. Arrow-down overlay. Canvas should scroll back down.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "scroller",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts(
                    {"type": "scroll_down", "x": 500, "y": 500, "clicks": 5},
                    {"type": "overlay", "points": [[500, 470], [490, 490], [510, 490]], "stroke": "#ff4455", "stroke_width": 2, "closed": True},
                ),
            ),
        },
    },
    {
        "id": "T16", "needs_prep": False,
        "name": "Multi-overlay shapes",
        "desc": "No actions. 3 overlays: filled rect, open polyline, closed triangle. Verifies all overlay shape types render in browser.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "overlay-test",
            "messages": _msg(_SYS_SHAPES,
                _img(),
                _acts(
                    _ov_rect(200, 200, 400, 400, "#c084fc", "rgba(192,132,252,0.15)"),
                    {"type": "overlay", "points": [[100, 500], [300, 400], [500, 500], [700, 400], [900, 500]], "stroke": "#4a9eff", "stroke_width": 2, "closed": False},
                    {"type": "overlay", "points": [[500, 200], [650, 500], [350, 500]], "stroke": "#3ecf8e", "stroke_width": 2, "closed": True},
                ),
            ),
        },
    },
    {
        "id": "T17", "needs_prep": False,
        "name": "Multi-agent alpha",
        "desc": "Sends to agent 'alpha'. Verifies a new pane is created in the browser for this agent name.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "alpha",
            "messages": _msg(_SYS_OBSERVE, _img()),
        },
    },
    {
        "id": "T18", "needs_prep": False,
        "name": "Multi-agent beta",
        "desc": "Sends to agent 'beta'. Verifies third pane creation and grid column reflow.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "beta",
            "messages": _msg(_SYS_OBSERVE, _img()),
        },
    },
    {
        "id": "T19", "needs_prep": False,
        "name": "Capture size 320x320",
        "desc": "capture_size [320,320]. Verifies panel passes correct --width/--height to win32 capture subprocess.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 64, "stream": False,
            "agent": "observer",
            "capture_size": [320, 320],
            "messages": _msg(_SYS_OBSERVE, _img()),
        },
    },
    {
        "id": "T20", "needs_prep": False,
        "name": "Full combo",
        "desc": "drag 200,200->800,800 + click 500,500. Crosshair + diagonal overlay + image. Stress-tests full pipeline.",
        "payload": {
            "model": _CFG.model, "temperature": 0.1, "max_tokens": 128, "stream": False,
            "agent": "combo",
            "messages": _msg(_SYS_OBSERVE,
                _img(),
                _acts(
                    {"type": "drag", "x1": 200, "y1": 200, "x2": 800, "y2": 800},
                    {"type": "click", "x": 500, "y": 500},
                    _ov_line(200, 200, 800, 800, "#ff4455"),
                    *_ov_cross(500, 500, 25, "#4a9eff"),
                ),
            ),
        },
    },
]


def _send(payload: dict[str, Any]) -> str:
    body = copy.deepcopy(payload)
    body.setdefault("region", _region)
    body.setdefault("capture_size", [_cap_w, _cap_h])
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        _CFG.endpoint, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_CFG.timeout) as resp:
            obj = json.loads(resp.read())
        choices: list[Any] = obj.get("choices", [])
        return choices[0].get("message", {}).get("content", "").strip() if choices else ""
    except Exception as exc:
        return f"ERROR: {exc}"


def run() -> None:
    print(f"\nFranz Panel Tester — {len(_TESTS)} tests — region: {_region or 'full screen'} — capture: {_cap_w}x{_cap_h}\n")
    for t in _TESTS:
        print(f"[{t['id']}] {t['name']}")
        print(f"     {t['desc']}")
        prompt = "     Prepare your window, then press Enter to run, or 's'+Enter to skip: " if t["needs_prep"] else "     Press Enter to run, or 's'+Enter to skip: "
        ans = input(prompt).strip().lower()
        if ans.startswith("s"):
            print("     SKIPPED\n" + "-" * 60)
            continue
        result = _send(t["payload"])
        print(f"     VLM: {result}\n" + "-" * 60)


if __name__ == "__main__":
    proc = subprocess.run([sys.executable, str(_WIN32), "select_region"], capture_output=True, text=True)
    if proc.returncode == 2:
        raise SystemExit(0)
    _region = proc.stdout.strip()
    proc2 = subprocess.run([sys.executable, str(_WIN32), "select_region"], capture_output=True, text=True)
    if proc2.returncode != 2 and proc2.stdout.strip():
        x1, _y1, x2, _y2 = map(int, proc2.stdout.strip().split(","))
        scale = (x2 - x1) / 1000
        _cap_w = round(1000 * scale)
        _cap_h = round(1000 * scale)
    run()
