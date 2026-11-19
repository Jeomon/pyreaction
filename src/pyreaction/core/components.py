"""Built-in components: forms with pending state, Suspense, Activity and Head."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .element import Element, component, html
from .hooks import CURRENT, Ref, create_context, use_context, use_ref, use_state, use_transition

__all__ = ["Activity", "Form", "FormStatus", "Head", "Suspense", "use_form_status"]


class FormStatus:
    """What :func:`use_form_status` returns: ``.pending`` and the submitted ``.data``."""

    __slots__ = ("pending", "data")

    def __init__(self, pending: bool = False, data: Any = None) -> None:
        self.pending = pending
        self.data = data

    def __repr__(self) -> str:
        return f"FormStatus(pending={self.pending}, data={self.data!r})"


_FormStatusContext = create_context(FormStatus(), "FormStatus")


def use_form_status() -> FormStatus:
    """The status of the :class:`Form` around this component, for a submit button
    that shows "Saving..." or disables itself while the action runs."""
    return use_context(_FormStatusContext)


@component
def Form(*children: Any, action: Callable[[Any], Any], reset: bool = True, **props: Any) -> Element:
    """A ``<form>`` whose submit runs ``action(fields)`` in the background.

    ``fields`` is the submitted :class:`FormFields`. ``action`` may be ``async``;
    a plain function runs in a worker thread. While it runs, components inside
    read ``use_form_status().pending``; on success the form's fields are cleared
    (``reset=False`` keeps them). Other props go to the ``<form>`` element.
    """
    pending, start = use_transition()
    form: Ref[Any] = use_ref(None)
    last: Ref[Any] = use_ref(None)

    def after() -> None:
        if reset and form.current is not None:
            form.current.call("reset")

    if inspect.iscoroutinefunction(action):

        async def run(fields: Any) -> None:
            await action(fields)
            after()

    else:

        def run(fields: Any) -> None:  # type: ignore[misc]
            result = action(fields)
            if inspect.isawaitable(result):
                raise TypeError("Form action returned an awaitable; make it 'async def' instead")
            after()

    def submit(e: Any) -> None:
        last.current = e.form
        start(run, e.form)

    return _FormStatusContext(
        html.form(*children, ref=form, on_submit=submit, **props),
        value=FormStatus(pending, last.current),
    )


@component
def Suspense(*children: Any, fallback: Any = None) -> Any:
    """Show ``fallback`` while a component inside waits for :func:`use_data`.

    The children stay mounted (hidden) while waiting, so the ones that already
    have their data keep their state.
    """
    waiting, set_waiting = use_state(0)
    node = CURRENT.get()
    assert node is not None

    def suspend(task: Any) -> None:
        set_waiting(lambda n: n + 1)
        if task is None or task.done():
            set_waiting(lambda n: n - 1)
        else:
            task.add_done_callback(lambda _: set_waiting(lambda n: n - 1))

    node.suspend = suspend
    hidden = waiting > 0
    node.waiting = hidden
    return [
        fallback if hidden else None,
        html.div(*children, hidden=hidden, style=None if hidden else "display:contents"),
    ]


@component
def Activity(*children: Any, mode: str = "visible") -> Element:
    """Hide ``children`` (``mode="hidden"``) without unmounting them, so their state
    is kept for when they show again: tabs, wizards, a closed side panel."""
    hidden = mode == "hidden"
    return html.div(*children, hidden=hidden, style=None if hidden else "display:contents")


@component
def Head(*children: Any) -> None:
    """Put ``<meta>``, ``<link>`` and other tags into the page's ``<head>``, from
    anywhere in the tree, in the first HTML too. They are removed when the
    component unmounts. Use :func:`use_title` for the title."""
    node = CURRENT.get()
    assert node is not None
    node.root.set_head(node, tuple(c for c in children if isinstance(c, Element)))
    return None
