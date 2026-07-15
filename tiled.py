#!/usr/bin/env python3
"""Convert a video into a seamlessly looping, sliding tileset.

The input timeline is split across a grid of rows*cols tiles (wrapped like
text). Every tile plays its chunk of the video in realtime while the whole
grid slides one tile width per loop, so tile k ends exactly where tile k+1
began: the output loops seamlessly.
"""
import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,duration",
         "-show_entries", "format=duration", "-of", "json", path],
        check=True, capture_output=True).stdout
    info = json.loads(out)
    s = info["streams"][0]
    num, den = map(int, s["avg_frame_rate"].split("/"))
    dur = float(s.get("duration") or info["format"]["duration"])
    frames = int(s["nb_frames"]) if s.get("nb_frames") else round(dur * num / den)
    return s["width"], s["height"], num, den, frames


def layout(in_w, in_h, out_w, out_h, rows):
    th = out_h // rows
    tw = max(1, round(th * in_w / in_h))
    cols = math.ceil(out_w / tw)
    return tw, th, cols


def subpixel(n, n_loop, tw):
    dx = tw * n / n_loop
    a = int(dx)
    return a, dx - a


def tile_x(k, r, dx, cols, tw, snake):
    if snake and r % 2:
        return ((r + 1) * cols - 1 - k) * tw - dx
    return (k - r * cols) * tw + dx


def blit(frame, tile, x, y):
    th, tw = tile.shape[:2]
    x0, x1 = max(x, 0), min(x + tw, frame.shape[1])
    if x0 < x1:
        frame[y:y + th, x0:x1] = tile[:, x0 - x:x1 - x]


def decode(inp, tw, th, total, subs, sub_style):
    """Yield up to `total` tile-sized rgb24 frames."""
    if subs is None:
        cmd = ["ffmpeg", "-v", "error", "-i", inp, "-map", "0:v:0",
               "-vf", f"scale={tw}:{th}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    else:
        # homebrew ffmpeg often lacks libass; mpv always bundles it
        cmd = ["mpv", inp, "--msg-level=all=error", "--no-audio",
               f"--sid={subs + 1}",
               f"--vf=scale={tw}:{th},format=rgb24",
               "--of=rawvideo", "--ovc=rawvideo", "--o=-"]
        if sub_style:
            cmd.append(f"--sub-ass-style-overrides={sub_style}")
    dec = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fsize = tw * th * 3
    n_read = 0
    for f in range(total):
        buf = dec.stdout.read(fsize)
        if len(buf) < fsize:
            break
        n_read = f + 1
        if f % 256 == 0:
            print(f"\rdecoding {f}/{total} ({100 * f // total}%)",
                  end="", file=sys.stderr, flush=True)
        yield np.frombuffer(buf, np.uint8).reshape(th, tw, 3)
    print(f"\rdecoded {n_read} frames        ", file=sys.stderr)
    while dec.stdout.read(1 << 20):
        pass
    dec.stdout.close()
    if dec.wait():
        raise RuntimeError("decode failed")


def render_tiles(canvas, tiles, n_loop, pad, rows, cols, tw, snake):
    out_w = canvas.shape[3]
    for f, tile in enumerate(tiles):
        k, n = pad + f // n_loop, f % n_loop
        a, _ = subpixel(n, n_loop, tw)
        for r in range(rows):
            for dx, plane in ((a, canvas[0]), (a + 1, canvas[1])):
                x = tile_x(k, r, dx, cols, tw, snake)
                if -tw < x < out_w:
                    blit(plane[n], tile, x, r * tile.shape[0])


def encode(canvas, out, num, den, qp, loops, tw):
    _, n_loop, out_h, out_w, _ = canvas.shape
    if out.endswith(".webm"):
        codec = ["-c:v", "libvpx-vp9", "-crf", str(qp), "-b:v", "0",
                 "-row-mt", "1", "-cpu-used", "4"]
    else:
        codec = ["-c:v", "libx264", "-crf", str(qp), "-preset", "veryfast"]
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{out_w}x{out_h}", "-r", f"{num}/{den}", "-i", "-",
         *codec, "-pix_fmt", "yuv420p", out],
        stdin=subprocess.PIPE)
    for _ in range(loops):
        for n in range(n_loop):
            _, frac = subpixel(n, n_loop, tw)
            if frac:
                frac = np.float32(frac)
                frame = (canvas[0, n] * (1 - frac) + canvas[1, n] * frac
                         + 0.5).astype(np.uint8)
            else:
                frame = canvas[0, n]
            enc.stdin.write(frame.tobytes())
    enc.stdin.close()
    if enc.wait():
        raise RuntimeError("ffmpeg encode failed")


