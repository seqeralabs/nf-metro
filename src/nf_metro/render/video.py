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

All four containers are muxed in-process through PyAV, which ships FFmpeg in
its own wheels: nothing has to be found on ``PATH`` and no subprocess is
spawned, the same way ``resvg-py`` gives the PNG path a rasteriser without a
system library.  Frames reach the encoder one at a time, so a long loop never
exists all at once.
"""

from __future__ import annotations

__all__ = [
    "LARGE_LOOP_FRAMES",
    "VIDEO_FORMATS",
    "AnimationExport",
    "NotAnimatedError",
    "VideoFormat",
    "write_animation",
]

from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from nf_metro.render.animate import (
    FRAME_SLOT,
    animation_frame_markup,
    build_animation_timeline,
)
from nf_metro.render.plan import RenderPlan
from nf_metro.render.raster import svg_to_png

if TYPE_CHECKING:
    import av
    from PIL import Image

VideoFormat = Literal["gif", "webp", "mp4", "webm"]
VIDEO_FORMATS: tuple[VideoFormat, ...] = get_args(VideoFormat)

#: Past this many frames an export is minutes of work.  Nothing is refused --
#: a long, smooth, high-scale loop is a legitimate thing to want, and only the
#: caller knows what it is worth -- but the cost is quoted up front, while
#: there is still time to stop.
LARGE_LOOP_FRAMES = 400

#: Ticks a frame delay is quantised to, by container.  GIF stores a delay in
#: hundredths of a second and WebP in thousandths, and a requested fps rarely
#: divides evenly into either; :func:`_frame_ticks` carries the rounding error
#: forward so the loop still comes out the length that was asked for.  MP4 and
#: WebM are constant-rate and need no such thing.
_TICKS_PER_SECOND = {"gif": 100, "webp": 1000}


@dataclass(frozen=True)
class _Encoder:
    """How one container is muxed: codec, pixel format, and their options."""

    codec: str
    pix_fmt: str
    container_options: dict[str, str]
    codec_options: dict[str, str]

    @property
    def needs_even_size(self) -> bool:
        """Whether the pixel format's chroma subsampling requires even sides."""
        return self.pix_fmt == "yuv420p"


_ENCODERS: dict[str, _Encoder] = {
    # pal8 takes the palette-indexed frames Pillow quantises, so the GIF keeps
    # an adaptive palette rather than the fixed one the encoder would pick,
    # and transdiff writes each later frame as its difference from the last.
    "gif": _Encoder("gif", "pal8", {"loop": "0"}, {"gifflags": "+transdiff"}),
    # Lossless is both the smaller and the better encode here: a map is flat
    # colour and hard edges, which is what WebP's lossless mode is good at and
    # what lossy DCT is worst at. On the rnaseq example a lossless loop is
    # 190KB and pixel-exact, where quality=90 is 334KB and visibly rings
    # around the labels.
    "webp": _Encoder("libwebp_anim", "bgra", {"loop": "0"}, {"lossless": "1"}),
    # faststart moves the index to the front so a <video> starts on the first
    # bytes instead of waiting for the whole file.
    "mp4": _Encoder(
        "libx264",
        "yuv420p",
        {"movflags": "+faststart"},
        {"crf": "23", "preset": "medium"},
    ),
    "webm": _Encoder(
        "libvpx-vp9", "yuv420p", {}, {"crf": "32", "b": "0", "row-mt": "1"}
    ),
}


class NotAnimatedError(ValueError):
    """Raised when the SVG to export carries no animation to sample."""


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
        notify(_cost_note(plan, count=count, scale=scale, width=width))

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
    _encode(frames, output, fmt, count=count, loop=loop)
    return AnimationExport(output, frames=count, duration=loop, fps=count / loop)


def _frame_pixels(
    plan: RenderPlan, *, scale: float, width: int | None
) -> tuple[int, int]:
    """Return the pixel size each frame rasterises to, as the PNG path sizes it."""
    if width is not None:
        return width, max(1, round(plan.svg_height * width / plan.svg_width))
    return max(1, round(plan.svg_width * scale)), max(1, round(plan.svg_height * scale))


def _cost_note(plan: RenderPlan, *, count: int, scale: float, width: int | None) -> str:
    """Describe what a large export is about to spend, before it spends it."""
    pixel_width, pixel_height = _frame_pixels(plan, scale=scale, width=width)
    return (
        f"{count} frames to rasterise at {pixel_width}x{pixel_height}; "
        "this will take a while. --fps and --duration set the frame count, "
        "--scale and --raster-width the size."
    )


