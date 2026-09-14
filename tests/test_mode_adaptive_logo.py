"""Tests for mode-adaptive logo path selection."""

import base64
import io
import re
from pathlib import Path

import pytest
from PIL import Image as PILImage

from nf_metro.api import render_string
from nf_metro.parser.model import MetroGraph
from nf_metro.render.legend import (
    logo_image_kwargs,
    logo_is_resolvable,
    open_logo_image,
)
from nf_metro.render.ns import adaptive_logo_mask_ids as _adaptive_logo_mask_ids
from nf_metro.render.svg import (
    _effective_logo_path,
    _has_adaptive_logos,
    _is_adaptive_mode,
    _resolve_logo,
)


def _png_data_uri(*, width: int = 100, height: int = 50) -> str:
    img = PILImage.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _graph_with_logo(
    *,
    logo_path: str = "",
    logo_path_light: str = "",
    logo_path_dark: str = "",
) -> MetroGraph:
    g = MetroGraph()
    g.logo_path = logo_path
    g.logo_path_light = logo_path_light
    g.logo_path_dark = logo_path_dark
    return g


def test_single_path_returned_regardless_of_mode():
    g = _graph_with_logo(logo_path="logo.png")
    assert _effective_logo_path(g, "dark") == "logo.png"
    assert _effective_logo_path(g, "light") == "logo.png"


def test_dark_variant_chosen_for_dark_mode():
    g = _graph_with_logo(logo_path_light="light.png", logo_path_dark="dark.png")
    assert _effective_logo_path(g, "dark") == "dark.png"


def test_light_variant_chosen_for_light_mode():
    g = _graph_with_logo(logo_path_light="light.png", logo_path_dark="dark.png")
    assert _effective_logo_path(g, "light") == "light.png"


def test_light_variant_chosen_case_insensitive():
    g = _graph_with_logo(logo_path_light="light.png", logo_path_dark="dark.png")
    assert _effective_logo_path(g, "Light") == "light.png"


def test_fallback_to_logo_path_when_no_variants():
    g = _graph_with_logo(logo_path="fallback.png")
    assert _effective_logo_path(g, "light") == "fallback.png"


def test_fallback_to_logo_path_when_light_variant_missing():
    g = _graph_with_logo(logo_path="fallback.png", logo_path_dark="dark.png")
    assert _effective_logo_path(g, "light") == "fallback.png"


def test_fallback_to_logo_path_when_dark_variant_missing():
    g = _graph_with_logo(logo_path="fallback.png", logo_path_light="light.png")
    assert _effective_logo_path(g, "dark") == "fallback.png"


def test_empty_when_no_logo_set():
    g = _graph_with_logo()
    assert _effective_logo_path(g, "dark") == ""


def test_effective_logo_path_honors_mode_independent_of_brand_style():
    """``%%metro style:`` selects the brand (nfcore/seqera); the light/dark
    display mode is an independent axis. The logo variant returned should
    track the resolved mode passed by the caller, not the brand style."""
    g = _graph_with_logo(logo_path_light="light.png", logo_path_dark="dark.png")
    g.style = "dark"
    assert _effective_logo_path(g, "light") == "light.png"
    assert _effective_logo_path(g, "dark") == "dark.png"


def test_adaptive_logo_mask_ids_dark_light_differ():
    dark_id, light_id = _adaptive_logo_mask_ids()
    assert dark_id != light_id


_LOGO_MAP = (
    "%%metro logo: light.png | dark.png\n"
    "%%metro line: a | A | #f00\n"
    "graph LR\n  n1[N1] -->|a| n2[N2]\n"
)


def _map_dir(root: Path, name: str, *, size: tuple[int, int]) -> Path:
    """Write a logo-bearing map and its asset pair into ``root/name``."""
    d = root / name
    d.mkdir(parents=True)
    (d / "map.mmd").write_text(_LOGO_MAP)
    for variant, colour in (("light", (255, 0, 0)), ("dark", (0, 0, 255))):
        img = PILImage.new("RGB", size, color=colour)
        img.save(d / f"{variant}.png", format="PNG")
    return d


def _mask_ids(svg: str) -> list[str]:
    """Every mask ID the adaptive logo emits, whole rather than by prefix.

    Matching a prefix would pass a keyed ID straight through: the part that
    would carry the key is exactly the part a prefix match drops.
    """
    return sorted(re.findall(r'<mask id="([^"]+)"', _mask_defs(svg)))


def _mask_defs(svg: str) -> str:
    """The one ``<defs>`` block carrying the adaptive-logo masks.

    The renderer emits other defs blocks, so this selects by content rather
    than by position.
    """
    blocks = [
        m.group(0)
        for m in re.finditer(r"<defs>.*?</defs>", svg, re.S)
        if "nfm-logo-mask" in m.group(0)
    ]
    assert len(blocks) == 1, f"expected one logo mask defs block, got {len(blocks)}"
    return blocks[0]


