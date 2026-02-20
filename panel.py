from __future__ import annotations

import ast
import base64
import http.server
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Final

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 1234
UPSTREAM: Final[str] = "http://127.0.0.1:1235/v1/chat/completions"
LOG_BASE: Final[Path] = Path(__file__).parent / "panel_log"
HTML_FILE: Final[Path] = Path(__file__).parent / "panel.html"
CANVAS_FILE: Final[Path] = Path(__file__).parent / "canvas.html"
MAIN_SCRIPT: Final[Path] = Path(__file__).parent / "main.py"
EXECUTE_SCRIPT: Final[Path] = Path(__file__).parent / "execute.py"

_run_dir: Path = LOG_BASE
_turns = 0
_turns_lock = threading.Lock()
_main_proc: subprocess.Popen[str] | None = None
_main_lock = threading.Lock()
_sse_clients: list[queue.Queue[str]] = []
_sse_lock = threading.Lock()
_shutdown = threading.Event()
_t0 = time.monotonic()

ALL_TOOLS: Final[tuple[str, ...]] = (
    "click", "right_click", "double_click", "drag", "write", "remember", "recall"
)

_prev_actions: list[dict[str, object]] = []
_prev_actions_lock = threading.Lock()

_render_job: dict[str, object] | None = None
_render_job_lock = threading.Lock()
_render_result: dict[str, object] = {}
_render_result_lock = threading.Lock()
_render_event = threading.Event()
_render_seq: int = 0

_log_file: Path | None = None
_log_lock = threading.Lock()

_turn_store: dict[int, dict[str, object]] = {}
_turn_store_lock = threading.Lock()
_MAX_STORED_TURNS: Final[int] = 200

_PART_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^PART\s+\d+\s*--\s*(?:Actions?\s*)?", re.IGNORECASE
)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _out(msg: str) -> None:
    sys.stdout.write(f"[panel][{_ts()}] {msg}\n")
    sys.stdout.flush()


def _next_turn() -> int:
    global _turns
    with _turns_lock:
        _turns += 1
        return _turns


def _parse_actions_from_vlm(vlm_text: str) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for line in vlm_text.splitlines():
        cleaned = _PART_PREFIX_RE.sub("", line.strip()).strip()
        if not cleaned:
            continue
        try:
            tree = ast.parse(cleaned, mode="eval")
        except SyntaxError:
            continue
        if not isinstance(tree.body, ast.Call):
            continue
        func = tree.body.func
        name: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name not in ("click", "right_click", "double_click", "drag"):
            continue
        int_args: list[int] = []
        for arg in tree.body.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                int_args.append(arg.value)
            elif isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                if isinstance(arg.operand, ast.Constant) and isinstance(arg.operand.value, int):
                    int_args.append(-arg.operand.value)
        actions.append({"name": name, "args": int_args})
    return actions


def _try_put(q: queue.Queue[str], msg: str) -> bool:
    try:
        q.put_nowait(msg)
        return True
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(msg)
            return True
        except queue.Full:
            return False


def _sse_broadcast(data: str) -> None:
    msg = f"data: {data}\n\n"
    with _sse_lock:
        _sse_clients[:] = [q for q in _sse_clients if _try_put(q, msg)]


def _sse_register() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=2000)
    with _sse_lock:
        _sse_clients.append(q)
    return q


def _sse_unregister(q: queue.Queue[str]) -> None:
    with _sse_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


def _init_run() -> Path:
    LOG_BASE.mkdir(parents=True, exist_ok=True)
    d = LOG_BASE / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_log(entry: dict[str, object]) -> None:
    global _log_file
    if _log_file is None:
        return
    with _log_lock:
        try:
            with _log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            _out(f"Log write error: {exc}")


def _save_screenshot(turn: int, b64: str) -> None:
    if not b64:
        return
    try:
        (_run_dir / f"turn_{turn:04d}.png").write_bytes(base64.b64decode(b64))
    except Exception as exc:
        _out(f"Screenshot save error turn {turn}: {exc}")


