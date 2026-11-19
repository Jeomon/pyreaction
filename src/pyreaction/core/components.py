"""Built-in components: forms with pending state, Suspense, Activity and Head."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .element import Element, component, html
from .hooks import BROWSER_ONLY, CURRENT, Ref, create_context, use_context, use_ref, use_state, use_transition

__all__ = [
    "Activity", "Form", "FormStatus", "Head", "Profiler", "StrictMode", "Suspense", "ViewTransition",
    "use_form_status",
]


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
def Profiler(*children: Any, id: Any, on_render: Callable[..., Any]) -> Any:
    """Measure how long the components inside take to render, as React's Profiler.

    After each render pass that rendered any of them, ``on_render(id, phase,
    actual_duration, start_time, commit_time)`` is called: ``phase`` is "mount" or
    "update", durations and times are milliseconds (``time.perf_counter`` based).
    Profilers may be nested; an outer one's time includes the inner one's."""
    node = CURRENT.get()
    assert node is not None
    node.profile = (id, on_render)
    node.root._profiling = True
    return list(children)


Profiler.profiler = True


@component
def ViewTransition(*children: Any, name: str | None = None) -> Any:
    """Animate changes to ``children`` with the browser's View Transitions API, as
    React's ``<ViewTransition>``: when an update changes anything inside, the page
    applies it in ``document.startViewTransition``, which cross-fades by default and
    can be styled with CSS (``::view-transition-old(name)``). Browsers without the
    API just update. Unlike React, every update inside animates, not only those
    from transitions.

    With ``name`` the children sit in a ``<div>`` with that ``view-transition-name``,
    so the browser can animate it on its own (a card moving to a new place: give
    it the same name on both pages). Names must be unique on the page."""
    node = CURRENT.get()
    assert node is not None
    node.view_transition = True
    if name is None:
        return list(children)
    return html.div(*children, style={"view_transition_name": name})


@component
def StrictMode(*children: Any) -> Any:
    """Finds impure components and effects without cleanup, in debug mode (as
    ``pyreaction dev`` runs) only, like React's development builds: components
    inside render twice, and each effect is set up, cleaned up and set up again
    when it first runs. Outside debug mode it does nothing."""
    node = CURRENT.get()
    assert node is not None
    node.strict = True
    return list(children)


@component
def Suspense(*children: Any, fallback: Any = None) -> Any:
    """Show ``fallback`` while a component inside waits for :func:`use_data`.

    The children stay mounted (hidden) while waiting, so the ones that already
    have their data keep their state.
    """
    waiting, set_waiting = use_state(0)
    browser_only = use_ref(False)
    node = CURRENT.get()
    assert node is not None

    def suspend(task: Any) -> None:
        if task is BROWSER_ONLY:
            # Content left to the browser: the fallback stays until the page is live.
            if not browser_only.current:
                browser_only.current = True
                set_waiting(lambda n: n + 1)

                def release() -> None:
                    browser_only.current = False
                    set_waiting(lambda n: n - 1)

                node.root.on_live(node, release)
            return
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
    component unmounts. A ``<title>`` among them sets the page title."""
    node = CURRENT.get()
    assert node is not None
    tags = []
    for child in children:
        if isinstance(child, Element) and child.type is Hoisted:
            child = child.args[0]
        if not isinstance(child, Element):
            continue
        if child.type == "title":
            node.root.set_title(_text(child))
        else:
            tags.append(child)
    node.root.set_head(node, tuple(tags))
    return None


def _text(el: Element) -> str:
    return "".join(str(c) for c in el.children if c is not None and c is not False and c is not True)


@component
def Hoisted(element: Element) -> Any:
    """What ``html.title``, ``html.meta`` and ``html.link`` make: written anywhere in
    the tree, the tag goes into the page's ``<head>`` (the title sets the page
    title), in the first HTML too, as React 19 does. A ``<title>`` inside ``<svg>``
    (a tooltip) and tags with ``itemprop`` (page metadata) stay where they are."""
    node = CURRENT.get()
    assert node is not None
    host = node.parent
    while host is not None and not hasattr(host, "ns"):
        host = host.parent
    if "itemprop" in element.attrs or (host is not None and host.ns == "svg"):
        return element
    if element.type == "title":
        node.root.set_title(_text(element))
    else:
        node.root.set_head(node, (element,))
    return None
