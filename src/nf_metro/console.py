"""Shared Rich console and helpers for nf-metro's human-readable CLI output.

Every human-facing line (progress, summaries, warnings, notes) goes to stderr
through :data:`console`, leaving stdout for machine-readable results. Color is
forced in CI (GitHub Actions) so piped logs keep their formatting, mirroring
rich-click's own detection.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from rich.console import Console as _RichConsole
from rich.panel import Panel
from rich.progress import Progress, ProgressColumn, Task, TextColumn
from rich.syntax import Syntax
from rich.text import Text

# In GitHub Actions, force color for the whole Rich stack (rich-click's help and
# errors, and our own output) so captured logs keep their formatting. Rich and
# rich-click both read FORCE_COLOR. NO_COLOR still wins.
if "GITHUB_ACTIONS" in os.environ and "NO_COLOR" not in os.environ:
    os.environ.setdefault("FORCE_COLOR", "1")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


def force_color() -> bool | None:
    """Whether to force ANSI color, matching rich-click's CI detection.

    True in GitHub Actions (and when ``FORCE_COLOR``/``PY_COLORS`` are set) so
    captured CI logs keep their color; ``None`` elsewhere for auto-detection.
    ``NO_COLOR`` always wins.
    """
    if "NO_COLOR" in os.environ:
        return None
    for var in ("FORCE_COLOR", "PY_COLORS", "GITHUB_ACTIONS"):
        if var in os.environ:
            return _truthy(os.environ[var])
    return None


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


#: Stderr console for all human output. stdout stays machine-readable.
#: highlight=False so plain text (paths, "file(s)") is not auto-recolored; all
#: styling is explicit.
console = Console(
    stderr=True, force_terminal=force_color(), highlight=False, width=_width()
)


def print_banner() -> None:
    """Print the banner to stderr, ahead of any of the command's own output.

    The rails are drawn to fit the name and version they frame. Assembled
    rather than written as markup: a backslash escapes the tag that follows
    it, which would eat the carriage's own sides.
    """
    from nf_metro import __version__

    rail = "green"
    name, version = "nf-metro", f"v{__version__}"
    span = len(name) + len(version) + 7
    console.print(
        Text.assemble(
            "\n○",
            ("_" * span, rail),
            "\n  ",
            ("\\", rail),
            f"  {name} ",
            (version, "dim"),
            "  ",
            ("\\", rail),
            "\n   ",
            ("‾" * (span + 1), rail),
            "○\n",
        )
    )


def echo_yaml(text: str) -> None:
    """Write *text* to stdout, syntax-highlighted only on a real terminal.

    Piped stdout is a machine-readable contract - the GitHub Action parses
    these lines - so highlighting is gated on an interactive terminal and
    never on the CI colour forcing that :func:`force_color` applies to
    stderr, which would put escape codes inside a quoted path.
    """
    # Deliberately sys.stdout.isatty() rather than Rich's is_terminal, which
    # FORCE_COLOR turns on: the GitHub Action runs with colour forced and
    # reads this through a pipe.
    if "NO_COLOR" in os.environ or not sys.stdout.isatty():
        print(text)
        return
    body = Syntax(text, "yaml", background_color="default").highlight(text)
    body.rstrip()
    # A hex colour is worth more shown than named, so paint each one itself.
    for match in re.finditer(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", text):
        body.stylize(match.group(), match.start(), match.end())
    _RichConsole(highlight=False, width=_width()).print(body)


def panel(body: str, *, title: str, style: str = "yellow") -> Panel:
    """A titled box around *body*, for a block of warnings or errors.

    Markup in *body* is rendered; the caller escapes anything it interpolates.
    """
    return Panel(
        Text.from_markup(body),
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
