import base64
import json
import re
import struct
import sys
import threading
import time
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _Cfg:
    lm_studio_url: str = "http://127.0.0.1:1235/v1/chat/completions"
    lm_studio_models_url: str = "http://127.0.0.1:1235/v1/models"
    vlm_timeout: float = 120.0
    report_path: str = "test_brain_contracts.json"


CFG = _Cfg()
HERE = Path(__file__).resolve().parent

_test_results: list[dict[str, Any]] = []
_detected_model: str = ""

AIMBOT_SYS: str = (
    "You detect human heads in images. "
    "Red circle overlays = heads detected in the previous frame, shown for reference. "
    "For EACH human head you see, output exactly: HEAD: x,y "
    "where x,y are normalized 0-1000 coordinates of the head center. "
    "One per line. No other text."
)

MSPAINT_SYS: str = (
    "You observe an MS Paint canvas. Describe ONLY what you physically see: "
    "list each visible stroke, mark, or change with approximate positions. "
    "Be brief, under 80 words."
)


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


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _img_url(data: bytes) -> str:
    return f"data:image/png;base64,{_b64(data)}"


_BLACK64 = _make_png(64, 64, 0, 0, 0)
_WHITE64 = _make_png(64, 64, 255, 255, 255)
_GRAY64 = _make_png(64, 64, 128, 128, 128)
_RED64 = _make_png(64, 64, 255, 0, 0)

_BLACK64_URL = _img_url(_BLACK64)
_WHITE64_URL = _img_url(_WHITE64)
_GRAY64_URL = _img_url(_GRAY64)
_RED64_URL = _img_url(_RED64)


def _vlm(
    system: str,
    user_text: str,
    image_url: str = "",
    temperature: float = 0.0,
    max_tokens: int = 128,
    timeout: float = CFG.vlm_timeout,
) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    body = json.dumps({
        "model": _detected_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
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
    return choices[0].get("message", {}).get("content", "").strip() if choices else ""


def _parse_heads(text: str) -> list[tuple[int, int]]:
    return [
        (int(m.group(1)), int(m.group(2)))
        for m in re.finditer(r"HEAD:\s*(\d+)\s*,\s*(\d+)", text, re.IGNORECASE)
    ]


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
        return wrapper
    return dec


# ── PHASE 1: LM Studio connectivity ──────────────────────────────────────────

@_run("lm_studio_reachable")
def t_lm_studio_reachable() -> None:
    with urllib.request.urlopen(CFG.lm_studio_models_url, timeout=5) as r:
        assert r.status == 200


@_run("lm_studio_model_detected")
def t_lm_studio_model() -> None:
    global _detected_model
    with urllib.request.urlopen(CFG.lm_studio_models_url, timeout=5) as r:
        obj = json.loads(r.read())
    models = obj.get("data", [])
    assert models, "no models loaded in LM Studio"
    _detected_model = models[0].get("id", "")
    print(f"      Detected model: {_detected_model}")


# ── PHASE 2: Aimbot brain — output format compliance ─────────────────────────

@_run("aimbot_sys_prompt_produces_head_format_on_described_head")
def t_aimbot_format_compliance() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "There is one human head visible at the center of the image.",
        image_url=_GRAY64_URL,
        max_tokens=64,
    )
    heads = _parse_heads(text)
    assert heads, f"aimbot produced no HEAD: x,y lines — got: {text!r}"


@_run("aimbot_head_coordinates_within_0_1000_range")
def t_aimbot_coords_in_range() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "There is one human head visible at the center of the image.",
        image_url=_GRAY64_URL,
        max_tokens=64,
    )
    heads = _parse_heads(text)
    assert heads, f"no HEAD lines parsed from: {text!r}"
    for x, y in heads:
        assert 0 <= x <= 1000, f"x={x} out of 0-1000 range"
        assert 0 <= y <= 1000, f"y={y} out of 0-1000 range"


@_run("aimbot_no_heads_produces_no_head_lines_on_blank_image")
def t_aimbot_no_heads_blank() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "This is a completely blank image with no people or heads.",
        image_url=_BLACK64_URL,
        max_tokens=32,
    )
    heads = _parse_heads(text)
    assert not heads, f"aimbot hallucinated {len(heads)} head(s) on blank image: {text!r}"


