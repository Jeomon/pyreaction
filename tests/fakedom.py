"""A minimal DOM that applies the reconciler's operations, to check them without a browser."""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Any

from reaction.core.ssr import VOID


class Node:
    def __init__(self, tag: str | None, text: str = "") -> None:
        self.tag = tag
        self.text = text
        self.attrs: dict[str, Any] = {}
        self.events: dict[str, Any] = {}
        self.children: list[Node] = []
        self.parent: Node | None = None
        self.js: dict[str, Any] | None = None

    def remove(self) -> None:
        if self.parent is not None:
            self.parent.children.remove(self)
            self.parent = None

    def html(self) -> str:
        if self.tag is None:
            return escape(self.text, quote=False)
        attrs = "".join(f' {k}="{escape(str(v))}"' for k, v in sorted(self.attrs.items()))
        if self.tag in VOID:
            return f"<{self.tag}{attrs}>"
        return f"<{self.tag}{attrs}>{self.inner()}</{self.tag}>"

    def inner(self) -> str:
        return "".join(c.html() for c in self.children)


class FakeDom:
    def __init__(self) -> None:
        self.root = Node("#root")
        self.nodes: dict[int, Node] = {0: self.root}
        self.calls: list[tuple[Node, str, list[Any]]] = []

    def apply(self, ops: list[list[Any]]) -> None:
        for op in ops:
            kind = op[0]
            if kind == "c":
                node = Node(op[2])
                node.attrs = dict(op[3])
                node.events = dict(op[4])
                self.nodes[op[1]] = node
            elif kind == "t":
                self.nodes[op[1]] = Node(None, op[2])
            elif kind == "j":
                node = Node(op[2])
                node.js = {"module": op[3], "export": op[4], "props": op[5]}
                self.nodes[op[1]] = node
            elif kind == "J":
                self.nodes[op[1]].js["props"] = op[2]
            elif kind == "A":
                parent = self.nodes[op[1]]
                for i in op[2]:
                    self._insert(parent, self.nodes[i], None)
            elif kind == "a":
                before = None if op[3] is None else self.nodes[op[3]]
                self._insert(self.nodes[op[1]], self.nodes[op[2]], before)
            elif kind == "r":
                node = self.nodes[op[1]]
                node.remove()
                self._forget(op[1], node)
            elif kind == "p":
                node = self.nodes[op[1]]
                if op[3] is None:
                    node.attrs.pop(op[2], None)
                else:
                    node.attrs[op[2]] = op[3]
            elif kind == "x":
                self.nodes[op[1]].text = op[2]
            elif kind == "e":
                node = self.nodes[op[1]]
                if op[3] is None:
                    node.events.pop(op[2], None)
                else:
                    node.events[op[2]] = op[3]
            elif kind == "m":
                self.calls.append((self.nodes[op[1]], op[2], op[3]))
            else:
                raise AssertionError(f"unknown op {op!r}")

    def _insert(self, parent: Node, node: Node, before: Node | None) -> None:
        assert before is None or before.parent is parent, "insert before a node of another parent"
        node.remove()
        index = len(parent.children) if before is None else parent.children.index(before)
        parent.children.insert(index, node)
        node.parent = parent

    def _forget(self, node_id: int, node: Node) -> None:
        del self.nodes[node_id]
        for other_id, other in list(self.nodes.items()):
            n = other
            while n is not None and n is not node:
                n = n.parent
            if n is node:
                del self.nodes[other_id]

    def html(self) -> str:
        return self.root.inner()

    def adopt(self, page: str, ops: list[list[Any]]) -> None:
        """Take over ``page``'s markup with a hydrate message, as client.js does: the
        HTML gives the structure, the operations give node ids, attributes and events.
        ``page`` is a whole page, an ``embed()`` snippet, or a fragment of markup."""
        if any(op[0] == "P" for op in ops):
            raise AssertionError("FakeDom.adopt does not handle portals")
        found = _dom_order(_body(page), self.root)
        made = [op for op in ops if op[0] in ("c", "t", "j")]
        mismatch = len(made) != len(found) or any(
            (n.tag is not None or n.text != op[2]) if op[0] == "t" else n.tag != op[2] for op, n in zip(made, found)
        )
        if mismatch:
            raise AssertionError(f"the HTML does not match the render: {[op[:3] for op in made]} vs {found}")
        for op, node in zip(made, found):
            self.nodes[op[1]] = node
            if op[0] == "c":
                node.attrs, node.events = dict(op[3]), dict(op[4])
            elif op[0] == "j":
                node.js = {"module": op[3], "export": op[4], "props": op[5]}
        self.apply([op for op in ops if op[0] == "m"])


