"""Test components without a browser.

    from reaction.testing import render

    def test_counter():
        with render(Counter()) as page:
            page.click("button")
            assert page.find("span").text == "1"

``render`` mounts the component on a real event loop, with a small fake browser
that applies the operations the server sends. Events go through the same path as
in a live page (worker threads, async handlers, effects), and every action waits
until the page has settled. Errors raised by handlers, effects or renders fail
the test, unless ``raise_errors=False``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from html import escape
from typing import Any

from .core.element import Element
from .core.vdom import Root

__all__ = ["LiveServer", "Page", "TestNode", "poll", "render", "serve"]

VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


class TestNode:
    """One node of the fake page: ``.tag``, ``.attrs``, ``.text``, ``.html()``."""

    __test__ = False  # not a pytest test class

    def __init__(self, tag: str | None, text: str = "") -> None:
        self.id = -1
        self.tag = tag
        self._text = text
        self.attrs: dict[str, Any] = {}
        self.events: dict[str, Any] = {}
        self.children: list[TestNode] = []
        self.parent: TestNode | None = None
        self.js: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        """The text inside, as a browser's ``textContent``."""
        if self.tag is None:
            return self._text
        return "".join(c.text for c in self.children)

    @property
    def value(self) -> Any:
        return self.attrs.get("value", self.attrs.get("defaultValue"))

    def html(self) -> str:
        if self.tag is None:
            return escape(self._text, quote=False)
        attrs = "".join(
            f' {k}="{escape(str(v))}"' for k, v in sorted(self.attrs.items()) if k != "#html"
        )
        if self.tag in VOID:
            return f"<{self.tag}{attrs}>"
        inner = str(self.attrs["#html"]) if "#html" in self.attrs else self.inner_html()
        return f"<{self.tag}{attrs}>{inner}</{self.tag}>"

    def inner_html(self) -> str:
        return "".join(c.html() for c in self.children)

    def walk(self) -> Iterator[TestNode]:
        for child in self.children:
            yield child
            yield from child.walk()

    def remove(self) -> None:
        if self.parent is not None:
            self.parent.children.remove(self)
            self.parent = None

    def __repr__(self) -> str:
        return f"<TestNode {self.tag or 'text'} {self.id}>"


_SIMPLE = re.compile(r"(?P<tag>[a-zA-Z][\w-]*)|#(?P<id>[\w-]+)|\.(?P<cls>[\w-]+)|\[(?P<attr>[\w:-]+)(?:=(?P<q>[\"']?)(?P<val>.*?)(?P=q))?\]")


def _parse_selector(selector: str) -> list[list[tuple[str, str, str | None]]]:
    """``"form .row input[name=email]"`` as a list of compound parts (descendant chain)."""
    chain = []
    for part in selector.split():
        tests: list[tuple[str, str, str | None]] = []
        pos = 0
        while pos < len(part):
            m = _SIMPLE.match(part, pos)
            if m is None or m.end() == pos:
                raise ValueError(f"unsupported selector: {selector!r}")
            if m.group("tag"):
                tests.append(("tag", m.group("tag"), None))
            elif m.group("id"):
                tests.append(("id", m.group("id"), None))
            elif m.group("cls"):
                tests.append(("cls", m.group("cls"), None))
            elif m.group("attr"):
                tests.append(("attr", m.group("attr"), m.group("val")))
            pos = m.end()
        chain.append(tests)
    return chain


def _matches_one(node: TestNode, tests: list[tuple[str, str, str | None]]) -> bool:
    if node.tag is None:
        return False
    for kind, name, value in tests:
        if kind == "tag" and node.tag != name:
            return False
        if kind == "id" and node.attrs.get("id") != name:
            return False
        if kind == "cls" and name not in str(node.attrs.get("class", "")).split():
            return False
        if kind == "attr" and (name not in node.attrs or (value is not None and str(node.attrs[name]) != value)):
            return False
    return True


def _matches(node: TestNode, chain: list[list[tuple[str, str, str | None]]]) -> bool:
    if not _matches_one(node, chain[-1]):
        return False
    rest = chain[:-1]
    ancestor = node.parent
    while rest and ancestor is not None:
        if _matches_one(ancestor, rest[-1]):
            rest = rest[:-1]
        ancestor = ancestor.parent
    return not rest