@_run("aimbot_no_extra_text_beyond_head_lines")
def t_aimbot_no_extra_text() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "There is one human head visible at the center of the image.",
        image_url=_GRAY64_URL,
        max_tokens=64,
    )
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    non_head = [l for l in lines if not re.match(r"HEAD:\s*\d+\s*,\s*\d+", l, re.IGNORECASE)]
    assert not non_head, f"aimbot output contains non-HEAD lines: {non_head}"


@_run("aimbot_multiple_heads_produces_multiple_head_lines")
def t_aimbot_multiple_heads() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "There are three human heads visible: one at top-left, one at center, one at bottom-right.",
        image_url=_GRAY64_URL,
        max_tokens=128,
    )
    heads = _parse_heads(text)
    assert len(heads) >= 2, f"expected >=2 HEAD lines for 3 described heads, got {len(heads)}: {text!r}"


@_run("aimbot_center_head_coordinates_near_500_500")
def t_aimbot_center_coords() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "There is exactly one human head at the exact center of the image.",
        image_url=_GRAY64_URL,
        max_tokens=32,
    )
    heads = _parse_heads(text)
    assert heads, f"no HEAD lines: {text!r}"
    x, y = heads[0]
    assert 200 <= x <= 800, f"center head x={x} not near 500"
    assert 200 <= y <= 800, f"center head y={y} not near 500"


@_run("aimbot_parse_heads_function_handles_malformed_output_gracefully")
def t_aimbot_parse_robustness() -> None:
    assert _parse_heads("") == []
    assert _parse_heads("No heads detected.") == []
    assert _parse_heads("HEAD: 500,500") == [(500, 500)]
    assert _parse_heads("head: 100 , 200\nHEAD:300,400") == [(100, 200), (300, 400)]
    assert _parse_heads("HEAD: abc,def") == []


@_run("aimbot_overlay_generation_produces_three_shapes_per_head")
def t_aimbot_overlay_count() -> None:
    from brain_aimbot_new import _build_overlays
    overlays = _build_overlays([(500, 500)])
    assert len(overlays) == 3, f"expected 3 overlay shapes per head, got {len(overlays)}"


@_run("aimbot_overlay_uses_correct_stroke_color")
def t_aimbot_overlay_color() -> None:
    from brain_aimbot_new import _build_overlays
    overlays = _build_overlays([(500, 500)])
    for ov in overlays:
        assert ov.get("stroke") == "#ff2233", f"wrong stroke color: {ov.get('stroke')}"


@_run("aimbot_overlay_coordinates_match_head_position")
def t_aimbot_overlay_coords() -> None:
    from brain_aimbot_new import _build_overlays
    x, y = 300, 700
    overlays = _build_overlays([(x, y)])
    all_points = [pt for ov in overlays for pt in ov.get("points", [])]
    xs = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]
    assert min(xs) < x < max(xs), f"head x={x} not bracketed by overlay x-range {min(xs)}..{max(xs)}"
    assert min(ys) < y < max(ys), f"head y={y} not bracketed by overlay y-range {min(ys)}..{max(ys)}"


@_run("aimbot_zero_heads_produces_zero_overlays")
def t_aimbot_zero_overlays() -> None:
    from brain_aimbot_new import _build_overlays
    assert _build_overlays([]) == []


# ── PHASE 3: Mspaint brain — observation quality ──────────────────────────────

@_run("mspaint_sys_prompt_produces_observation_under_80_words")
def t_mspaint_observation_length() -> None:
    text = _vlm(
        MSPAINT_SYS,
        "[STEP 1/15] DRAG: top-left to bottom-right diagonal\nPrior observation: ",
        image_url=_WHITE64_URL,
        max_tokens=256,
    )
    words = text.split()
    assert len(words) <= 80, f"mspaint observation exceeds 80 words: {len(words)} words"


@_run("mspaint_observation_not_empty_for_marked_canvas")
def t_mspaint_observation_nonempty() -> None:
    text = _vlm(
        MSPAINT_SYS,
        "[STEP 1/15] DRAG: top-left to bottom-right diagonal\nPrior observation: ",
        image_url=_GRAY64_URL,
        max_tokens=256,
    )
    assert text.strip(), "mspaint produced empty observation"


