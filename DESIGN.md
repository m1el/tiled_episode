# Design notes

Why `tiled.py` is built the way it is. This documents the decisions behind the
code — the reasoning, the trade-offs accepted, and the alternatives that were
tried and rejected — rather than what each function does (the code and `--help`
cover that).

The guiding constraints for the whole project were: **optimize for speed and
simplicity, no unnecessary comments or code, raw exceptions are fine, commit
often, tests in a separate file, and use `uv` for everything.** Almost every
decision below is a specialization of "keep it small and correct."

---

## 1. The core idea: a seamless loop from a wrapped, sliding grid

The input timeline is split across a `rows × cols` grid, **wrapped like text**.
Tile *k* plays input frames `[k·N, (k+1)·N)`; the whole grid slides exactly one
tile width per `N`-frame loop.

**Why this loops seamlessly.** At frame `N` the grid has slid exactly one tile
width and every tile has advanced exactly its `N` frames, so **tile k ends
exactly where tile k+1 began** — in both *position* and *video time*. Two
identities make it work:

- geometric: `tile_x(k, r, tw) == tile_x(k+1, r, 0)`
- content: `k·N + N == (k+1)·N`

Because the handoff is exact, **one loop is all that is ever needed.** A 24-minute
episode at the default 20 rows collapses to a ~3.7 s loop in which the whole
episode is on screen at once.

**Frame N is never rendered.** It would be *pixel-identical* to frame 0, not
merely adjacent to it. The correct seamless interval is the half-open `[0, N)`;
the last emitted frame sits one subpixel step before the repeat, at
`dx = tw·(N−1)/N`.

- *Rejected:* emitting the shared frame N. On repeat you would see frame 0 twice
  in a row — a one-frame stutter every loop.

**The spatial wrap reuses the temporal wrap.** No extra column is rendered
either. A tile entering from the left edge (`−tw < x < 0`) is the same content
leaving the row above it on the right; clipping in `blit` handles the partial
tiles at both edges. Mid-slide a row shows `cols + 1` slots (one entering, one
exiting, `cols − 1` full); at `dx = 0` it is back to `cols`. The visibility
predicate is exactly `−tw < x < out_w`.

**The one accepted content cut.** For row 0 the entering slot would be tile `−1`,
which doesn't exist, so it stays background: the video's *start* appears at the
row-0 left edge on each wrap, and its *end* slides off the bottom-right. Motion
is still perfectly seamless; only *content* has a discontinuity there. `--pad 1`
moves that cut onto a background→background transition — the polished way to run
it (see §7).

---

## 2. Subpixel motion via a dual-plane canvas blend

At the default layout the grid moves ~1.09 px/frame. Rounding each slide to a
whole pixel means moving 1 px on most frames and 2 px on roughly every 11th — an
uneven cadence the eye reads as jank.

**Decision.** Composite every frame at *both* neighboring integer offsets,
`floor(dx)` and `floor(dx)+1`, into two canvas planes, and linearly blend them by
the fractional part `frac(dx)` at encode time. This is exactly bilinear
resampling restricted to the x-axis (a tent reconstruction filter).

**Why two whole-frame planes instead of a per-pixel resample.** The lerp commutes
with the shift, so blending the two whole shifted frames per-pixel is identical
to a per-pixel gather at the true fractional position — but it costs only two
integer blits plus one vectorized multiply-add per frame.

- **Correct at tile seams:** adjacent tiles are contiguous in *both* planes, so
  at a k/k+1 boundary the blend mixes exactly the two pixels a true fractional
  shift would mix — no seam artifacts.
- **Correct for snake rows:** odd rows slide the opposite direction and `tile_x`
  already negates the offset, so passing `a` and `a+1` shifts them by 0 and −1 px
  — the right pair for that direction.

*Measured:* on static SMPTE color bars (every frame identical, so a
frame-to-frame diff is a pure proxy for slide distance) the motion variance
dropped ~10×, diff std `4.77 → 0.47`, at the same mean motion.

