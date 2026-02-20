# annotate.py
"""Annotate a screenshot with red action markers using tkinter.

Reads JSON from stdin: {"image_b64": "...", "actions": [{"name": "click", "args": [500, 300]}, ...]}
Writes JSON to stdout: {"image_b64": "..."}

Uses only Python standard library (tkinter ships with Python on Windows).
Coordinates are 0-1000 normalized.

Requires Python 3.13+. Uses only ASCII characters.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import struct
import zlib


def _log(msg: str) -> None:
    sys.stderr.write(f"[annotate] {msg}\n")
    sys.stderr.flush()


def _decode_png_dimensions(data: bytes) -> tuple[int, int]:
    """Extract width and height from PNG IHDR chunk."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a PNG file")
    # IHDR is always the first chunk after the 8-byte signature
    # Chunk: 4 bytes length, 4 bytes type, data, 4 bytes CRC
    ihdr_start = 8
    chunk_len = struct.unpack(">I", data[ihdr_start:ihdr_start + 4])[0]
    chunk_type = data[ihdr_start + 4:ihdr_start + 8]
    if chunk_type != b"IHDR":
        raise ValueError("First chunk is not IHDR")
    ihdr_data = data[ihdr_start + 8:ihdr_start + 8 + chunk_len]
    width = struct.unpack(">I", ihdr_data[0:4])[0]
    height = struct.unpack(">I", ihdr_data[4:8])[0]
    return width, height


