"""The reconciler: keeps the mounted tree and turns re-renders into DOM operations.

Each DOM node the browser holds has an integer id. A render produces a flat list
of operations on those ids, so an update costs what changed, not what exists:

    ["c", id, tag, attrs, events]   create an element (events: {name: spec})
    ["t", id, text]                 create a text node
    ["A", parent, [ids]]            append children to a new element
    ["a", parent, id, before]       insert or move a child before ``before`` (null: at the end)
    ["r", id]                       remove a node and everything under it
    ["p", id, name, value]          set an attribute or property (null removes it)
    ["x", id, text]                 change a text node
    ["e", id, name, spec]           listen to an event (null stops listening)
    ["m", id, method, args]         call a method on an element, e.g. focus

A message carries ``ops`` and ``ack``, and when they change ``title``, ``nav``
(a URL to push into the browser's history) and, in debug mode, ``errors``.

The first message of a connection describes the whole tree in document order,
which is what lets the browser adopt server-rendered HTML without markers.
"""

from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import logging
import threading
import traceback
from bisect import bisect_left
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qs

from .element import FRAGMENT, Component, Element
from .hooks import CURRENT, Context, EffectSlot, same

logger = logging.getLogger("pyreaction")

__all__ = ["ElementHandle", "Event", "Location", "Root"]

_SKIP = object()


class TextNode:
    __slots__ = ("id", "text", "key", "parent")
    type = str

    def __init__(self, id: int, text: str, key: Any, parent: Any) -> None:
        self.id = id
        self.text = text
        self.key = key
        self.parent = parent


class HostNode:
    __slots__ = ("id", "type", "key", "parent", "element", "attrs", "events", "children", "dom", "depth")

    def __init__(self, id: int, element: Element, key: Any, parent: Any) -> None:
        self.id = id
        self.type = element.type
        self.key = key
        self.parent = parent
        self.element = element
        self.attrs = element.attrs
        self.events = element.events
        self.children: list[Any] = []
        self.dom: list[int] = []
        self.depth = parent.depth + 1 if parent is not None else 0


class ComponentNode:
    """A component, fragment or context provider: it owns children but no DOM node."""

    __slots__ = (
        "type", "key", "parent", "root", "element", "depth", "children", "hooks",
        "hook_index", "pending_effects", "dirty", "mounted", "value", "subscribers",
        "subscriptions", "catch",
    )

    def __init__(self, root: Root, element: Element, key: Any, parent: Any) -> None:
        self.type = element.type
        self.key = key
        self.parent = parent
        self.root = root
        self.element = element
        self.depth = parent.depth + 1
        self.children: list[Any] = []
        self.hooks: list[Any] = []
        self.hook_index = 0
        self.pending_effects: list[EffectSlot] = []
        self.dirty = False
        self.mounted = True
        self.value: Any = element.kwargs.get("value") if isinstance(element.type, Context) else None
        self.subscribers: set[ComponentNode] = set()
        self.subscriptions: list[ComponentNode] = []
        # Set by an error boundary: receives errors thrown while rendering its descendants.
        self.catch: Callable[[BaseException], None] | None = None

    @property
    def name(self) -> str:
        return getattr(self.type, "__name__", str(self.type))


class Event(dict):
    """Event data from the browser. Keys read as attributes: ``e.value``, ``e.target.checked``.

    A missing key reads as None, since which keys arrive depends on the event type.
    """

    def __getattr__(self, name: str) -> Any:
        value = self.get(name)
        return Event(value) if isinstance(value, dict) and not isinstance(value, Event) else value


