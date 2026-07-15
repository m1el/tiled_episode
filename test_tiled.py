import subprocess
from argparse import Namespace

import numpy as np
import pytest

from tiled import blit, layout, parse_color, parse_size, render, subpixel, tile_x


def run_render(inp, out, **kw):
    args = dict(input=inp, output=out, rows=3, size=None, qp=30, pad=0,
                bg=np.zeros(3, np.uint8), snake=False, loops=1,
                subs=None, sub_style=None)
    args.update(kw)
    render(Namespace(**args))


def test_layout():
    tw, th, cols = layout(1920, 1080, 1920, 1080, 20)
    assert (tw, th, cols) == (96, 54, 20)


def test_layout_odd():
    tw, th, cols = layout(1280, 720, 640, 480, 7)
    assert th == 480 // 7
    assert tw == round(th * 16 / 9)
    assert cols * tw >= 640 > (cols - 1) * tw


@pytest.mark.parametrize("snake", [False, True])
def test_seamless_handoff(snake):
    rows, cols, tw, out_w = 5, 4, 10, 40
    for k in range(rows * cols):
        for r in range(rows):
            assert tile_x(k, r, tw, cols, tw, snake) == \
                tile_x(k + 1, r, 0, cols, tw, snake)


def test_every_tile_visible():
    rows, cols, tw, out_w = 3, 4, 10, 40
    for k in range(rows * cols):
        assert any(0 <= tile_x(k, r, 0, cols, tw, False) <= out_w - tw
                   for r in range(rows))


def test_subpixel():
    assert subpixel(0, 88, 96) == (0, 0)
    for n in range(88):
        a, f = subpixel(n, 88, 96)
        assert 0 <= f < 1
        assert a + f == pytest.approx(96 * n / 88)


def test_blit_clips():
    frame = np.zeros((4, 10, 3), np.uint8)
    tile = np.full((2, 4, 3), 7, np.uint8)
    blit(frame, tile, -2, 0)
    assert frame[0, :2].sum() == 42 and frame[0, 2:].sum() == 0
    blit(frame, tile, 8, 2)
    assert frame[2, 8:].sum() == 42
    blit(frame, tile, 20, 0)


def test_parsers():
    assert parse_size("640X360") == (640, 360)
    assert list(parse_color("#ff0080")) == [255, 0, 128]


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("in") / "clip.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=64x36:rate=10:duration=10", path], check=True)
    return path


def probe_out(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,nb_read_frames,avg_frame_rate",
         "-of", "csv=p=0", path], check=True, capture_output=True, text=True).stdout
    w, h, fps, n = out.strip().split(",")
    return int(w), int(h), fps, int(n)


def test_render_e2e(clip, tmp_path):
    out = str(tmp_path / "out.mp4")
    run_render(clip, out)
    # 100 frames, grid 3x4 -> ceil to 9 frames per tile, last tile part-bg
    assert probe_out(out) == (64, 36, "10/1", 9)


def decode_raw(path):
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True).stdout


def test_render_subs(tmp_path):
    d = tmp_path / "it's [a], test;dir"
    d.mkdir()
    (d / "s.srt").write_text("1\n00:00:00,000 --> 00:00:09,000\nHELLO WORLD\n")
    inp = str(d / "clip.mkv")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=64x36:rate=10:duration=10",
         "-i", str(d / "s.srt"), "-c:s", "srt", inp], check=True)
    plain, subbed = str(tmp_path / "plain.mp4"), str(tmp_path / "subbed.mp4")
    run_render(inp, plain, qp=4)
    run_render(inp, subbed, qp=4, subs=0)
    a, b = decode_raw(plain), decode_raw(subbed)
    assert len(a) == len(b)
    assert a != b


def test_render_size_guard(clip, tmp_path):
    # 1 row upscaled to 4k: loop = all 100 frames, temp canvas ~4.6 GiB
    with pytest.raises(ValueError, match="GiB"):
        run_render(clip, str(tmp_path / "x.mp4"), rows=1, size=(3840, 2160))


def test_render_pad_bg(clip, tmp_path):
    out = str(tmp_path / "out.webm")
    run_render(clip, out, size=(64, 36), pad=1, bg=parse_color("ff0000"),
               snake=True, loops=2)
    w, h, fps, n = probe_out(out)
    assert (w, h, n) == (64, 36, 20)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", out, "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(36, 64, 3)
    r, g, b = frame[2, 2]
    assert r > 200 and g < 60 and b < 60  # pad tile 0 is background red