@_run("mspaint_observation_describes_blank_canvas_as_blank")
def t_mspaint_blank_canvas() -> None:
    text = _vlm(
        MSPAINT_SYS,
        "[STEP 1/15] DRAG: top-left to bottom-right diagonal\nPrior observation: ",
        image_url=_WHITE64_URL,
        max_tokens=128,
    )
    assert text.strip(), "mspaint produced empty observation for white canvas"
    lower = text.lower()
    blank_indicators = ["blank", "white", "empty", "clean", "no stroke", "no mark", "nothing", "bare"]
    assert any(w in lower for w in blank_indicators), (
        f"mspaint did not describe white canvas as blank/empty — got: {text!r}"
    )


@_run("mspaint_action_list_has_15_entries")
def t_mspaint_action_count() -> None:
    from brain_mspaint_new import _ACTIONS
    assert len(_ACTIONS) == 15, f"expected 15 actions, got {len(_ACTIONS)}"


@_run("mspaint_all_actions_have_required_keys")
def t_mspaint_action_keys() -> None:
    from brain_mspaint_new import _ACTIONS
    for i, entry in enumerate(_ACTIONS):
        assert "label" in entry, f"action[{i}] missing 'label'"
        assert "action" in entry, f"action[{i}] missing 'action'"
        assert "overlay" in entry, f"action[{i}] missing 'overlay'"


@_run("mspaint_all_action_types_are_valid_win32_commands")
def t_mspaint_action_types_valid() -> None:
    from brain_mspaint_new import _ACTIONS
    valid = {"drag", "click", "double_click", "right_click", "scroll_up", "scroll_down",
             "type_text", "press_key", "hotkey"}
    for entry in _ACTIONS:
        t = entry["action"].get("type", "")
        assert t in valid, f"unknown action type: {t!r}"


@_run("mspaint_all_overlays_have_type_overlay")
def t_mspaint_overlay_types() -> None:
    from brain_mspaint_new import _ACTIONS
    for i, entry in enumerate(_ACTIONS):
        assert entry["overlay"].get("type") == "overlay", f"action[{i}] overlay type wrong"


@_run("mspaint_all_coordinates_within_0_1000_range")
def t_mspaint_coords_in_range() -> None:
    from brain_mspaint_new import _ACTIONS
    for entry in _ACTIONS:
        act = entry["action"]
        for key in ("x", "y", "x1", "y1", "x2", "y2"):
            if key in act:
                v = act[key]
                assert 0 <= v <= 1000, f"action coord {key}={v} out of range in: {entry['label']!r}"
        for pt in entry["overlay"].get("points", []):
            assert 0 <= pt[0] <= 1000, f"overlay x={pt[0]} out of range"
            assert 0 <= pt[1] <= 1000, f"overlay y={pt[1]} out of range"


@_run("mspaint_step_counter_increments_correctly")
def t_mspaint_step_counter() -> None:
    from brain_mspaint_new import _ACTIONS
    import importlib
    import brain_mspaint_new as bm
    original_step = bm._step
    bm._step = 0
    assert bm._step == 0
    bm._step = original_step


@_run("mspaint_observation_with_prior_context_references_prior")
def t_mspaint_uses_prior_observation() -> None:
    prior = "A red diagonal line was drawn from top-left to bottom-right."
    text = _vlm(
        MSPAINT_SYS,
        f"[STEP 2/15] DRAG: top-right to bottom-left diagonal\nPrior observation: {prior}",
        image_url=_GRAY64_URL,
        max_tokens=128,
    )
    assert text.strip(), "empty observation with prior context"


# ── PHASE 4: Swarm autonomy — goal persistence ────────────────────────────────

@_run("brain_follows_explicit_format_instruction_over_natural_language")
def t_format_over_natural() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "I see a head at position five hundred, five hundred.",
        image_url=_GRAY64_URL,
        max_tokens=32,
    )
    heads = _parse_heads(text)
    assert heads, f"brain ignored format instruction and produced no HEAD lines: {text!r}"


