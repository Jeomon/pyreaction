"""Turn an HTML string into elements, for Markdown output or CMS content."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from .element import Element
from .ssr import VOID

__all__ = ["parse_html"]

# Removed with their contents unless allow_unsafe: they run code or load other pages.
_UNSAFE_TAGS = frozenset({"script", "iframe", "object", "embed", "frame", "frameset", "base", "meta", "link"})
_URL_ATTRS = frozenset({"href", "src", "action", "formaction", "xlink:href", "poster", "srcset"})
_BLOCKS = ("p", "div", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr", "section")
_CLOSED_BY: dict[str, tuple[str, ...]] = {
    "li": ("li", "p"),
    "dt": ("dt", "dd", "p"),
    "dd": ("dt", "dd", "p"),
    "tr": ("tr", "td", "th"),
    "td": ("td", "th"),
    "th": ("td", "th"),
    "option": ("option",),
    **{block: ("p",) for block in _BLOCKS},
}


def _safe_url(value: str) -> bool:
    v = "".join(value.split()).lower()
    if v.startswith(("javascript:", "vbscript:")):
        return False
    return not v.startswith("data:") or v.startswith("data:image/")


class _Builder(HTMLParser):
    def __init__(self, allow_unsafe: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.allow_unsafe = allow_unsafe
        self.root: list[Any] = []
        self.stack: list[tuple[str, dict[str, str], list[Any]]] = []
        self.skipping = 0  # depth inside a removed tag

    def _children(self) -> list[Any]:
        return self.stack[-1][2] if self.stack else self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skipping or (not self.allow_unsafe and tag in _UNSAFE_TAGS):
            if tag not in VOID:
                self.skipping += 1
            return
        clean: dict[str, str] = {}
        for name, value in attrs:
            value = "" if value is None else value
            if not self.allow_unsafe:
                if name.startswith("on") or (name in _URL_ATTRS and not _safe_url(value)):
                    continue
                if name == "style" and "expression(" in value.lower():
                    continue
            clean[name] = value
        # Tags HTML closes implicitly: a new <li> ends the open one, a block ends a <p>.
        while self.stack and self.stack[-1][0] in _CLOSED_BY.get(tag, ()):
            self._close()
        if tag in VOID:
            self._children().append(Element(tag, None, clean))
        else:
            self.stack.append((tag, clean, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID and not self.skipping:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            if tag not in VOID:
                self.skipping -= 1
            return
        # Close up to the matching tag, forgiving unclosed ones in between.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                while len(self.stack) > i:
                    self._close()
                return

    def _close(self) -> None:
        tag, attrs, children = self.stack.pop()
        self._children().append(Element(tag, None, attrs, {}, tuple(children)))

    def handle_data(self, data: str) -> None:
        if not self.skipping and data:
            self._children().append(data)

    def finish(self) -> list[Any]:
        self.close()
        while self.stack:
            self._close()
        return self.root


def parse_html(text: str, *, allow_unsafe: bool = False) -> list[Any]:
    """Elements (and strings) for an HTML string: ``html.div(parse_html(markdown_html))``.

    Unlike ``dangerously_set_inner_html``, the result is part of the tree, so
    later renders patch it like any other content. By default it removes what
    could run code: ``<script>``, ``<iframe>``, ``<object>``, ``<embed>``,
    ``<base>``, ``<meta>``, ``<link>``, ``on*`` attributes and ``javascript:``
    URLs. Pass ``allow_unsafe=True`` only for HTML you wrote yourself.
    """
    builder = _Builder(allow_unsafe)
    builder.feed(text)
    return builder.finish()
