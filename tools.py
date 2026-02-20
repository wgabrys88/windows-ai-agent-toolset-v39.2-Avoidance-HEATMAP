from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import time
from pathlib import Path
from typing import Final

_INPUT_MOUSE: Final = 0
_INPUT_KEYBOARD: Final = 1
_MOUSEEVENTF_LEFTDOWN: Final = 0x0002
_MOUSEEVENTF_LEFTUP: Final = 0x0004
_MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
_MOUSEEVENTF_RIGHTUP: Final = 0x0010
_MOUSEEVENTF_ABSOLUTE: Final = 0x8000
_MOUSEEVENTF_MOVE: Final = 0x0001
_KEYEVENTF_KEYUP: Final = 0x0002
_KEYEVENTF_UNICODE: Final = 0x0004
_MEMORY_CAP: Final = 20


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_user32: ctypes.WinDLL | None = None
_screen_w: int = 0
_screen_h: int = 0
_physical: bool = False
_executed: list[str] = []
_run_dir: str = ""
_crop_x1: int = 0
_crop_y1: int = 0
_crop_x2: int = 0
_crop_y2: int = 0
_crop_active: bool = False


def _init_win32() -> None:
    global _user32, _screen_w, _screen_h
    if _user32 is not None:
        return
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _screen_w = _user32.GetSystemMetrics(0)
    _screen_h = _user32.GetSystemMetrics(1)
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint


def _send_inputs(items: list[_INPUT]) -> None:
    assert _user32 is not None
    if not items:
        return
    arr = (_INPUT * len(items))(*items)
    if _user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)) != len(items):
        raise OSError(ctypes.get_last_error())


def _send_mouse(flags: int, abs_x: int | None = None, abs_y: int | None = None) -> None:
    inp = _INPUT(type=_INPUT_MOUSE)
    f, dx, dy = flags, 0, 0
    if abs_x is not None and abs_y is not None:
        dx, dy, f = abs_x, abs_y, f | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_MOVE
    inp.u.mi = _MOUSEINPUT(dx, dy, 0, f, 0, 0)
    _send_inputs([inp])


def _send_unicode(text: str) -> None:
    items: list[_INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        code = 0x000D if ch == "\n" else ord(ch)
        for fl in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            inp = _INPUT(type=_INPUT_KEYBOARD)
            inp.u.ki = _KEYBDINPUT(0, code, fl, 0, 0)
            items.append(inp)
    _send_inputs(items)


def _to_abs(x_px: int, y_px: int) -> tuple[int, int]:
    return (
        max(0, min(65535, int((x_px / max(1, _screen_w - 1)) * 65535))),
        max(0, min(65535, int((y_px / max(1, _screen_h - 1)) * 65535))),
    )


def _smooth_move(tx: int, ty: int) -> None:
    assert _user32 is not None
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    ddx, ddy = tx - sx, ty - sy
    for i in range(21):
        t = i / 20
        t = t * t * (3.0 - 2.0 * t)
        _send_mouse(0, *_to_abs(int(sx + ddx * t), int(sy + ddy * t)))
        time.sleep(0.01)


def _remap(v: int, dim: int) -> int:
    if _crop_active:
        span = _crop_x2 - _crop_x1 if dim == _screen_w else _crop_y2 - _crop_y1
        origin = _crop_x1 if dim == _screen_w else _crop_y1
        return origin + int((v / 1000) * span)
    return int((v / 1000) * dim)


_CLICK_BUTTONS: Final = {
    "click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, False),
    "right_click": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP, False),
    "double_click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, True),
}


def _phys_click(name: str, x: int, y: int) -> None:
    down, up, double = _CLICK_BUTTONS[name]
    _smooth_move(_remap(x, _screen_w), _remap(y, _screen_h))
    time.sleep(0.12)
    _send_mouse(down); time.sleep(0.02); _send_mouse(up)
    if double:
        time.sleep(0.06)
        _send_mouse(down); time.sleep(0.02); _send_mouse(up)


def _phys_drag(x1: int, y1: int, x2: int, y2: int) -> None:
    _smooth_move(_remap(x1, _screen_w), _remap(y1, _screen_h))
    time.sleep(0.08)
    _send_mouse(_MOUSEEVENTF_LEFTDOWN); time.sleep(0.06)
    _smooth_move(_remap(x2, _screen_w), _remap(y2, _screen_h))
    time.sleep(0.06)
    _send_mouse(_MOUSEEVENTF_LEFTUP)


def configure(*, physical: bool, run_dir: str, crop: dict | None = None) -> None:
    global _physical, _executed, _run_dir
    global _crop_x1, _crop_y1, _crop_x2, _crop_y2, _crop_active
    _physical = physical
    _executed = []
    _run_dir = run_dir
    if physical:
        _init_win32()
    if crop and all(k in crop for k in ("x1", "y1", "x2", "y2")):
        _crop_x1 = int(crop["x1"])
        _crop_y1 = int(crop["y1"])
        _crop_x2 = int(crop["x2"])
        _crop_y2 = int(crop["y2"])
        _crop_active = _crop_x2 > _crop_x1 and _crop_y2 > _crop_y1
    else:
        _crop_active = False


def get_results() -> list[str]:
    return list(_executed)


def _valid(name: str, v: object) -> int:
    if not isinstance(v, int | float):
        raise TypeError(f"{name} must be a number, got {type(v).__name__}")
    iv = int(v)
    if not 0 <= iv <= 1000:
        raise ValueError(f"{name}={iv} outside 0-1000")
    return iv


def _record(canon: str) -> bool:
    _executed.append(canon)
    return _physical


def click(x: int, y: int) -> None:
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"click({ix}, {iy})"):
        _phys_click("click", ix, iy)


def right_click(x: int, y: int) -> None:
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"right_click({ix}, {iy})"):
        _phys_click("right_click", ix, iy)


def double_click(x: int, y: int) -> None:
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"double_click({ix}, {iy})"):
        _phys_click("double_click", ix, iy)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    c = [_valid(n, v) for n, v in zip(("x1", "y1", "x2", "y2"), (x1, y1, x2, y2))]
    if _record(f"drag({c[0]}, {c[1]}, {c[2]}, {c[3]})"):
        _phys_drag(*c)


def write(text: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"write() requires str, got {type(text).__name__}")
    if _record(f"write({json.dumps(text)})"):
        _send_unicode(text)


def _memory_path() -> Path:
    return Path(_run_dir) / "memory.json" if _run_dir else Path("memory.json")


def remember(text: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"remember() requires str, got {type(text).__name__}")
    p = _memory_path()
    items: list[str] = []
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            items = []
    except Exception:
        pass
    if text not in items:
        items.append(text)
    items = items[-_MEMORY_CAP:]
    p.write_text(json.dumps(items, indent=2), encoding="utf-8")
    _record(f"remember({json.dumps(text)})")


def recall() -> str:
    try:
        items = json.loads(_memory_path().read_text(encoding="utf-8"))
        if isinstance(items, list) and items:
            return "\n".join(f"- {s}" for s in items[-_MEMORY_CAP:])
    except Exception:
        pass
    return "(no memories yet)"


TOOL_NAMES: Final[tuple[str, ...]] = (
    "click", "right_click", "double_click", "drag", "write", "remember", "recall",
)
