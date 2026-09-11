"""Looping raster exports of an animated map: GIF, animated WebP, MP4, WebM.

The animated SVG moves its balls with CSS ``offset-path``, which no
rasteriser evaluates -- resvg would draw every ball parked at its path
start.  So a frame is not "the SVG at time t" rendered by someone else: the
map is emitted once with :data:`~nf_metro.render.animate.FRAME_SLOT` standing
in for the balls, and each frame substitutes the static circles
:mod:`nf_metro.render.animate` computes for that moment of the cycle.  Frame
and SVG therefore share one geometry and one clock, and the slot sits where
the animated balls would, so the stacking matches too.

The frame sequence spans exactly one animation cycle and stops one frame
short of repeating it, so playback loops seamlessly in either direction of
the wrap.

GIF and WebP are written by Pillow, already a dependency.  MP4 and WebM need
an encoder: ``ffmpeg`` on ``PATH``, else the binary the optional
``imageio-ffmpeg`` package bundles (``pip install nf-metro[video]``).
"""

from __future__ import annotations

__all__ = [
    "LARGE_LOOP_FRAMES",
    "VIDEO_FORMATS",
    "AnimationExport",
    "FfmpegNotFoundError",
    "NotAnimatedError",
    "VideoFormat",
    "write_animation",
]

import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from itertools import chain
from pathlib import Path
from typing import Any, Literal, get_args

from nf_metro.render.animate import (
    FRAME_SLOT,
    animation_frame_markup,
    build_animation_timeline,
)
from nf_metro.render.plan import RenderPlan
from nf_metro.render.raster import svg_to_png

VideoFormat = Literal["gif", "webp", "mp4", "webm"]
VIDEO_FORMATS: tuple[VideoFormat, ...] = get_args(VideoFormat)

#: Formats Pillow writes itself; the rest go through ffmpeg.
_PILLOW_FORMATS: frozenset[str] = frozenset({"gif", "webp"})

#: Past this many frames an export is minutes of work, and for GIF and WebP a
#: gigabyte-scale buffer.  Nothing is refused -- a long, smooth, high-scale
#: loop is a legitimate thing to want, and only the caller knows what it is
#: worth -- but the cost is quoted up front, while there is still time to stop.
LARGE_LOOP_FRAMES = 400

#: Bytes Pillow holds per pixel of every frame until the file is written, by
#: format: a GIF frame is palette-indexed, a WebP frame full colour.  ffmpeg
#: takes the frames as a stream and holds none of them.
_BYTES_PER_PIXEL = {"gif": 1, "webp": 3}

#: GIF stores a frame delay in hundredths of a second, so a requested fps
#: rarely divides evenly.  Delays are rounded with the leftover carried into
#: the next frame, which keeps the loop's total length right even though
#: individual frames land a few milliseconds either side.
_GIF_TICK_MS = 10


class NotAnimatedError(ValueError):
    """Raised when the SVG to export carries no animation to sample."""


class FfmpegNotFoundError(RuntimeError):
    """Raised when an MP4/WebM export finds no ffmpeg to encode with."""


@dataclass(frozen=True)
class AnimationExport:
    """What a completed export wrote, for the caller to report."""

    path: Path
    frames: int
    duration: float
    fps: float


