"""A minimal DOM that applies the reconciler's operations, to check them without a browser."""

from __future__ import annotations

from html import escape
from typing import Any

from pyreaction.core.ssr import VOID


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


def tree_html(root: Any) -> str:
    """The mounted tree in FakeDom's format, built straight from the reconciler's nodes."""
    from pyreaction.core.element import JsComponent
    from pyreaction.core.reconciler import ComponentNode, TextNode

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