- *Rejected:* a fancier reconstruction kernel (cubic, Lanczos). Linear costs a
  little blur at moving edges, maximal at `frac = 0.5`, but at ~1 px/frame with
  tiles already downscaled ~20× it is well below noticeable.
- *Cost accepted:* one extra blit per tile (negligible) and a doubled temp
  canvas (§4).
- *Fast path:* when `frac == 0`, encode writes plane 0 untouched.

The subpixel explanation deliberately lives as a comment at the **canvas
allocation**, not split across `render_tiles` and `encode`. The trick is a
contract *between* the two functions — neither half explains it alone — and the
`(2, …)` canvas shape is the one artifact that ties them together, exactly where
a reader first asks "why two planes?"

---

## 3. Layout math: round up, don't trim

**Tile height rounds up:** `th = ceil(out_h / rows)`, and each row lands at
`y = round(r · out_h / rows)`.

- *Why:* flooring (`out_h // rows`) leaves a permanent background strip at the
  bottom whenever `rows` doesn't divide the height (e.g. `-r 7` on 1080p → a 1 px
  black stripe). Rounding up plus rounded row placement distributes the leftover
  pixels evenly and fills the full height.
- *Consequence accepted:* `rows·th` can exceed `out_h`, so rows overlap by ≤1 px
  (later rows deterministically overdraw) and the last row can overhang the
  bottom — handled by bottom-clipping in `blit`.

**Loop length rounds up:** `n_loop = ceil(frames / slots)`.

- *Why:* this is the tail-to-background decision (§9). Flooring trims up to
  `slots − 1` frames (~17 s worst case) off the end of the video. Ceiling keeps
  every frame's slot; the canvas is pre-filled with background and the decode
  EOF-break leaves the partial last tile ending in background.

**Width:** `tw = max(1, round(th · in_w / in_h))` keeps the input aspect ratio;
`cols = ceil(out_w / tw)` adds just enough columns to cover the frame, so the
rendered band always extends past the right edge and the output frame is a fixed
crop window onto a seamless band. Worst-case horizontal overhang is `tw − 1` px,
always in the cropped-off last column, so always invisible.

Both ceilings are spelled `(x + div − 1) // div` and the comments keep the word
**"ceil"** — a small style point that was churned during development
(`-(-x // y)` and the wording "round up" were both tried and reverted) because it
names the operation precisely.

---

## 4. Memory: a disk-backed canvas with an up-front size guard

**Composite into an `np.memmap` temp file, not RAM.** At low row counts the loop
is thousands of frames long; a memmap keeps memory constant regardless of loop
length.

**Guard before allocating.** `tmp_bytes = 2 · n_loop · out_h · out_w · 3`; if it
exceeds 4 GiB, raise `ValueError` with the size and a hint. Fewer rows → longer
loops → the file grows fast (4 rows would quietly burn ~27 GiB). The check runs
the moment loop length is known, **before** any allocation or decode, so a
mistake fails instantly instead of after minutes of decoding. The `2×` is the two
blend planes.

**The canvas is rgb24, not yuv420p.** It costs ~50% more temp space (3 B/px vs
1.5) but keeps blit and the lerp trivially correct. Subsampled chroma at half
resolution would not align with odd x offsets, breaking both the subpixel blend
and odd-offset blits. YUV appears only at the final encode step, for codec and
player compatibility.

The temp file is created in `Path(out).parent` (same volume as the output, so it
uses the output disk's free space) with suffix `.rgb24.tmp`, and is unlinked only
after encode has consumed it. This location was itself a reversal: it was briefly
moved to the system temp dir after a stray 1 GB canvas got committed, then moved
back once `.gitignore` (`tmp*`, `*.tmp`) made the same-directory placement safe.

---

## 5. Pipeline: decode → render_tiles → encode

`render(a)` is a short orchestrator (probe, layout, guards, canvas) that reads
like a table of contents; the work lives in three stages that map one-to-one onto
the data flow, each stage's signature stating what it needs.

- **`decode` is a generator** yielding tile-sized rgb24 frames, and owns the
  whole decoder lifecycle (command, progress, drain, exit-code check). Streaming
  one frame at a time keeps memory constant, keeps the run decode-bound, and
  avoids any intermediate codec round-trip.
- **`render(a)` takes the argparse `Namespace`** and destructures it in its first
  lines; `main` only fills the output default.
  - *Rejected:* a dedicated config struct/dataclass — judged worse than
    destructuring the args object for a script this size.

---

## 6. Subtitles: mpv, because homebrew ffmpeg lacks libass

When `--subs` is given, the decode leg switches from ffmpeg to **mpv**
(`--sid`, `--vf=scale,format=rgb24`, `--of/--ovc=rawvideo`, `--o=-`).

- *Why:* the local homebrew ffmpeg — every versioned keg — was built without
  libass, so the `subtitles` filter simply doesn't exist (and there's no
  `drawtext` either). mpv bundles libass and renders ASS tracks natively. The
  user's custom ffmpeg build was deliberately left untouched.
