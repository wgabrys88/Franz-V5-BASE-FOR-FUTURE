import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

_ENDPOINT: str = "http://127.0.0.1:1236/v1/chat/completions"
_MODEL: str = "qwen3.5-0.8b"
_WIN32: Path = Path(__file__).resolve().parent / "win32.py"
_SYS: str = (
    "You detect human heads in images. "
    "Red circle overlays = heads detected in the previous frame, shown for reference. "
    "For EACH human head you see, output exactly: HEAD: x,y "
    "where x,y are normalized 0-1000 coordinates of the head center. "
    "One per line. No other text."
)

_region: str = ""
_cap_w: int = 640
_cap_h: int = 640


def _build_overlays(heads: list[tuple[int, int]]) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    for x, y in heads:
        r = 30
        overlays.append({
            "type": "overlay",
            "points": [
                [x - r, y], [x - r + 6, y - 10],
                [x, y - r], [x + 6, y - r + 10],
                [x + r, y], [x + r - 6, y + 10],
                [x, y + r], [x - 6, y + r - 10],
                [x - r, y],
            ],
            "stroke": "#ff2233",
            "stroke_width": 3,
            "closed": False,
        })
        overlays.append({
            "type": "overlay",
            "points": [[x - 12, y], [x + 12, y]],
            "stroke": "#ff2233",
            "stroke_width": 2,
            "closed": False,
        })
        overlays.append({
            "type": "overlay",
            "points": [[x, y - 12], [x, y + 12]],
            "stroke": "#ff2233",
            "stroke_width": 2,
            "closed": False,
        })
    return overlays


def _parse_heads(text: str) -> list[tuple[int, int]]:
    return [
        (int(m.group(1)), int(m.group(2)))
        for m in re.finditer(r"HEAD:\s*(\d+)\s*,\s*(\d+)", text, re.IGNORECASE)
    ]


def run() -> None:
    obs: str = ""
    step: int = 0
    while True:
        step += 1
        overlays = _build_overlays(_parse_heads(obs)) if step > 1 else []
        body = json.dumps({
            "model": _MODEL,
            "temperature": 0.1,
            "top_p": 0.9,
            "max_tokens": 128,
            "stream": False,
            "region": _region,
            "agent": "aimbot",
            "capture_size": [_cap_w, _cap_h],
            "messages": [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": ""}},
                    {"type": "actions", "actions": overlays},
                ]},
            ],
        }).encode()
        req = urllib.request.Request(
            _ENDPOINT, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                obj = json.loads(resp.read())
            choices: list[Any] = obj.get("choices", [])
            obs = choices[0].get("message", {}).get("content", "").strip() if choices else ""
        except Exception as exc:
            obs = f"ERROR: {exc}"


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