def test_mask_ids_do_not_depend_on_where_the_asset_lives(tmp_path):
    """The same asset reached from two directories yields the same mask IDs.

    A mask ID derived from the resolved path would put the location of the
    checkout into the rendered bytes, so the same map rendered from two
    checkouts would not agree.
    """
    here = _map_dir(tmp_path, "here", size=(100, 50))
    there = _map_dir(tmp_path / "nested" / "deeper", "there", size=(100, 50))

    ids_here = _mask_ids(
        render_string((here / "map.mmd").read_text(), source_dir=str(here))
    )
    ids_there = _mask_ids(
        render_string((there / "map.mmd").read_text(), source_dir=str(there))
    )
    assert ids_here == ids_there
    assert ids_here == ["nfm-logo-mask-dark", "nfm-logo-mask-light"]


def test_mask_defs_are_identical_for_two_different_assets(tmp_path):
    """Two unrelated assets share one mask block, and that is correct.

    Each mask is a ``light-dark()`` rect in ``objectBoundingBox`` units, so it
    clips whichever element references it to that element's own box; nothing in
    it depends on the asset. Keying an ID on the asset would therefore claim a
    uniqueness the mask does not have.
    """
    wide = _map_dir(tmp_path, "wide", size=(200, 40))
    tall = _map_dir(tmp_path, "tall", size=(40, 200))

    wide_svg = render_string((wide / "map.mmd").read_text(), source_dir=str(wide))
    tall_svg = render_string((tall / "map.mmd").read_text(), source_dir=str(tall))

    assert _mask_defs(wide_svg) == _mask_defs(tall_svg)
    # The assets themselves differ, so the shared defs are not a rendering fluke.
    assert wide_svg != tall_svg


def test_has_adaptive_logos_false_when_files_missing():
    g = _graph_with_logo(
        logo_path_light="nonexistent_light.png", logo_path_dark="nonexistent_dark.png"
    )
    assert not _has_adaptive_logos(g)


def test_has_adaptive_logos_false_when_variants_empty():
    g = _graph_with_logo(logo_path="single.png")
    assert not _has_adaptive_logos(g)


def test_is_adaptive_mode_true_when_both_set():
    g = _graph_with_logo(logo_path_light="light.png", logo_path_dark="dark.png")
    assert _is_adaptive_mode(g)


def test_is_adaptive_mode_true_when_light_only():
    g = _graph_with_logo(logo_path_light="light.png")
    assert _is_adaptive_mode(g)


def test_is_adaptive_mode_true_when_dark_only():
    g = _graph_with_logo(logo_path_dark="dark.png")
    assert _is_adaptive_mode(g)


def test_is_adaptive_mode_false_for_single_path():
    g = _graph_with_logo(logo_path="logo.png")
    assert not _is_adaptive_mode(g)


def test_is_adaptive_mode_false_when_no_logo():
    g = _graph_with_logo()
    assert not _is_adaptive_mode(g)