class _Dom:
    def __init__(self) -> None:
        self.root = TestNode("#root")
        self.root.id = 0
        # Portal boxes, which the browser puts elsewhere in the document (<body>).
        self.portals = TestNode("#portals")
        self.nodes: dict[int, TestNode] = {0: self.root}

    def apply(self, ops: list[list[Any]], on_call: Callable[[list[Any]], None]) -> None:
        for op in ops:
            kind = op[0]
            if kind in ("c", "j"):
                node = TestNode(op[2])
                node.id = op[1]
                if kind == "c":
                    node.attrs, node.events = dict(op[3]), dict(op[4])
                else:
                    node.js = {"module": op[3], "export": op[4], "props": op[5]}
                self.nodes[op[1]] = node
            elif kind == "t":
                node = TestNode(None, op[2])
                node.id = op[1]
                self.nodes[op[1]] = node
            elif kind == "A":
                parent = self.nodes[op[1]]
                for i in op[2]:
                    self._insert(parent, self.nodes[i], None)
            elif kind == "a":
                before = None if op[3] is None else self.nodes[op[3]]
                self._insert(self.nodes[op[1]], self.nodes[op[2]], before)
            elif kind == "r":
                node = self.nodes.pop(op[1])
                node.remove()
                for child in node.walk():
                    self.nodes.pop(child.id, None)
            elif kind == "p":
                attrs = self.nodes[op[1]].attrs
                if op[3] is None:
                    attrs.pop(op[2], None)
                else:
                    attrs[op[2]] = op[3]
            elif kind == "x":
                self.nodes[op[1]]._text = op[2]
            elif kind == "e":
                events = self.nodes[op[1]].events
                if op[3] is None:
                    events.pop(op[2], None)
                else:
                    events[op[2]] = op[3]
            elif kind == "J":
                js = self.nodes[op[1]].js
                if js is not None:
                    js["props"] = op[2]
            elif kind == "m":
                on_call(op)
            elif kind == "P":
                node = TestNode("div")
                node.id = op[1]
                node.attrs = {"data-pyreaction-portal": op[2]}
                self.nodes[op[1]] = node
                self._insert(self.portals, node, None)

    @staticmethod
    def _insert(parent: TestNode, node: TestNode, before: TestNode | None) -> None:
        node.remove()
        index = len(parent.children) if before is None else parent.children.index(before)
        parent.children.insert(index, node)
        node.parent = parent