def write_animation(
    svg: str,
    plan: RenderPlan,
    output: Path,
    fmt: VideoFormat,
    *,
    fps: float,
    duration: float | None = None,
    scale: float = 1.0,
    width: int | None = None,
    notify: Callable[[str], None] | None = None,
    progress: Callable[[Iterator[bytes], int], Iterator[bytes]] | None = None,
) -> AnimationExport:
    """Write one loop of *svg*'s animation to *output* as *fmt*.

    *svg* must have been emitted with ``animation_frame_slot`` set.  *duration*
    replaces the map's own cycle length, compressing (or stretching) the whole
    loop into that many seconds -- the balls then move faster or slower than
    the SVG does, which is usually what a long map needs to fit a README.
    *scale* and *width* size the frames exactly as they size a PNG.

    A big loop is minutes of rasterising, so *notify* is called once with what
    it is about to cost, and *progress* is handed the frame stream and its
    length to wrap in whatever indicator the caller has.  Both default to
    nothing, leaving the export silent.
    """
    if FRAME_SLOT not in svg:
        raise NotAnimatedError(
            "this map has no animation to export: add --animate (or a "
            "%%metro animate: true directive) to put balls on the lines."
        )

    timeline = build_animation_timeline(
        plan.graph,  # type: ignore[arg-type]
        plan.routes,  # type: ignore[arg-type]
        plan.station_offsets,  # type: ignore[arg-type]
        plan.theme,  # type: ignore[arg-type]
    )
    if not timeline.tracks or timeline.balls_per_line < 1:
        # Animated, but with nothing in motion: the loop would be one still
        # repeated a few hundred times, which is worth saying rather than
        # writing out.
        raise NotAnimatedError(
            "this map's animation has no moving balls to export: no metro "
            "line chains into a path for one to travel along."
        )
    loop = duration if duration is not None else timeline.cycle
    count = max(1, round(loop * fps))
    if notify is not None and count > LARGE_LOOP_FRAMES:
        notify(_cost_note(plan, fmt, count=count, scale=scale, width=width))

    theme: Any = plan.theme
    frames: Iterator[bytes] = (
        svg_to_png(
            svg.replace(FRAME_SLOT, animation_frame_markup(timeline, theme, i / count)),
            scale=scale,
            width=width,
        )
        for i in range(count)
    )
    if progress is not None:
        frames = progress(frames, count)

    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt in _PILLOW_FORMATS:
        _write_with_pillow(frames, output, fmt, count=count, loop=loop)
    else:
        _write_with_ffmpeg(frames, output, fmt, fps=count / loop)
    return AnimationExport(output, frames=count, duration=loop, fps=count / loop)


def _frame_pixels(
    plan: RenderPlan, *, scale: float, width: int | None
) -> tuple[int, int]:
    """Return the pixel size each frame rasterises to, as the PNG path sizes it."""
    if width is not None:
        return width, max(1, round(plan.svg_height * width / plan.svg_width))
    return max(1, round(plan.svg_width * scale)), max(1, round(plan.svg_height * scale))


def _cost_note(
    plan: RenderPlan, fmt: str, *, count: int, scale: float, width: int | None
) -> str:
    """Describe what a large export is about to spend, before it spends it."""
    pixel_width, pixel_height = _frame_pixels(plan, scale=scale, width=width)
    note = (
        f"{count} frames to rasterise at {pixel_width}x{pixel_height}; "
        "this will take a while. --fps and --duration set the frame count, "
        "--scale and --png-width the size."
    )
    per_pixel = _BYTES_PER_PIXEL.get(fmt)
    if per_pixel is None:
        return note
    held = count * pixel_width * pixel_height * per_pixel / 1024**3
    return (
        f"{note} {fmt.upper()} also holds the whole loop in memory while it "
        f"encodes (about {held:.1f} GB here); MP4 and WebM stream instead."
    )


def _frame_delays_ms(count: int, loop: float) -> list[int]:
    """Split *loop* seconds into *count* whole-tick GIF delays.

    Rounding each delay independently would drift the loop by up to half a
    tick per frame; carrying the remainder forward keeps the total within one
    tick of *loop* however badly the fps divides.
    """
    delays: list[int] = []
    written = 0.0
    for i in range(1, count + 1):
        target = loop * 1000.0 * i / count
        tick = max(
            _GIF_TICK_MS, round((target - written) / _GIF_TICK_MS) * _GIF_TICK_MS
        )
        delays.append(tick)
        written += tick
    return delays


