"""Routing on the page's URL, without reloading the page."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from .element import Component, Element, component, html
from .hooks import CURRENT, use_effect, use_location, use_memo, use_navigate, use_status

__all__ = ["Link", "Navigate", "Router"]

_PARAM = re.compile(r"\{(\w+)(?::(path))?\}")


def _compile(pattern: str) -> re.Pattern[str]:
    """``/users/{id}`` matches one path segment; ``/files/{rest:path}`` matches the rest."""
    out, pos = [], 0
    for m in _PARAM.finditer(pattern):
        out.append(re.escape(pattern[pos : m.start()]))
        out.append(f"(?P<{m.group(1)}>.+)" if m.group(2) else f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile("".join(out) + "/?")


@component
def _NotFound(fallback: Any) -> Any:
    use_status(404)
    if fallback is None or isinstance(fallback, Element):
        return fallback
    return fallback()


@component
def Router(routes: Mapping[str, Component | Callable[..., Any]], fallback: Any = None) -> Any:
    """Render the first route whose pattern matches the path.

    ``Router({"/": Home, "/users/{id}": User})`` renders ``User(id="7")`` at
    ``/users/7``. ``fallback`` (an element, or a component called with no
    arguments) renders when nothing matches, and the page's first response then
    has HTTP status 404.
    """
    compiled = use_memo(lambda: [(_compile(p), view) for p, view in routes.items()], [tuple(routes.items())])
    path = use_location().path
    for regex, view in compiled:
        m = regex.fullmatch(path)
        if m is not None:
            return view(**m.groupdict())
    return _NotFound(fallback)


def Link(*children: Any, to: str, replace: bool = False, **props: Any) -> Element:
    """A link that changes the URL without reloading the page.

    Ctrl/Cmd-click and middle-click still open it in a new tab.
    """
    node = CURRENT.get()
    href = node.root.location.href(to) if node is not None else to  # under a mount: its prefix first
    return html.a(*children, href=href, data_pyreaction_link="replace" if replace else "push", **props)


@component
def Navigate(to: str, replace: bool = True) -> None:
    """Go to ``to`` as soon as this renders: a redirect.

    In the page's first response it is an HTTP redirect (307); on a live page it
    changes the URL without reloading.
    """
    root = CURRENT.get().root  # type: ignore[union-attr]
    if root._sink is None:
        root.redirect = to
    navigate = use_navigate()
    use_effect(lambda: navigate(to, replace=replace), [to])
    return None
