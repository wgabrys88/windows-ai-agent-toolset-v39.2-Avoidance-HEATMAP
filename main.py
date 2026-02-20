from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Final

import config as _cfg

API: Final[str] = "http://localhost:1234/v1/chat/completions"
EXECUTE_SCRIPT: Final[Path] = Path(__file__).parent / "execute.py"

_run_dir = Path(os.environ.get("FRANZ_RUN_DIR", ""))
if not _run_dir.is_dir():
    _run_dir = Path(__file__).parent / "panel_log" / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    _run_dir.mkdir(parents=True, exist_ok=True)

RUN_DIR: Final[Path] = _run_dir
STATE_FILE: Final[Path] = RUN_DIR / "state.json"
PAUSE_FILE: Final[Path] = RUN_DIR / "PAUSED"

_SYSTEM_PROMPT: Final[str] = """\
You are a Python-experienced AI agent controlling a Windows computer.
Each turn you receive your previous report and a screenshot.
Red marks on the screenshot show where your previous actions landed.
Orange areas on the screenshot are zones you have been clicking repeatedly -- avoid them this turn.

Respond with exactly two parts:

PART 1 -- Updated report (plain text, 2-4 sentences):
Describe what the screen shows NOW. State your next goal and why.
Do not repeat observations or plans from your previous report.

PART 2 -- Actions (bare Python calls, last lines of your response):
Write at least two calls, one per line. Available:
  click(x, y)   right_click(x, y)   double_click(x, y)   drag(x1, y1, x2, y2)
Coordinates are integers 0-1000.

Example response:

The screen shows a desktop with a file explorer open. I will open the Documents
folder by double-clicking it, then click the address bar to type a path.

double_click(350, 400)
click(500, 50)

Rules:
- Function calls MUST be the last lines. Nothing after them.
- Do not write markdown, code fences, or the word "action".
"""

INITIAL_STORY: Final[str] = (
    "The screen has just appeared. I will look at what is visible"
    " and interact with it.\n\nclick(500, 500)\nclick(500, 500)\n"
)

_PART_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^PART\s+\d+\s*--\s*(?:Actions?\s*)?", re.IGNORECASE
)
_BARE_ACTION_WORD_RE: Final[re.Pattern[str]] = re.compile(r"^action[s]?$", re.IGNORECASE)


def _log(msg: str) -> None:
    print(f"[main][{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _looks_like_call(line: str) -> bool:
    if not line:
        return False
    try:
        tree = ast.parse(line, mode="eval")
    except SyntaxError:
        return False
    return isinstance(tree.body, ast.Call)


def _load_state() -> tuple[str, int, int]:
    try:
        obj = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            story = str(obj.get("story", "")).strip()
            if story:
                return story, int(obj.get("turn", 0)), int(obj.get("fail_streak", 0))
    except Exception:
        pass
    return INITIAL_STORY, 0, 0


def _save_state(turn: int, story: str, execution_result: dict[str, object], fails: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({
            "turn": turn, "story": story,
            "executed": execution_result.get("executed", []),
            "malformed": execution_result.get("malformed", []),
            "fail_streak": fails,
            "timestamp": datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _sanitize_output(raw: str) -> str:
    prose_lines: list[str] = []
    for ln in raw.splitlines():
        stripped = ln.strip()
        if _BARE_ACTION_WORD_RE.match(stripped):
            continue
        cleaned = _PART_PREFIX_RE.sub("", stripped).strip()
        if stripped != cleaned and not cleaned:
            continue
        if cleaned and _looks_like_call(cleaned):
            continue
        if stripped and _looks_like_call(stripped):
            continue
        prose_lines.append(ln)
    text = "\n".join(prose_lines).rstrip()
    return text if text else raw.strip()


def _emergency_reset(turn: int) -> str:
    return (
        f"Turn {turn}: previous actions failed. Starting fresh.\n"
        "I will look at the screenshot and click on the most prominent element.\n\n"
        "click(500, 400)\n"
        "click(300, 300)"
    )


def _run_subprocess(script: Path, payload: dict[str, object], timeout: int = 120) -> dict[str, object]:
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload), capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, Exception) as exc:
        _log(f"Subprocess {script.name} error: {exc}")
        return {}
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            _log(f"[{script.stem}] {line}")
    if not result.stdout or not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _infer(story: str, screenshot_b64: str) -> str:
    user_content: list[dict[str, object]] = [{"type": "text", "text": story}]
    if screenshot_b64:
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}})

    payload: dict[str, object] = {
        "model": str(_cfg.MODEL),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": float(_cfg.TEMPERATURE),
        "top_p": float(_cfg.TOP_P),
        "max_tokens": int(getattr(_cfg, "MAX_TOKENS", 1000)),
    }
    if getattr(_cfg, "CACHE_PROMPT", False):
        payload["cache_prompt"] = True

    body = json.dumps(payload).encode()
    delay = 1.0
    last_err: Exception | None = None

    for attempt in range(5):
        try:
            req = urllib.request.Request(
                API, body, {"Content-Type": "application/json", "Connection": "keep-alive"}
            )
            with urllib.request.urlopen(req) as resp:
                content: str = json.load(resp)["choices"][0]["message"]["content"]
                if content:
                    _log(f"VLM: {len(content)} chars")
                return content
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = exc
            _log(f"Infer {attempt + 1}/5 failed: {exc}")
            time.sleep(delay)
            delay = min(delay * 2, 16)

    raise RuntimeError(f"VLM failed: {last_err}")


def main() -> None:
    story, turn, fails = _load_state()
    _log(f"Start: run_dir={RUN_DIR}, turn={turn}")
    raw_output = story

    while True:
        if PAUSE_FILE.exists():
            _log("PAUSED")
            while PAUSE_FILE.exists():
                time.sleep(2)
            _log("Resumed")

        turn += 1

        try:
            importlib.reload(_cfg)
        except Exception:
            pass

        _log(f"--- Turn {turn} ---")

        er: dict[str, object] = _run_subprocess(
            EXECUTE_SCRIPT, {"raw": raw_output, "run_dir": str(RUN_DIR)}
        )
        screenshot = str(er.get("screenshot_b64", ""))
        executed: list[object] = er.get("executed", [])
        errors: list[object] = er.get("malformed", [])

        if errors and not executed:
            fails += 1
        elif executed:
            fails = 0

        if 2 <= fails < 4:
            raw_output = _emergency_reset(turn)
            story = _sanitize_output(raw_output)
            fails = 0
            _log(f"STORY RESET at turn {turn}")
            _save_state(turn, story, er, fails)
            time.sleep(max(float(getattr(_cfg, "LOOP_DELAY", 1.5)), 1.0))
            continue

        if fails >= 4:
            _log(f"AUTO-PAUSE: {fails} consecutive error turns")
            try:
                PAUSE_FILE.write_text(f"Paused: {datetime.now().isoformat()}\n", encoding="utf-8")
            except Exception:
                pass
            _save_state(turn, story, er, fails)
            continue

        _log(f"Actions: {len(executed)} | Screenshot: {'yes' if screenshot else 'NO'}")

        try:
            raw_output = _infer(story, screenshot)
        except RuntimeError as exc:
            _log(str(exc))
            raw_output = ""

        if raw_output and raw_output.strip():
            story = _sanitize_output(raw_output)
        else:
            _log("VLM returned empty -- keeping previous story")
            raw_output = story

        _save_state(turn, story, er, fails)
        time.sleep(max(float(getattr(_cfg, "LOOP_DELAY", 1.5)), 1.0))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