def _extract_user(msgs: list[dict[str, object]]) -> dict[str, object]:
    r: dict[str, object] = {"sst_text": "", "has_image": False, "image_b64": ""}
    for msg in reversed(msgs):
        if msg.get("role") != "user":
            continue
        c = msg.get("content", "")
        if isinstance(c, list):
            text_parts: list[str] = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    text_parts.append(str(p.get("text", "")))
                elif p.get("type") == "image_url":
                    r["has_image"] = True
                    url = str(p.get("image_url", {}).get("url", ""))
                    marker = "data:image/png;base64,"
                    if url.startswith(marker):
                        r["image_b64"] = url[len(marker):]
            r["sst_text"] = text_parts[0] if text_parts else ""
        elif isinstance(c, str):
            r["sst_text"] = c
        break
    return r


def _parse_req(raw: bytes) -> dict[str, object]:
    r: dict[str, object] = {
        "model": "", "sampling": {}, "messages_count": 0,
        "system_prompt": "", "parse_error": None,
        "sst_text": "", "has_image": False, "image_b64": "",
    }
    try:
        obj = json.loads(raw)
        r["model"] = str(obj.get("model", ""))
        msgs = obj.get("messages", [])
        r["messages_count"] = len(msgs)
        for k in ("temperature", "top_p", "max_tokens"):
            if k in obj:
                s = r.get("sampling")
                if isinstance(s, dict):
                    s[k] = obj[k]
        for msg in msgs:
            if msg.get("role") == "system":
                r["system_prompt"] = str(msg.get("content", ""))
                break
        r.update(_extract_user(msgs))
    except Exception as exc:
        r["parse_error"] = str(exc)
    return r


def _parse_resp(raw: bytes) -> dict[str, object]:
    r: dict[str, object] = {
        "vlm_text": "", "finish_reason": "", "usage": {},
        "response_id": "", "parse_error": None,
    }
    try:
        obj = json.loads(raw)
        r["response_id"] = str(obj.get("id", ""))
        ch = obj.get("choices", [])
        if ch and isinstance(ch, list):
            r["vlm_text"] = str(ch[0].get("message", {}).get("content", ""))
            r["finish_reason"] = str(ch[0].get("finish_reason", ""))
        if isinstance(obj.get("usage"), dict):
            r["usage"] = obj["usage"]
    except Exception as exc:
        r["parse_error"] = str(exc)
    return r


def _get_render_config() -> tuple[int, int]:
    try:
        import config as _cfg
        return int(_cfg.WIDTH), int(_cfg.HEIGHT)
    except Exception:
        return 512, 288


def _dispatch_render(
    seq: int,
    image_b64: str,
    actions: list[dict[str, object]],
) -> str:
    rw, rh = _get_render_config()
    job: dict[str, object] = {
        "seq": seq,
        "image_b64": image_b64,
        "actions": actions,
        "render_w": rw,
        "render_h": rh,
    }
    with _render_job_lock:
        global _render_job
        _render_job = job
    _render_event.clear()
    _render_event.wait()
    with _render_result_lock:
        result = str(_render_result.get("image_b64", ""))
        result_seq = _render_result.get("seq", -1)
    return result if result_seq == seq and result else image_b64


def _swap_image_in_request(raw_req: bytes, new_b64: str) -> bytes:
    try:
        obj = json.loads(raw_req)
        for msg in obj.get("messages", []):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                part["image_url"]["url"] = "data:image/png;base64," + new_b64
                return json.dumps(obj).encode()
    except Exception as exc:
        _out(f"Image swap error: {exc}")
    return raw_req


def _is_paused() -> bool:
    try:
        for d in LOG_BASE.iterdir():
            if d.is_dir() and (d / "PAUSED").exists():
                return True
    except Exception:
        pass
    return False


def _pause_agent() -> bool:
    try:
        for d in sorted(LOG_BASE.iterdir(), reverse=True):
            if d.is_dir() and d.name.startswith("run_"):
                (d / "PAUSED").write_text(
                    f"Paused via panel: {datetime.now().isoformat()}\n", encoding="utf-8"
                )
                return True
    except Exception:
        pass
    return False


def _unpause_agent() -> bool:
    ok = False
    try:
        for d in LOG_BASE.iterdir():
            if d.is_dir():
                pf = d / "PAUSED"
                if pf.exists():
                    pf.unlink()
                    ok = True
    except Exception:
        pass
    return ok