def render(a):
    inp, out, rows, pad = a.input, a.output, a.rows, a.pad
    size, bg, snake, qp, loops = a.size, a.bg, a.snake, a.qp, a.loops
    subs, sub_style = a.subs, a.sub_style
    in_w, in_h, num, den, frames = probe(inp)
    out_w, out_h = size or (in_w, in_h)
    tw, th, cols = layout(in_w, in_h, out_w, out_h, rows)
    slots = rows * cols - 2 * pad
    if slots < 1:
        raise ValueError(f"pad={pad} leaves no room in a {rows}x{cols} grid")
    # ceil: a partial last tile ends with background instead of the
    # remainder frames being trimmed off the end of the video
    n_loop = -(-frames // slots)
    if n_loop < 1:
        raise ValueError("input has no frames")
    tmp_bytes = 2 * n_loop * out_h * out_w * 3
    if tmp_bytes > 4 << 30:
        raise ValueError(f"temp canvas would be {tmp_bytes / (1 << 30):.1f} GiB "
                         f"({n_loop} loop frames); increase rows or reduce size")
    print(f"grid {rows}x{cols}, tile {tw}x{th}, "
          f"loop {n_loop} frames ({n_loop * den / num:.2f}s)")

    with tempfile.NamedTemporaryFile(dir=Path(out).parent,
                                     suffix=".rgb24.tmp") as tmp:
        # The grid slides a fractional number of pixels per frame; rounding
        # to whole pixels gives visibly uneven motion. So render_tiles
        # composites every frame twice — plane 0 at offset floor(dx),
        # plane 1 at floor(dx)+1 — and encode lerps the planes by frac(dx),
        # i.e. bilinear sampling at the true subpixel position.
        canvas = np.memmap(tmp.name, np.uint8, "w+",
                           shape=(2, n_loop, out_h, out_w, 3))
        canvas[:] = bg

        tiles = decode(inp, tw, th, slots * n_loop, subs, sub_style)
        render_tiles(canvas, tiles, n_loop, pad, rows, cols, tw, snake)
        encode(canvas, out, num, den, qp, loops, tw)
    print(f"wrote {out}")


def parse_size(s):
    w, h = map(int, s.lower().split("x"))
    return w, h


def parse_color(s):
    s = s.lstrip("#")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="input video file")
    p.add_argument("-o", "--output",
                   help="output file, .mp4 (x264) or .webm (VP9); "
                        "default: <input>_tiled.mp4")
    p.add_argument("-r", "--rows", type=int, default=20,
                   help="tile rows in the grid (default: 20)")
    p.add_argument("-s", "--size", type=parse_size, metavar="WxH",
                   help="output resolution (default: input resolution)")
    p.add_argument("-q", "--qp", type=int, default=24,
                   help="encoder CRF, lower = better (default: 24)")
    p.add_argument("-p", "--pad", type=int, default=0, metavar="N",
                   help="pad start and end of the tile sequence with N "
                        "background tiles each (default: 0)")
    p.add_argument("-b", "--bg", type=parse_color, default="000000",
                   metavar="RRGGBB", help="background hex color (default: black)")
    p.add_argument("--snake", action="store_true",
                   help="boustrophedon order: odd rows run right-to-left "
                        "and slide the other way")
    p.add_argument("-l", "--loops", type=int, default=1,
                   help="repeat the loop N times in the output (default: 1)")
    p.add_argument("-S", "--subs", type=int, metavar="N",
                   help="burn in subtitle track N (0-based; decodes via mpv)")
    p.add_argument("--sub-style", metavar="STYLE",
                   help="libass style overrides, e.g. 'FontSize=64'")
    a = p.parse_args()
    a.output = a.output or Path(a.input).stem + "_tiled.mp4"
    render(a)


if __name__ == "__main__":
    main()
