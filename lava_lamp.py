#!/usr/bin/env python3
"""Live lava-lamp simulation for the Green Building's 17 by 9 display.

Python 3.10+; no external dependencies.

Simulation time uses a fixed timestep of 1/--fps so --seed stays reproducible
even when the network sends frames more slowly than the target rate.
"""
from __future__ import annotations

import argparse
import colorsys
import http.client
import math
import random
import struct
import sys
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Display contract: row-major RGB, origin top-left, y=0 is the roof.
WIDTH = 9
HEIGHT = 17
CHANNELS = 3
FRAME_BYTES = WIDTH * HEIGHT * CHANNELS
BACKGROUND = (5, 2, 15)
Y_ASPECT = 0.85  # windows are slightly taller than they are wide
GAUSS_FALLOFF = 2.5
GLOW_GAMMA = 1.35
PNG_SCALE = 24

# Blob population and size.
START_BLOBS = 6
MIN_BLOBS = 5
MAX_BLOBS = 7
MIN_RADIUS = 1.6
MAX_RADIUS = 4.2

# Physics. y increases downward; heat lives at the bottom of the building.
X_MIN, X_MAX = 0.3, WIDTH - 1.3
Y_MIN, Y_MAX = 0.3, HEIGHT - 1.3
MAX_SPEED = 1.8
RESTITUTION = 0.85
NEUTRAL_TEMP = 0.5
HEAT_RATE = 0.22
BUOYANCY = 3.4
DRAG = 0.55
WANDER = 0.9
WANDER_Y = 0.28
REPEL_REACH = 0.55
REPEL_STRENGTH = 1.1
MERGE_FACTOR = 0.32
SPLIT_RADIUS = 3.15
SPLIT_TEMP = 0.68
SPLIT_RATE = 0.35
HUE_SPREAD = 0.62
HUE_DRIFT = 0.033
HUE_SPEED_JITTER = 0.024
SAT_BASE = 0.82
SAT_TEMP = 0.18
VAL_BASE = 0.65
VAL_TEMP = 0.35

DRY_RUN_DEFAULT_FRAMES = 600
MAX_FAILURES = 10
HTTP_TIMEOUT = 10
USER_AGENT = "green-building-lava-lamp/2"
PROGRESS_EVERY = 2.0
DEFAULT_BASE_URL = "https://sundai.willsarg.com"
INSTANCE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"

EPILOG = """
Frame protocol:
  POST {base}/api/i/{instance}/frame
  Content-Type: application/octet-stream
  Body: 459 bytes, row-major RGB, 17 rows x 9 columns, origin top-left
  Success: HTTP 204

--fps is both the send rate and the physics timestep. A slower network
lowers displayed FPS but does not change seeded motion.

--dry-run with no --frames validates 600 frames. --preview and --dump-*
do not require an instance.

PIM545 controller:
  python3 lava_lamp.py crisp-owl --controller
  Tap A/B/X/Y on the pack; the same pebble is dropped on this 9x17 facade.
"""


@dataclass
class Blob:
    x: float
    y: float
    vx: float
    vy: float
    radius: float
    hue: float
    hue_speed: float
    phase: float
    temp: float


def ambient_temp(y: float) -> float:
    """Hot at the bottom of the facade, cool at the roof."""
    span = Y_MAX - Y_MIN
    ny = 0.0 if span <= 0 else (y - Y_MIN) / span
    ny = max(0.0, min(1.0, ny))
    return 0.08 + 0.92 * ny


def _mass(blob: Blob) -> float:
    return blob.radius * blob.radius


def spawn_blobs(rng: random.Random, count: int = START_BLOBS) -> list[Blob]:
    blobs = []
    start_hue = rng.random()
    for i in range(count):
        y = rng.uniform(Y_MIN + 0.8, Y_MAX - 0.8)
        blobs.append(
            Blob(
                x=rng.uniform(X_MIN + 0.8, X_MAX - 0.8),
                y=y,
                vx=rng.uniform(-0.45, 0.45),
                vy=rng.uniform(-0.65, 0.65),
                radius=rng.uniform(2.0, 3.4),
                hue=(start_hue + i / max(count, 1) * HUE_SPREAD) % 1.0,
                hue_speed=HUE_DRIFT + rng.uniform(-HUE_SPEED_JITTER, HUE_SPEED_JITTER),
                phase=rng.uniform(0, math.tau),
                temp=max(0.0, min(1.0, ambient_temp(y) + rng.uniform(-0.15, 0.15))),
            )
        )
    return blobs


