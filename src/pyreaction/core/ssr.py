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
from .reconciler import ComponentNode, HostNode, Root, TextNode

__all__ = ["render_head", "render_to_string"]

VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
RAW_TEXT = frozenset({"script", "style"})


def render_to_string(target: Root | Element) -> str:
    """Render a root, or an element (mounted just for this), to HTML."""
    root = target if isinstance(target, Root) else Root(target)
    out: list[str] = []
    _children(root.container.children, out, raw=False)
    if root is not target:
        root.close()
    return "".join(out)


def _children(children: list[Any], out: list[str], raw: bool) -> None:
    prev_text = False
    stack = list(reversed(children))
    while stack:
        node = stack.pop()
        if isinstance(node, ComponentNode):
            stack.extend(reversed(node.children))
            continue
        if isinstance(node, TextNode):
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
            _host(node, out)
            prev_text = False


def render_head(elements: list[Element]) -> str:
    """Tags for the page's <head>, each marked so the browser can replace them later."""
    out = []
    for el in elements:
        marked = Element(el.type, el.key, {**el.attrs, "data-pyreaction-head": ""}, {}, el.children)
        out.append(render_to_string(marked))
    return "".join(out)


def _host(node: HostNode, out: list[str]) -> None:
    if node.type == PORTAL:
        return  # portals appear once the page is live
    if isinstance(node.type, JsComponent):
        # The module draws into this box once the page is live. Until then it shows
        # the fallback; Python children sit in a slot the module places later.
        tag = node.type.tag
        out.append(f"<{tag}>")
        fallback = node.element.kwargs.get("fallback")
        if fallback:
            out.append(f'<div data-pyreaction-fallback style="display:contents">{fallback}</div>')
        if node.children:
            out.append('<div data-pyreaction-slot style="display:contents">')
            _children(node.children, out, raw=False)
            out.append("</div>")
        out.append(f"</{tag}>")
        return
    tag = node.type
    out.append("<" + tag)
    for name, value in node.attrs.items():
        if name == RAW_HTML:
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
        out.append(f' {name}="{escape(str(value))}"')
    out.append(">")
    if tag in VOID:
        return
    if RAW_HTML in node.attrs:
        out.append(str(node.attrs[RAW_HTML]))
    elif tag == "textarea" and not node.children:
        out.append(escape(str(node.attrs.get("value", "")), quote=False))
    else:
        _children(node.children, out, raw=tag in RAW_TEXT)
    out.append(f"</{tag}>")
