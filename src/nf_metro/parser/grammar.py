"""Lark front-end for the Mermaid subset nf-metro accepts.

The line shapes (graph header, subgraph, node declarations across all
Mermaid shapes, edges, %%metro directives, comments) are described by a
small ``lark`` grammar; a transformer turns the parse tree into an ordered
list of typed statements which the driver in :mod:`nf_metro.parser.mermaid`
applies to build the :class:`MetroGraph`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lark import Lark, Token, Transformer, UnexpectedInput

# A node label's inner text. It excludes the edge arrows so a shaped edge
# endpoint (``x[X] --> y[Y]``) doesn't greedily swallow the arrow into one
# label; the shape stops at the arrow and the line parses as an edge with
# shaped endpoints.
_SHAPE_INNER = r"(?:(?!-->|---|==>).)+"

# Grammar for the Mermaid subset nf-metro accepts. The document is a sequence
# of newline-separated statements; inline whitespace (indentation, spacing
# around arrows) is ignored, but whitespace inside the whole-line terminals
# (labels, directive bodies) is part of the match. Whole-line terminals carry
# an explicit priority so they win the tie against NAME on their keyword.
# ``_I_`` in the SHAPE terminal is substituted with ``_SHAPE_INNER`` below.
_GRAMMAR = r"""
start: (_statement? _NL)* _statement?

_statement: graph_header
          | subgraph_header
          | end_stmt
          | directive
          | comment
          | edge
          | node
          | junk

graph_header: GRAPH_HEADER
subgraph_header: SUBGRAPH_HEADER
end_stmt: END
directive: DIRECTIVE
comment: COMMENT
edge: NAME SHAPE? ARROW EDGELABEL? NAME SHAPE?
node: NAME SHAPE?
junk: JUNK

GRAPH_HEADER.5: /graph[ \t][^\n]*/
SUBGRAPH_HEADER.5: /subgraph\b[^\n]*/
END.5: /end(?=[ \t]*(?:\n|$))/
DIRECTIVE.5: /%%metro[^\n]*/
COMMENT.3: /%%[^\n]*/
ARROW.4: /-->|---|==>/
EDGELABEL.4: /\|[^|\n]*\|/
SHAPE.4: /\(\[_I_\]\)|\[\[_I_\]\]|\(\(_I_\)\)|\[_I_\]|\(_I_\)|\{_I_\}/
NAME: /[a-zA-Z_][a-zA-Z0-9_]*/
JUNK.-10: /[^\n]+/

_NL: /\r?\n/
%ignore /[ \t]+/
"""


@dataclass
class _GraphHeader:
    """A ``graph <DIR>`` header line."""

    line: str


@dataclass
class _Subgraph:
    """A ``subgraph id [Name]`` header, opening a section."""

    section_id: str
    name: str
    line_no: int | None = None


@dataclass
class _End:
    """An ``end`` line, closing the current section."""


@dataclass
class _Directive:
    """A ``%%metro key: value`` directive, split into key and stripped value."""

    key: str
    value: str
    line_no: int | None = None


@dataclass
class _Comment:
    """A ``%%`` comment (or a directive with no colon); ignored silently."""


@dataclass
class _Junk:
    """A non-blank line matching no statement; ignored with a warning."""

    text: str


@dataclass
class _Node:
    """A node declaration with its id and (shape-stripped) label."""

    node_id: str
    label: str
    line_no: int | None = None


@dataclass
class _Edge:
    """An edge with its endpoints, line ids, and any inline endpoint labels.

    ``line_ids`` is one or more line ids (``["default"]`` when the source
    carried no ``|...|`` annotation). ``source_label`` / ``target_label`` are
    set only when an endpoint was written with an inline shape, in which case
    that endpoint also declares the node.
    """

    source: str
    target: str
    line_ids: list[str]
    source_label: str | None = None
    target_label: str | None = None
    line_no: int | None = None


_Statement = (
    _GraphHeader | _Subgraph | _End | _Directive | _Comment | _Junk | _Node | _Edge
)


# Subgraph pattern: subgraph id [Display Name]
_SUBGRAPH_PATTERN = re.compile(r"^subgraph\s+(\w+)\s*(?:\[(.+?)\])?\s*$")


def _unquote(text: str) -> str:
    """Strip one pair of surrounding double quotes from a title or label.

    Mermaid requires special characters such as parentheses to be wrapped in
    double quotes (e.g. ``["Liftover (Picard)"]``) so the diagram parses on
    GitHub. The quotes are escaping syntax, not part of the displayed text, so
    they are removed here, leaving the inner text untouched.
    """
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _shape_label(shape: str) -> str:
    """Return the inner text of a shaped node label, delimiters stripped.

    The Mermaid shape delimiters are one or two characters per side; the model
    records only id + label (never which shape was used), so distinguishing
    them beyond delimiter width is unnecessary.
    """
    two_char_opens = ("([", "[[", "((")
    width = 2 if shape[:2] in two_char_opens else 1
    return shape[width:-width]


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated value into stripped, non-empty parts."""
    return [part.strip() for part in value.split(",") if part.strip()]