def _write_png(path: Path, color: tuple[int, int, int] = (255, 0, 0)) -> None:
    img = PILImage.new("RGB", (100, 50), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    path.write_bytes(buf.getvalue())


def test_resolve_logo_errors_on_missing_single_path():
    """A set logo_path that doesn't exist on disk should raise ValueError."""
    g = MetroGraph()
    g.logo_path = "does/not/exist.png"
    with pytest.raises(ValueError, match="does/not/exist.png"):
        _resolve_logo(g, adaptive=False, mode="dark")


def test_resolve_logo_no_error_when_no_path_set():
    """Empty logo_path raises no error."""
    g = MetroGraph()
    show, _, _, _ = _resolve_logo(g, adaptive=False, mode="dark")
    assert not show


def test_resolve_logo_resolves_relative_to_source_dir(tmp_path):
    """A logo_path relative to source_dir resolves correctly."""
    logo_file = tmp_path / "mylogo.png"
    _write_png(logo_file)

    g = MetroGraph()
    g.logo_path = "mylogo.png"
    g.source_dir = str(tmp_path)

    show, w, h, effective = _resolve_logo(g, adaptive=False, mode="dark")
    assert show
    assert effective == str(logo_file)
    assert w > 0


def test_resolve_logo_errors_when_source_dir_set_but_still_missing(tmp_path):
    """A logo_path that doesn't exist relative to source_dir raises ValueError."""
    g = MetroGraph()
    g.logo_path = "nonexistent.png"
    g.source_dir = str(tmp_path)
    with pytest.raises(ValueError, match="nonexistent.png"):
        _resolve_logo(g, adaptive=False, mode="dark")


def test_resolve_logo_picks_variant_matching_mode(tmp_path):
    """The single-image (non-adaptive) resolution path honours *mode*."""
    light_file = tmp_path / "light.png"
    dark_file = tmp_path / "dark.png"
    _write_png(light_file)
    _write_png(dark_file)

    g = MetroGraph()
    g.logo_path_light = "light.png"
    g.logo_path_dark = "dark.png"
    g.source_dir = str(tmp_path)

    _, _, _, effective_light = _resolve_logo(g, adaptive=False, mode="light")
    assert effective_light == str(light_file)

    _, _, _, effective_dark = _resolve_logo(g, adaptive=False, mode="dark")
    assert effective_dark == str(dark_file)


def test_resolve_adaptive_logo_errors_on_missing_paths():
    """In adaptive mode, missing both variants should raise ValueError."""
    g = MetroGraph()
    g.logo_path_light = "missing_light.png"
    g.logo_path_dark = "missing_dark.png"
    with pytest.raises(ValueError, match="missing_light.png|missing_dark.png"):
        _resolve_logo(g, adaptive=True, mode="dark")


def test_resolve_adaptive_logo_resolves_relative_to_source_dir(tmp_path):
    """Adaptive logo paths resolve relative to source_dir."""
    light_file = tmp_path / "light.png"
    dark_file = tmp_path / "dark.png"
    _write_png(light_file)
    _write_png(dark_file)

    g = MetroGraph()
    g.logo_path_light = "light.png"
    g.logo_path_dark = "dark.png"
    g.source_dir = str(tmp_path)

    show, w, h, _ = _resolve_logo(g, adaptive=True, mode="dark")
    assert show
    assert w > 0


def test_logo_is_resolvable_for_data_uri():
    assert logo_is_resolvable(_png_data_uri())


def test_logo_is_resolvable_false_for_missing_file():
    assert not logo_is_resolvable("does/not/exist.png")


def test_open_logo_image_decodes_data_uri():
    img = open_logo_image(_png_data_uri(width=40, height=20))
    assert (img.width, img.height) == (40, 20)


def test_open_logo_image_rejects_non_base64_data_uri():
    with pytest.raises(ValueError, match="base64"):
        open_logo_image("data:image/png,not-base64")


def test_open_logo_image_rejects_corrupt_base64_payload():
    """A well-formed ``;base64,`` header with a payload that fails to decode
    (e.g. bytes appended after the legitimate base64 alphabet) raises a
    ``ValueError`` rather than an unhandled ``binascii.Error``."""
    corrupt = _png_data_uri(width=40, height=20) + '" onerror="alert(1)'
    with pytest.raises(ValueError, match="base64"):
        open_logo_image(corrupt)


def test_resolve_logo_accepts_data_uri_single_path():
    """A data URI needs no filesystem access, so it resolves with no source_dir."""
    g = MetroGraph()
    g.logo_path = _png_data_uri(width=40, height=20)

    show, w, h, effective = _resolve_logo(g, adaptive=False, mode="dark")
    assert show
    assert effective == g.logo_path
    assert w / h == 2.0


def test_resolve_adaptive_logo_accepts_data_uris():
    g = MetroGraph()
    g.logo_path_light = _png_data_uri()
    g.logo_path_dark = _png_data_uri()

    show, w, h, _ = _resolve_logo(g, adaptive=True, mode="dark")
    assert show
    assert w > 0


def _embedded_image_bytes(svg: str) -> bytes:
    match = re.search(r'href="data:image/[^;]+;base64,([^"]+)"', svg)
    assert match, "expected an embedded <image> data URI in the SVG"
    return base64.b64decode(match.group(1))


def test_baked_mode_embeds_matching_logo_variant(tmp_path):
    """A concrete ``--mode`` bake (``chrome_css=False``) must embed that
    mode's logo variant. The pipeline's brand style (nfcore/seqera, set here
    via ``dark`` which aliases nfcore) must not influence the choice."""
    light_file = tmp_path / "light.png"
    dark_file = tmp_path / "dark.png"
    _write_png(light_file, color=(255, 0, 0))
    _write_png(dark_file, color=(0, 0, 255))

    text = (
        f"%%metro logo: {light_file} | {dark_file}\n"
        "%%metro style: dark\n"
        "%%metro line: main | Main | #0570b0\n"
        "graph LR\n"
        "    a[A] -->|main| b[B]\n"
    )

    light_svg = render_string(text, mode="light", chrome_css=False)
    dark_svg = render_string(text, mode="dark", chrome_css=False)

    assert _embedded_image_bytes(light_svg) == light_file.read_bytes()
    assert _embedded_image_bytes(dark_svg) == dark_file.read_bytes()


def test_logo_image_kwargs_escapes_quote_in_data_uri():
    malicious = _png_data_uri() + '" onerror="alert(1)'
    kwargs = logo_image_kwargs(malicious)

    assert kwargs["embed"] is False
    assert '"' not in kwargs["path"]
    assert "&quot;" in kwargs["path"]


def test_data_uri_logo_with_quote_is_rejected_before_rendering():
    """Appending an attack suffix to a data-URI logo's base64 payload
    corrupts it; ``open_logo_image`` (via ``compute_logo_dimensions``,
    called while resolving the logo) rejects that corrupt payload with a
    ``ValueError`` before any rendering happens, rather than letting the
    corrupt bytes reach ``PIL.Image.open`` unguarded, or letting a
    malformed-but-decodable payload's escaped text reach SVG attribute
    emission with no dimensions computed for it."""
    malicious = _png_data_uri(width=40, height=20) + '" onerror="alert(1)'
    text = (
        f"%%metro logo: {malicious}\n"
        "%%metro line: main | Main | #0570b0\n"
        "graph LR\n"
        "    a[A] -->|main| b[B]\n"
    )

    with pytest.raises(ValueError, match="base64"):
        render_string(text, chrome_css=False)