class Page:
    """A rendered component and a fake browser to drive it. Use :func:`render`."""

    def __init__(
        self,
        element: Element,
        *,
        location: str = "/",
        connection: Any = None,
        raise_errors: bool = True,
        timeout: float = 5.0,
        on_call: Callable[[TestNode, str, list[Any]], Any] | None = None,
    ) -> None:
        self.timeout = timeout
        self.raise_errors = raise_errors
        self.errors: list[BaseException] = []
        self.calls: list[tuple[TestNode, str, list[Any]]] = []
        self.on_call = on_call
        self.title: str | None = None
        self.head = ""
        self.history: list[str] = []
        self._dom = _Dom()
        self._seq = 0
        self._runner = asyncio.Runner()
        self.root: Root = self._run(self._start(element, location, connection))

    # ---- lifecycle -------------------------------------------------------------

    async def _start(self, element: Element, location: str, connection: Any) -> Root:
        root = Root(
            element, location=location, connection=connection, debug=False,
            on_error=lambda exc, where: self.errors.append(exc),
        )
        root.attach(self._receive)
        await root.settle(self.timeout)
        return root

    def _run(self, coro: Any) -> Any:
        return self._runner.run(coro)

    def close(self) -> None:
        if self._runner is not None:
            self.root.close()
            self._runner.close()
            self._runner = None  # type: ignore[assignment]

    def __enter__(self) -> Page:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _receive(self, message: dict[str, Any]) -> None:
        self._dom.apply(message["ops"], self._call)
        if message.get("title") is not None:
            self.title = message["title"]
        if message.get("head") is not None:
            self.head = message["head"]
        nav = message.get("nav")
        if nav is not None:
            self.history.append(nav["to"])

    def _call(self, op: list[Any]) -> None:
        node = self._dom.nodes.get(op[1])
        if node is None:
            return
        self.calls.append((node, op[2], op[3]))
        if len(op) > 4 and op[4]:
            value = self.on_call(node, op[2], op[3]) if self.on_call is not None else None
            self.root.dispatch(0, 0, "#reply", {"r": op[4], "value": value})

    def settle(self) -> None:
        """Wait until queued events, effects and data loads are done and rendered."""
        if not self._run(self.root.settle(self.timeout)):
            raise TimeoutError(f"the page did not settle within {self.timeout}s")
        if self.raise_errors and self.errors:
            error = self.errors.pop(0)
            self.errors.clear()
            raise error

    # ---- reading ---------------------------------------------------------------

    def html(self) -> str:
        """The page's markup, attributes sorted by name, then any portals' boxes."""
        return self._dom.root.inner_html() + self._dom.portals.inner_html()

    @property
    def text(self) -> str:
        return self._dom.root.text + self._dom.portals.text

    @property
    def location(self) -> str:
        return str(self.root.location)

    def find_all(self, selector: str | None = None, *, text: str | None = None) -> list[TestNode]:
        """Elements matching a CSS selector (tags, ``#id``, ``.class``, ``[attr]``,
        ``[attr=value]``, and descendants with spaces) and/or containing ``text``."""
        chain = _parse_selector(selector) if selector else None
        out = []
        for node in (*self._dom.root.walk(), *self._dom.portals.walk()):
            if node.tag is None:
                continue
            if chain is not None and not _matches(node, chain):
                continue
            if text is not None and text not in node.text:
                continue
            out.append(node)
        if text is not None and chain is None:
            # The innermost elements containing the text, not all their ancestors.
            out = [n for n in out if not any(c.tag and text in c.text for c in n.children)]
        return out

    def find(self, selector: str | None = None, *, text: str | None = None) -> TestNode:
        """The first element :meth:`find_all` would return; LookupError if none."""
        found = self.find_all(selector, text=text)
        if not found:
            raise LookupError(f"nothing matches {selector!r}" + (f" with text {text!r}" if text else ""))
        return found[0]

    # ---- acting ----------------------------------------------------------------

    def _node(self, target: str | TestNode) -> TestNode:
        return self.find(target) if isinstance(target, str) else target

    def dispatch(self, target: str | TestNode, event: str, data: dict[str, Any] | None = None) -> None:
        """Fire ``event`` (a DOM event name, e.g. ``"keydown"``) at an element, and settle.

        It propagates as in a browser: capture handlers (``on_click_capture``) from
        the outside in, then handlers from the target outwards, until one stops it."""
        node = self._node(target)
        path: list[TestNode] = []
        at: TestNode | None = node
        while at is not None and at.id != 0:
            path.append(at)
            at = at.parent
        capture = f"{event}:capture"
        listeners = [(n, capture) for n in reversed(path) if capture in n.events]
        listeners += [(n, event) for n in path if event in n.events]
        if not listeners:
            raise LookupError(f"nothing at or above {target!r} listens to {event!r}")
        payload = {"type": event, **(data or {})}
        for owner, name in listeners:
            spec = owner.events[name]
            if len(spec) > 7 and spec[7] and "form" not in payload:
                form = owner
                while form is not None and form.tag != "form":
                    form = form.parent  # type: ignore[assignment]
                if form is not None:
                    payload["form"] = self._fields(form, submitter=owner)
            keys = spec[4] if len(spec) > 4 else None
            if keys and payload.get("key") not in keys:
                continue  # filtered out in the browser: not sent, not stopped
            if not (len(spec) > 5 and spec[5]):  # a js() handler runs in the browser only
                self._seq += 1
                self.root.dispatch(self._seq, owner.id, name, payload)
            if spec[1] or (len(spec) > 6 and spec[6]):
                break
        self.settle()

    def click(self, target: str | TestNode) -> None:
        self.dispatch(target, "click")

    def type(self, target: str | TestNode, value: str) -> None:
        """Set an input's value and fire ``input``, as typing does (``on_change``)."""
        node = self._node(target)
        node.attrs["value"] = value
        self.dispatch(node, "input", {"value": value, "target": {"value": value, "name": node.attrs.get("name")}})

    def check(self, target: str | TestNode, checked: bool = True) -> None:
        node = self._node(target)
        node.attrs["checked"] = checked
        self.dispatch(node, "input", {"checked": checked, "target": {"checked": checked, "value": node.attrs.get("value", "on")}})

    def key(self, target: str | TestNode, key: str, event: str = "keydown") -> None:
        self.dispatch(target, event, {"key": key, "code": key})

    def submit(self, target: str | TestNode = "form", fields: dict[str, Any] | None = None) -> None:
        """Submit a form. ``fields`` defaults to its inputs' current names and values;
        a list value gives a field several values."""
        node = self._node(target)
        if fields is None:
            pairs = self._fields(node)
        else:
            pairs = [[k, str(x)] for k, v in fields.items() for x in (v if isinstance(v, list) else [v])]
        self.dispatch(node, "submit", {"form": pairs})

    @staticmethod
    def _fields(form: TestNode, submitter: TestNode | None = None) -> list[list[str]]:
        """A form's fields as the browser's FormData gives them."""
        pairs = []
        for el in form.walk():
            name = el.attrs.get("name") if el.tag in ("input", "select", "textarea") else None
            if not name:
                continue
            if el.attrs.get("type") in ("checkbox", "radio"):
                if el.attrs.get("checked", el.attrs.get("defaultChecked")) in (None, False):
                    continue
                pairs.append([name, str(el.attrs.get("value", "on"))])
            else:
                pairs.append([name, str(el.value or "")])
        if submitter is not None and submitter.attrs.get("name"):
            pairs.append([submitter.attrs["name"], str(submitter.attrs.get("value", ""))])
        return pairs

    def navigate(self, url: str) -> None:
        """Follow a link or the back button to ``url``."""
        self._seq += 1
        self.root.dispatch(self._seq, 0, "#navigate", {"to": url})
        self.settle()