def _mix_hue(h1: float, w1: float, h2: float, w2: float) -> float:
    x = w1 * math.cos(h1 * math.tau) + w2 * math.cos(h2 * math.tau)
    y = w1 * math.sin(h1 * math.tau) + w2 * math.sin(h2 * math.tau)
    return (math.atan2(y, x) / math.tau) % 1.0


def _integrate(blob: Blob, t: float, dt: float) -> None:
    blob.temp += (ambient_temp(blob.y) - blob.temp) * min(1.0, HEAT_RATE * dt)
    blob.temp = max(0.0, min(1.0, blob.temp))
    blob.hue = (blob.hue + blob.hue_speed * dt) % 1.0
    blob.vx += math.sin(t * 0.73 + blob.phase) * WANDER * dt
    blob.vy += math.sin(t * 0.41 + blob.phase * 1.3) * WANDER_Y * dt
    blob.vy += (NEUTRAL_TEMP - blob.temp) * BUOYANCY * dt
    blob.vx -= blob.vx * DRAG * dt
    blob.vy -= blob.vy * DRAG * dt


def _repel(blobs: list[Blob], dt: float) -> None:
    for i, a in enumerate(blobs):
        for b in blobs[i + 1 :]:
            dx = b.x - a.x
            dy = b.y - a.y
            dist = math.hypot(dx, dy)
            reach = (a.radius + b.radius) * REPEL_REACH
            if 0 < dist < reach:
                force = (reach - dist) * REPEL_STRENGTH * dt
                ux, uy = dx / dist, dy / dist
                a.vx -= force * ux
                a.vy -= force * uy
                b.vx += force * ux
                b.vy += force * uy


def _bounce(blob: Blob, dt: float) -> None:
    blob.vx = max(-MAX_SPEED, min(MAX_SPEED, blob.vx))
    blob.vy = max(-MAX_SPEED, min(MAX_SPEED, blob.vy))
    blob.x += blob.vx * dt
    blob.y += blob.vy * dt
    if blob.x < X_MIN or blob.x > X_MAX:
        blob.x = max(X_MIN, min(X_MAX, blob.x))
        blob.vx *= -RESTITUTION
    if blob.y < Y_MIN or blob.y > Y_MAX:
        blob.y = max(Y_MIN, min(Y_MAX, blob.y))
        blob.vy *= -RESTITUTION
    blob.radius = max(MIN_RADIUS, min(MAX_RADIUS, blob.radius))


def _try_merges(blobs: list[Blob]) -> list[Blob]:
    blobs = list(blobs)
    while len(blobs) > MIN_BLOBS:
        best: tuple[int, int] | None = None
        best_dist = 0.0
        for i, a in enumerate(blobs):
            for j in range(i + 1, len(blobs)):
                b = blobs[j]
                dist = math.hypot(b.x - a.x, b.y - a.y)
                if dist < MERGE_FACTOR * (a.radius + b.radius):
                    if best is None or dist < best_dist:
                        best, best_dist = (i, j), dist
        if best is None:
            break
        i, j = best
        a, b = blobs[i], blobs[j]
        m1, m2 = _mass(a), _mass(b)
        total = m1 + m2
        merged = Blob(
            x=(a.x * m1 + b.x * m2) / total,
            y=(a.y * m1 + b.y * m2) / total,
            vx=(a.vx * m1 + b.vx * m2) / total,
            vy=(a.vy * m1 + b.vy * m2) / total,
            radius=min(MAX_RADIUS, math.sqrt(total)),
            hue=_mix_hue(a.hue, m1, b.hue, m2),
            hue_speed=(a.hue_speed * m1 + b.hue_speed * m2) / total,
            phase=a.phase,
            temp=(a.temp * m1 + b.temp * m2) / total,
        )
        blobs.pop(j)
        blobs.pop(i)
        blobs.append(merged)
    return blobs


