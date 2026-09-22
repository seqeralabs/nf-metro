"""Shared Rich console and helpers for nf-metro's human-readable CLI output.

Every human-facing line (progress, summaries, warnings, notes) goes to stderr
through :data:`console`, leaving stdout for machine-readable results.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from rich.console import Console as _RichConsole
from rich.highlighter import RegexHighlighter, ReprHighlighter
from rich.panel import Panel
from rich.progress import Progress, ProgressColumn, Task, TextColumn
from rich.text import Text
from rich.theme import Theme


class Console(_RichConsole):
    """Rich console that soft-wraps by default.

    Soft-wrap keeps a message on one logical line instead of hard-wrapping it at
    the (80-col) width Rich assumes for a non-TTY, so a piped or CI log shows
    each message whole rather than broken mid-sentence.
    """

    def print(self, *objects: Any, **kwargs: Any) -> None:  # type: ignore[override]  # noqa: ANN401
        kwargs.setdefault("soft_wrap", True)
        super().print(*objects, **kwargs)


def _width() -> int | None:
    """The width messages wrap at, or ``None`` to let Rich decide.

    ``TERMINAL_WIDTH`` is rich-click's own override, read here too so a
    panel and the help it sits beside wrap at the same column.
    """
    try:
        return int(os.environ["TERMINAL_WIDTH"])
    except (KeyError, ValueError):
        return None


#: Rich reads the "N file(s)" plural idiom these messages use as a call to a
#: function named file, and bolds the brackets. Nothing here prints a call, so
#: both rules are turned off; paths, numbers and URLs still highlight.
#: The two brand greens, shared so the banner, the help panels and the YAML
#: output cannot drift apart.
GREEN_DARK = "#158668"
GREEN_LIGHT = "#2EC09C"


_THEME = Theme(
    {
        "repr.call": "none",
        "repr.brace": "none",
        "yaml.key": GREEN_LIGHT,
        "yaml.punct": GREEN_DARK,
    }
)

console = Console(stderr=True, width=_width(), theme=_THEME)


class _YamlHighlighter(RegexHighlighter):
    """Keys and their punctuation, in the brand greens.

    nf-metro writes the YAML it prints, so it is always this shape: a key at
    the start of a line, optionally behind a list dash.
    """

    base_style = "yaml."
    highlights = [
        r"(?m)^\s*(?:-\s+)?(?P<key>\"[^\"]*\"|[\w.\-]+)(?P<punct>:)",
        r"(?m)^\s*(?P<punct>-)\s",
    ]


def print_banner() -> None:
    """Print the banner to stderr, ahead of any of the command's own output.

    Assembled rather than written as markup, where a backslash would escape
    the tag after it and eat the carriage's own sides.
    """
    from nf_metro import __version__

    top, bottom = GREEN_DARK, GREEN_LIGHT
    name, version = "nf-metro", f"v{__version__}"
    span = len(name) + len(version) + 7
    console.print(
        Text.assemble(
            "○",
            ("_" * span, top),
            "\n  ",
            ("\\", bottom),
            f"  {name} ",
            (version, "dim"),
            "  ",
            ("\\", top),
            "\n   ",
            ("‾" * (span + 1), bottom),
            "○",
        )
    )


def echo_yaml(text: str) -> None:
    """Write *text* to stdout, syntax-highlighted only on a real terminal.

    Piped stdout is a machine-readable contract - the GitHub Action parses
    these lines - so a pipe gets the text bare. Rich's own is_terminal will
    not do: ``FORCE_COLOR`` turns it on through a pipe, and an escape code
    inside a path breaks that parse. ``RICH_CODEX`` is the one exception:
    rich-codex sets it while capturing the docs screenshots, which go through
    a pipe but have to show what a terminal shows.
    """
    if not (sys.stdout.isatty() or os.environ.get("RICH_CODEX")):
        print(text)
        return
    body = _YamlHighlighter()(Text(text.rstrip()))
    # The matched hex doubles as the style, so each colour prints as itself.
    for match in re.finditer(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", text):
        body.stylize(match.group(), match.start(), match.end())
    # force_terminal for the RICH_CODEX case, where stdout is a pipe. It does
    # not override NO_COLOR, which Rich still honours.
    _RichConsole(width=_width(), theme=_THEME, force_terminal=True).print(body)


def panel(body: str, *, title: str, style: str = "yellow") -> Panel:
    """A titled box around *body*, for a block of warnings or errors.

    Markup in *body* is rendered; the caller escapes anything it interpolates.
    Rich highlights a plain string it is handed but not a Text, so the paths
    and numbers in a message are highlighted here to match every other line.
    """
    return Panel(
        ReprHighlighter()(Text.from_markup(body)),
        title=f"[bold {style}]{title}[/]",
        title_align="left",
        border_style=style,
        padding=(0, 1),
        expand=False,
    )


class DiamondBar(ProgressColumn):
    """A narrow progress bar drawn with diamonds: ◆ done, ◇ remaining."""

    def __init__(self, width: int = 24) -> None:
        self.width = width
        super().__init__()

    def render(self, task: Task) -> Text:
        fraction = task.percentage / 100 if task.total else 0.0
        filled = int(round(fraction * self.width))
        bar = Text()
        bar.append("◆" * filled, style="green" if fraction >= 1 else "cyan")
        bar.append("◇" * (self.width - filled), style="grey37")
        return bar


def progress_bar(description: str) -> Progress:
    """A narrow diamond progress bar over a known number of steps.

    Transient, so it clears itself when the work finishes and leaves the final
    summary as the only trace.
    """
    return Progress(
        TextColumn("[cyan]{task.description}"),
        DiamondBar(),
        TextColumn("[dim]{task.completed}/{task.total}"),
        console=console,
        transient=True,
    )
