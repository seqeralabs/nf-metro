#!/usr/bin/env python3
"""Reflow Markdown/MDX prose to one sentence per line (semantic line breaks).

Hard-wrapped paragraphs are unwrapped into a single logical line, then split so
that every sentence starts a new line. Everything that is not prose is emitted
byte-for-byte: frontmatter, fenced code, tables, headings, raw HTML/JSX blocks,
import/export statements and blockquotes.

Usage:
    python scripts/format_docs_sentences.py docs            # rewrite in place
    python scripts/format_docs_sentences.py docs --check    # report, write nothing
    python scripts/format_docs_sentences.py docs --diff     # print unified diffs

The rewrite only ever joins and splits at whitespace, so the file's
whitespace-normalised character stream is invariant. `--check` verifies that on
every file and fails loudly if it ever does not hold.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Block recognition
# --------------------------------------------------------------------------

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})(\s|$)")
SETEXT_RE = re.compile(r"^\s{0,3}(=+|-{2,})\s*$")
THEMATIC_RE = re.compile(r"^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$")
TABLE_RE = re.compile(r"^\s*\|")
DIRECTIVE_RE = re.compile(r"^\s*:::")
IMPORT_RE = re.compile(r"^\s*(import|export)\s")
QUOTE_RE = re.compile(r"^\s*>")
COMMENT_OPEN_RE = re.compile(r"^\s*(<!--|\{/\*)")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$")

# `<script>`-style elements whose contents are not Markdown at all: pass through
# until the matching close tag.
OPAQUE_TAGS = ("script", "style", "svg", "object", "pre", "table")
OPAQUE_OPEN_RE = re.compile(r"^\s*<(" + "|".join(OPAQUE_TAGS) + r")\b", re.I)

# Any other tag on its own line is a container (`<Steps>`, `<Aside>`, `<div>`) or
# a component whose attributes may spill over several lines (`<Metro ... />`).
TAG_OPEN_RE = re.compile(r"^\s*</?[A-Za-z][A-Za-z0-9.-]*")

# Starlight components whose children are Markdown prose. Prettier reflows such
# children when they are *indented* under the tag, but leaves them alone when
# they sit at column zero with a blank line either side — the form Starlight
# documents. Children are dedented into that form so the sentence-per-line
# result survives `prettier --check` in CI.
PROSE_CONTAINERS = ("Steps", "Aside", "Details", "TabItem")
PROSE_OPEN_RE = re.compile(r"^(\s*)<(" + "|".join(PROSE_CONTAINERS) + r")\b[^>]*>\s*$")


def _tag_is_complete(line: str) -> bool:
    """True when a tag that opened on this line also finishes on it."""
    return line.rstrip().endswith((">", "/>"))


# --------------------------------------------------------------------------
# Masking: hide periods that live inside code spans, links and JSX expressions
# --------------------------------------------------------------------------

SENTINEL = "\x00"
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)", re.S)
LINK_DEST_RE = re.compile(r"\]\([^()\s]*(?:\([^()]*\)[^()\s]*)*(?:\s+\"[^\"]*\")?\)")
AUTOLINK_RE = re.compile(r"<[^<>\s]+(?:://|@)[^<>\s]*>")
BARE_URL_RE = re.compile(r"\bhttps?://\S+")
JSX_EXPR_RE = re.compile(r"\{[^{}]*\}")

MASK_PATTERNS = (CODE_SPAN_RE, LINK_DEST_RE, AUTOLINK_RE, BARE_URL_RE, JSX_EXPR_RE)


def mask(text: str) -> tuple[str, list[str]]:
    """Replace non-prose inline spans with sentinels so periods inside them
    cannot be mistaken for sentence ends."""
    store: list[str] = []

    def swap(match: re.Match[str]) -> str:
        store.append(match.group(0))
        return f"{SENTINEL}{len(store) - 1}{SENTINEL}"

    for pattern in MASK_PATTERNS:
        text = pattern.sub(swap, text)
    return text, store


def unmask(text: str, store: list[str]) -> str:
    for index in reversed(range(len(store))):
        text = text.replace(f"{SENTINEL}{index}{SENTINEL}", store[index])
    return text


# --------------------------------------------------------------------------
# Sentence splitting
# --------------------------------------------------------------------------

# Abbreviations that end in a period without ending a sentence. Matched
# case-insensitively against the word immediately before the period.
ABBREVIATIONS = frozenset(
    """
    al approx cf dr e eg etc fig ie inc jr mr mrs ms no prof sr st vs vol
    """.split()
)

# Lowercase words that legitimately open a sentence, because they are product
# names rather than prose. Extend this rather than loosening BOUNDARY_RE.
LOWERCASE_STARTERS = ("nf-metro", "nf-core", "nf-test", "nextflow")

# A sentence ends at `.`, `!` or `?` (plus any closing quotes or brackets),
# followed by whitespace and the start of something that can open a sentence:
# a capital, a digit, the inline markup docs prose habitually starts with, or
# one of the lowercase product names above.
BOUNDARY_RE = re.compile(
    r"""
    (?P<punct>[.!?]{1,3})
    (?P<close>["'”’)\]]*)
    [ \t]+
    (?=[A-Z0-9`\[(*_"'“‘<\x00]|(?:"""
    + "|".join(re.escape(name) for name in LOWERCASE_STARTERS)
    + r""")\b)
    """,
    re.VERBOSE,
)


def _is_real_boundary(text: str, match: re.Match[str]) -> bool:
    punct = match.group("punct")

    # `...` is an ellipsis, not a full stop.
    if set(punct) == {"."} and len(punct) > 1:
        return False

    if punct.endswith("."):
        before = text[: match.start()]

        word = re.search(r"([A-Za-z]+)$", before)
        if word:
            token = word.group(1)
            if token.lower() in ABBREVIATIONS:
                return False
            # A lone letter is an initial or the tail of `e.g.`, but only when
            # it stands on its own. The `s` of `the host's.` does not.
            standalone = word.start() == 0 or before[word.start() - 1] in " \t([."
            if len(token) == 1 and standalone:
                return False

        # A number that itself follows a sentence end is an ordered-list
        # marker a previous reflow swallowed, not a sentence end of its own.
        # Keeping `4.` attached to the text it introduces lets `_emit_ordered`
        # restore the list item. A number that merely closes a sentence
        # ("must be greater than 0.") is a normal boundary.
        if re.search(r"(?:^|[.!?]\s+)\d{1,9}$", before):
            return False

    return True


def split_sentences(text: str) -> list[str]:
    """Split one logical line of prose into sentences."""
    masked, store = mask(text)

    pieces: list[str] = []
    start = 0
    for match in BOUNDARY_RE.finditer(masked):
        if not _is_real_boundary(masked, match):
            continue
        pieces.append(masked[start : match.end("close")])
        start = match.end()
    pieces.append(masked[start:])

    return [unmask(piece, store).strip() for piece in pieces if piece.strip()]


# --------------------------------------------------------------------------
# The reformatter
# --------------------------------------------------------------------------


def _is_block_start(line: str) -> bool:
    """True when `line` opens something that is not a continuation of prose."""
    if not line.strip():
        return True
    return bool(
        FENCE_RE.match(line)
        or HEADING_RE.match(line)
        or THEMATIC_RE.match(line)
        or TABLE_RE.match(line)
        or DIRECTIVE_RE.match(line)
        or IMPORT_RE.match(line)
        or QUOTE_RE.match(line)
        or COMMENT_OPEN_RE.match(line)
        or LIST_RE.match(line)
        or TAG_OPEN_RE.match(line)
    )


def _emit(prefix: str, hang: str, sentences: list[str]) -> list[str]:
    if not sentences:
        return []
    out = [prefix + sentences[0]]
    out.extend(hang + sentence for sentence in sentences[1:])
    return out


ORDERED_MARKER_RE = re.compile(r"^(\d{1,9})([.)])\s+(\S.*)$")


def _emit_ordered(
    indent: str, number: int, delim: str, gap: str, sentences: list[str]
) -> list[str]:
    """Emit an ordered list item, re-splitting siblings a previous reflow merged.

    A hard-wrapped ordered list can end up with `... item three. 4. item four`
    on one line. Splitting that into sentences would leave a bare `4.` dangling
    at the continuation indent and silently break the list, so a sentence that
    opens with the next expected number is promoted back to a list item.
    """
    out: list[str] = []
    prefix = f"{indent}{number}{delim}{gap}"
    hang = " " * len(prefix)
    expected = number + 1
    first = True

    for sentence in sentences:
        sibling = ORDERED_MARKER_RE.match(sentence)
        if sibling and not first and int(sibling.group(1)) == expected:
            number, delim, rest = expected, sibling.group(2), sibling.group(3)
            prefix = f"{indent}{number}{delim}{gap}"
            hang = " " * len(prefix)
            expected = number + 1
            out.append(prefix + rest)
            continue
        out.append((prefix if first else hang) + sentence)
        first = False

    return out


def _normalise_container_body(body: list[str], tag_indent: str) -> list[str]:
    """Dedent a prose container's children to column zero and reflow them.

    Indented children are MDX flow text that prettier rewraps; the same content
    at column zero, blank-line separated, is plain Markdown that prettier leaves
    alone. Dedenting is what makes the reflow stick.
    """
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return [""]

    widths = [len(row) - len(row.lstrip()) for row in body if row.strip()]
    shift = min(widths) if widths else 0
    if shift <= len(tag_indent):
        # Already at (or outside) the tag's own column: leave the structure be
        # and only reflow the sentences.
        return ["", *reformat("\n".join(body)).split("\n"), ""]

    dedented = [row[shift:] if row.strip() else "" for row in body]
    return ["", *reformat("\n".join(dedented)).split("\n"), ""]


def reformat(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    total = len(lines)

    # Leading YAML frontmatter.
    if lines and lines[0].strip() == "---":
        out.append(lines[0])
        index = 1
        while index < total and lines[index].strip() != "---":
            out.append(lines[index])
            index += 1
        if index < total:
            out.append(lines[index])
            index += 1

    while index < total:
        line = lines[index]
        stripped = line.strip()

        # Blank lines, headings, tables, thematic breaks and the like: verbatim.
        if not stripped:
            out.append(line)
            index += 1
            continue

        fence = FENCE_RE.match(line)
        if fence:
            char, opener = fence.group(2)[0], len(fence.group(2))
            out.append(line)
            index += 1
            while index < total:
                out.append(lines[index])
                close = FENCE_RE.match(lines[index])
                index += 1
                if (
                    close
                    and close.group(3).strip() == ""
                    and close.group(2)[0] == char
                    and len(close.group(2)) >= opener
                ):
                    break
            continue

        if OPAQUE_OPEN_RE.match(line):
            tag = OPAQUE_OPEN_RE.match(line).group(1)
            closer = re.compile(rf"</{tag}\s*>", re.I)
            out.append(line)
            selfclosed = line.rstrip().endswith("/>") or closer.search(line)
            index += 1
            while not selfclosed and index < total:
                out.append(lines[index])
                if closer.search(lines[index]):
                    index += 1
                    break
                index += 1
            continue

        if COMMENT_OPEN_RE.match(line):
            out.append(line)
            closed = "-->" in line or "*/}" in line
            index += 1
            while not closed and index < total:
                out.append(lines[index])
                closed = "-->" in lines[index] or "*/}" in lines[index]
                index += 1
            continue

        container = PROSE_OPEN_RE.match(line)
        if container:
            tag_indent, tag = container.groups()
            close_re = re.compile(rf"^\s*</{tag}\s*>\s*$")
            open_re = re.compile(rf"^\s*<{tag}\b[^>]*[^/]>\s*$")
            body: list[str] = []
            depth = 1
            cursor = index + 1
            while cursor < total:
                if close_re.match(lines[cursor]):
                    depth -= 1
                    if depth == 0:
                        break
                elif open_re.match(lines[cursor]):
                    depth += 1
                body.append(lines[cursor])
                cursor += 1

            if cursor < total:
                out.append(line)
                out.extend(_normalise_container_body(body, tag_indent))
                out.append(lines[cursor])
                index = cursor + 1
                continue

        if TAG_OPEN_RE.match(line):
            # A tag on its own line: emit it, plus any attribute lines it spills
            # onto. Its Markdown children are handled by the loop that follows.
            out.append(line)
            complete = _tag_is_complete(line)
            index += 1
            while not complete and index < total:
                out.append(lines[index])
                complete = _tag_is_complete(lines[index])
                index += 1
            continue

        if (
            HEADING_RE.match(line)
            or SETEXT_RE.match(line)
            or THEMATIC_RE.match(line)
            or TABLE_RE.match(line)
            or DIRECTIVE_RE.match(line)
            or IMPORT_RE.match(line)
            or QUOTE_RE.match(line)
        ):
            out.append(line)
            index += 1
            continue

        # Prose: either a list item or a plain paragraph. Gather every line that
        # continues it, join them, then split the result into sentences.
        item = LIST_RE.match(line)
        if item:
            indent, marker, gap, first = item.groups()
            parts = [first]
            index += 1
            while index < total and not _is_block_start(lines[index]):
                parts.append(lines[index].strip())
                index += 1
            joined = " ".join(part for part in parts if part)
            sentences = split_sentences(joined)
            if marker[-1] in ".)" and marker[:-1].isdigit():
                out.extend(
                    _emit_ordered(indent, int(marker[:-1]), marker[-1], gap, sentences)
                )
            else:
                prefix = f"{indent}{marker}{gap}"
                out.extend(_emit(prefix, " " * len(prefix), sentences))
            continue

        indent = line[: len(line) - len(line.lstrip())]
        parts = [stripped]
        index += 1
        while index < total and not _is_block_start(lines[index]):
            parts.append(lines[index].strip())
            index += 1
        joined = " ".join(part for part in parts if part)
        out.extend(_emit(indent, indent, split_sentences(joined)))

    return "\n".join(out)


# --------------------------------------------------------------------------
# Safety net
# --------------------------------------------------------------------------


def normalised(text: str) -> str:
    """The file's character stream with every whitespace run collapsed.

    Joining and splitting only ever happens at whitespace, so this string must
    be identical before and after. Any difference means characters were lost or
    invented, and the rewrite is rejected.
    """
    return " ".join(text.split())


# Pages written by a generator script. Reformatting them is churn: the next
# generator run overwrites the result. These mirror the docs entries in
# .prettierignore.
GENERATED = (
    "docs/gallery/",
    "docs/pipelines/",
    "docs/dev/routing_gate_coverage.md",
)


def is_generated(path: Path) -> bool:
    posix = path.resolve().as_posix()
    return any(f"/{marker}" in f"{posix}/" for marker in GENERATED)


def process(path: Path, write: bool, show_diff: bool) -> tuple[bool, str | None]:
    original = path.read_text(encoding="utf-8")
    updated = reformat(original)

    if normalised(original) != normalised(updated):
        diff = difflib.unified_diff(
            normalised(original).split(" "),
            normalised(updated).split(" "),
            lineterm="",
            n=2,
        )
        return False, "content changed, not just line breaks:\n" + "\n".join(
            list(diff)[:40]
        )

    twice = reformat(updated)
    if twice != updated:
        return False, "not idempotent: a second pass would change the file again"

    if updated == original:
        return False, None

    if show_diff:
        sys.stdout.writelines(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    if write:
        path.write_text(updated, encoding="utf-8")
    return True, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=["docs"],
        type=Path,
        help="files or directories to reformat (default: docs)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report which files would change and write nothing",
    )
    parser.add_argument("--diff", action="store_true", help="print a unified diff")
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help="also reformat generated pages that .prettierignore excludes",
    )
    args = parser.parse_args()

    targets: list[Path] = []
    for target in args.paths:
        if target.is_dir():
            targets.extend(sorted(target.rglob("*.md")))
            targets.extend(sorted(target.rglob("*.mdx")))
        elif target.is_file():
            targets.append(target)
        else:
            print(f"no such path: {target}", file=sys.stderr)
            return 2

    if not args.include_generated:
        targets = [path for path in targets if not is_generated(path)]

    changed = 0
    failed = 0
    for path in sorted(set(targets)):
        did_change, error = process(path, write=not args.check, show_diff=args.diff)
        if error:
            print(f"SKIPPED {path}: {error}", file=sys.stderr)
            failed += 1
        elif did_change:
            changed += 1
            print(f"{'would reformat' if args.check else 'reformatted'} {path}")

    verb = "would be reformatted" if args.check else "reformatted"
    print(f"\n{changed} file(s) {verb}, {len(targets) - changed - failed} unchanged")
    if failed:
        print(f"{failed} file(s) skipped — see errors above", file=sys.stderr)
        return 1
    return 1 if (args.check and changed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