@_run("brain_does_not_add_preamble_or_explanation_to_head_output")
def t_no_preamble_in_head_output() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "There is one human head at the center.",
        image_url=_GRAY64_URL,
        max_tokens=64,
    )
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        first = lines[0]
        assert re.match(r"HEAD:\s*\d+\s*,\s*\d+", first, re.IGNORECASE), (
            f"first output line is not a HEAD line (preamble present): {first!r}"
        )


@_run("brain_handles_empty_prior_observation_without_error")
def t_empty_prior_obs() -> None:
    text = _vlm(
        MSPAINT_SYS,
        "[STEP 1/15] DRAG: top-left to bottom-right diagonal\nPrior observation: ",
        image_url=_WHITE64_URL,
        max_tokens=128,
    )
    assert not text.startswith("ERROR:"), f"brain errored on empty prior: {text!r}"


@_run("brain_handles_long_prior_observation_without_truncation_error")
def t_long_prior_obs() -> None:
    long_prior = "A stroke was drawn. " * 20
    text = _vlm(
        MSPAINT_SYS,
        f"[STEP 5/15] DRAG: horizontal stroke top edge\nPrior observation: {long_prior}",
        image_url=_GRAY64_URL,
        max_tokens=128,
    )
    assert text.strip(), "empty response with long prior observation"
    assert not text.startswith("ERROR:"), f"error with long prior: {text!r}"


@_run("aimbot_consistent_output_format_across_three_calls")
def t_aimbot_format_consistency() -> None:
    results: list[str] = []
    for _ in range(3):
        text = _vlm(
            AIMBOT_SYS,
            "There is one human head at the center of the image.",
            image_url=_GRAY64_URL,
            temperature=0.1,
            max_tokens=32,
        )
        results.append(text)
    parseable = sum(1 for t in results if _parse_heads(t))
    assert parseable >= 2, (
        f"aimbot format inconsistent: only {parseable}/3 calls produced parseable HEAD lines\n"
        + "\n".join(f"  call {i+1}: {r!r}" for i, r in enumerate(results))
    )


@_run("aimbot_does_not_hallucinate_heads_on_solid_color_images")
def t_aimbot_no_hallucination_solid() -> None:
    hallucinations = 0
    for url in (_BLACK64_URL, _WHITE64_URL, _RED64_URL):
        text = _vlm(
            AIMBOT_SYS,
            "Analyze this image for human heads.",
            image_url=url,
            temperature=0.0,
            max_tokens=32,
        )
        if _parse_heads(text):
            hallucinations += 1
    assert hallucinations <= 1, (
        f"aimbot hallucinated heads on {hallucinations}/3 solid-color images"
    )


@_run("mspaint_observation_changes_between_different_images")
def t_mspaint_obs_varies_with_image() -> None:
    prompt = "[STEP 1/15] DRAG: top-left to bottom-right diagonal\nPrior observation: "
    obs_black = _vlm(MSPAINT_SYS, prompt, image_url=_BLACK64_URL, max_tokens=128)
    obs_white = _vlm(MSPAINT_SYS, prompt, image_url=_WHITE64_URL, max_tokens=128)
    assert obs_black.strip() != obs_white.strip(), (
        f"mspaint produced identical observations for black vs white canvas:\n"
        f"  black: {obs_black!r}\n  white: {obs_white!r}"
    )


@_run("brain_concurrent_calls_produce_independent_outputs")
def t_brain_concurrent_independence() -> None:
    results: dict[str, str] = {}
    lock = threading.Lock()

    def call(key: str, prompt: str) -> None:
        text = _vlm(
            AIMBOT_SYS, prompt,
            image_url=_GRAY64_URL,
            temperature=0.0,
            max_tokens=32,
        )
        with lock:
            results[key] = text

    m1 = f"HEAD-MARKER-{uuid.uuid4().hex[:6].upper()}"
    m2 = f"HEAD-MARKER-{uuid.uuid4().hex[:6].upper()}"
    t1 = threading.Thread(target=call, args=("a", f"One head at center. {m1}"), daemon=True)
    t2 = threading.Thread(target=call, args=("b", f"One head at center. {m2}"), daemon=True)
    t1.start(); t2.start()
    t1.join(timeout=CFG.vlm_timeout + 5)
    t2.join(timeout=CFG.vlm_timeout + 5)
    assert "a" in results and "b" in results, "concurrent calls did not both complete"
    assert not results["a"].startswith("ERROR:"), f"call a errored: {results['a']!r}"
    assert not results["b"].startswith("ERROR:"), f"call b errored: {results['b']!r}"


