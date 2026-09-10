"""PNG output: format selection, sizing, and the colours a rasteriser sees."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from nf_metro.cli import cli

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
# Names no asset resolved relative to the map file, so it survives a copy into
# a temp directory.
STANDALONE_MMD = EXAMPLES_DIR / "variant_calling.mmd"
FONTS_DIR = Path(__file__).resolve().parents[1] / "src" / "nf_metro" / "fonts"


def _render(tmp_path: Path, *args: str) -> Path:
    out = tmp_path / args[0]
    result = CliRunner().invoke(
        cli, ["render", str(STANDALONE_MMD), "-o", str(out), *args[1:]]
    )
    assert result.exit_code == 0, result.output
    return out


def _image(path: Path) -> Image.Image:
    return Image.open(io.BytesIO(path.read_bytes()))


@pytest.mark.parametrize("weight", ["Regular", "Bold"])
def test_bundled_ttf_matches_its_woff2(weight: str) -> None:
    """The TTF the rasteriser loads carries the same glyphs as the embedded WOFF2.

    ``embed_font`` inlines the WOFF2 and the layout measures against it, while
    ``svg_to_png`` hands resvg the TTF, so the two drifting apart would move
    text without moving the geometry reserved for it.
    """
    ttLib = pytest.importorskip("fontTools.ttLib", reason="needs nf-metro[font]")

    woff2 = ttLib.TTFont(FONTS_DIR / f"Inter-{weight}.woff2")
    ttf = ttLib.TTFont(FONTS_DIR / f"Inter-{weight}.ttf")

    assert ttf.getGlyphOrder() == woff2.getGlyphOrder()
    assert ttf["cmap"].getBestCmap() == woff2["cmap"].getBestCmap()
    assert ttf["hmtx"].metrics == woff2["hmtx"].metrics


def test_png_extension_selects_png_without_a_format_flag(tmp_path: Path) -> None:
    assert _image(_render(tmp_path, "map.png")).format == "PNG"


def test_explicit_format_wins_over_the_extension(tmp_path: Path) -> None:
    out = _render(tmp_path, "map.png", "--format", "svg")
    assert "<svg" in out.read_text()


def test_scale_multiplies_the_pixel_dimensions(tmp_path: Path) -> None:
    single = _image(_render(tmp_path, "one.png", "--scale", "1"))
    double = _image(_render(tmp_path, "two.png", "--scale", "2"))
    assert double.size == (single.width * 2, single.height * 2)


def test_png_width_pins_the_width_and_keeps_the_aspect_ratio(tmp_path: Path) -> None:
    """--png-width resizes the picture, where --width grows the SVG canvas.

    The distinction matters: --width leaves the map at its natural size and
    pads the canvas around it, so it cannot stand in for the raster width the
    old cairosvg recipe used.
    """
    natural = _image(_render(tmp_path, "nat.png", "--scale", "1"))
    pinned = _image(_render(tmp_path, "pinned.png", "--png-width", "800"))

    assert pinned.width == 800
    assert pinned.height == round(natural.height * 800 / natural.width)


def test_png_width_overrides_scale(tmp_path: Path) -> None:
    pinned = _image(_render(tmp_path, "p.png", "--png-width", "600", "--scale", "4"))
    assert pinned.width == 600


@pytest.mark.parametrize(
    ("mode", "expect_light"),
    [("light", True), ("dark", False)],
)
def test_png_bakes_the_requested_mode(
    tmp_path: Path, mode: str, expect_light: bool
) -> None:
    """PNG output resolves the chrome colours itself (#863, #1205).

    A rasteriser has no CSS custom properties and no viewer colour-scheme, so
    the PNG path forces ``--no-chrome-css`` and a concrete mode. Without that
    the canvas either fails to draw or takes the wrong palette. Sampling the
    background is the cheapest end-to-end proof that it took the right one.
    """
    image = _image(_render(tmp_path, f"{mode}.png", "--mode", mode, "--scale", "1"))
    red, green, blue = image.convert("RGB").getpixel((2, 2))
    assert ((red + green + blue) / 3 > 128) is expect_light


def test_repeated_output_writes_every_format_from_one_run(tmp_path: Path) -> None:
    """`-o map.svg -o map.png` is the whole point: one command, both assets."""
    svg, png = tmp_path / "map.svg", tmp_path / "map.png"
    result = CliRunner().invoke(
        cli, ["render", str(STANDALONE_MMD), "-o", str(svg), "-o", str(png)]
    )
    assert result.exit_code == 0, result.output
    assert "<svg" in svg.read_text()
    assert _image(png).format == "PNG"


def test_explicit_format_overrides_every_repeated_output(tmp_path: Path) -> None:
    """--format pins one format for all outputs, extensions notwithstanding."""
    first, second = tmp_path / "a.svg", tmp_path / "b.png"
    result = CliRunner().invoke(
        cli,
        [
            "render",
            str(STANDALONE_MMD),
            "-o",
            str(first),
            "-o",
            str(second),
            "--format",
            "svg",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "<svg" in first.read_text()
    assert "<svg" in second.read_text()


def test_repeated_output_still_refuses_several_inputs(tmp_path: Path) -> None:
    other = EXAMPLES_DIR / "rnaseq_sections.mmd"
    result = CliRunner().invoke(
        cli,
        [
            "render",
            str(STANDALONE_MMD),
            str(other),
            "-o",
            str(tmp_path / "a.svg"),
            "-o",
            str(tmp_path / "b.png"),
        ],
    )
    assert result.exit_code != 0
    assert "single INPUT_FILE" in result.output
