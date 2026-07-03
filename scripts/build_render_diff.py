#!/usr/bin/env python3
"""Compare two directories of SVG renders and generate an HTML diff page.

Usage:
    python scripts/build_render_diff.py BASE_DIR PR_DIR OUTPUT_DIR [--pr NUMBER]

Compares SVG files in BASE_DIR (main branch renders) against PR_DIR (PR branch
renders). Generates a self-contained HTML page showing side-by-side before/after
for changed outputs only, grouped by section (read from manifest.json).

Exit codes:
    0 - changes detected, diff page written
    1 - error
    2 - no changes detected
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from layout_metrics import (  # noqa: E402
    METRICS,
    delta_direction,
    format_delta,
    format_value,
)

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="nf-metro-render-run" content="{marker}">
<title>Render diff{title_suffix}</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: light-dark(#f5f5f7, #1e1e2e);
    --surface: light-dark(#eaeaef, #2a2a3c);
    --border: light-dark(#c9c9d4, #3a3a4c);
    --text: light-dark(#1e1e2e, #e0e0e0);
    --muted: light-dark(#666, #888);
    --accent: light-dark(#1a8575, #4ec9b0);
    --added: light-dark(rgba(46,160,67,.25), rgba(46,160,67,.44));
    --removed: light-dark(rgba(218,54,52,.25), rgba(218,54,52,.44));
  }}
  [data-scheme="dark"] {{ color-scheme: dark; }}
  [data-scheme="light"] {{ color-scheme: light; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 2rem;
    line-height: 1.5;
  }}
  .page-header {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }}
  h1 {{
    font-size: 1.5rem;
    color: var(--accent);
  }}
  .scheme-btn {{
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 0.35rem 0.75rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
    white-space: nowrap;
  }}
  .scheme-btn:hover {{ background: var(--border); }}
  .summary {{
    color: var(--muted);
    margin-bottom: 2rem;
    font-size: 0.95rem;
  }}
  .toc {{
    margin-bottom: 2rem;
    padding: 1rem 1.5rem;
    background: var(--surface);
    border-radius: 8px;
    border: 1px solid var(--border);
  }}
  .toc h2 {{
    font-size: 1rem;
    margin-bottom: 0.5rem;
    color: var(--accent);
  }}
  .toc h3 {{
    font-size: 0.85rem;
    color: var(--muted);
    margin-top: 0.5rem;
    margin-bottom: 0.25rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .toc ul {{
    list-style: none;
    columns: 2;
    column-gap: 2rem;
  }}
  .toc li {{
    margin-bottom: 0.25rem;
  }}
  .toc a {{
    color: var(--text);
    text-decoration: none;
  }}
  .toc a:hover {{
    color: var(--accent);
    text-decoration: underline;
  }}
  .badge {{
    display: inline-block;
    font-size: 0.7rem;
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
    margin-left: 0.4rem;
    vertical-align: middle;
  }}
  .badge-changed {{ background: var(--border); }}
  .badge-added {{ background: var(--added); }}
  .badge-removed {{ background: var(--removed); }}
  .section-header {{
    font-size: 1.3rem;
    color: var(--accent);
    margin-top: 2.5rem;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid var(--border);
  }}
  .diff-entry {{
    margin-bottom: 3rem;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }}
  .diff-entry h3 {{
    font-size: 1.1rem;
    padding: 0.75rem 1rem;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }}
  .comparison {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0;
  }}
  .side {{
    padding: 1rem;
    overflow-x: auto;
  }}
  .side:first-child {{
    border-right: 1px solid var(--border);
  }}
  .side h4 {{
    font-size: 0.85rem;
    color: var(--muted);
    margin-bottom: 0.5rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .svg-wrapper {{ overflow-x: auto; }}
  .svg-wrapper svg, .side img, .side-only img {{
    max-width: 100%;
    height: auto;
    display: block;
    border-radius: 4px;
  }}
  .side-only {{
    padding: 1rem;
  }}
  .empty {{
    color: var(--muted);
    font-style: italic;
    padding: 2rem;
    text-align: center;
  }}
  .toggle-bar {{
    display: flex;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }}
  .toggle-bar button {{
    background: var(--border);
    color: var(--text);
    border: none;
    padding: 0.3rem 0.8rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
  }}
  .toggle-bar button.active {{
    background: var(--accent);
    color: var(--bg);
  }}
  .intro {{
    margin-bottom: 2rem;
    padding: 1rem 1.5rem;
    background: var(--surface);
    border-radius: 8px;
    border: 1px solid var(--border);
    font-size: 0.9rem;
    line-height: 1.6;
  }}
  .intro summary {{
    cursor: pointer;
    color: var(--accent);
    font-weight: 600;
  }}
  .intro p {{
    margin-top: 0.75rem;
  }}
  .intro ul {{
    margin-top: 0.5rem;
    padding-left: 1.5rem;
  }}
  .intro li {{
    margin-bottom: 0.25rem;
  }}
  .intro code {{
    background: var(--border);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    font-size: 0.85em;
  }}
  .intro a {{
    color: var(--accent);
  }}
  .metrics {{
    margin-bottom: 2rem;
    padding: 1rem 1.5rem;
    background: var(--surface);
    border-radius: 8px;
    border: 1px solid var(--border);
  }}
  .metrics h2 {{
    font-size: 1rem;
    margin-bottom: 0.25rem;
    color: var(--accent);
  }}
  .metrics .caption {{
    color: var(--muted);
    font-size: 0.8rem;
    margin-bottom: 0.75rem;
  }}
  .metrics table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 0.82rem;
  }}
  .metrics th, .metrics td {{
    padding: 0.3rem 0.6rem;
    text-align: right;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }}
  .metrics th:first-child, .metrics td:first-child {{
    text-align: left;
  }}
  .metrics th {{
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.7rem;
  }}
  .metrics td a {{ color: var(--text); text-decoration: none; }}
  .metrics td a:hover {{ color: var(--accent); }}
  .m-flat {{ color: var(--muted); }}
  .m-better {{ color: light-dark(#1a8575, #4ec9b0); font-weight: 600; }}
  .m-worse {{ color: light-dark(#c0392b, #f38ba8); font-weight: 600; }}
</style>
</head>
<body>
<div class="page-header">
  <h1>Render diff{title_suffix}</h1>
  <button id="scheme-toggle" class="scheme-btn">System</button>
</div>
<p class="summary">{summary}</p>
<details class="intro">
<summary>What is this page?</summary>
<p>
<a href="https://github.com/pinin4fjords/nf-metro">nf-metro</a>
generates metro-map-style SVG diagrams from Mermaid graph definitions.
This page is automatically generated for every pull request and shows
<strong>only the renders that changed</strong> compared to the
<code>main</code> branch.
</p>
<p>
Use it to check that code changes produce the intended visual result
without unexpected side-effects on other diagrams. Each entry shows
the <strong>base</strong> (main) render on the left and the
<strong>PR</strong> render on the right. Use the toggle buttons to
switch between side-by-side, base-only, and PR-only views.
</p>
<p><strong>What to look for:</strong></p>
<ul>
<li>Intended improvements in the PR column</li>
<li>Unintended regressions (overlapping lines, shifted labels,
    broken routing) in diagrams you did not mean to change</li>
<li>New renders (green <em>added</em> badge) or removed renders
    (red <em>removed</em> badge)</li>
</ul>
</details>
{metrics}
{toc}
{entries}
<script>
(function() {{
  const root = document.documentElement;
  const btn = document.getElementById('scheme-toggle');
  const schemes = ['auto', 'dark', 'light'];
  const labels = {{'auto': 'System', 'dark': 'Dark', 'light': 'Light'}};
  let idx = 0;
  function apply() {{
    const s = schemes[idx];
    if (s === 'auto') {{
      root.removeAttribute('data-scheme');
    }} else {{
      root.setAttribute('data-scheme', s);
    }}
    btn.textContent = labels[s];
  }}
  apply();
  btn.addEventListener('click', function() {{
    idx = (idx + 1) % schemes.length;
    apply();
  }});
}})();
document.querySelectorAll('.toggle-bar button').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const entry = btn.closest('.diff-entry');
    const mode = btn.dataset.mode;
    const buttons = entry.querySelectorAll('.toggle-bar button');
    buttons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const comp = entry.querySelector('.comparison');
    const sideBase = comp.querySelector('.side-base');
    const sidePr = comp.querySelector('.side-pr');
    if (mode === 'side-by-side') {{
      comp.style.gridTemplateColumns = '1fr 1fr';
      sideBase.style.display = '';
      sidePr.style.display = '';
    }} else if (mode === 'base') {{
      comp.style.gridTemplateColumns = '1fr';
      sideBase.style.display = '';
      sidePr.style.display = 'none';
    }} else if (mode === 'pr') {{
      comp.style.gridTemplateColumns = '1fr';
      sideBase.style.display = 'none';
      sidePr.style.display = '';
    }}
  }});
}});
</script>
</body>
</html>
"""

