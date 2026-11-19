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

from .element import Element
from .reconciler import ComponentNode, HostNode, Root, TextNode

__all__ = ["render_to_string"]

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


def _host(node: HostNode, out: list[str]) -> None:
    tag = node.type
    out.append("<" + tag)
    for name, value in node.attrs.items():
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
    if tag == "textarea" and not node.children:
        out.append(escape(str(node.attrs.get("value", "")), quote=False))
    _children(node.children, out, raw=tag in RAW_TEXT)
    out.append(f"</{tag}>")
