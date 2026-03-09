import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

_ENDPOINT: str = "http://127.0.0.1:1236/v1/chat/completions"
_MODEL: str = "qwen3.5-0.8b"
_WIN32: Path = Path(__file__).resolve().parent / "win32.py"
_SYS: str = (
    "You observe an MS Paint canvas. Describe ONLY what you physically see: "
    "list each visible stroke, mark, or change with approximate positions. "
    "Be brief, under 80 words."
)

_ACTIONS: list[dict[str, Any]] = [
    {
        "label": "DRAG: top-left to bottom-right diagonal",
        "action": {"type": "drag", "x1": 10, "y1": 10, "x2": 990, "y2": 990},
        "overlay": {"type": "overlay", "points": [[10, 10], [990, 990]],
                    "stroke": "#ff4455", "stroke_width": 3, "closed": False},
    },
    {
        "label": "DRAG: top-right to bottom-left diagonal",
        "action": {"type": "drag", "x1": 990, "y1": 10, "x2": 10, "y2": 990},
        "overlay": {"type": "overlay", "points": [[990, 10], [10, 990]],
                    "stroke": "#4a9eff", "stroke_width": 3, "closed": False},
    },
    {
        "label": "DRAG: horizontal stroke top edge",
        "action": {"type": "drag", "x1": 10, "y1": 10, "x2": 990, "y2": 10},
        "overlay": {"type": "overlay", "points": [[10, 10], [990, 10]],
                    "stroke": "#3ecf8e", "stroke_width": 3, "closed": False},
    },
    {
        "label": "DRAG: vertical stroke left edge",
        "action": {"type": "drag", "x1": 10, "y1": 10, "x2": 10, "y2": 990},
        "overlay": {"type": "overlay", "points": [[10, 10], [10, 990]],
                    "stroke": "#f0a000", "stroke_width": 3, "closed": False},
    },
    {
        "label": "DRAG: horizontal stroke bottom edge",
        "action": {"type": "drag", "x1": 10, "y1": 990, "x2": 990, "y2": 990},
        "overlay": {"type": "overlay", "points": [[10, 990], [990, 990]],
                    "stroke": "#c084fc", "stroke_width": 3, "closed": False},
    },
    {
        "label": "DRAG: vertical stroke right edge",
        "action": {"type": "drag", "x1": 990, "y1": 10, "x2": 990, "y2": 990},
        "overlay": {"type": "overlay", "points": [[990, 10], [990, 990]],
                    "stroke": "#c084fc", "stroke_width": 3, "closed": False},
    },
    {
        "label": "CLICK: top-left corner",
        "action": {"type": "click", "x": 10, "y": 10},
        "overlay": {"type": "overlay", "points": [[5, 5], [15, 5], [15, 15], [5, 15]],
                    "stroke": "#ff4455", "stroke_width": 2, "closed": True},
    },
    {
        "label": "CLICK: top-right corner",
        "action": {"type": "click", "x": 990, "y": 10},
        "overlay": {"type": "overlay", "points": [[985, 5], [995, 5], [995, 15], [985, 15]],
                    "stroke": "#ff4455", "stroke_width": 2, "closed": True},
    },
    {
        "label": "CLICK: bottom-left corner",
        "action": {"type": "click", "x": 10, "y": 990},
        "overlay": {"type": "overlay", "points": [[5, 985], [15, 985], [15, 995], [5, 995]],
                    "stroke": "#ff4455", "stroke_width": 2, "closed": True},
    },
    {
        "label": "CLICK: bottom-right corner",
        "action": {"type": "click", "x": 990, "y": 990},
        "overlay": {"type": "overlay", "points": [[985, 985], [995, 985], [995, 995], [985, 995]],
                    "stroke": "#ff4455", "stroke_width": 2, "closed": True},
    },
    {
        "label": "DOUBLE_CLICK: canvas center",
        "action": {"type": "double_click", "x": 500, "y": 500},
        "overlay": {"type": "overlay", "points": [[490, 490], [510, 490], [510, 510], [490, 510]],
                    "stroke": "#4a9eff", "stroke_width": 2, "closed": True},
    },
    {
        "label": "RIGHT_CLICK: canvas center",
        "action": {"type": "right_click", "x": 500, "y": 500},
        "overlay": {"type": "overlay", "points": [[488, 488], [512, 488], [512, 512], [488, 512]],
                    "stroke": "#f0a000", "stroke_width": 2, "closed": True},
    },
    {
        "label": "SCROLL_UP: top-center",
        "action": {"type": "scroll_up", "x": 500, "y": 10, "clicks": 3},
        "overlay": {"type": "overlay", "points": [[500, 30], [490, 10], [510, 10]],
                    "stroke": "#3ecf8e", "stroke_width": 2, "closed": True},
    },
    {
        "label": "SCROLL_UP: bottom-center",
        "action": {"type": "scroll_up", "x": 500, "y": 990, "clicks": 3},
        "overlay": {"type": "overlay", "points": [[500, 970], [490, 990], [510, 990]],
                    "stroke": "#3ecf8e", "stroke_width": 2, "closed": True},
    },
    {
        "label": "DRAG: border rectangle",
        "action": {"type": "drag", "x1": 10, "y1": 10, "x2": 990, "y2": 10},
        "overlay": {"type": "overlay",
                    "points": [[10, 10], [990, 10], [990, 990], [10, 990]],
                    "stroke": "#c084fc", "stroke_width": 2, "closed": True},
    },
]

_step: int = 0
_region: str = ""
_cap_w: int = 640
_cap_h: int = 640


def on_action_execution(obs: str) -> None:
    global _step
    if _step >= len(_ACTIONS):
        return
    idx = _step
    _step += 1
    entry = _ACTIONS[idx]
    label: str = entry["label"]
    body = json.dumps({
        "model": _MODEL,
        "temperature": 0.7,
        "top_p": 0.8,
        "presence_penalty": 1.5,
        "top_k": 20,
        "max_tokens": 256,
        "stream": False,
        "region": _region,
        "agent": "mspaint",
        "capture_size": [_cap_w, _cap_h],
        "messages": [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": [
                {"type": "text", "text": f"[STEP {_step}/{len(_ACTIONS)}] {label}\nPrior observation: {obs}"},
                {"type": "image_url", "image_url": {"url": ""}},
                {"type": "actions", "actions": [entry["action"], entry["overlay"]]},
            ]},
        ],
    }).encode()
    req = urllib.request.Request(
        _ENDPOINT, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=360) as resp:
            obj = json.loads(resp.read())
        choices: list[Any] = obj.get("choices", [])
        result: str = choices[0].get("message", {}).get("content", "").strip() if choices else ""
    except Exception as exc:
        result = f"ERROR: {exc}"
    if result:
        on_action_execution(result)


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
    on_action_execution("")
