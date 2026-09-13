"""Live multicolor lava lamp for the Green Building's 17 by 9 display.

Python 3.10+; no external dependencies.
"""
import argparse
import colorsys
import http.client
import math
import random
import time
from urllib.parse import urlsplit


def frames(fps=20, seed=None):
    """Yield RGB frames with wandering, interacting blobs and blended colors."""
    rng = random.Random(seed)
    blobs = [[rng.uniform(1, 7), rng.uniform(1, 15), rng.uniform(-0.5, 0.5),
          rng.uniform(-0.7, 0.7), rng.uniform(2.3, 3.1), i / 6,
          rng.uniform(0, math.tau)] for i in range(6)]
    dt, t = 1 / fps, 0.0
    while True:
        for b in blobs:
            x, y, vx, vy, radius, hue, phase = b
            b[2] += (0.8 * math.sin(t * 0.63 + phase) - vx * 0.4) * dt
            b[3] += (0.65 * math.sin(t * 0.39 + phase) + (8 - y) * 0.035 - vy * 0.25) * dt
        for i, a in enumerate(blobs):
            for b in blobs[i + 1:]:
                dx, dy = b[0] - a[0], b[1] - a[1]
                dist = math.hypot(dx, dy)
                reach = (a[4] + b[4]) * 0.65
                if 0 < dist < reach:
                    force = (reach - dist) * 0.7 * dt
                    for axis, delta in ((2, dx), (3, dy)):
                        a[axis] -= force * delta / dist
                        b[axis] += force * delta / dist
        for b in blobs:
            for pos, vel, low, high in ((0, 2, 0.3, 7.7), (1, 3, 0.3, 15.7)):
                b[vel] = max(-1.5, min(1.5, b[vel]))
                b[pos] += b[vel] * dt
                if b[pos] < low or b[pos] > high:
                    b[pos] = max(low, min(high, b[pos]))
                    b[vel] *= -0.85
        colors = [colorsys.hsv_to_rgb((b[5] + t * 0.012) % 1, 0.85, 1) for b in blobs]
        pixels = bytearray()
        for y in range(17):
            for x in range(9):
                weights = [math.exp(-2.5 * ((x - b[0]) ** 2 + ((y - b[1]) * 0.85) ** 2) / b[4] ** 2) for b in blobs]
                total = sum(weights)
                glow = min(1, total ** 1.4)
                for channel, base in enumerate((5, 2, 15)):
                    color = sum(w * c[channel] for w, c in zip(weights, colors)) / max(total, 1e-12)
                    pixels.append(round(base * (1 - glow) + 255 * color * glow))
        yield bytes(pixels)
        t += dt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", help="Existing simulator instance, e.g. crisp-owl")
    parser.add_argument("--base-url", default="https://sundai.willsarg.com")
    parser.add_argument("--fps", type=int, default=20, choices=range(1, 31), metavar="1-30")
    parser.add_argument("--seed", type=int, help="Reproducible simulation seed")
    parser.add_argument("--frames", type=int, default=0, help="Stop after N frames; default runs until Ctrl+C")
    parser.add_argument("--dry-run", action="store_true", help="Generate and validate frames without networking")
    args = parser.parse_args()
    if args.frames < 0:
        parser.error("--frames must be nonnegative")
    if not args.instance or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in args.instance):
        parser.error("Use the exact instance name from your simulator URL")
    url = urlsplit(args.base_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment:
        parser.error("--base-url must be an HTTP(S) server URL")
    path = url.path.rstrip("/") + "/api/i/" + args.instance + "/frame"
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = None
    count, failures = 0, 0
    limit = args.frames or (600 if args.dry_run else 0)
    print("Validating locally" if args.dry_run else f"Streaming to {args.base_url.rstrip('/')}/{args.instance}?view=close", flush=True)
    try:
        for pixels in frames(args.fps, args.seed):
            started = time.monotonic()
            assert len(pixels) == 17 * 9 * 3
            if not args.dry_run:
                try:
                    if conn is None:
                        conn = connection_type(url.netloc, timeout=10)
                    conn.request("POST", path, body=pixels, headers={
                        "Content-Type": "application/octet-stream",
                        "User-Agent": "green-building-lava-lamp/1",
                    })
                    response = conn.getresponse()
                    body = response.read()
                    if response.status == 429 or response.status >= 500:
                        raise OSError(f"HTTP {response.status}")
                    if response.status != 204:
                        raise SystemExit(f"Simulator rejected frame: HTTP {response.status}: {body.decode(errors='replace')}")
                    failures = 0
                except (OSError, http.client.HTTPException) as error:
                    failures += 1
                    if conn:
                        conn.close()
                        conn = None
                    if failures >= 10:
                        raise SystemExit(f"Stopped after 10 connection failures: {error}")
                    print(f"Connection interrupted; retrying: {error}", flush=True)
                    time.sleep(min(failures, 5))
                    continue
                time.sleep(max(0, 1 / args.fps - (time.monotonic() - started)))
            count += 1
            if limit and count >= limit:
                break
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if conn:
            conn.close()
    print(f"{count} frames {'validated' if args.dry_run else 'sent'}.", flush=True)


if __name__ == "__main__":
    main()