def _try_splits(blobs: list[Blob], rng: random.Random, dt: float) -> list[Blob]:
    out: list[Blob] = []
    remaining = list(blobs)
    while remaining:
        blob = remaining.pop()
        live = len(out) + 1 + len(remaining)
        if (
            live < MAX_BLOBS
            and blob.radius > SPLIT_RADIUS
            and blob.temp > SPLIT_TEMP
            and rng.random() < SPLIT_RATE * dt
        ):
            angle = rng.uniform(0, math.tau)
            offset = blob.radius * 0.35
            radius = max(MIN_RADIUS, blob.radius / math.sqrt(2))
            for sign in (-1, 1):
                child = Blob(
                    x=blob.x + sign * math.cos(angle) * offset,
                    y=blob.y + sign * math.sin(angle) * offset,
                    vx=blob.vx + sign * math.cos(angle) * 0.3,
                    vy=blob.vy + sign * math.sin(angle) * 0.3,
                    radius=radius,
                    hue=(blob.hue + sign * rng.uniform(0.06, 0.14)) % 1.0,
                    hue_speed=blob.hue_speed + sign * rng.uniform(0.002, 0.01),
                    phase=blob.phase + sign * 0.7,
                    temp=blob.temp,
                )
                _bounce(child, 0.0)
                out.append(child)
        else:
            out.append(blob)
        if len(out) + len(remaining) >= MAX_BLOBS:
            out.extend(remaining)
            break
    return out


def step_blobs(blobs: list[Blob], rng: random.Random, t: float, dt: float) -> list[Blob]:
    for blob in blobs:
        _integrate(blob, t, dt)
    _repel(blobs, dt)
    for blob in blobs:
        _bounce(blob, dt)
    blobs = _try_merges(blobs)
    return _try_splits(blobs, rng, dt)


def pixel_index(x: int, y: int) -> int:
    return (y * WIDTH + x) * CHANNELS


def render_frame(blobs: list[Blob]) -> bytes:
    colors = [
        colorsys.hsv_to_rgb(
            blob.hue % 1.0,
            max(0.0, min(1.0, SAT_BASE - SAT_TEMP * blob.temp)),
            max(0.0, min(1.0, VAL_BASE + VAL_TEMP * blob.temp)),
        )
        for blob in blobs
    ]
    pixels = bytearray()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            weights = [
                math.exp(
                    -GAUSS_FALLOFF
                    * ((x - blob.x) ** 2 + ((y - blob.y) * Y_ASPECT) ** 2)
                    / blob.radius**2
                )
                for blob in blobs
            ]
            total = sum(weights)
            glow = min(1.0, total**GLOW_GAMMA)
            for channel, base in enumerate(BACKGROUND):
                color = sum(w * c[channel] for w, c in zip(weights, colors)) / max(total, 1e-12)
                pixels.append(round(base * (1 - glow) + 255 * color * glow))
    return bytes(pixels)


# PIM545 corner buttons mapped onto this 9x17 facade (A/B roof, X/Y ground).
CORNERS = {
    "B": (0.3, 0.3),
    "A": (WIDTH - 1.2, 0.3),
    "Y": (0.3, HEIGHT - 1.2),
    "X": (WIDTH - 1.2, HEIGHT - 1.2),
}

WAVE_C = 10.0
WAVE_LAMBDA = 5.4
WAVE_K = math.tau / WAVE_LAMBDA
WAVE_SIGMA = 1.40
WAVE_GAMMA = 0.32
WAVE_R0 = 1.35
WAVE_MAX_AGE = 2.4
WAVE_MAX_DROPS = 4


def _circular_wave(r: float, t: float, amp: float) -> float:
    if t < 0 or amp == 0:
        return 0.0
    psi = r - WAVE_C * t
    env = math.exp(-(psi * psi) / (2.0 * WAVE_SIGMA * WAVE_SIGMA))
    spread = 1.0 / math.sqrt(0.35 * r + WAVE_R0)
    tdamp = math.exp(-WAVE_GAMMA * t)
    return amp * tdamp * spread * env * math.cos(WAVE_K * psi)


def _impulse(blobs: list[Blob], x: float, y: float, strength: float = 2.2) -> None:
    for blob in blobs:
        dx = blob.x - x
        dy = blob.y - y
        dist = math.hypot(dx, dy) + 0.18
        mag = min(3.2, strength / (dist * dist))
        blob.vx += (dx / dist) * mag
        blob.vy += (dy / dist) * mag
        blob.temp = max(0.0, min(1.0, blob.temp + 0.08 * mag))


