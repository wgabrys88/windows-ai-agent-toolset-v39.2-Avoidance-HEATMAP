from __future__ import annotations

import ast
import builtins
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

import config as _cfg
import tools

_CAPTURE_SCRIPT: Final[Path] = Path(__file__).parent / "capture.py"
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_PART_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^PART\s+\d+\s*--\s*(?:Actions?\s*)?", re.IGNORECASE
)
_SAFE_NAMES: Final[tuple[str, ...]] = (
    "range", "int", "str", "float", "bool", "len", "abs", "max", "min",
    "round", "list", "tuple", "dict", "set", "isinstance", "type",
)
_SAFE_BUILTINS: Final[dict[str, object]] = {n: getattr(builtins, n) for n in _SAFE_NAMES}


def _log(msg: str) -> None:
    sys.stderr.write(f"[execute] {msg}\n")
    sys.stderr.flush()


def _clean_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = _PART_PREFIX_RE.sub("", cleaned).strip()
    for prefix in ("~~WORLD~~", "<<WORLD>>", "<FEEDBACK", "</FEEDBACK"):
        idx = cleaned.find(prefix)
        if idx >= 0:
            cleaned = cleaned[:idx].strip()
    return cleaned


def _is_valid_call(text: str) -> bool:
    if not text:
        return False
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return False
    return isinstance(tree.body, ast.Call)


def _call_func_name(text: str) -> str | None:
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    if not isinstance(tree.body, ast.Call):
        return None
    func = tree.body.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _extract_calls(raw: str, allowed: set[str]) -> tuple[list[str], list[str]]:
    fenced = _FENCE_RE.findall(raw)
    sources: list[str] = (["\n".join(block.strip() for block in fenced)] if fenced else []) + [raw]
    seen: set[str] = set()
    result: list[str] = []
    malformed: list[str] = []
    for src in sources:
        for line in src.splitlines():
            cleaned = _clean_line(line)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            if not _is_valid_call(cleaned):
                if re.match(r"^[a-zA-Z_]\w*\s*\(", cleaned):
                    malformed.append(f"UnrecognizedCall: '{cleaned}'")
                continue
            name = _call_func_name(cleaned)
            if name in allowed:
                result.append(cleaned)
            elif name is not None:
                malformed.append(f"UnknownTool: '{name}'")
    return result, malformed


def _capture(crop: dict[str, int] | None) -> str:
    try:
        result = subprocess.run(
            [sys.executable, str(_CAPTURE_SCRIPT)],
            input=json.dumps({"crop": crop}) if crop else "{}",
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, Exception) as exc:
        _log(f"capture failed: {exc}")
        return ""
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            _log(f"[capture] {line}")
    if not result.stdout or not result.stdout.strip():
        return ""
    try:
        return str(json.loads(result.stdout).get("screenshot_b64", ""))
    except json.JSONDecodeError:
        return ""


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _emit(executed: list[str], calls: list[str], errors: list[str], screenshot: str) -> None:
    sys.stdout.write(json.dumps({
        "executed": executed,
        "extracted_code": calls,
        "malformed": errors,
        "screenshot_b64": screenshot,
        "feedback": "",
    }))
    sys.stdout.flush()


def main() -> None:
    try:
        req: dict[str, object] = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _emit([], [], ["Bad JSON"], "")
        return

    raw = str(req.get("raw", ""))
    run_dir_str = str(req.get("run_dir", ""))
    is_debug = bool(req.get("debug", False))

    rd = Path(tempfile.mkdtemp(prefix="franz_debug_")) if is_debug or not run_dir_str else Path(run_dir_str)

    crop_path = rd.parent / "crop.json" if is_debug else rd / "crop.json"
    cd = _load_json(crop_path)
    crop: dict[str, int] | None = cd if isinstance(cd, dict) and all(k in cd for k in ("x1", "y1", "x2", "y2")) else None

    tools_path = rd.parent / "allowed_tools.json" if is_debug else rd / "allowed_tools.json"
    allowed_data = _load_json(tools_path)
    allowed: set[str] = set(allowed_data) & set(tools.TOOL_NAMES) if isinstance(allowed_data, list) and allowed_data else set(tools.TOOL_NAMES)

    tools.configure(physical=bool(_cfg.PHYSICAL_EXECUTION), run_dir=str(rd), crop=crop)

    ns: dict[str, object] = {"__builtins__": dict(_SAFE_BUILTINS)}
    for name in tools.TOOL_NAMES:
        ns[name] = getattr(tools, name)
    ns["print"] = lambda *a, **k: tools.write(k.get("sep", " ").join(str(x) for x in a) + str(k.get("end", "\n")))

    calls, parse_errors = _extract_calls(raw.strip(), allowed)
    errors: list[str] = list(parse_errors)

    for line in calls:
        try:
            eval(compile(line, "<agent>", "eval"), ns)  # noqa: S307
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors.append(err)
            _log(f"Error on '{line}': {err}")

    executed = tools.get_results()
    screenshot = _capture(crop)
    _emit(executed, calls, errors, screenshot)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        try:
            _emit([], [], [str(exc)], "")
        except Exception:
            pass
