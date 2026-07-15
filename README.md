# tiled

Convert a video into a seamlessly looping, sliding tileset.

The input timeline is split across a grid of `rows × cols` tiles, wrapped
like text. Every tile plays its chunk of the video in realtime while the
whole grid slides one tile width per loop, so tile k ends exactly where
tile k+1 began: the output loops seamlessly. A 24-minute episode at the
default 20 rows becomes a ~3.7 s loop in which the entire episode is
visible at once.

## Example

A 24 minute episode rendered to 3.7 second loop.

[out.webm](https://github.com/user-attachments/assets/03a82c72-0171-4089-9eee-4214cef5cec6)

## Requirements

- `ffmpeg` / `ffprobe` on PATH
- `mpv` on PATH, only for `--subs`
- `uv`

## Usage

```
uv run tiled.py input.mkv
```

```
usage: tiled.py [-h] [-o OUTPUT] [-r ROWS] [-s WxH] [-q QP] [-p N] [-b RRGGBB]
                [--snake] [-l LOOPS] [-S N] [--sub-style STYLE]
                input

positional arguments:
  input                input video file

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  output file, .mp4 (x264) or .webm (VP9); default:
                       <input>_tiled.mp4
  -r, --rows ROWS      tile rows in the grid (default: 20)
  -s, --size WxH       output resolution (default: input resolution)
  -q, --qp QP          encoder CRF, lower = better (default: 24)
  -p, --pad N          pad start and end of the tile sequence with N
                       background tiles each (default: 0)
  -b, --bg RRGGBB      background hex color (default: black)
  --snake              boustrophedon order: odd rows run right-to-left and
                       slide the other way
  -l, --loops LOOPS    repeat the loop N times in the output (default: 1)
  -S, --subs N         burn in subtitle track N (0-based; decodes via mpv)
  --sub-style STYLE    libass style overrides, e.g. 'FontSize=64'
```

Examples:

```
uv run tiled.py episode.mkv -r 10 -s 1280x720 -o out.webm
uv run tiled.py episode.mkv -p 2 -b 202020 --snake
uv run tiled.py episode.mkv -S 0 --sub-style FontSize=120
```

Output frame rate matches the input. Tile size and loop length are derived:
tiles keep the input aspect ratio at `height/rows`, and the loop is
`input_frames / (rows·cols − 2·pad)` frames long. The slide is subpixel
(frames are composited at the two neighboring integer offsets and blended),
so motion is smooth even below 1 px/frame. A temp file about the size of
`2 × loop_frames` uncompressed frames is used while rendering.

## Tests

```
uv run pytest
```
