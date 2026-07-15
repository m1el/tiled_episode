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
        return ((2 * r + 1) * cols - 1 - k) * tw - dx
    return (k - r * cols) * tw + dx


def blit(frame, tile, x, y):
    th, tw = tile.shape[:2]
    x0, x1 = max(x, 0), min(x + tw, frame.shape[1])
    if x0 < x1:
        frame[y:y + th, x0:x1] = tile[:, x0 - x:x1 - x]


def render(inp, out, rows, size, qp, pad, bg, snake, loops):
    in_w, in_h, num, den, frames = probe(inp)
    out_w, out_h = size or (in_w, in_h)
    tw, th, cols = layout(in_w, in_h, out_w, out_h, rows)
    slots = rows * cols - 2 * pad
    if slots < 1:
        raise ValueError(f"pad={pad} leaves no room in a {rows}x{cols} grid")
    n_loop = frames // slots
    if n_loop < 1:
        raise ValueError(f"input has {frames} frames, fewer than {slots} tiles")
    print(f"grid {rows}x{cols}, tile {tw}x{th}, "
          f"loop {n_loop} frames ({n_loop * den / num:.2f}s)")

    with tempfile.NamedTemporaryFile(dir=Path(out).parent or None) as tmp:
        # The grid slides a fractional number of pixels per frame; rounding
        # to whole pixels gives visibly uneven motion. So each frame is
        # composited twice, at offsets floor(dx) and floor(dx)+1, and the
        # encode pass lerps the planes by frac(dx) — equivalent to bilinear
        # sampling of the strip at the true subpixel position.
        canvas = np.memmap(tmp.name, np.uint8, "w+",
                           shape=(2, n_loop, out_h, out_w, 3))
        canvas[:] = bg

        dec = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", inp, "-map", "0:v:0",
             "-vf", f"scale={tw}:{th}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE)
        fsize = tw * th * 3
        for f in range(slots * n_loop):
            buf = dec.stdout.read(fsize)
            if len(buf) < fsize:
                break
            tile = np.frombuffer(buf, np.uint8).reshape(th, tw, 3)
            k, n = pad + f // n_loop, f % n_loop
            a, _ = subpixel(n, n_loop, tw)
            for r in range(rows):
                for dx, plane in ((a, canvas[0]), (a + 1, canvas[1])):
                    x = tile_x(k, r, dx, cols, tw, snake)
                    if -tw < x < out_w:
                        blit(plane[n], tile, x, r * th)
        while dec.stdout.read(1 << 20):
            pass
        dec.stdout.close()
        dec.wait()

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
    print(f"wrote {out}")


def parse_size(s):
    w, h = map(int, s.lower().split("x"))
    return w, h


def parse_color(s):
    s = s.lstrip("#")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("-o", "--output", help="output file (.mp4 or .webm)")
    p.add_argument("-r", "--rows", type=int, default=20)
    p.add_argument("-s", "--size", type=parse_size, help="WxH, default input size")
    p.add_argument("-q", "--qp", type=int, default=24, help="codec CRF")
    p.add_argument("-p", "--pad", type=int, default=0,
                   help="background tiles at start and end")
    p.add_argument("-b", "--bg", type=parse_color, default="000000",
                   help="background hex color")
    p.add_argument("--snake", action="store_true",
                   help="boustrophedon rows, alternating slide direction")
    p.add_argument("-l", "--loops", type=int, default=1,
                   help="repeat the loop this many times in the output")
    a = p.parse_args()
    out = a.output or Path(a.input).stem + "_tiled.mp4"
    render(a.input, out, a.rows, a.size, a.qp, a.pad, a.bg, a.snake, a.loops)


if __name__ == "__main__":
    main()