def _edge_line_ids(edge_label: str) -> list[str]:
    """Split a ``|a, b|`` token into line ids; ``["default"]`` when absent/empty."""
    inner = edge_label[1:-1].strip() if edge_label else ""
    return _split_csv(inner) if inner else ["default"]


class _StatementTransformer(Transformer[Token, list[_Statement]]):
    """Turn the parse tree into a flat, ordered list of typed statements."""

    def graph_header(self, items: list[Token]) -> _Statement:
        return _GraphHeader(str(items[0]))

    def subgraph_header(self, items: list[Token]) -> _Statement:
        m = _SUBGRAPH_PATTERN.match(str(items[0]))
        if not m:
            return _Junk(str(items[0]))
        return _Subgraph(
            m.group(1),
            _unquote((m.group(2) or m.group(1)).strip()),
            line_no=items[0].line,
        )

    def end_stmt(self, items: list[Token]) -> _Statement:
        return _End()

    def directive(self, items: list[Token]) -> _Statement:
        content = str(items[0])[len("%%metro") :].strip()
        key, sep, rest = content.partition(":")
        if not sep:
            return _Comment()
        return _Directive(key, rest.strip(), line_no=items[0].line)

    def comment(self, items: list[Token]) -> _Statement:
        return _Comment()

    def junk(self, items: list[Token]) -> _Statement:
        return _Junk(str(items[0]))

    def node(self, items: list[Token]) -> _Statement:
        name = str(items[0])
        label = _shape_label(str(items[1])) if len(items) > 1 else name
        return _Node(name, label, line_no=items[0].line)

    def edge(self, items: list[Token]) -> _Statement:
        # items: NAME [SHAPE] ARROW [EDGELABEL] NAME [SHAPE]
        source = str(items[0])
        rest = items[1:]
        source_label = None
        if rest and rest[0].type == "SHAPE":
            source_label = _shape_label(str(rest[0]))
            rest = rest[1:]
        rest = rest[1:]  # drop ARROW
        edge_label = ""
        if rest and rest[0].type == "EDGELABEL":
            edge_label = str(rest[0])
            rest = rest[1:]
        target = str(rest[0])
        target_label = (
            _shape_label(str(rest[1]))
            if len(rest) > 1 and rest[1].type == "SHAPE"
            else None
        )
        return _Edge(
            source,
            target,
            _edge_line_ids(edge_label),
            source_label,
            target_label,
            line_no=items[0].line,
        )

    def start(self, items: list[_Statement]) -> list[_Statement]:
        return [it for it in items if not isinstance(it, Token)]


# earley + the dynamic lexer are required, not a default: a line that begins
# like a statement but then hits an unexpected token must fall back to the
# low-priority junk rule and be dropped. A lalr/contextual parser commits
# token-by-token and cannot backtrack such a line to junk, so it would turn a
# dropped line (e.g. an inline-shaped edge endpoint) into a fatal error.
_PARSER = Lark(_GRAMMAR.replace("_I_", _SHAPE_INNER), parser="earley", lexer="dynamic")
_TRANSFORMER = _StatementTransformer()


def parse_statements(text: str) -> list[_Statement]:
    """Parse Mermaid source into an ordered list of typed statements.

    Raises ``ValueError`` with a helpful, located message when the grammar
    rejects the input.
    """
    try:
        tree = _PARSER.parse(text)
    except UnexpectedInput as e:
        context = e.get_context(text).rstrip("\n")
        raise ValueError(
            f"Could not parse the diagram at line {e.line}, column {e.column}.\n\n"
            f"{context}\n\n"
            "Expected a node (fastqc[FastQC]), an edge (fastqc -->|qc| falco), a "
            "subgraph header, an 'end', or a %%metro directive. A stray "
            "character here is the usual cause."
        ) from e
    return _TRANSFORMER.transform(tree)