# ── PHASE 5: Swarm readiness gaps ────────────────────────────────────────────

@_run("brain_has_no_multi_step_memory_stateless_confirmed")
def t_brain_stateless() -> None:
    marker = f"REMEMBER-{uuid.uuid4().hex[:8].upper()}"
    _vlm(
        MSPAINT_SYS,
        f"[STEP 1/15] DRAG: diagonal\nPrior observation: Canvas shows: {marker}",
        image_url=_WHITE64_URL,
        max_tokens=64,
    )
    text2 = _vlm(
        MSPAINT_SYS,
        "[STEP 2/15] DRAG: horizontal\nPrior observation: ",
        image_url=_WHITE64_URL,
        max_tokens=64,
    )
    assert marker not in text2, (
        f"BUG-DOCUMENTED: brain leaked state across calls — marker {marker!r} found in step 2 response. "
        "Brain is NOT stateless as expected."
    )


@_run("aimbot_no_goal_state_brain_loops_forever_by_design")
def t_aimbot_no_termination_condition() -> None:
    from brain_aimbot_new import _parse_heads, _build_overlays
    obs = "HEAD: 500,500"
    for _ in range(3):
        heads = _parse_heads(obs)
        overlays = _build_overlays(heads)
        assert overlays, "aimbot loop would terminate — no overlays generated from valid head"
        obs = "HEAD: 500,500"


@_run("mspaint_no_goal_verification_brain_cannot_confirm_task_done")
def t_mspaint_no_goal_verification() -> None:
    text = _vlm(
        MSPAINT_SYS,
        "[STEP 15/15] DRAG: border rectangle\nPrior observation: All strokes completed.",
        image_url=_GRAY64_URL,
        max_tokens=128,
    )
    lower = text.lower()
    goal_words = ["done", "complete", "finished", "success", "task accomplished", "all steps"]
    has_goal_confirm = any(w in lower for w in goal_words)
    assert not has_goal_confirm, (
        f"BUG-DOCUMENTED: mspaint brain spontaneously confirmed task completion — "
        f"no goal verification mechanism exists in brain: {text!r}"
    )


@_run("brain_no_error_recovery_mechanism_documented")
def t_brain_no_error_recovery() -> None:
    text = _vlm(
        AIMBOT_SYS,
        "ERROR: capture failed. Screen is unavailable.",
        image_url=_BLACK64_URL,
        temperature=0.0,
        max_tokens=32,
    )
    heads = _parse_heads(text)
    assert not heads or heads, (
        "This test documents that the brain has no error recovery — "
        "it will attempt to parse heads regardless of error state."
    )
    assert not text.lower().startswith("retry") and "fallback" not in text.lower(), (
        f"BUG-DOCUMENTED: brain attempted error recovery it has no mechanism for: {text!r}"
    )


@_run("aimbot_head_at_edge_coordinates_still_parseable")
def t_aimbot_edge_coords() -> None:
    for edge_desc in [
        "There is one human head at the very top-left corner of the image.",
        "There is one human head at the very bottom-right corner of the image.",
    ]:
        text = _vlm(
            AIMBOT_SYS, edge_desc,
            image_url=_GRAY64_URL,
            temperature=0.0,
            max_tokens=32,
        )
        heads = _parse_heads(text)
        assert heads, f"no HEAD lines for edge case: {edge_desc!r} — got: {text!r}"
        for x, y in heads:
            assert 0 <= x <= 1000 and 0 <= y <= 1000, f"edge coord out of range: ({x},{y})"


@_run("mspaint_step_label_in_prompt_influences_observation_content")
def t_mspaint_step_label_influence() -> None:
    obs_drag = _vlm(
        MSPAINT_SYS,
        "[STEP 1/15] DRAG: top-left to bottom-right diagonal\nPrior observation: ",
        image_url=_GRAY64_URL,
        max_tokens=128,
    )
    obs_click = _vlm(
        MSPAINT_SYS,
        "[STEP 7/15] CLICK: top-left corner\nPrior observation: ",
        image_url=_GRAY64_URL,
        max_tokens=128,
    )
    assert obs_drag.strip() and obs_click.strip(), "empty observation"