def render(element: Element, **options: Any) -> Page:
    """Render ``element`` into a :class:`Page`; use it as a context manager or call
    ``close()``. Options: ``location``, ``connection``, ``raise_errors``,
    ``timeout``, and ``on_call(node, method, args)`` to answer element calls."""
    return Page(element, **options)


# ---- real browsers ------------------------------------------------------------------


class LiveServer:
    """An app served by uvicorn in a background thread, for tests in a real browser.

    ``url`` is its address. After ``page.goto(server.url)``, wait for the page to be
    live with ``page.wait_for_selector(server.READY)`` (Playwright), or for the
    ``data-pyreaction-ready`` attribute on ``<html>`` with any other driver.
    """

    READY = "html[data-pyreaction-ready]"

    def __init__(self, app: Any, host: str = "127.0.0.1", **options: Any) -> None:
        import socket
        import threading

        import uvicorn

        from .server import create_app

        asgi = app if options.pop("asgi", False) else create_app(app, **options)
        with socket.socket() as probe:
            probe.bind((host, 0))
            port = probe.getsockname()[1]
        self.url = f"http://{host}:{port}"
        self._server = uvicorn.Server(uvicorn.Config(asgi, host=host, port=port, log_level="warning", lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, name="reaction-live-server", daemon=True)
        self._thread.start()
        import time

        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the live server did not start")
            time.sleep(0.01)

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def __enter__(self) -> LiveServer:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def poll(check: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02) -> Any:
    """Call ``check`` until it returns something truthy, and return that; for tests
    against a live server, where the page updates in its own time::

        poll(lambda: page.text_content("#count") == "3")
        rows = poll(lambda: db.fetch_rows())               # waits for a non-empty result

        def saved():
            assert store.get("draft") == "hello"           # assertions retry too
        poll(saved)

    An ``AssertionError`` from ``check`` counts as "not yet". After ``timeout``
    seconds the last one is raised, or an ``AssertionError`` saying the condition
    was never met. Other exceptions are raised at once.
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        failure: AssertionError | None = None
        try:
            result = check()
        except AssertionError as exc:
            failure = exc
        else:
            if result:
                return result
        if time.monotonic() >= deadline:
            if failure is not None:
                raise failure
            raise AssertionError(f"{getattr(check, '__name__', 'check')} was not true within {timeout}s (last: {result!r})")
        time.sleep(interval)


def serve(app: Any, **options: Any) -> LiveServer:
    """Serve ``app`` (a component, element or factory, with :func:`create_app`'s
    options) on a free local port until closed. With ``asgi=True``, ``app`` is an
    ASGI app you built yourself (``create_app`` with extra routes, a FastAPI app)::

        with serve(App) as server:
            page.goto(server.url)
            page.wait_for_selector(server.READY)
    """
    return LiveServer(app, **options)
