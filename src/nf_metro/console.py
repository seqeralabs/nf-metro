"""Shared Rich console and helpers for nf-metro's human-readable CLI output.

Every human-facing line (progress, summaries, warnings, notes) goes to stderr
through :data:`console`, leaving stdout for machine-readable results. Color is
forced in CI (GitHub Actions) so piped logs keep their formatting, mirroring
rich-click's own detection.
"""

from __future__ import annotations

import os
from typing import Any

from rich.console import Console as _RichConsole
from rich.progress import Progress, ProgressColumn, Task, TextColumn
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


#: Stderr console for all human output. stdout stays machine-readable.
#: highlight=False so plain text (paths, "file(s)") is not auto-recolored; all
#: styling is explicit.
console = Console(stderr=True, force_terminal=force_color(), highlight=False)


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
