"""Common base for the errors nf-metro's public API can raise.

:func:`nf_metro.render_string` and :func:`nf_metro.prepare_graph` propagate the
pipeline's typed parse- and layout-time errors rather than wrapping them (the
CLI does that wrapping into ``click.ClickException`` for its own surface). An
embedder that wants one ``except`` clause covering "nf-metro rejected this
input or layout" - without enumerating every specific error type - can catch
this base instead. Each specific error remains individually catchable and
keeps its other base too (most are also :class:`ValueError`, since they
describe an actionable problem with the input), so an ``except ValueError``
or ``except SomeSpecificError`` call site catches exactly what it did before.
"""

from __future__ import annotations


class NfMetroError(Exception):
    """Base class for nf-metro's parse- and layout-time authoring/engine errors.

    Every error :func:`~nf_metro.render_string` and
    :func:`~nf_metro.prepare_graph` can raise for a parse failure or a layout
    authoring/invariant problem subclasses this. See the ``prepare_graph``
    docstring for the exhaustive list and when each is raised.
    """