class Location:
    """The page's path and query string, as the browser shows them."""

    __slots__ = ("path", "search")

    def __init__(self, url: str = "/") -> None:
        path, sep, query = url.partition("?")
        self.path = path or "/"
        self.search = sep + query if query else ""

    @property
    def query(self) -> dict[str, str]:
        """Query parameters, the first value of each."""
        return {k: v[0] for k, v in parse_qs(self.search[1:]).items()}

    def __str__(self) -> str:
        return self.path + self.search

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Location) and str(self) == str(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Location({str(self)!r})"


class ElementHandle:
    """What a ``ref`` attached to an element holds: tells the browser to act on it."""

    __slots__ = ("_root", "id")

    def __init__(self, root: Root, node_id: int) -> None:
        self._root = root
        self.id = node_id

    def call(self, method: str, *args: Any) -> None:
        """Call a method of the DOM element, e.g. ``call("setSelectionRange", 0, 3)``."""
        self._root.command(self.id, method, list(args))

    def focus(self) -> None:
        self.call("focus")

    def blur(self) -> None:
        self.call("blur")

    def click(self) -> None:
        self.call("click")

    def select(self) -> None:
        self.call("select")

    def scroll_into_view(self, behavior: str = "auto", block: str = "start") -> None:
        self.call("scrollIntoView", {"behavior": behavior, "block": block})

    def __repr__(self) -> str:
        return f"<ElementHandle {self.id}>"


def _wants_event(fn: Callable[..., Any]) -> bool:
    code = getattr(fn, "__code__", None)
    if code is not None:
        n = code.co_argcount + code.co_kwonlyargcount
        if inspect.ismethod(fn):
            n -= 1
        return n > 0 or bool(code.co_flags & inspect.CO_VARARGS)
    try:
        return len(inspect.signature(fn).parameters) > 0
    except (TypeError, ValueError):
        return True


def _stable_positions(seq: list[int]) -> set[int]:
    """Positions in ``seq`` forming its longest increasing run of old indices (-1 = new).

    Nodes at those positions keep their place; only the rest are moved.
    """
    tails: list[int] = []  # values
    tails_pos: list[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        if v < 0:
            continue
        j = bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_pos.append(i)
        else:
            tails[j] = v
            tails_pos[j] = i
        prev[i] = tails_pos[j - 1] if j > 0 else -1
    keep: set[int] = set()
    i = tails_pos[-1] if tails_pos else -1
    while i >= 0:
        keep.add(i)
        i = prev[i]
    return keep


def _flatten_items(items: Iterable[Any], prefix: tuple[Any, ...], out: list[tuple[Any, Any]]) -> None:
    """Children as (key, item) pairs. Keys are scoped to the list they came from, and
    None/False holes keep their index, so a conditional child never shifts its siblings."""
    for i, item in enumerate(items):
        if item is None or item is True or item is False:
            continue
        if isinstance(item, Element):
            key = (prefix, "$", item.key) if item.key is not None else (*prefix, i)
            out.append((key, item))
        elif isinstance(item, str):
            out.append(((*prefix, i), item))
        elif isinstance(item, (int, float)):
            out.append(((*prefix, i), str(item)))
        elif isinstance(item, (list, tuple)) or inspect.isgenerator(item):
            _flatten_items(item, (*prefix, i), out)
        else:
            out.append(((*prefix, i), str(item)))


class Root:
    """One live UI: the mounted tree, its pending updates, and the operations they produce.

    ``location`` is the page URL (path and query), ``connection`` the request or
    WebSocket it serves (read with ``use_request``), and ``debug`` sends errors to
    the browser as well as the log.
    """

    def __init__(
        self,
        element: Element,
        *,
        location: str = "/",
        connection: Any = None,
        debug: bool = False,
    ) -> None:
        self.container = HostNode(0, Element("#root"), None, None)
        self.hosts: dict[int, HostNode] = {}
        self.ops: list[list[Any]] = []
        self.location = Location(location)
        self.connection = connection
        self.debug = debug
        self.title: str | None = None
        self.ack = 0
        self._title_dirty = False
        self._nav: dict[str, Any] | None = None
        self._errors: list[str] = []
        self._location_subs: set[ComponentNode] = set()
        self._ids = itertools.count(1)
        self._dirty: list[tuple[int, int, ComponentNode]] = []
        self._seq = itertools.count()
        self._effects: list[EffectSlot] = []
        self._flush_scheduled = False
        self._flushing = False
        self._sink: Callable[[dict[str, Any]], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: int | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.container.children = self._reconcile(self.container, [], (element,))
        self.container.dom = self._dom_ids(self.container.children)
        # An error boundary that caught during the first render re-renders now,
        # so the first paint already shows its fallback.
        self._render_dirty()

    # ---- lifecycle -------------------------------------------------------------

    def attach(self, sink: Callable[[dict[str, Any]], None], hydrate: bool = False) -> None:
        """Start sending: the whole tree goes out, then effects run and updates flow.

        Must be called from the event loop that will serve this root.
        """
        self._loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()
        # Operations so far only built the tree; the snapshot replaces them.
        # Element commands made before now still have to run.
        commands = [op for op in self.ops if op[0] == "m"]
        self.ops = []
        if self.title is not None:
            self._title_dirty = True
        message = self._message(self.snapshot() + commands)
        message["init"] = "hydrate" if hydrate else "replace"
        self._sink = sink
        sink(message)
        self._run_effects()
        if self._dirty or self.ops or self._nav or self._errors or self._title_dirty:
            self.flush()

    def close(self) -> None:
        """Unmount everything, running effect cleanups and cancelling tasks."""
        self._sink = None
        for child in self.container.children:
            self._unmount(child, emit=False)
        self.container.children = []
        for task in list(self._tasks):
            task.cancel()

    def snapshot(self) -> list[list[Any]]:
        """Operations that build the current tree from nothing, in document order."""
        ops: list[list[Any]] = []

        def walk(host: HostNode) -> None:
            for node in self._dom_nodes(host.children):
                if isinstance(node, TextNode):
                    ops.append(["t", node.id, node.text])
                    continue
                ops.append(["c", node.id, node.type, node.attrs, {n: h.spec() for n, h in node.events.items()}])
                walk(node)
                if node.dom:
                    ops.append(["A", node.id, node.dom])

        walk(self.container)
        ops.append(["A", 0, self.container.dom])
        return ops

    # ---- scheduling ------------------------------------------------------------

    def call(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on this root's event loop; state setters go through here."""
        if self._loop is not None and threading.get_ident() != self._thread:
            self._loop.call_soon_threadsafe(fn)
        else:
            fn()

    def schedule(self, node: ComponentNode) -> None:
        if node.dirty or not node.mounted:
            return
        node.dirty = True
        heapq.heappush(self._dirty, (node.depth, next(self._seq), node))
        self.request_flush()

    def request_flush(self) -> None:
        """Send what is pending at the end of this event-loop tick."""
        if self._loop is not None and not self._flush_scheduled and not self._flushing:
            self._flush_scheduled = True
            self._loop.call_soon(self.flush)

    def flush(self) -> None:
        """Re-render every dirty component, shallowest first, and send one batch of ops.

        Updates made in the same event-loop tick are batched into one flush.
        Before :meth:`attach` this only renders; nothing is sent and no effect runs.
        """
        self._flush_scheduled = False
        self._render_dirty()
        if self._sink is None:
            return
        ops, self.ops = self.ops, []
        message = self._message(ops)
        if ops or len(message) > 2:
            self._sink(message)
        self._run_effects()

    def _render_dirty(self) -> None:
        self._flushing = True
        try:
            while self._dirty:
                _, _, node = heapq.heappop(self._dirty)
                if not node.dirty or not node.mounted:
                    continue
                before = self._dom_ids(node.children)
                self._render(node, node.element)
                if self._dom_ids(node.children) != before:
                    self._sync_dom(self._host_of(node))
        finally:
            self._flushing = False

    def _message(self, ops: list[list[Any]]) -> dict[str, Any]:
        message: dict[str, Any] = {"ops": ops, "ack": self.ack}
        if self._title_dirty:
            message["title"] = self.title
            self._title_dirty = False
        if self._nav is not None:
            message["nav"] = self._nav
            self._nav = None
        if self._errors:
            message["errors"] = self._errors
            self._errors = []
        return message

    def _run_effects(self) -> None:
        effects, self._effects = self._effects, []
        for slot in effects:
            if not slot.node.mounted:
                continue
            self._cleanup(slot)
            try:
                result = slot.fn()  # type: ignore[misc]
            except Exception as exc:
                self.report(exc, f"an effect in {slot.node.name}")
                continue
            if inspect.iscoroutine(result):
                task = self.spawn(result)
                slot.cleanup = task.cancel
            elif callable(result):
                slot.cleanup = result

    def _cleanup(self, slot: EffectSlot) -> None:
        cleanup, slot.cleanup = slot.cleanup, None
        if cleanup is not None:
            try:
                cleanup()
            except Exception as exc:
                self.report(exc, f"an effect cleanup in {slot.node.name}")

    def spawn(self, coro: Any) -> asyncio.Task[Any]:
        """Run a coroutine as a task owned by this root, cancelled on close."""
        task = asyncio.ensure_future(coro, loop=self._loop)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.report(task.exception(), "a background task")  # type: ignore[arg-type]

    def report(self, exc: BaseException, where: str) -> None:
        """Log an error; in debug mode also show it in the browser."""
        logger.error("Error in %s", where, exc_info=exc)
        if self.debug:
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self._errors.append(f"Error in {where}\n\n{text}")
            self.request_flush()

    # ---- page state ------------------------------------------------------------

    def set_title(self, title: str) -> None:
        if title != self.title:
            self.title = title
            self._title_dirty = True
            self.request_flush()

    def set_location(self, url: str) -> None:
        """The browser moved to ``url`` (a link or the back button): re-render what reads it."""
        location = Location(url)
        if location == self.location:
            return
        self.location = location
        for node in list(self._location_subs):
            self.schedule(node)

    def navigate(self, url: str, replace: bool = False) -> None:
        """Go to ``url`` without reloading the page."""

        def apply() -> None:
            self.set_location(url)
            self._nav = {"to": str(self.location), "replace": replace}
            self.request_flush()

        self.call(apply)

    def subscribe_location(self, node: ComponentNode) -> None:
        self._location_subs.add(node)

    def command(self, node_id: int, method: str, args: list[Any]) -> None:
        """Ask the browser to call ``method`` on an element."""

        def apply() -> None:
            if node_id in self.hosts:
                self.ops.append(["m", node_id, method, args])
                self.request_flush()

        self.call(apply)

    # ---- events ----------------------------------------------------------------

    def dispatch(self, seq: int, node_id: int, name: str, data: dict[str, Any]) -> None:
        """Deliver a browser event. ``seq`` is acknowledged once the handler finishes,
        which tells the browser that edits it sent before then are reflected."""
        if node_id == 0 and name == "#navigate":
            to = data.get("to")
            if isinstance(to, str) and to.startswith("/"):
                self.set_location(to)
            self._ack(seq)
            return
        node = self.hosts.get(node_id)
        handler = node.events.get(name) if node is not None else None
        if handler is None:
            self._ack(seq)
            return
        fn = handler.fn
        try:
            result = fn(Event(data)) if _wants_event(fn) else fn()
        except Exception as exc:
            self.report(exc, f"the {name!r} handler")
            self._ack(seq)
            return
        if inspect.iscoroutine(result):
            task = self.spawn(result)
            task.add_done_callback(lambda _: self._ack(seq))
        else:
            self._ack(seq)

    def _ack(self, seq: int) -> None:
        if seq > self.ack:
            self.ack = seq

    # ---- reconciliation --------------------------------------------------------

    def _reconcile(self, parent: Any, old: list[Any], items: Iterable[Any]) -> list[Any]:
        pairs: list[tuple[Any, Any]] = []
        _flatten_items(items, (), pairs)
        by_key = {child.key: child for child in old}
        result = []
        seen: set[Any] = set()
        for key, item in pairs:
            if key in seen:
                logger.warning("Duplicate key %r under %s", key[-1], getattr(parent, "name", parent.type))
                key = (key, len(result))
            seen.add(key)
            prev = by_key.pop(key, None)
            item_type = str if isinstance(item, str) else item.type
            # Tag names are compared by value: strings built at runtime are not interned.
            if prev is not None and (prev.type is item_type or prev.type == item_type):
                self._update(prev, item)
                result.append(prev)
            else:
                if prev is not None:
                    self._unmount(prev, emit=True)
                result.append(self._mount(item, key, parent))
        for leftover in by_key.values():
            self._unmount(leftover, emit=True)
        return result

    def _mount(self, item: Any, key: Any, parent: Any) -> Any:
        if isinstance(item, str):
            node_id = next(self._ids)
            self.ops.append(["t", node_id, item])
            return TextNode(node_id, item, key, parent)
        if isinstance(item.type, str) and item.type != FRAGMENT:
            host = HostNode(next(self._ids), item, key, parent)
            self.hosts[host.id] = host
            self.ops.append(["c", host.id, item.type, item.attrs, {n: h.spec() for n, h in item.events.items()}])
            host.children = self._reconcile(host, [], item.children)
            host.dom = self._dom_ids(host.children)
            if host.dom:
                self.ops.append(["A", host.id, host.dom])
            if item.ref is not None:
                item.ref.current = ElementHandle(self, host.id)
            return host
        node = ComponentNode(self, item, key, parent)
        self._render(node, item)
        return node

    def _update(self, node: Any, item: Any) -> None:
        if isinstance(node, TextNode):
            if node.text != item:
                node.text = item
                self.ops.append(["x", node.id, item])
            return
        if isinstance(node, HostNode):
            if node.element is item:
                return
            self._diff_host(node, item)
            return
        if not node.dirty:
            if node.element is item:
                return
            if (
                isinstance(node.type, Component)
                and node.type.memo
                and len(item.args) == len(node.element.args)
                and all(same(a, b) for a, b in zip(item.args, node.element.args))
                and item.kwargs.keys() == node.element.kwargs.keys()
                and all(same(v, node.element.kwargs[k]) for k, v in item.kwargs.items())
            ):
                node.element = item
                return
        self._render(node, item)

    def _diff_host(self, node: HostNode, item: Element) -> None:
        ops = self.ops
        old_attrs, new_attrs = node.attrs, item.attrs
        if old_attrs != new_attrs:
            for name, value in new_attrs.items():
                if old_attrs.get(name, _SKIP) != value:
                    ops.append(["p", node.id, name, value])
            for name in old_attrs.keys() - new_attrs.keys():
                ops.append(["p", node.id, name, None])
        old_events, new_events = node.events, item.events
        if old_events or new_events:
            for name, handler in new_events.items():
                prev = old_events.get(name)
                if prev is None or prev.spec() != handler.spec():
                    ops.append(["e", node.id, name, handler.spec()])
            for name in old_events.keys() - new_events.keys():
                ops.append(["e", node.id, name, None])
        old_ref, new_ref = node.element.ref, item.ref
        if old_ref is not new_ref:
            self._release_ref(node)
            if new_ref is not None:
                new_ref.current = ElementHandle(self, node.id)
        node.element = item
        node.attrs = new_attrs
        node.events = new_events
        node.children = self._reconcile(node, node.children, item.children)
        self._sync_dom(node)

    def _render(self, node: ComponentNode, item: Element) -> None:
        node.dirty = False
        node.element = item
        kind = node.type
        if isinstance(kind, Component):
            node.hook_index = 0
            token = CURRENT.set(node)
            try:
                out = kind.fn(*item.args, **item.kwargs)
            except Exception as exc:
                node.pending_effects.clear()
                boundary = self._boundary_for(node)
                if boundary is None:
                    self.report(exc, f"rendering {node.name}")
                else:
                    logger.error("Error rendering %s, caught by an error boundary", node.name, exc_info=exc)
                    boundary.catch(exc)  # type: ignore[misc]
                if node.children:
                    return  # keep showing the last good output
                out = None
            finally:
                CURRENT.reset(token)
            children = out if isinstance(out, (list, tuple)) else (out,)
        else:
            if isinstance(kind, Context):
                value = item.kwargs.get("value")
                if not same(value, node.value):
                    node.value = value
                    for sub in list(node.subscribers):
                        self.schedule(sub)
            children = item.children
        node.children = self._reconcile(node, node.children, children)
        if node.pending_effects:
            # Queued after the children's, so children's effects run first, as in React.
            self._effects.extend(node.pending_effects)
            node.pending_effects.clear()

    @staticmethod
    def _boundary_for(node: ComponentNode) -> ComponentNode | None:
        parent = node.parent
        while parent is not None:
            if isinstance(parent, ComponentNode) and parent.catch is not None:
                return parent
            parent = parent.parent
        return None

    def _sync_dom(self, host: HostNode) -> None:
        """Move and insert ``host``'s DOM children to match its new child order."""
        new = self._dom_ids(host.children)
        old = host.dom
        if new == old:
            return
        host.dom = new
        old_index = {node_id: i for i, node_id in enumerate(old)}
        keep = _stable_positions([old_index.get(node_id, -1) for node_id in new])
        before = None
        ops = self.ops
        for j in range(len(new) - 1, -1, -1):
            node_id = new[j]
            if j not in keep:
                ops.append(["a", host.id, node_id, before])
            before = node_id

    def _release_ref(self, node: HostNode) -> None:
        ref = node.element.ref
        if ref is not None and isinstance(ref.current, ElementHandle) and ref.current.id == node.id:
            ref.current = None

    def _unmount(self, node: Any, emit: bool) -> None:
        if isinstance(node, TextNode):
            if emit:
                self.ops.append(["r", node.id])
            return
        if isinstance(node, HostNode):
            if emit:
                self.ops.append(["r", node.id])
            del self.hosts[node.id]
            self._release_ref(node)
            for child in node.children:
                self._unmount(child, emit=False)
            return
        node.mounted = False
        node.dirty = False
        for child in node.children:
            self._unmount(child, emit)
        for slot in node.hooks:
            if isinstance(slot, EffectSlot):
                self._cleanup(slot)
        for provider in node.subscriptions:
            provider.subscribers.discard(node)
        self._location_subs.discard(node)

    # ---- tree helpers ----------------------------------------------------------

    @staticmethod
    def _dom_nodes(children: list[Any]) -> list[Any]:
        """The DOM-level nodes (hosts and texts) among ``children``, components flattened."""
        out: list[Any] = []
        stack = list(reversed(children))
        while stack:
            node = stack.pop()
            if isinstance(node, ComponentNode):
                stack.extend(reversed(node.children))
            else:
                out.append(node)
        return out

    @classmethod
    def _dom_ids(cls, children: list[Any]) -> list[int]:
        return [node.id for node in cls._dom_nodes(children)]

    @staticmethod
    def _host_of(node: Any) -> HostNode:
        parent = node.parent
        while not isinstance(parent, HostNode):
            parent = parent.parent
        return parent