class Ripples:
    def __init__(self) -> None:
        self.drops: list[tuple[float, float, float, float]] = []  # x, y, t, amp

    def drop(self, x: float, y: float, amp: float = 1.0) -> None:
        if len(self.drops) >= WAVE_MAX_DROPS:
            self.drops.pop(0)
        self.drops.append((x, y, 0.0, amp))

    def step(self, dt: float, blobs: list[Blob]) -> None:
        nxt = []
        for x, y, t, amp in self.drops:
            t += dt
            if t > WAVE_MAX_AGE:
                continue
            nxt.append((x, y, t, amp))
            for blob in blobs:
                dx = blob.x - x
                dy = blob.y - y
                r = math.hypot(dx, dy)
                if r < 0.08:
                    continue
                mag = _circular_wave(r, t, amp) * dt * 16.0
                blob.vx += (dx / r) * mag
                blob.vy += (dy / r) * mag
        self.drops = nxt

    def height(self, px: float, py: float) -> float:
        h = 0.0
        for x, y, t, amp in self.drops:
            h += _circular_wave(math.hypot(px - x, py - y), t, amp)
        return h

    def apply(self, pixels: bytes) -> bytes:
        if not self.drops:
            return pixels
        out = bytearray(pixels)
        for y in range(HEIGHT):
            for x in range(WIDTH):
                h = max(-1.6, min(1.6, self.height(x, y)))
                if h == 0:
                    continue
                add = round(h * 230)
                i = pixel_index(x, y)
                for c in range(CHANNELS):
                    out[i + c] = max(0, min(255, out[i + c] + add))
        return bytes(out)


class Simulation:
    def __init__(self, fps: int = 20, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.blobs = spawn_blobs(self.rng)
        self.ripples = Ripples()
        self.dt = 1 / fps
        self.t = 0.0

    def drop_corner(self, name: str) -> bool:
        key = name.strip().upper()[:1]
        if key not in CORNERS:
            return False
        x, y = CORNERS[key]
        self.ripples.drop(x, y)
        _impulse(self.blobs, x, y)
        return True

    def step(self) -> bytes:
        self.blobs = step_blobs(self.blobs, self.rng, self.t, self.dt)
        self.ripples.step(self.dt, self.blobs)
        pixels = self.ripples.apply(render_frame(self.blobs))
        self.t += self.dt
        return pixels


def frames(fps: int = 20, seed: int | None = None) -> Iterator[bytes]:
    """Yield RGB frames of heat-driven blobs that merge, split, and blend."""
    sim = Simulation(fps, seed)
    while True:
        yield sim.step()


def ansi_preview(pixels: bytes) -> str:
    """Two-column ANSI cells so each window is roughly square in a terminal."""
    reset = "\x1b[0m"
    rows = []
    for y in range(HEIGHT):
        cells = []
        for x in range(WIDTH):
            i = pixel_index(x, y)
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            cells.append(f"\x1b[48;2;{r};{g};{b}m  ")
        rows.append("".join(cells) + reset)
    return "\n".join(rows)


def write_ppm(path: str | Path, pixels: bytes, width: int = WIDTH, height: int = HEIGHT) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)


def upscale_rgb(pixels: bytes, width: int, height: int, scale: int) -> tuple[int, int, bytes]:
    out_w, out_h = width * scale, height * scale
    out = bytearray(out_w * out_h * CHANNELS)
    for y in range(height):
        for x in range(width):
            src = pixels[pixel_index(x, y) : pixel_index(x, y) + CHANNELS]
            for dy in range(scale):
                start = ((y * scale + dy) * out_w + x * scale) * CHANNELS
                for dx in range(scale):
                    o = start + dx * CHANNELS
                    out[o : o + CHANNELS] = src
    return out_w, out_h, bytes(out)