- *Bonus:* mpv takes the filename as a plain argv argument, so all the
  filtergraph escaping vanished from the design.
  - *Rejected (and deleted):* an `ffquote` double-escaping helper was written
    first (two nested escaping levels), tested against a hostile path
    `it's [a], test;dir`, and failed both levels — then thrown away entirely once
    the real problem (missing libass) was found.
- *Limitation accepted, not fixed:* subtitles burned at full resolution then
  scaled ~20× down become ~2 px tall — texture, not readable text. `--sub-style`
  (mpv's `--sub-ass-style-overrides`, e.g. `FontSize=120`) is the escape hatch; a
  54 px tile genuinely can't hold readable dialogue.
- *Safety:* the decoder exit code is now checked so a failed decode can't
  silently emit a background-only video.

---

## 7. Optional modes: snake and padding

**`--snake` (boustrophedon).** Odd rows run right-to-left and slide the opposite
way. Seamlessness holds under *any* constant per-row offset, so the handoff
property is preserved — and unit-tested — in both orderings.

- *Bug found and fixed:* the odd-row formula was `(2r+1)·cols − 1 − k` instead of
  `(r+1)·cols − 1 − k`. The extra `r·cols` made odd rows show the *next* even
  row's tiles (mirrored duplicates), then pure background once `2r·cols` exceeded
  the tile count. It went undetected because seamlessness holds under any
  constant per-row offset **and** the visibility test originally ran only with
  `snake=False`. The fix came with parametrizing `test_every_tile_visible` over
  snake so the bug can't return silently.

**`--pad N`.** Reserve `N` background tiles at the start and end of the sequence
(`slots = rows·cols − 2·pad`, with a `ValueError` if that leaves no room).

- *Why:* instead of the row-0 corner cutting on live video content (§1), padding
  moves that discontinuity onto a background→background transition — the polished
  way to run the tool. Background color is `--bg` (hex, default black).

---

## 8. Encoding

Codec is chosen by output extension: **x264 (`veryfast`) for `.mp4`**,
**libvpx-VP9 (`crf`, `b:v 0`, `row-mt`, `cpu-used 4`) for `.webm`**; both emit
`yuv420p` for compatibility. Default CRF (`-q/--qp`) is **24** — a sane
middle-of-the-road default; lower is better quality. `--loops` repeats the
seamless loop N times in the output for convenience.

---

## 9. Robustness: probe, EOF, draining, tail

**Probing.** A single `ffprobe` JSON call reads width/height/frame-rate/
`nb_frames`/`duration`; frame count falls back to `round(dur · num / den)` when
`nb_frames` is absent (as in mkv).

- *Rejected:* exact `ffprobe -count_packets` — it needs a second pass over the
  file, not worth it for sub-second accuracy at the tail. Over- or
  under-estimates are both benign: the EOF-break leaves unwritten slots as
  background, and extra decoded frames are drained.
- *Install hint:* a missing `ffprobe` is caught and turned into
  `ffprobe not found; install ffmpeg (…)` instead of a raw traceback — the one
  place the "raw exceptions are fine" rule is narrowed, because a missing
  toolchain is the most common first-run failure. The `ffmpeg` calls in
  decode/encode are intentionally *not* guarded: `probe` runs first, and a split
  install (ffprobe missing but ffmpeg present) is unlikely.

**Draining to EOF.** The decoder was closed with buffered frames still pending,
producing "Broken pipe" noise. `decode` now drains to EOF
(`while dec.stdout.read(1 << 20): pass`) before closing. Because `n_loop` is a
ceiling, `total ≥ frames` always, so the drain is bounded to a handful of frames.

**Early-EOF count.** Progress once reported "decoded 721 frames" for a 720-frame
file: on the EOF break, the loop index is the *failed* read. Fixed by advancing
the counter only on a successful read.

**Streams.** Decode progress goes to **stderr** (decode is ~all the runtime),
keeping **stdout** clean for the result lines and for piping.

---

## 10. Packaging: uv, and a standalone header

The project uses `uv` for everything — `uv init`, `uv add numpy`,
`uv add --dev pytest`, `uv run tiled.py`, `uv run pytest` — with dependencies in
`pyproject.toml` / `uv.lock` for development.

On top of that, `tiled.py` carries a **PEP 723 inline metadata header**
(`# /// script … dependencies = ["numpy"] … ///`) so it also runs standalone via
`uv run tiled.py input.mkv`, auto-provisioning numpy without the project files.
The two coexist: the project setup for dev/test, the inline header for a
copy-one-file-and-run experience.

The "no unnecessary comments" rule shows in the result: the file carries
essentially one prose comment — the subpixel explanation — because that's the one
thing the code cannot speak for itself.

---

## 11. Testing strategy

The property the whole project exists for is proven by construction, not just
measured:

- **`test_seamless_wrap`** renders one loop from synthetic index-stamped tiles
  (frame index in R, x/y coords in G/B, so any pixel says exactly which source
  frame landed where), independently builds the would-be frame `N` (grid slid a
  full tile width, each slot showing the next chunk's first frame), and asserts
  it is **byte-identical** to frame 0. Parametrized over snake.
- **`test_seamless_handoff`** checks the geometric identity
  `tile_x(k,r,tw) == tile_x(k+1,r,0)` directly, over both orderings.
- **`test_every_tile_visible`** guarantees every tile appears somewhere on
  screen — parametrized over snake specifically because that's what caught the
  snake-formula bug.
- **`test_render_size_guard`** asserts the 4 GiB guard fires (1-row 4K upscale).
- **`test_render_subs`** muxes an srt into an mkv inside a hostile directory
  named `it's [a], test;dir` and asserts subbed ≠ plain output, keeping path
  handling honest.
- Plus `test_layout*`, `test_subpixel`, `test_blit_clips` (including the
  bottom-overhang case), `test_uneven_rows_fill_height` (sentinel fill proves the
  full height is covered), `test_render_e2e`, `test_render_pad_bg`,
  `test_parsers`.

Motion uniformity was validated separately on `smptebars` — a static input where
a consecutive-frame diff isolates slide distance from content playback — which is
how the ~10× jank reduction (§2) was quantified.

---

## 12. Reversals worth remembering

Decisions that were tried and undone during development, kept here so the reasons
aren't relearned:

- **`ffquote` escaping helper → deleted**, replaced by mpv once libass was found
  missing (§6).
- **Snake `(2r+1)·cols` → `(r+1)·cols`**, plus a snake-parametrized visibility
  test to catch it (§7).
- **`th = floor` → `ceil`** to fill the frame height; **`n_loop = floor` → ceil**
  for the background tail (§3, §9).
- **Temp file in system temp → back beside the output**, once `.gitignore` made
  the same-directory placement safe (§4).
- **README example format churn** — `<video>` → `![](example.webm)` → discovered
  GitHub doesn't embed webm → re-rendered as `example.mp4` → settled on
  `<video src="example.mp4" autoplay loop muted>`.
- **A stray 1 GB canvas memmap** got swept into git by `git add -A` during a live
  background render and had to be purged from history (`git filter-branch`,
  60.93 MiB → 18.28 KiB pack). Root-cause fixes: the `.rgb24.tmp` naming and a
  `.gitignore` covering `*.mp4`, `*.webm`, `tmp*`, `*.tmp`.
