"""Looping video output: frame timing, loop seams, and what each format writes."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from nf_metro.cli import cli
from nf_metro.render import video
from nf_metro.render.animate import FRAME_SLOT, AnimationTimeline, BallTrack
from nf_metro.render.video import _cost_note, _frame_ticks

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
# Names no asset resolved relative to the map file, so it survives a copy into
# a temp directory.
STANDALONE_MMD = EXAMPLES_DIR / "variant_calling.mmd"

# Every frame is a full rasterisation, so the CLI cases stay deliberately tiny;
# what they assert is timing and container shape, not picture quality.
TINY = ("--duration", "1", "--fps", "4")


def _render(tmp_path: Path, name: str, *args: str) -> Path:
    out = tmp_path / name
    result = CliRunner().invoke(
        cli, ["render", str(STANDALONE_MMD), "-o", str(out), *args]
    )
    assert result.exit_code == 0, result.output
    return out


def _frames(path: Path) -> list[Image.Image]:
    with Image.open(path) as animation:
        return [
            animation.seek(index) or animation.convert("RGB")  # type: ignore[func-returns-value]
            for index in range(animation.n_frames)
        ]


def _track(points: tuple[tuple[float, float], ...], move_frac: float) -> BallTrack:
    distances = [0.0]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        distances.append(distances[-1] + ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
    return BallTrack("", move_frac, points, tuple(distances))


# --- the sampled geometry --------------------------------------------------


def test_point_at_walks_the_track_by_arc_length() -> None:
    """A fraction indexes distance along the path, not waypoints.

    The long leg here holds three quarters of the length, so halfway along the
    track is halfway down it -- a waypoint-indexed sampler would answer the
    corner instead.
    """
    track = _track(((0.0, 0.0), (30.0, 0.0), (30.0, 10.0)), 1.0)

    assert track.point_at(0.0) == (0.0, 0.0)
    assert track.point_at(0.5) == pytest.approx((20.0, 0.0))
    assert track.point_at(1.0) == pytest.approx((30.0, 10.0))


def test_a_short_line_holds_at_its_terminus_for_the_rest_of_the_cycle() -> None:
    """move_frac is the share of the cycle spent moving, as in the keyframes."""
    track = _track(((0.0, 0.0), (100.0, 0.0)), 0.5)

    assert track.point_at(track.distance_frac(0.25)) == pytest.approx((50.0, 0.0))
    assert track.point_at(track.distance_frac(0.5)) == pytest.approx((100.0, 0.0))
    assert track.point_at(track.distance_frac(0.9)) == pytest.approx((100.0, 0.0))


def test_balls_on_one_track_are_spread_evenly_across_the_cycle() -> None:
    """Matching the negative animation-delays the CSS gives each ball."""
    timeline = AnimationTimeline(
        cycle=10.0,
        balls_per_line=4,
        tracks=(_track(((0.0, 0.0), (100.0, 0.0)), 1.0),),
    )

    assert timeline.ball_positions(0.0) == pytest.approx(
        [(0.0, 0.0), (25.0, 0.0), (50.0, 0.0), (75.0, 0.0)]
    )


def test_phase_one_is_phase_zero_again() -> None:
    """The seam a frame sequence over [0, 1) wraps across has to be invisible."""
    timeline = AnimationTimeline(
        cycle=10.0,
        balls_per_line=3,
        tracks=(
            _track(((0.0, 0.0), (100.0, 0.0)), 1.0),
            _track(((0.0, 20.0), (40.0, 20.0), (40.0, 60.0)), 0.4),
        ),
    )

    assert timeline.ball_positions(1.0) == pytest.approx(timeline.ball_positions(0.0))


def test_frame_timestamps_span_the_loop_without_drifting() -> None:
    """GIF delays are whole centiseconds; the rounding must not shorten the loop.

    Seven frames over a second divide into 14.28 centiseconds each. Rounding
    every frame down on its own would lose nearly a frame's worth by the wrap.
    """
    ticks = _frame_ticks(count=7, loop=1.0, ticks_per_second=100)

    assert ticks[0] == 0
    assert ticks == sorted(ticks)
    # The last frame is shown for the remainder, so the loop closes on 100.
    assert 100 - ticks[-1] == pytest.approx(100 / 7, abs=1)


# --- the emitted SVG -------------------------------------------------------


def test_the_frame_slot_replaces_the_css_balls_only_when_asked(
    tmp_path: Path,
) -> None:
    """An ordinary animated SVG still animates; only a video render is slotted."""
    animated = _render(tmp_path, "map.svg", "--animate").read_text()

    assert "offset-path" in animated
    assert FRAME_SLOT not in animated


# --- the written files -----------------------------------------------------


def test_gif_extension_writes_a_gif_that_loops_forever(tmp_path: Path) -> None:
    with Image.open(_render(tmp_path, "map.gif", *TINY)) as gif:
        assert gif.format == "GIF"
        assert gif.n_frames == 4
        assert gif.info["loop"] == 0


def test_webp_extension_writes_an_animated_webp(tmp_path: Path) -> None:
    with Image.open(_render(tmp_path, "map.webp", *TINY)) as webp:
        assert webp.format == "WEBP"
        assert webp.n_frames == 4
        assert webp.info["loop"] == 0


def test_frame_count_follows_fps_and_duration(tmp_path: Path) -> None:
    with Image.open(
        _render(tmp_path, "map.gif", "--duration", "2", "--fps", "3")
    ) as gif:
        assert gif.n_frames == 6


def test_a_video_animates_a_map_that_was_not_asked_to(tmp_path: Path) -> None:
    """Asking for a loop asks for the animation, --animate or not.

    A map with no balls would export as one still repeated a few hundred
    times, which is never what the request meant.
    """
    frames = _frames(_render(tmp_path, "map.gif", *TINY))

    assert frames[0].tobytes() != frames[2].tobytes()


def test_the_last_frame_is_not_a_repeat_of_the_first(tmp_path: Path) -> None:
    """The sequence covers one cycle and stops short of restarting it.

    Ending on a copy of frame 0 would hold the picture for two frame times
    every time the loop wraps.
    """
    frames = _frames(_render(tmp_path, "map.gif", *TINY))

    assert frames[-1].tobytes() != frames[0].tobytes()


def test_video_defaults_to_natural_size_where_png_doubles(tmp_path: Path) -> None:
    """A loop is a few hundred stills in one file; doubling them quadruples it."""
    with Image.open(_render(tmp_path, "map.png")) as png:
        doubled = png.size
    with Image.open(_render(tmp_path, "map.gif", *TINY)) as gif:
        natural = gif.size

    assert doubled == (natural[0] * 2, natural[1] * 2)


def test_scale_still_resizes_a_video(tmp_path: Path) -> None:
    with Image.open(_render(tmp_path, "one.gif", *TINY)) as single:
        natural = single.size
    with Image.open(_render(tmp_path, "two.gif", *TINY, "--scale", "2")) as double:
        assert double.size == (natural[0] * 2, natural[1] * 2)


@pytest.mark.parametrize("suffix", ["mp4", "webm"])
def test_video_containers_are_written_in_process(tmp_path: Path, suffix: str) -> None:
    """PyAV muxes these, so they need nothing found on PATH."""
    out = _render(tmp_path, f"map.{suffix}", *TINY)

    assert out.stat().st_size > 0
    # A container's own magic, so a mislabelled encode cannot pass.
    header = out.read_bytes()[:12]
    assert (
        (b"ftyp" in header) if suffix == "mp4" else header.startswith(b"\x1a\x45\xdf")
    )


def test_render_many_exports_a_video_job(tmp_path: Path) -> None:
    """A manifest job gets the same animation forcing a `render` job does.

    The two reach `_render_one` by different routes, and a video job that
    skipped the forcing would fail as a map with nothing to export.
    """
    import json

    out = tmp_path / "map.gif"
    manifest = tmp_path / "jobs.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "input": str(STANDALONE_MMD),
                    "output": str(out),
                    "format": "gif",
                    "duration": 1,
                    "fps": 4,
                }
            ]
        )
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])

    assert result.exit_code == 0, result.output
    with Image.open(out) as gif:
        assert gif.n_frames == 4


def test_a_video_beside_an_svg_leaves_the_svg_unanimated(tmp_path: Path) -> None:
    """Repeated -o shares one prepared graph per backend, but not the animation.

    `-o map.svg -o map.gif` asks for a still SVG and a moving loop. Handing
    both the same graph would put balls on the SVG nobody asked to animate.
    """
    svg, gif = tmp_path / "map.svg", tmp_path / "map.gif"
    result = CliRunner().invoke(
        cli,
        ["render", str(STANDALONE_MMD), "-o", str(svg), "-o", str(gif), *TINY],
    )

    assert result.exit_code == 0, result.output
    assert "offset-path" not in svg.read_text()
    with Image.open(gif) as loop:
        assert loop.n_frames == 4


# --- the refusals ----------------------------------------------------------


def test_a_map_with_the_animation_turned_off_cannot_be_exported(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "render",
            str(STANDALONE_MMD),
            "-o",
            str(tmp_path / "map.gif"),
            "--no-animate",
        ],
    )

    assert result.exit_code != 0
    assert "no animation to export" in result.output


# --- the cost of a big loop -------------------------------------------------


class _Plan:
    """Stands in for the frame dimensions a RenderPlan carries."""

    svg_width = 1000
    svg_height = 400


def test_the_cost_note_quotes_the_frame_count_and_size() -> None:
    """A caller deciding whether to wait needs both numbers, not just a wait."""
    note = _cost_note(
        _Plan(),  # type: ignore[arg-type]
        count=1000,
        scale=2.0,
        width=None,
    )

    assert "1000 frames" in note
    assert "2000x800" in note


def test_a_big_loop_is_quoted_rather_than_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long, smooth, high-resolution loop is a legitimate thing to ask for.

    Only the caller knows what one is worth, so the cost is put in front of
    them while there is still time to stop, and the export runs.
    """
    monkeypatch.setattr(video, "LARGE_LOOP_FRAMES", 1)
    out = tmp_path / "map.gif"
    result = CliRunner().invoke(
        cli, ["render", str(STANDALONE_MMD), "-o", str(out), *TINY]
    )

    assert result.exit_code == 0, result.output
    assert "frames to rasterise at" in result.output
    assert out.exists()