def _frame_ticks(count: int, loop: float, ticks_per_second: int) -> list[int]:
    """Split *loop* seconds into *count* cumulative presentation timestamps.

    A container that stores a whole-tick delay per frame would drift by up to
    half a tick each time if every frame rounded on its own; rounding the
    running total instead keeps the loop within one tick of *loop* however
    badly the fps divides.
    """
    return [round(loop * ticks_per_second * i / count) for i in range(count)]


def _even(size: tuple[int, int]) -> tuple[int, int]:
    """Round a frame size up to the even sides 4:2:0 chroma needs."""
    width, height = size
    return width + width % 2, height + height % 2


def _prepare(
    image: Image.Image,
    spec: _Encoder,
    canvas: tuple[int, int],
    background: tuple[int, ...],
    palette: Image.Image | None,
) -> av.VideoFrame:
    """Turn one decoded frame into the ``av.VideoFrame`` the encoder wants."""
    import av
    from PIL import Image as PILImage

    if image.size != canvas:
        # Pad rather than rescale, so the map keeps the size its frames were
        # rasterised at; the map's own background colour makes the seam
        # invisible where an odd dimension had to grow by a pixel.
        padded = PILImage.new("RGB", canvas, background)
        padded.paste(image, (0, 0))
        image = padded
    if palette is None:
        return av.VideoFrame.from_image(image).reformat(format=spec.pix_fmt)
    return _pal8_frame(image.quantize(palette=palette, dither=PILImage.Dither.NONE))


def _pal8_frame(image: Image.Image) -> av.VideoFrame:
    """Build a ``pal8`` frame from a palette-indexed image.

    FFmpeg keeps the indices in plane 0 and the palette in plane 1, as 256
    native-endian ``0xAARRGGBB`` words.  Plane 0 is stride-padded, so the rows
    are copied into a buffer of the plane's own line size rather than handed
    over as the image's tightly packed bytes.
    """
    import av

    frame = av.VideoFrame(image.width, image.height, "pal8")
    width, height = image.width, image.height
    indices = image.tobytes()
    stride = frame.planes[0].line_size
    if stride != width:
        pad = bytes(stride - width)
        indices = b"".join(
            indices[row * width : (row + 1) * width] + pad for row in range(height)
        )
    frame.planes[0].update(indices)

    table = list(image.getpalette() or [])
    table += [0] * (768 - len(table))
    words = array(
        "I",
        (
            0xFF000000 | (table[i] << 16) | (table[i + 1] << 8) | table[i + 2]
            for i in range(0, 768, 3)
        ),
    )
    # array("I") is native-endian, which is the order FFmpeg reads the palette
    # in; it is also 4 bytes wide everywhere CPython builds.
    frame.planes[1].update(words.tobytes())
    return frame


def _encode(
    frames: Iterator[bytes], output: Path, fmt: str, *, count: int, loop: float
) -> None:
    """Mux *frames* into *output* as one loop of *fmt*.

    The PNGs are decoded and encoded one at a time, so neither the raw frames
    nor the encoded ones are ever all in memory at once.
    """
    import av
    from PIL import Image as PILImage

    spec = _ENCODERS[fmt]
    first = PILImage.open(BytesIO(next(frames))).convert("RGB")
    canvas = _even(first.size) if spec.needs_even_size else first.size
    # A map is drawn on its theme's background with padding around it, so the
    # corner pixel is that background.
    background = cast("tuple[int, ...]", first.getpixel((0, 0)))
    # One palette for the whole loop, taken from the first frame: the map is
    # static behind the balls, so a per-frame palette would only make the
    # background shimmer between quantisations -- and a stable background is
    # what lets the encoder write later frames as small differences.
    palette = first.quantize(colors=256) if spec.pix_fmt == "pal8" else None

    rate = Fraction(count / loop).limit_denominator(65535)
    ticks = _TICKS_PER_SECOND.get(fmt)
    timestamps: list[int] | range = (
        _frame_ticks(count, loop, ticks) if ticks is not None else range(count)
    )
    with av.open(str(output), "w", options=spec.container_options) as container:
        stream = container.add_stream(spec.codec, rate=rate)
        stream.width, stream.height = canvas
        stream.pix_fmt = spec.pix_fmt
        stream.codec_context.options = spec.codec_options
        if ticks is not None:
            # The codec context is what the encoder reads timestamps against;
            # setting it on the stream instead leaves the muxer to rescale
            # from the nominal rate and the delays come out several times too
            # long.
            stream.codec_context.time_base = Fraction(1, ticks)

        images = (PILImage.open(BytesIO(data)).convert("RGB") for data in frames)
        for pts, image in zip(timestamps, chain([first], images)):
            frame = _prepare(image, spec, canvas, background, palette)
            frame.pts = pts
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
