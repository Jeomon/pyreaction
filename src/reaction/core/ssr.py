"""HTML for the first paint, from a mounted tree.

The browser adopts this markup by walking it in document order and pairing the
k-th node with id k, so the output must contain exactly the tree's nodes in that
order. Two comment markers keep text nodes countable: ``<!---->`` between
adjacent texts (which the HTML parser would otherwise merge) and ``<!--$-->``
for an empty text (which would otherwise vanish).
"""

from __future__ import annotations

from html import escape
from typing import Any

from .element import PORTAL, RAW_HTML, Element, JsComponent
from .vdom import ComponentNode, HostNode, Root, TextNode, _gc_paused

__all__ = ["render_children", "render_head", "render_portals", "render_to_static_markup", "render_to_string"]

VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
RAW_TEXT = frozenset({"script", "style"})


def render_to_string(target: Root | Element, boundaries: dict[Any, int] | None = None) -> str:
    """Render a root, or an element (mounted just for this), to HTML.

    With ``boundaries`` (for streaming), each Suspense boundary is wrapped in
    ``<!--pr-s:N-->`` ... ``<!--/pr-s:N-->`` comments, and its number is recorded
    in the dict, so a later chunk can replace what is between them.
    """
    root = target if isinstance(target, Root) else Root(target)
    out: list[str] = []
    with _gc_paused():
        _children(root.container.children, out, raw=False, boundaries=boundaries)
    if root is not target:
        root.close()
    return "".join(out)


def render_to_static_markup(target: Root | Element) -> str:
    """Render to plain HTML that will never go live, as React's
    ``renderToStaticMarkup``: for emails, static snippets and other pages. Leaves out
    what the live page needs to adopt the HTML (comment markers between texts,
    ``data-pyreaction-*`` attributes), and data still loading is not waited for."""
    root = target if isinstance(target, Root) else Root(target)
    out: list[str] = []
    _children(root.container.children, out, raw=False, static=True)
    if root is not target:
        root.close()
    return "".join(out)


def render_children(children: list[Any], boundaries: dict[Any, int]) -> str:
    """HTML for a list of mounted nodes, marking Suspense boundaries as above."""
    out: list[str] = []
    _children(children, out, raw=False, boundaries=boundaries)
    return "".join(out)


class _End:
    __slots__ = ("n",)

    def __init__(self, n: int) -> None:
        self.n = n


def _children(
    children: list[Any], out: list[str], raw: bool, boundaries: dict[Any, int] | None = None, static: bool = False
) -> None:
    prev_text = False
    stack: list[Any] = list(reversed(children))
    while stack:
        node = stack.pop()
        kind = type(node)
        if kind is _End:
            out.append(f"<!--/pr-s:{node.n}-->")
            prev_text = False
            continue
        if kind is ComponentNode:
            if boundaries is not None and node.suspend is not None:
                n = boundaries.setdefault(node, len(boundaries) + 1)
                out.append(f"<!--pr-s:{n}-->")
                prev_text = False
                stack.append(_End(n))
            stack.extend(reversed(node.children))
            continue
        if kind is TextNode:
            if static:
                text = node.text.replace("</", "<\\/") if raw else escape(node.text, quote=False)
                out.append(text)
                continue
            if prev_text:
                out.append("<!---->")
            if not node.text:
                out.append("<!--$-->")
            elif raw:
                out.append(node.text.replace("</", "<\\/"))
            else:
                out.append(escape(node.text, quote=False))
            prev_text = True
        else:
            _host(node, out, boundaries, static)
            prev_text = False


def render_head(elements: list[Element]) -> str:
    """Tags for the page's <head>, each marked so the browser can replace them later."""
    out = []
    for el in elements:
        marked = Element(el.type, el.key, {**el.attrs, "data-pyreaction-head": ""}, None, el.children)
        out.append(render_to_string(marked))
    return "".join(out)


# Moves server-rendered portal boxes whose target is not <body> into place.
_PLACE_PORTALS = (
    "<script>document.querySelectorAll('[data-pyreaction-portal][data-target]').forEach(function(b){"
    "var t=document.querySelector(b.dataset.target);if(t)t.appendChild(b)})</script>"
)


def render_portals(root: Root, boundaries: dict[Any, int] | None = None) -> str:
    """Boxes holding each portal's content, for the end of the page's <body>.

    They are numbered in the order the first live message lists portals (the
    page's tree first, then each portal's own nested portals), which is how the
    browser pairs them up to adopt them.
    """
    queue: list[HostNode] = []

    def collect(host: HostNode) -> None:
        for node in Root._dom_nodes(host.children, queue):
            if isinstance(node, HostNode):
                collect(node)

    collect(root.container)
    out: list[str] = []
    placed = False
    i = 0
    while i < len(queue):
        portal = queue[i]
        collect(portal)  # nested portals go to the end of the queue, as live
        target = portal.attrs.get("target", "body")
        where = "" if target == "body" else f' data-target="{escape(target)}"'
        placed = placed or bool(where)
        out.append(f'<div data-pyreaction-portal="{i}"{where} style="display:contents">')
        _children(portal.children, out, raw=False, boundaries=boundaries)
        out.append("</div>")
        i += 1
    if placed:
        out.append(_PLACE_PORTALS)
    return "".join(out)


def _host(node: HostNode, out: list[str], boundaries: dict[Any, int] | None = None, static: bool = False) -> None:
    if node.type == PORTAL:
        return  # rendered by render_portals, at the end of the page
    if isinstance(node.type, JsComponent):
        # The module draws into this box once the page is live. Until then it shows
        # the fallback; Python children sit in a slot the module places later.
        tag = node.type.tag
        out.append(f"<{tag}>")
        fallback = node.element.kwargs.get("fallback")
        if static:
            out.append(str(fallback or ""))
            _children(node.children, out, raw=False, static=True)
        else:
            if fallback:
                out.append(f'<div data-pyreaction-fallback style="display:contents">{fallback}</div>')
            if node.children:
                out.append('<div data-pyreaction-slot style="display:contents">')
                _children(node.children, out, raw=False, boundaries=boundaries)
                out.append("</div>")
        out.append(f"</{tag}>")
        return
    tag = node.type
    out.append("<" + tag)
    for name, value in node.attrs.items():
        if name == RAW_HTML or (static and name.startswith("data-pyreaction-")):
            continue
        if name == "value":
            if tag == "textarea" or tag == "select":
                continue  # set as a property once the page is live
            value = "" if value is None else str(value)
        elif name in ("checked", "selected"):
            if not value:
                continue
            value = ""
        elif name in ("indeterminate", "muted"):
            continue
        elif name == "defaultValue":
            if tag != "input":
                continue  # a textarea's is its text; a select's is set once live
            name = "value"
        elif name == "defaultChecked":
            name = "checked"
        out.append(f' {name}="{escape(str(value))}"')
    out.append(">")
    if tag in VOID:
        return
    if RAW_HTML in node.attrs:
        out.append(str(node.attrs[RAW_HTML]))
    elif tag == "textarea" and not node.children:
        text = node.attrs.get("value", node.attrs.get("defaultValue", ""))
        out.append(escape(str(text), quote=False))
    elif node.text is not None:
        # Its only child, kept as its own text: one text node in the HTML (none when
        # empty), which is what the browser finds when it adopts the page.
        out.append(node.text.replace("</", "<\\/") if tag in RAW_TEXT else escape(node.text, quote=False))
    else:
        _children(node.children, out, raw=tag in RAW_TEXT, boundaries=boundaries, static=static)
    out.append(f"</{tag}>")