def _write_with_pillow(
    frames: Iterator[bytes],
    output: Path,
    fmt: str,
    *,
    count: int,
    loop: float,
) -> None:
    """Write *frames* as a looping GIF or animated WebP.

    Frames are handed over as a generator, but Pillow materialises
    ``append_images`` for both containers and holds the loop until it is
    written -- see :data:`MAX_FRAMES`, which bounds that.  ffmpeg has no such
    limit, so a very long loop is cheaper as MP4 or WebM.
    """
    from PIL import Image

    delays = _frame_delays_ms(count, loop)
    first = Image.open(BytesIO(next(frames))).convert("RGB")

    if fmt == "gif":
        # One palette for the whole loop, taken from the first frame: the map
        # is static behind the balls, so a per-frame palette would only make
        # the background shimmer between quantisations -- and a stable
        # background is what lets Pillow write later frames as small deltas.
        palette = first.quantize(colors=256)

        def prepare(image: Image.Image) -> Image.Image:
            return image.quantize(palette=palette, dither=Image.Dither.NONE)

        options: dict[str, object] = {"optimize": False, "disposal": 1}
    else:

        def prepare(image: Image.Image) -> Image.Image:
            return image

        # minimize_size makes the encoder reuse what it can between frames
        # rather than sizing each on its own; on a map that is static behind
        # the balls it takes roughly a third off the file.
        options = {"method": 4, "quality": 80, "minimize_size": True}

    rest = (prepare(Image.open(BytesIO(data)).convert("RGB")) for data in frames)
    prepare(first).save(
        output,
        save_all=True,
        append_images=rest,
        duration=delays,
        loop=0,
        **options,
    )


def _resolve_ffmpeg() -> str:
    """Return an ffmpeg executable, preferring the one on ``PATH``."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError:
        raise FfmpegNotFoundError(
            "MP4 and WebM need ffmpeg. Install it on your PATH, or run "
            "`pip install nf-metro[video]` for a bundled build. GIF and "
            "animated WebP need neither."
        ) from None
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def _edge_color(png: bytes) -> str:
    """Return the frame's top-left pixel as an ffmpeg ``0xRRGGBB`` colour.

    A map is drawn on its theme's background with padding around it, so the
    corner pixel is that background -- the one colour a pad column can be
    without showing as a stripe down the side of the video.
    """
    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        r, g, b = image.convert("RGB").getpixel((0, 0))  # type: ignore[misc]
    return f"0x{r:02x}{g:02x}{b:02x}"


def _encoder_args(fmt: str, pad_color: str) -> list[str]:
    """Return the codec arguments for *fmt*.

    Both are 4:2:0 8-bit with even dimensions, the combination every browser
    and every phone will play; an odd pixel dimension is padded in the
    background colour rather than rescaled, so the map keeps the size its
    frames were rasterised at and the seam is invisible.
    """
    common = [
        "-vf",
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:color={pad_color}",
        "-pix_fmt",
        "yuv420p",
        "-an",
    ]
    if fmt == "mp4":
        # faststart moves the index to the front so a <video> starts on the
        # first bytes instead of waiting for the whole file.
        return [
            *common,
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-preset",
            "medium",
            "-movflags",
            "+faststart",
        ]
    return [*common, "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1"]


def _write_with_ffmpeg(
    frames: Iterator[bytes], output: Path, fmt: str, *, fps: float
) -> None:
    """Pipe *frames* through ffmpeg into *output*.

    The PNGs stream in over stdin, so a long loop never has to exist all at
    once -- on disk or in memory.
    """
    ffmpeg = _resolve_ffmpeg()
    first = next(frames)
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "image2pipe",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        "-",
        *_encoder_args(fmt, _edge_color(first)),
        str(output),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    try:
        try:
            for data in chain([first], frames):
                process.stdin.write(data)
        except BrokenPipeError:
            pass  # ffmpeg died; its exit code below carries the real complaint.
        finally:
            # A pipe ffmpeg has already dropped refuses the close too, and
            # that is not the failure worth reporting.
            with suppress(BrokenPipeError):
                process.stdin.close()
    finally:
        # Reached even when a frame failed to rasterise, so a half-written
        # export never leaves an encoder behind waiting on a pipe.
        status = process.wait()
    if status != 0:
        raise RuntimeError(f"ffmpeg failed writing {output} (exit {status})")