_SVG_ROOT_RE = re.compile(r"(<svg)(\s[^>]*)(>)", re.DOTALL)
_SELF_SCHEME_RE = re.compile(r'\s+style="color-scheme:\s*light\s+dark"')
_WIDTH_RE = re.compile(r'\bwidth="(\d+)"')
_HEIGHT_RE = re.compile(r'\bheight="(\d+)"')


def _inline_svg(path: Path) -> str:
    """Read SVG; strip self-declared color-scheme and add viewBox for CSS scaling.

    SVGs rendered with self_color_scheme=True carry style="color-scheme: light dark"
    on their root element. When inlined into a host page, that self-declaration
    overrides the page's color-scheme. Stripping it lets light-dark() values inside
    the SVG resolve against the host page's color-scheme instead, so the page-level
    toggle controls the renders.

    Adding viewBox (when absent) enables proportional CSS scaling via max-width/height.
    """
    content = path.read_text()
    m = _SVG_ROOT_RE.search(content)
    if not m:
        return content
    tag_open, attrs, tag_close = m.group(1), m.group(2), m.group(3)

    attrs = _SELF_SCHEME_RE.sub("", attrs)

    if "viewBox" not in attrs:
        w = _WIDTH_RE.search(attrs)
        h = _HEIGHT_RE.search(attrs)
        if w and h:
            attrs += f' viewBox="0 0 {w.group(1)} {h.group(1)}"'

    return content[: m.start()] + tag_open + attrs + tag_close + content[m.end() :]


