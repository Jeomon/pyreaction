"""Error boundaries: show a fallback instead of a part of the page that failed to render."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .element import component, html
from .hooks import CURRENT, Ref, use_ref, use_state

__all__ = ["ErrorBoundary"]


@component
def ErrorBoundary(*children: Any, fallback: Any = None, reset_keys: Sequence[Any] = ()) -> Any:
    """Render ``children``; if rendering one of them raises, render ``fallback`` instead.

    ``fallback`` is an element, or ``fallback(error, reset)`` returning one, where
    ``reset()`` tries the children again. The boundary also resets by itself when
    any of ``reset_keys`` changes, e.g. ``reset_keys=[use_location().path]`` so
    that leaving a broken page clears the error.

    Errors in event handlers and effects are not caught here, as in React; they
    are logged (and shown in the browser in debug mode).
    """
    no_error: BaseException | None = None
    error, set_error = use_state(no_error)
    last_keys: Ref[tuple[Any, ...] | None] = use_ref(None)
    keys = tuple(reset_keys)
    if error is not None and last_keys.current is not None and keys != last_keys.current:
        error = None
        set_error(None)
    last_keys.current = keys
    node = CURRENT.get()
    assert node is not None
    if error is None:
        node.catch = set_error
        return children
    # While the fallback shows, errors go to the next boundary up: catching them
    # here would re-render the same fallback forever.
    node.catch = None
    if callable(fallback):
        return fallback(error, lambda: set_error(None))
    if fallback is not None:
        return fallback
    return html.div("Something went wrong.", role="alert")