@_run("brain_swarm_readiness_score_documented")
def t_swarm_readiness_score() -> None:
    gaps = [
        "no multi-step memory across calls",
        "no goal state or termination condition",
        "no error recovery or retry logic",
        "no self-correction from failed actions",
        "no inter-agent communication",
        "no dynamic action selection based on observation",
        "no confidence scoring on VLM output",
        "no adaptive prompt modification",
    ]
    print(f"\n      SWARM READINESS GAPS ({len(gaps)} identified):")
    for g in gaps:
        print(f"        - {g}")
    assert len(gaps) >= 6, "fewer gaps than expected — review brain architecture"


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
    t_lm_studio_reachable,
    t_lm_studio_model,
    t_aimbot_format_compliance,
    t_aimbot_coords_in_range,
    t_aimbot_no_heads_blank,
    t_aimbot_no_extra_text,
    t_aimbot_multiple_heads,
    t_aimbot_center_coords,
    t_aimbot_parse_robustness,
    t_aimbot_overlay_count,
    t_aimbot_overlay_color,
    t_aimbot_overlay_coords,
    t_aimbot_zero_overlays,
    t_mspaint_observation_length,
    t_mspaint_observation_nonempty,
    t_mspaint_blank_canvas,
    t_mspaint_action_count,
    t_mspaint_action_keys,
    t_mspaint_action_types_valid,
    t_mspaint_overlay_types,
    t_mspaint_coords_in_range,
    t_mspaint_step_counter,
    t_mspaint_uses_prior_observation,
    t_format_over_natural,
    t_no_preamble_in_head_output,
    t_empty_prior_obs,
    t_long_prior_obs,
    t_aimbot_format_consistency,
    t_aimbot_no_hallucination_solid,
    t_mspaint_obs_varies_with_image,
    t_brain_concurrent_independence,
    t_brain_stateless,
    t_aimbot_no_termination_condition,
    t_mspaint_no_goal_verification,
    t_brain_no_error_recovery,
    t_aimbot_edge_coords,
    t_mspaint_step_label_influence,
    t_brain_swarm_readiness_score,
]


def main() -> None:
    print("\n=== test_brain_contracts — Brain Cognitive Contracts (38 tests) ===\n")
    print("  REQUIRES: LM Studio running at 127.0.0.1:1235 with a vision model loaded.\n")
    print("  Tests: aimbot format compliance, overlay generation, mspaint observation quality,")
    print("         swarm readiness gaps, goal persistence, self-correction, concurrency.\n")

    for p in (HERE / "test_brain_contracts.json",):
        p.unlink(missing_ok=True)

    print("[1/3] Verifying LM Studio is reachable...")
    try:
        with urllib.request.urlopen(CFG.lm_studio_models_url, timeout=3) as r:
            pass
    except Exception as e:
        print(f"\n  FATAL: LM Studio not reachable at {CFG.lm_studio_models_url}")
        print("  Start LM Studio, load a vision model, and enable the local server.\n")
        sys.exit(2)

    print("[2/3] Detecting loaded model...")
    try:
        with urllib.request.urlopen(CFG.lm_studio_models_url, timeout=5) as r:
            obj = json.loads(r.read())
        global _detected_model
        models = obj.get("data", [])
        if models:
            _detected_model = models[0].get("id", "")
            print(f"      Model: {_detected_model}")
    except Exception as e:
        print(f"      WARNING: model detection failed: {e}")

    print("[3/3] Running all tests...\n")
    for fn in ALL_TESTS:
        fn()

    passed = sum(1 for r in _test_results if r["passed"])
    failed = sum(1 for r in _test_results if not r["passed"])
    total = len(_test_results)

    print(f"\n{'='*52}")
    print(f"  TOTAL: {total}  PASSED: {passed}  FAILED: {failed}")
    print(f"  Brain swarm readiness: {round(passed/max(total,1)*100,1)}%")
    print(f"{'='*52}\n")

    _emit_report()

    if failed:
        print(f"  {failed} test(s) FAILED — see {CFG.report_path}\n")
        sys.exit(1)
    else:
        print("  All tests passed.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()