def _write_run_json(name: str, data: object) -> bool:
    try:
        (_run_dir / name).write_text(json.dumps(data), encoding="utf-8")
        return True
    except Exception:
        return False


def _read_run_json(name: str, default: object = None) -> object:
    try:
        return json.loads((_run_dir / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _get_preview_b64() -> str:
    try:
        from capture import preview_b64
        return preview_b64(960)
    except Exception as exc:
        _out(f"Preview capture failed: {exc}")
        return ""


def _get_screen_size() -> tuple[int, int]:
    try:
        from capture import screen_size
        return screen_size()
    except Exception:
        return (1920, 1080)


def _run_debug_executor(raw: str) -> dict[str, object]:
    try:
        r = subprocess.run(
            [sys.executable, str(EXECUTE_SCRIPT)],
            input=json.dumps({"raw": raw, "run_dir": str(_run_dir), "debug": True}),
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, Exception) as exc:
        return {"error": str(exc)}
    stderr_lines: list[str] = []
    if r.stderr:
        for line in r.stderr.strip().splitlines():
            stderr_lines.append(line)
            _out(f"[debug-exec] {line}")
    if not r.stdout or not r.stdout.strip():
        return {"error": "No output from executor", "stderr": stderr_lines}
    try:
        result: dict[str, object] = json.loads(r.stdout)
        result["stderr"] = stderr_lines
        return result
    except json.JSONDecodeError:
        return {"error": "Bad JSON from executor", "raw_stdout": r.stdout, "stderr": stderr_lines}


def _store_turn(turn: int, entry: dict[str, object]) -> None:
    with _turn_store_lock:
        _turn_store[turn] = entry
        if len(_turn_store) > _MAX_STORED_TURNS:
            del _turn_store[min(_turn_store)]


def _get_stored_turn(turn: int) -> dict[str, object] | None:
    with _turn_store_lock:
        return _turn_store.get(turn)


def _forward(raw: bytes) -> tuple[int, bytes, str]:
    req = urllib.request.Request(
        UPSTREAM, data=raw,
        headers={"Content-Type": "application/json", "Connection": "keep-alive"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), ""
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return exc.code, body, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        err = f"URLError: {exc.reason}"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return 502, json.dumps({"error": err}).encode(), err


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FranzPanel/15"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_FILE.read_bytes() if HTML_FILE.exists() else b"<h1>Not found</h1>", "text/html; charset=utf-8")
            return

        if self.path == "/canvas":
            self._send(200, CANVAS_FILE.read_bytes() if CANVAS_FILE.exists() else b"<h1>Not found</h1>", "text/html; charset=utf-8")
            return

        if self.path == "/events":
            self._sse()
            return

        if self.path == "/health":
            sw, sh = _get_screen_size()
            self._send(200, json.dumps({
                "status": "ok", "turn": _turns,
                "uptime_s": round(time.monotonic() - _t0, 1),
                "sse_clients": len(_sse_clients),
                "main_running": _main_proc is not None and _main_proc.poll() is None,
                "paused": _is_paused(), "screen_w": sw, "screen_h": sh,
            }).encode(), "application/json")
            return

        if self.path == "/preview":
            self._send(200, json.dumps({"image_b64": _get_preview_b64()}).encode(), "application/json")
            return

        if self.path == "/crop":
            data = _read_run_json("crop.json")
            self._send(200, json.dumps(data if data else {}).encode(), "application/json")
            return

        if self.path == "/allowed_tools":
            data = _read_run_json("allowed_tools.json")
            if not isinstance(data, list):
                data = list(ALL_TOOLS)
            self._send(200, json.dumps(data).encode(), "application/json")
            return

        if self.path == "/render_job":
            with _render_job_lock:
                job = _render_job
            self._send(200, json.dumps(job if job else {}).encode(), "application/json")
            return

        m = re.match(r"^/turn/(\d+)/screenshot$", self.path)
        if m:
            turn_num = int(m.group(1))
            entry = _get_stored_turn(turn_num)
            if entry:
                uri = str(entry.get("image_data_uri", ""))
                if uri:
                    self._send(200, json.dumps({"image_data_uri": uri}).encode(), "application/json")
                    return
            png_path = _run_dir / f"turn_{turn_num:04d}.png"
            if png_path.exists():
                b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
                self._send(200, json.dumps({"image_data_uri": f"data:image/png;base64,{b64}"}).encode(), "application/json")
                return
            self._send(404, b'{"error":"no screenshot"}', "application/json")
            return

        self.send_error(404)

    def do_POST(self) -> None:
        cl = int(self.headers.get("Content-Length", 0))
        raw_req = self.rfile.read(cl) if cl > 0 else b""

        if self.path == "/pause":
            self._send(200, json.dumps({"paused": True, "ok": _pause_agent()}).encode(), "application/json")
            return

        if self.path == "/unpause":
            self._send(200, json.dumps({"paused": False, "ok": _unpause_agent()}).encode(), "application/json")
            return

        if self.path == "/crop":
            try:
                data = json.loads(raw_req)
                ok = _write_run_json("crop.json", data)
                _out(f"Crop set: {data}")
                self._send(200, json.dumps({"ok": ok, "crop": data}).encode(), "application/json")
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if self.path == "/allowed_tools":
            try:
                data = json.loads(raw_req)
                if not isinstance(data, list):
                    data = list(ALL_TOOLS)
                data = [t for t in data if t in ALL_TOOLS]
                ok = _write_run_json("allowed_tools.json", data)
                _out(f"Allowed tools: {data}")
                self._send(200, json.dumps({"ok": ok, "tools": data}).encode(), "application/json")
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if self.path == "/annotated":
            try:
                data = json.loads(raw_req)
                seq = data.get("seq")
                b64 = str(data.get("image_b64", ""))
                with _render_result_lock:
                    _render_result["seq"] = seq
                    _render_result["image_b64"] = b64
                _render_event.set()
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        if self.path == "/debug/execute":
            try:
                data = json.loads(raw_req)
                raw_text = str(data.get("raw", ""))
                if not raw_text.strip():
                    self._send(400, json.dumps({"error": "Empty raw text"}).encode(), "application/json")
                    return
                result = _run_debug_executor(raw_text)
                self._send(200, json.dumps(result, default=str).encode(), "application/json")
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        global _render_seq
        turn = _next_turn()
        t0 = time.monotonic()
        rp = _parse_req(raw_req)

        _out(f"turn={turn} fwd ({len(raw_req)}b{' +IMG' if rp['has_image'] else ''})...")

        with _prev_actions_lock:
            actions = list(_prev_actions)

        image_b64 = str(rp.get("image_b64", ""))
        annotated_b64 = image_b64

        if image_b64:
            _render_seq += 1
            annotated_b64 = _dispatch_render(_render_seq, image_b64, actions)
            if annotated_b64 != image_b64:
                _out(f"Canvas annotated turn={turn} seq={_render_seq}")

        final_req = _swap_image_in_request(raw_req, annotated_b64) if annotated_b64 != image_b64 else raw_req

        status, raw_resp, error = _forward(final_req)
        latency = (time.monotonic() - t0) * 1000
        resp_p = _parse_resp(raw_resp)

        vlm_text = str(resp_p.get("vlm_text", ""))
        new_actions = _parse_actions_from_vlm(vlm_text)
        with _prev_actions_lock:
            _prev_actions[:] = new_actions
        if new_actions:
            _out(f"Parsed {len(new_actions)} action(s) for next annotation")

        try:
            self.send_response(status)
            for k, v in (
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(raw_resp))),
                ("Connection", "keep-alive"),
            ):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw_resp)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

        image_data_uri = f"data:image/png;base64,{annotated_b64}" if annotated_b64 else ""

        entry: dict[str, object] = {
            "turn": turn,
            "timestamp": datetime.now().isoformat(),
            "latency_ms": round(latency, 1),
            "image_data_uri": image_data_uri,
            "request": {
                "model": rp["model"],
                "story_text": rp["sst_text"],
                "has_image": rp["has_image"],
                "sampling": rp["sampling"],
                "messages_count": rp["messages_count"],
                "body_size": len(raw_req),
                "parse_error": rp["parse_error"],
            },
            "response": {
                "status": status,
                "response_id": resp_p["response_id"],
                "vlm_text": vlm_text,
                "vlm_text_length": len(vlm_text),
                "finish_reason": resp_p["finish_reason"],
                "usage": resp_p["usage"],
                "body_size": len(raw_resp),
                "parse_error": resp_p["parse_error"],
                "error": error,
            },
            "actions": new_actions,
        }
        _store_turn(turn, entry)
        _save_screenshot(turn, annotated_b64)

        log_entry = {k: v for k, v in entry.items() if k != "image_data_uri"}
        _write_log(log_entry)

        _out(f"turn={turn} {latency:.0f}ms status={status} vlm={len(vlm_text)}c")

        try:
            se = {k: v for k, v in entry.items() if k != "image_data_uri"}
            _sse_broadcast(json.dumps(se, default=str))
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ct: str) -> None:
        self.send_response(code)
        for k, v in (("Content-Type", ct), ("Content-Length", str(len(body))), ("Cache-Control", "no-cache")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sse(self) -> None:
        self.send_response(200)
        for k, v in (
            ("Content-Type", "text/event-stream"),
            ("Cache-Control", "no-cache"),
            ("Connection", "keep-alive"),
            ("Access-Control-Allow-Origin", "*"),
        ):
            self.send_header(k, v)
        self.end_headers()
        q = _sse_register()
        try:
            self.wfile.write(b'data: {"type":"connected"}\n\n')
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _sse_unregister(q)


class Server(http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def process_request(self, request: object, client_address: tuple[str, int]) -> None:
        threading.Thread(target=self._handle, args=(request, client_address), daemon=True).start()

    def _handle(self, request: object, client_address: tuple[str, int]) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def _pipe(stream: object, prefix: str) -> None:
    try:
        for line in stream:
            t = line.rstrip("\n\r")
            if t:
                _out(f"{prefix} {t}")
    except (ValueError, OSError):
        pass


def _run_main() -> None:
    global _main_proc
    _out("Waiting 3s before launching main.py...")
    if _shutdown.wait(3):
        return
    env = {**os.environ, "FRANZ_RUN_DIR": str(_run_dir)}
    while not _shutdown.is_set():
        _out("Launching main.py...")
        with _main_lock:
            _main_proc = subprocess.Popen(
                [sys.executable, str(MAIN_SCRIPT)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, bufsize=1,
            )
        threads = [
            threading.Thread(target=_pipe, args=(s, p), daemon=True)
            for s, p in ((_main_proc.stdout, "[main.out]"), (_main_proc.stderr, "[main.err]"))
        ]
        for t in threads:
            t.start()
        rc = _main_proc.wait()
        for t in threads:
            t.join(timeout=5)
        if _shutdown.is_set():
            break
        _out(f"main.py exited ({rc}), restarting in 3s...")
        if _shutdown.wait(3):
            break


def _stop_main() -> None:
    with _main_lock:
        if _main_proc and _main_proc.poll() is None:
            _main_proc.terminate()
            try:
                _main_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _main_proc.kill()
                _main_proc.wait(5)


def main() -> None:
    global _run_dir, _log_file
    try:
        pc = Path(__file__).parent / "__pycache__"
        if pc.is_dir():
            shutil.rmtree(pc)
    except Exception:
        pass
    _run_dir = _init_run()
    (_run_dir / "PAUSED").write_text(
        f"Auto-paused at start: {datetime.now().isoformat()}\n", encoding="utf-8"
    )
    _log_file = _run_dir / "turns.jsonl"
    srv = Server((HOST, PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _out(f"Proxy http://{HOST}:{PORT}/ -> {UPSTREAM}")
    _out(f"Dashboard http://{HOST}:{PORT}/")
    _out(f"Logging to {_run_dir}")
    webbrowser.open(f"http://{HOST}:{PORT}/")
    threading.Thread(target=_run_main, daemon=True).start()
    _out("Ready. Paused -- select crop region in dashboard, then Resume.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _out("Shutting down...")
        _shutdown.set()
        _stop_main()
        srv.shutdown()
        _out("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