def _body(page: str) -> str:
    """The app's markup inside a page or an ``embed()`` snippet."""
    marker = re.search(r'<div id="pyreaction-[\w-]*">', page) or re.search(r'<div id="pyreaction-root">', page)
    if marker is None:
        return page
    rest = page[marker.end():]
    for end in ("</div>\n", "</div><script"):
        if end in rest:
            return rest[: rest.index(end)]
    return rest


class _Builder(HTMLParser):
    """Parses markup into Nodes the way client.js's domOrder walks the DOM."""

    def __init__(self, root: Node) -> None:
        super().__init__(convert_charrefs=True)
        self.stack = [root]
        self.order: list[Node] = []
        self.skip = 0  # inside a fallback or portal box: not part of the tree
        self.opaque = 0  # inside raw HTML or a textarea: the element is, its contents are not

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = dict(attrs)
        if self.skip or "data-pyreaction-fallback" in names or "data-pyreaction-portal" in names:
            if tag not in VOID:
                self.skip += 1
            return
        if "data-pyreaction-slot" in names:  # a js_component's Python children
            return
        node = Node(tag)
        if not self.opaque:
            self.order.append(node)
            self._add(node)
        if tag not in VOID:
            self.stack.append(node)
            if self.opaque or tag == "textarea" or "data-pyreaction-raw" in names:
                self.opaque += 1

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            if tag not in VOID:
                self.skip -= 1
            return
        if len(self.stack) > 1 and self.stack[-1].tag == tag:
            self.stack.pop()
            if self.opaque:
                self.opaque -= 1

    def handle_data(self, data: str) -> None:
        if self.skip or self.opaque:
            return
        node = Node(None, data)
        self.order.append(node)
        self._add(node)

    def handle_comment(self, data: str) -> None:
        if not self.skip and not self.opaque and data == "$":  # an empty text node
            node = Node(None, "")
            self.order.append(node)
            self._add(node)

    def _add(self, node: Node) -> None:
        parent = self.stack[-1]
        parent.children.append(node)
        node.parent = parent


def _dom_order(markup: str, root: Node) -> list[Node]:
    builder = _Builder(root)
    builder.feed(markup)
    builder.close()
    return builder.order


def tree_html(root: Any) -> str:
    """The mounted tree in FakeDom's format, built straight from the reconciler's nodes."""
    from reaction.core.element import JsComponent
    from reaction.core.vdom import ComponentNode, TextNode

    def nodes(children: list[Any]) -> str:
        out = []
        for child in children:
            if isinstance(child, ComponentNode):
                out.append(nodes(child.children))
            elif isinstance(child, TextNode):
                out.append(escape(child.text, quote=False))
            elif isinstance(child.type, JsComponent):
                out.append(f"<{child.type.tag}></{child.type.tag}>")
            else:
                node = Node(child.type)
                node.attrs = dict(child.attrs)
                html = node.html()
                if child.type not in VOID:
                    html = html[: -len(f"</{child.type}>")] + nodes(child.children) + f"</{child.type}>"
                out.append(html)
        return "".join(out)

    return nodes(root.container.children)