def _draw_on_pixels(
    pixels: list[list[tuple[int, int, int, int]]],
    width: int,
    height: int,
    actions: list[dict[str, object]],
) -> None:
    """Draw red markers directly on RGBA pixel grid."""
    red = (255, 40, 40, 255)
    dark_red = (180, 0, 0, 255)

    def _set(x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            pixels[y][x] = color

    def _circle(cx: int, cy: int, r: int, color: tuple[int, int, int, int]) -> None:
        """Draw circle outline using midpoint algorithm."""
        x = 0
        y = r
        d = 1 - r
        while x <= y:
            for dx, dy in [
                (x, y), (-x, y), (x, -y), (-x, -y),
                (y, x), (-y, x), (y, -x), (-y, -x),
            ]:
                _set(cx + dx, cy + dy, color)
            x += 1
            if d < 0:
                d += 2 * x + 1
            else:
                y -= 1
                d += 2 * (x - y) + 1

    def _filled_circle(cx: int, cy: int, r: int, color: tuple[int, int, int, int]) -> None:
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy <= r * r:
                    _set(cx + dx, cy + dy, color)

    def _line(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int, int], thickness: int = 3) -> None:
        """Draw thick line using Bresenham with thickness."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        half = thickness // 2
        while True:
            for tx in range(-half, half + 1):
                for ty in range(-half, half + 1):
                    _set(x0 + tx, y0 + ty, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def _crosshair(cx: int, cy: int, size: int, color: tuple[int, int, int, int]) -> None:
        for i in range(-size, size + 1):
            _set(cx + i, cy, color)
            _set(cx + i, cy + 1, color)
            _set(cx, cy + i, color)
            _set(cx + 1, cy + i, color)

    def _arrowhead(x1: int, y1: int, x0: int, y0: int, color: tuple[int, int, int, int]) -> None:
        """Draw a small arrowhead at (x1, y1) pointing from (x0, y0)."""
        import math
        dx = x1 - x0
        dy = y1 - y0
        length = math.sqrt(dx * dx + dy * dy)
        if length < 1:
            return
        ndx = dx / length
        ndy = dy / length
        arrow_len = min(15, int(length * 0.3))
        px = -ndy
        py = ndx
        for t in range(arrow_len):
            frac = t / max(arrow_len, 1)
            bx = x1 - ndx * t
            by = y1 - ndy * t
            spread = int(frac * arrow_len * 0.5)
            for s in range(-spread, spread + 1):
                _set(int(bx + px * s), int(by + py * s), color)

    for action in actions:
        name = str(action.get("name", ""))
        args_raw = action.get("args", [])
        if not isinstance(args_raw, list):
            continue
        args = [int(a) for a in args_raw if isinstance(a, (int, float))]

        if name in ("click", "right_click", "double_click") and len(args) >= 2:
            px = args[0] * width // 1000
            py = args[1] * height // 1000
            _circle(px, py, 14, red)
            _circle(px, py, 15, dark_red)
            _crosshair(px, py, 8, red)
            if name == "double_click":
                _circle(px, py, 20, red)
                _circle(px, py, 21, dark_red)
            if name == "right_click":
                # Draw "R" indicator: small filled square offset
                for dx in range(-3, 4):
                    for dy in range(-3, 4):
                        _set(px + 18 + dx, py - 18 + dy, dark_red)
                # White R letter approximation
                for dy in range(-2, 3):
                    _set(px + 16, py - 18 + dy, (255, 255, 255, 255))

        elif name == "drag" and len(args) >= 4:
            x0 = args[0] * width // 1000
            y0 = args[1] * height // 1000
            x1 = args[2] * width // 1000
            y1 = args[3] * height // 1000
            _line(x0, y0, x1, y1, red, 3)
            _filled_circle(x0, y0, 6, red)
            _circle(x1, y1, 8, red)
            _arrowhead(x1, y1, x0, y0, dark_red)


def _pixels_to_png(
    pixels: list[list[tuple[int, int, int, int]]],
    width: int,
    height: int,
) -> bytes:
    """Encode RGBA pixel grid as PNG using only struct + zlib."""
    raw_data = bytearray()
    for row in pixels:
        raw_data.append(0)  # filter byte: None
        for r, g, b, a in row:
            raw_data.extend((r, g, b, a))

    compressed = zlib.compress(bytes(raw_data), 6)

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    # bit depth=8, color_type=6 (RGBA), compression=0, filter=0, interlace=0

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", compressed)
    png += _chunk(b"IEND", b"")
    return png


def _png_to_pixels(data: bytes) -> tuple[list[list[tuple[int, int, int, int]]], int, int]:
    """Decode PNG to RGBA pixel grid. Handles only 8-bit RGBA and RGB PNGs."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a PNG")

    pos = 8
    width = height = 0
    bit_depth = 0
    color_type = 0
    idat_chunks: list[bytes] = []

    while pos < len(data):
        chunk_len = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + chunk_len]
        pos += 12 + chunk_len  # 4 len + 4 type + data + 4 crc

        if chunk_type == b"IHDR":
            width = struct.unpack(">I", chunk_data[0:4])[0]
            height = struct.unpack(">I", chunk_data[4:8])[0]
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if bit_depth != 8 or color_type not in (2, 6):
        # For unsupported formats, use tkinter fallback
        raise ValueError(
            f"Unsupported PNG format: bit_depth={bit_depth}, color_type={color_type}"
        )

    raw = zlib.decompress(b"".join(idat_chunks))

    channels = 4 if color_type == 6 else 3
    stride = 1 + width * channels  # +1 for filter byte

    pixels: list[list[tuple[int, int, int, int]]] = []

    prev_row = bytes(width * channels)
    row_pos = 0

    for y in range(height):
        filter_byte = raw[row_pos]
        row_data = bytearray(raw[row_pos + 1:row_pos + stride])
        row_pos += stride

        # Apply PNG filters
        if filter_byte == 1:  # Sub
            for i in range(channels, len(row_data)):
                row_data[i] = (row_data[i] + row_data[i - channels]) & 0xFF
        elif filter_byte == 2:  # Up
            for i in range(len(row_data)):
                row_data[i] = (row_data[i] + prev_row[i]) & 0xFF
        elif filter_byte == 3:  # Average
            for i in range(len(row_data)):
                left = row_data[i - channels] if i >= channels else 0
                up = prev_row[i]
                row_data[i] = (row_data[i] + (left + up) // 2) & 0xFF
        elif filter_byte == 4:  # Paeth
            for i in range(len(row_data)):
                left = row_data[i - channels] if i >= channels else 0
                up = prev_row[i]
                up_left = prev_row[i - channels] if i >= channels else 0
                p = left + up - up_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pr = left
                elif pb <= pc:
                    pr = up
                else:
                    pr = up_left
                row_data[i] = (row_data[i] + pr) & 0xFF

        prev_row = bytes(row_data)

        row_pixels: list[tuple[int, int, int, int]] = []
        for x in range(width):
            offset = x * channels
            r = row_data[offset]
            g = row_data[offset + 1]
            b = row_data[offset + 2]
            a = row_data[offset + 3] if channels == 4 else 255
            row_pixels.append((r, g, b, a))
        pixels.append(row_pixels)

    return pixels, width, height


def main() -> None:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        json.dump({"error": "Bad JSON"}, sys.stdout)
        return

    image_b64 = str(req.get("image_b64", ""))
    actions = req.get("actions", [])

    if not image_b64 or not actions:
        json.dump({"image_b64": image_b64}, sys.stdout)
        return

    try:
        png_data = base64.b64decode(image_b64)
        pixels, width, height = _png_to_pixels(png_data)
    except ValueError:
        # Fallback: try using tkinter for unsupported PNG formats
        try:
            pixels, width, height = _decode_with_tkinter(image_b64)
        except Exception as exc:
            _log(f"Cannot decode image: {exc}")
            json.dump({"image_b64": image_b64}, sys.stdout)
            return

    if not isinstance(actions, list):
        json.dump({"image_b64": image_b64}, sys.stdout)
        return

    _draw_on_pixels(pixels, width, height, actions)
    out_png = _pixels_to_png(pixels, width, height)
    out_b64 = base64.b64encode(out_png).decode("ascii")
    json.dump({"image_b64": out_b64}, sys.stdout)


def _decode_with_tkinter(image_b64: str) -> tuple[list[list[tuple[int, int, int, int]]], int, int]:
    """Fallback decoder using tkinter PhotoImage."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()

    photo = tk.PhotoImage(data=image_b64)
    width = photo.width()
    height = photo.height()

    pixels: list[list[tuple[int, int, int, int]]] = []
    for y in range(height):
        row: list[tuple[int, int, int, int]] = []
        for x in range(width):
            r, g, b = photo.get(x, y)
            row.append((r, g, b, 255))
        pixels.append(row)

    root.destroy()
    return pixels, width, height


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        try:
            json.dump({"error": str(exc)}, sys.stdout)
        except Exception:
            pass