def _load_json(render_dir: Path, filename: str) -> dict:
    """Load a JSON sidecar from a render directory, or return an empty dict."""
    path = render_dir / filename
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _build_metrics_html(
    changed: list[tuple[str, str]],
    base_metrics: dict[str, dict[str, float]],
    pr_metrics: dict[str, dict[str, float]],
) -> str:
    """Build the advisory layout-quality delta table for the changed renders.

    Returns an empty string when no scorecard is available on either side (the
    base branch predates the metric, or computation failed everywhere).
    """
    if not base_metrics and not pr_metrics:
        return ""

    header_cells = "".join(f"<th>{spec.label}</th>" for spec in METRICS)
    rows: list[str] = []
    for name, _kind in changed:
        stem = name.removesuffix(".svg")
        base = base_metrics.get(name)
        pr = pr_metrics.get(name)
        if base is None and pr is None:
            continue
        cells = [f'<td><a href="#{stem}">{stem}</a></td>']
        for spec in METRICS:
            bv = base.get(spec.key) if base else None
            pv = pr.get(spec.key) if pr else None
            direction = delta_direction(bv, pv)
            cls = {-1: "m-better", 1: "m-worse", 0: "m-flat"}[direction]
            both_present = bv is not None and pv is not None
            if both_present and direction != 0:
                text = (
                    f"{format_value(spec, bv)}&rarr;{format_value(spec, pv)} "
                    f"({format_delta(spec, bv, pv)})"
                )
            elif both_present:
                text = format_value(spec, pv)
            elif bv is None:
                text = f"&ndash;&rarr;{format_value(spec, pv)}"
            else:
                text = f"{format_value(spec, bv)}&rarr;&ndash;"
            cells.append(f'<td class="{cls}">{text}</td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")

    if not rows:
        return ""

    return (
        '<div class="metrics">\n<h2>Layout-quality metrics</h2>\n'
        '<p class="caption">Advisory only &mdash; nothing gates on these. '
        "Lower is better; "
        '<span class="m-better">green</span> improved, '
        '<span class="m-worse">red</span> regressed.</p>\n'
        f"<table>\n<tr><th>Render</th>{header_cells}</tr>\n"
        + "\n".join(rows)
        + "\n</table>\n</div>"
    )


def build_diff(
    base_dir: Path,
    pr_dir: Path,
    output_dir: Path,
    pr_number: str | None = None,
    marker: str = "",
) -> bool:
    """Compare renders and generate diff page. Returns True if changes found."""
    base_svgs = {p.name for p in base_dir.glob("*.svg")} if base_dir.exists() else set()
    pr_svgs = {p.name for p in pr_dir.glob("*.svg")} if pr_dir.exists() else set()
    all_names = sorted(base_svgs | pr_svgs)

    changed: list[tuple[str, str]] = []  # (name, kind)
    for name in all_names:
        base_path = base_dir / name
        pr_path = pr_dir / name
        if name in base_svgs and name in pr_svgs:
            if base_path.read_bytes() != pr_path.read_bytes():
                changed.append((name, "changed"))
        elif name in pr_svgs:
            changed.append((name, "added"))
        else:
            changed.append((name, "removed"))

    if not changed:
        return False

    # Load manifest from PR renders (preferred) with base as fallback
    manifest = _load_json(pr_dir, "manifest.json")
    base_manifest = _load_json(base_dir, "manifest.json")
    for name, _ in changed:
        if name not in manifest and name in base_manifest:
            manifest[name] = base_manifest[name]

    base_metrics = _load_json(base_dir, "metrics.json")
    pr_metrics = _load_json(pr_dir, "metrics.json")

    # Group changed files by section
    section_order: list[str] = []
    by_section: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, kind in changed:
        section = manifest.get(name, "Other")
        if section not in by_section:
            section_order.append(section)
        by_section[section].append((name, kind))

    output_dir.mkdir(parents=True, exist_ok=True)

    # Build HTML
    title_suffix = f" - PR #{pr_number}" if pr_number else ""
    n_changed = sum(1 for _, k in changed if k == "changed")
    n_added = sum(1 for _, k in changed if k == "added")
    n_removed = sum(1 for _, k in changed if k == "removed")
    parts = []
    if n_changed:
        parts.append(f"{n_changed} changed")
    if n_added:
        parts.append(f"{n_added} added")
    if n_removed:
        parts.append(f"{n_removed} removed")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary = (
        f"{', '.join(parts)} out of {len(all_names)} total renders."
        f" Generated {timestamp}."
    )

    # Table of contents (grouped by section)
    toc_parts = ['<div class="toc">\n<h2>Changed renders</h2>']
    for section in section_order:
        items = by_section[section]
        sec_id = section.lower().replace(" ", "-")
        toc_parts.append(f'<h3><a href="#section-{sec_id}">{section}</a></h3>\n<ul>')
        for name, kind in items:
            stem = name.removesuffix(".svg")
            badge_class = f"badge-{kind}"
            toc_parts.append(
                f'<li><a href="#{stem}">{stem}</a>'
                f'<span class="badge {badge_class}">{kind}</span></li>'
            )
        toc_parts.append("</ul>")
    toc_parts.append("</div>")
    toc = "\n".join(toc_parts)

    # Diff entries (grouped by section)
    entries_html = []
    for section in section_order:
        sec_id = section.lower().replace(" ", "-")
        entries_html.append(
            f'<h2 class="section-header" id="section-{sec_id}">{section}</h2>'
        )
        for name, kind in by_section[section]:
            stem = name.removesuffix(".svg")
            heading = stem.replace("_", " ").title()
            badge = f"badge-{kind}"
            h3 = f'<h3>{heading} <span class="badge {badge}">{kind}</span></h3>'
            div_open = f'<div class="diff-entry" id="{stem}">'

            if kind == "changed":
                base_svg = _inline_svg(base_dir / name)
                pr_svg = _inline_svg(pr_dir / name)
                toggle = (
                    '<div class="toggle-bar">'
                    '<button class="active" data-mode="side-by-side">'
                    "Side by side</button>"
                    '<button data-mode="base">Base only</button>'
                    '<button data-mode="pr">PR only</button>'
                    "</div>"
                )
                entry = (
                    f"{div_open}\n{h3}\n{toggle}\n"
                    f'<div class="comparison">\n'
                    f'<div class="side side-base"><h4>Base (main)</h4>'
                    f'<div class="svg-wrapper">{base_svg}</div></div>\n'
                    f'<div class="side side-pr"><h4>PR</h4>'
                    f'<div class="svg-wrapper">{pr_svg}</div></div>\n'
                    f"</div>\n</div>"
                )
            elif kind == "added":
                pr_svg = _inline_svg(pr_dir / name)
                entry = (
                    f"{div_open}\n{h3}\n"
                    f'<div class="side-only"><h4>New in PR</h4>'
                    f'<div class="svg-wrapper">{pr_svg}</div></div>\n'
                    f"</div>"
                )
            else:  # removed
                base_svg = _inline_svg(base_dir / name)
                entry = (
                    f"{div_open}\n{h3}\n"
                    f'<div class="side-only"><h4>Removed (was in base)</h4>'
                    f'<div class="svg-wrapper">{base_svg}</div></div>\n'
                    f"</div>"
                )
            entries_html.append(entry)

    ordered_changed = [
        item for section in section_order for item in by_section[section]
    ]
    metrics_html = _build_metrics_html(ordered_changed, base_metrics, pr_metrics)

    html = HTML_TEMPLATE.format(
        title_suffix=title_suffix,
        summary=summary,
        metrics=metrics_html,
        toc=toc,
        entries="\n\n".join(entries_html),
        marker=marker,
    )
    (output_dir / "index.html").write_text(html)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate render diff page")
    parser.add_argument("base_dir", type=Path, help="Base branch render directory")
    parser.add_argument("pr_dir", type=Path, help="PR branch render directory")
    parser.add_argument("output_dir", type=Path, help="Output directory for diff site")
    parser.add_argument("--pr", default=None, help="PR number for title")
    parser.add_argument(
        "--marker",
        default="",
        help="Opaque build id embedded in the page so a publisher can confirm "
        "this exact render is the one being served.",
    )
    args = parser.parse_args()

    has_changes = build_diff(
        args.base_dir, args.pr_dir, args.output_dir, args.pr, args.marker
    )
    if has_changes:
        print(f"Diff page written to {args.output_dir}/index.html")
        sys.exit(0)
    else:
        print("No render changes detected.")
        sys.exit(2)


if __name__ == "__main__":
    main()
