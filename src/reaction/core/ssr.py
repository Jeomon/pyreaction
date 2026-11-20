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

__all__ = ["image_preloads", "render_children", "render_head", "render_portals", "render_to_static_markup", "render_to_string"]

VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
RAW_TEXT = frozenset({"script", "style"})


def render_to_string(
    target: Root | Element, boundaries: dict[Any, int] | None = None, images: list[HostNode] | None = None
) -> str:
    """Render a root, or an element (mounted just for this), to HTML.

    With ``boundaries`` (for streaming), each Suspense boundary is wrapped in
    ``<!--pr-s:N-->`` ... ``<!--/pr-s:N-->`` comments, and its number is recorded
    in the dict, so a later chunk can replace what is between them. ``images``
    collects the <img> elements written, in order, for the page to preload.
    """
    root = target if isinstance(target, Root) else Root(target)
    out: list[str] = []
    with _gc_paused():
        if images is None:
            _children(root.container.children, out, raw=False, boundaries=boundaries)
        else:
            _children_fast(root.container.children, out, boundaries, images)
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
    if not raw and not static:
        _children_fast(children, out, boundaries)
    else:
        _children_general(children, out, raw, boundaries, static)


# Attributes _host treats specially (live properties, raw HTML): an element with any
# of them takes the general path.
_SPECIAL_ATTRS = frozenset(
    {RAW_HTML, "value", "checked", "selected", "indeterminate", "muted", "defaultValue", "defaultChecked"}
)
_CLOSE: dict[str, str] = {}


def _children_fast(
    children: list[Any], out: list[str], boundaries: dict[Any, int] | None, images: list[HostNode] | None = None
) -> None:
    """``_children_general`` for live markup, as one loop: an element's closing tag
    goes on the stack instead of a call per element, closing tags are made once per
    tag name, and text is escaped only when it holds something to escape (React's
    escapeTextForBrowser fast path). Anything unusual (raw text, JS components,
    portals, special attributes, textarea) goes to ``_host``. The output is the same
    byte for byte."""
    append = out.append
    prev_text = False
    stack: list[Any] = list(reversed(children))
    pop = stack.pop
    push = stack.append
    while stack:
        node = pop()
        kind = type(node)
        if kind is str:  # a closing tag or a boundary's end marker
            append(node)
            prev_text = False
            continue
        if kind is HostNode:
            prev_text = False
            tag = node.type
            attrs = node.attrs
            if (
                type(tag) is not str or tag == PORTAL or tag in RAW_TEXT or tag == "textarea"
                or (attrs and not _SPECIAL_ATTRS.isdisjoint(attrs))
            ):
                _host(node, out, boundaries)
                continue
            if attrs:
                if tag == "img" and images is not None:
                    images.append(node)
                append("<" + tag)
                for name, value in attrs.items():
                    s = value if type(value) is str else str(value)
                    if "&" in s or "<" in s or ">" in s or '"' in s or "'" in s:
                        s = escape(s)
                    append(f' {name}="{s}"')
                append(">")
            elif tag not in VOID:
                text = node.text
                if text is not None:  # the most common element: <td>text</td>
                    if "&" in text or "<" in text or ">" in text:
                        text = escape(text, quote=False)
                    append(f"<{tag}>{text}</{tag}>")
                    continue
                append(f"<{tag}>")
            if tag in VOID:
                if not attrs:
                    append(f"<{tag}>")
                continue
            close = _CLOSE.get(tag)
            if close is None:
                close = _CLOSE[tag] = f"</{tag}>"
            text = node.text
            if text is not None:
                if "&" in text or "<" in text or ">" in text:
                    text = escape(text, quote=False)
                append(text)
                append(close)
            else:
                push(close)
                if node.children:
                    stack.extend(reversed(node.children))
            continue
        if kind is TextNode:
            if prev_text:
                append("<!---->")
            text = node.text
            if not text:
                append("<!--$-->")
            else:
                if "&" in text or "<" in text or ">" in text:
                    text = escape(text, quote=False)
                append(text)
            prev_text = True
            continue
        # A component: its children in its place, a Suspense boundary marked.
        if boundaries is not None and node.suspend is not None:
            n = boundaries.setdefault(node, len(boundaries) + 1)
            append(f"<!--pr-s:{n}-->")
            prev_text = False
            push(f"<!--/pr-s:{n}-->")
        stack.extend(reversed(node.children))


def _children_general(
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


# At most this many images are preloaded from the page's <head> (React's limit),
# besides those marked fetch_priority="high".
MAX_IMAGE_PRELOADS = 10


def image_preloads(images: list[HostNode]) -> list[tuple[tuple[str, str], Element]]:
    """Preload hints for the images a page's HTML shows, as React's Fizz writes them
    (pushImg): the browser starts fetching them from <head>, before it reaches them in
    a long page. Not for lazy or low-priority images, data: URLs, or images in
    <picture> (its <source> may pick another file) or <noscript>."""
    out: list[tuple[tuple[str, str], Element]] = []
    count = 0
    for node in images:
        attrs = node.attrs
        src, srcset = attrs.get("src"), attrs.get("src-set") or attrs.get("srcset")
        if not (src or srcset) or attrs.get("loading") == "lazy":
            continue
        priority = attrs.get("fetch-priority") or attrs.get("fetchpriority")
        if priority == "low" or any(str(u).lstrip()[:5].lower() == "data:" for u in (src, srcset) if u):
            continue
        host = node.parent
        while host is not None and type(host) is not HostNode:
            host = host.parent
        if host is not None and host.type in ("picture", "noscript"):
            continue
        if priority != "high":
            if count >= MAX_IMAGE_PRELOADS:
                continue
            count += 1
        link = {"rel": "preload", "as": "image"}
        if srcset:
            link["imagesrcset"] = str(srcset)
            if attrs.get("sizes"):
                link["imagesizes"] = str(attrs["sizes"])
        else:
            link["href"] = str(src)
        for name in ("crossorigin", "referrerpolicy"):
            if attrs.get(name):
                link[name] = str(attrs[name])
        if priority:
            link["fetchpriority"] = str(priority)
        out.append((("preload", str(srcset or src)), Element("link", None, link)))
    return out


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
    if not root.portals:
        return ""  # no walk of the whole tree for nothing (most pages have none)
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
        fallback = node.extra.fallback if node.extra is not None else None
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