def write_png(path: str | Path, width: int, height: int, rgb: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = bytearray()
    row = width * CHANNELS
    for y in range(height):
        raw.append(0)
        raw.extend(rgb[y * row : (y + 1) * row])
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def horizontal_strip(
    frame_list: list[bytes], scale: int = 12, gap: int = 2
) -> tuple[int, int, bytes]:
    if not frame_list:
        raise ValueError("frame_list must be non-empty")
    scaled = [upscale_rgb(frame, WIDTH, HEIGHT, scale) for frame in frame_list]
    frame_w, frame_h = scaled[0][0], scaled[0][1]
    count = len(scaled)
    out_w = count * frame_w + (count - 1) * gap
    out_h = frame_h
    out = bytearray(BACKGROUND * (out_w * out_h))
    for index, (_, _, rgb) in enumerate(scaled):
        x0 = index * (frame_w + gap)
        for y in range(frame_h):
            src = y * frame_w * CHANNELS
            dst = (y * out_w + x0) * CHANNELS
            out[dst : dst + frame_w * CHANNELS] = rgb[src : src + frame_w * CHANNELS]
    return out_w, out_h, bytes(out)


def frame_limit(frame_count: int | None, dry_run: bool) -> int:
    """Explicit N wins, including 0 for unlimited. Omitted dry-run defaults to 600."""
    if frame_count is not None:
        return frame_count
    if dry_run:
        return DRY_RUN_DEFAULT_FRAMES
    return 0


def fps_type(value: str) -> int:
    try:
        fps = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("fps must be an integer") from error
    if not 1 <= fps <= 30:
        raise argparse.ArgumentTypeError("fps must be between 1 and 30")
    return fps


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "instance",
        nargs="?",
        help="Existing simulator instance from the URL, e.g. crisp-owl. "
        "Optional with --dry-run, --preview, or --dump-*",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Simulator origin")
    parser.add_argument(
        "--fps",
        type=fps_type,
        default=20,
        metavar="1-30",
        help="Target send rate and physics timestep (default: 20)",
    )
    parser.add_argument("--seed", type=int, help="Reproducible simulation seed")
    parser.add_argument(
        "--frames",
        type=int,
        default=None,
        help="Stop after N frames. Default: 600 with --dry-run / local dumps, "
        "unlimited while streaming or with --frames 0",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate frames without networking",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Draw each frame as an ANSI color grid on stderr",
    )
    parser.add_argument(
        "--dump-ppm",
        metavar="DIR",
        help="Write frame-0001.ppm, frame-0002.ppm, ... into DIR",
    )
    parser.add_argument(
        "--dump-png",
        metavar="PATH",
        help=f"Write a {PNG_SCALE}x nearest-neighbor PNG of the last generated frame",
    )
    parser.add_argument(
        "--controller",
        nargs="?",
        const="auto",
        metavar="PORT",
        help="Read pebble taps from a PIM545 ESP32 (USB serial). "
        "Pass a device such as /dev/cu.usbserial-0001, or omit the path to auto-detect",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="Controller serial baud (default: 115200)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.frames is not None and args.frames < 0:
        parser.error("--frames must be nonnegative")
    local_only = bool(args.dry_run or args.preview or args.dump_ppm or args.dump_png)
    if args.controller and not args.instance and not local_only:
        parser.error("instance is required with --controller unless using --preview or --dry-run")
    if not args.instance and not local_only:
        parser.error("instance is required unless using --dry-run, --preview, or --dump-*")
    if args.instance and any(c not in INSTANCE_CHARS for c in args.instance):
        parser.error("Use the exact instance name from your simulator URL")
    url = urlsplit(args.base_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment:
        parser.error("--base-url must be an HTTP(S) server URL")
    args._url = url
    args._streaming = bool(args.instance) and not args.dry_run
    args._limit = frame_limit(args.frames, args.dry_run or not args._streaming)
    return args


def open_controller(port: str, baud: int):
    try:
        import serial  # type: ignore
        from serial.tools import list_ports  # type: ignore
    except ImportError as error:
        raise SystemExit("pip install pyserial  (needed for --controller)") from error

    if port == "auto":
        ports = list(list_ports.comports())
        candidates = [
            p.device
            for p in ports
            if any(
                token in (p.device + " " + (p.description or "")).lower()
                for token in ("usb", "wch", "cp210", "ch340", "uart", "serial", "esp")
            )
        ]
        if not candidates:
            names = ", ".join(p.device for p in ports) or "(none)"
            raise SystemExit(f"No USB serial device found. Ports: {names}")
        port = candidates[0]
        print(f"Controller on {port}", flush=True)
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0
    ser.dtr = False
    ser.rts = False
    ser.open()
    time.sleep(0.3)
    ser.reset_input_buffer()
    return ser


def poll_pebbles(ser, buf: bytearray) -> list[str]:
    """Pull PEBBLE A/B/X/Y events from the ESP32 serial log."""
    chunk = ser.read(256) if ser is not None else b""
    if chunk:
        buf.extend(chunk)
    events: list[str] = []
    while True:
        nl = buf.find(b"\n")
        if nl < 0:
            break
        line = bytes(buf[:nl]).decode("ascii", errors="replace").strip()
        del buf[: nl + 1]
        if line.startswith("PEBBLE ") and len(line) > 7:
            events.append(line[7].upper())
    return events


def _send_frame(conn: http.client.HTTPConnection, path: str, pixels: bytes) -> None:
    conn.request(
        "POST",
        path,
        body=pixels,
        headers={
            "Content-Type": "application/octet-stream",
            "User-Agent": USER_AGENT,
        },
    )
    response = conn.getresponse()
    body = response.read()
    if response.status == 429 or response.status >= 500:
        raise OSError(f"HTTP {response.status}")
    if response.status != 204:
        raise SystemExit(
            f"Simulator rejected frame: HTTP {response.status}: {body.decode(errors='replace')}"
        )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    url = args._url
    path = url.path.rstrip("/") + "/api/i/" + (args.instance or "") + "/frame"
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = None
    sent = 0
    failures = 0
    generated = 0
    last_pixels: bytes | None = None
    limit = args._limit
    streaming = args._streaming
    if args.dry_run or not streaming:
        print("Generating locally" + (" (preview on stderr)" if args.preview else ""), flush=True)
    else:
        print(
            f"Streaming to {args.base_url.rstrip('/')}/{args.instance}?view=close",
            flush=True,
        )
    started_run = time.monotonic()
    last_report = started_run
    cursor_hidden = False
    sim = Simulation(args.fps, args.seed)
    controller = None
    serial_buf = bytearray()
    try:
        if args.controller:
            controller = open_controller(args.controller, args.baud)
            print("Listening for PIM545 corner taps (PEBBLE A/B/X/Y)", flush=True)
        while True:
            loop_started = time.monotonic()
            if controller is not None:
                for name in poll_pebbles(controller, serial_buf):
                    if sim.drop_corner(name):
                        print(f"pebble {name.upper()} -> building", flush=True)
            pixels = sim.step()
            if len(pixels) != FRAME_BYTES:
                raise RuntimeError(f"expected {FRAME_BYTES}-byte frames, got {len(pixels)}")
            generated += 1
            last_pixels = pixels
            if args.dump_ppm:
                write_ppm(Path(args.dump_ppm) / f"frame-{generated:04d}.ppm", pixels)
            if args.preview:
                if not cursor_hidden:
                    sys.stderr.write("\x1b[?25l")
                    cursor_hidden = True
                elapsed = max(time.monotonic() - started_run, 1e-9)
                sys.stderr.write(
                    "\x1b[H\x1b[2J"
                    + ansi_preview(pixels)
                    + f"\n{generated} frames  {generated / elapsed:.1f} fps\n"
                )
                sys.stderr.flush()
            if streaming:
                try:
                    if conn is None:
                        conn = connection_type(url.netloc, timeout=HTTP_TIMEOUT)
                    _send_frame(conn, path, pixels)
                    failures = 0
                except (OSError, http.client.HTTPException) as error:
                    failures += 1
                    if conn:
                        conn.close()
                        conn = None
                    if failures >= MAX_FAILURES:
                        raise SystemExit(f"Stopped after {MAX_FAILURES} connection failures: {error}")
                    # Drop this frame and generate a fresh one. Resending stale
                    # pixels would hitch the lamp; a live stream can skip.
                    print(f"Connection interrupted; retrying: {error}", flush=True)
                    time.sleep(min(failures, 5))
                    continue
                time.sleep(max(0, 1 / args.fps - (time.monotonic() - loop_started)))
            elif args.preview:
                time.sleep(max(0, 1 / args.fps - (time.monotonic() - loop_started)))
            sent += 1
            now = time.monotonic()
            if not args.preview and now - last_report >= PROGRESS_EVERY:
                elapsed = max(now - started_run, 1e-9)
                verb = "validated" if not streaming else "sent"
                print(f"{sent} frames {verb} ({sent / elapsed:.1f} fps)", flush=True)
                last_report = now
            if limit and sent >= limit:
                break
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if cursor_hidden:
            sys.stderr.write("\x1b[?25h")
            sys.stderr.flush()
        if conn:
            conn.close()
        if controller is not None:
            controller.close()
        if args.dump_png and last_pixels is not None:
            out_w, out_h, rgb = upscale_rgb(last_pixels, WIDTH, HEIGHT, PNG_SCALE)
            write_png(args.dump_png, out_w, out_h, rgb)
    verb = "validated" if not streaming else "sent"
    print(f"{sent} frames {verb}.", flush=True)


if __name__ == "__main__":
    main()
