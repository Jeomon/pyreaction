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
    ["m", id, method, args, reply]  call a method on an element (or "#get"/"#set" a
                                    property); the browser answers [seq, 0, "#reply",
                                    {r: reply, value | error}]
    ["j", id, tag, module, export, props]   create an element a JavaScript module draws into
    ["J", id, props]               pass new props to that module

A message carries ``ops`` and ``ack``, and when they change ``title``, ``nav``
(a URL to push into the browser's history) and, in debug mode, ``errors``.

The first message of a connection describes the whole tree in document order,
which is what lets the browser adopt server-rendered HTML without markers.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import heapq
import inspect
import io
import itertools
import logging
import shutil
import threading
import traceback
from bisect import bisect_left
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qs

from .element import FRAGMENT, PORTAL, Component, Element, JsComponent
from .hooks import CURRENT, Context, EffectSlot, Suspended, same

logger = logging.getLogger("pyreaction")

__all__ = ["ElementHandle", "Event", "FormFields", "Location", "Reply", "Root", "UploadedFile"]

_SKIP = object()

# The event a worker thread is handling, so state updates it makes carry its ack.
_handling = threading.local()


class TextNode:
    __slots__ = ("id", "text", "key", "parent")
    type = str

    def __init__(self, id: int, text: str, key: Any, parent: Any) -> None:
        self.id = id
        self.text = text
        self.key = key
        self.parent = parent


class HostNode:
    __slots__ = (
        "id", "type", "key", "parent", "element", "attrs", "events", "children", "dom", "depth", "ns",
    )

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
        self.ns = _namespace(self.type, parent)


# Elements inside <svg> or <math> must be created in that XML namespace, or the
# browser treats them as unknown HTML tags and draws nothing.
def _namespace(tag: Any, parent: Any) -> str | None:
    if tag == "svg":
        return "svg"
    if tag == "math":
        return "math"
    host = parent
    while host is not None and not isinstance(host, HostNode):
        host = host.parent
    if host is None or host.ns is None:
        return None
    if host.ns == "svg" and host.type == "foreignObject":
        return None
    return host.ns


class ComponentNode:
    """A component, fragment or context provider: it owns children but no DOM node."""

    __slots__ = (
        "type", "key", "parent", "root", "element", "depth", "children", "hooks",
        "hook_index", "pending_effects", "dirty", "mounted", "value", "subscribers",
        "subscriptions", "catch", "suspend",
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
        # Set by a Suspense boundary: told when a descendant waits for data.
        self.suspend: Callable[[Any], None] | None = None

    @property
    def name(self) -> str:
        return getattr(self.type, "__name__", str(self.type))


class Event(dict):
    """Event data from the browser. Keys read as attributes: ``e.value``, ``e.target.checked``.

    A missing key reads as None, since which keys arrive depends on the event type.
    """

    def __getattr__(self, name: str) -> Any:
        value = self.get(name)
        return Event(value) if type(value) is dict else value


class FormFields(dict):
    """A submitted form's fields: ``e.form["email"]`` is a field's value (the last,
    when several share the name) and ``e.form.getlist("tags")`` all of them, for
    checkbox groups and multi-selects."""

    def __init__(self, pairs: Iterable[Any] = ()) -> None:
        super().__init__()
        self._lists: dict[str, list[str]] = {}
        for pair in pairs:
            if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str):
                key, value = pair[0], str(pair[1])
                self[key] = value
                self._lists.setdefault(key, []).append(value)

    def getlist(self, key: str) -> list[str]:
        return list(self._lists.get(key, []))


class UploadedFile:
    """A file the person picked, in ``e.files`` of a file input's event.

    ``.data`` is its bytes and ``.text()`` its text; for large files read ``.file``
    (a binary file object, spooled to disk past 1 MiB) or ``.save(path)``, which
    copy it without loading it whole. ``error`` is set, and the file empty, when
    it was refused (too large).
    """

    __slots__ = ("name", "type", "size", "error", "file", "_data")

    def __init__(
        self,
        name: str,
        type: str,
        data: bytes = b"",
        error: str | None = None,
        size: int | None = None,
        file: Any = None,
    ) -> None:
        self.name = name
        self.type = type
        self.error = error
        self.file = file if file is not None else io.BytesIO(data)
        self._data: bytes | None = None if file is not None else data
        self.size = (len(data) if file is None else 0) if size is None else size

    @property
    def data(self) -> bytes:
        if self._data is None:
            self.file.seek(0)
            self._data = self.file.read()
        return self._data

    def text(self, encoding: str = "utf-8") -> str:
        return self.data.decode(encoding)

    def save(self, path: Any) -> None:
        """Write the file to ``path``, streaming it."""
        self.file.seek(0)
        with open(path, "wb") as out:
            shutil.copyfileobj(self.file, out)

    def __repr__(self) -> str:
        return f"<UploadedFile {self.name!r} {self.size} bytes{' ' + self.error if self.error else ''}>"


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


class Reply:
    """The browser's answer to an element call: ``await`` it in an ``async`` handler,
    or call ``.result()`` in a plain one (which runs in a worker thread). Ignoring
    it is fine when you only want the call made."""

    __slots__ = ("_future", "_loop_thread")

    def __init__(self, future: concurrent.futures.Future[Any], loop_thread: int | None) -> None:
        self._future = future
        self._loop_thread = loop_thread

    def __await__(self) -> Any:
        return asyncio.wrap_future(self._future).__await__()

    def result(self, timeout: float | None = 5.0) -> Any:
        if not self._future.done() and threading.get_ident() == self._loop_thread:
            raise RuntimeError("waiting here would block the event loop; use 'await' in an async handler")
        return self._future.result(timeout)

    def done(self) -> bool:
        return self._future.done()


class ElementHandle:
    """What a ``ref`` attached to an element holds: acts on the element in the browser
    and reads values back from it."""

    __slots__ = ("_root", "id")

    def __init__(self, root: Root, node_id: int) -> None:
        self._root = root
        self.id = node_id

    def call(self, method: str, *args: Any) -> Reply:
        """Call a method of the DOM element and get its result, e.g.
        ``rect = await ref.current.call("getBoundingClientRect")``. Results come
        back as JSON values; a promise is awaited first."""
        return self._root.command(self.id, method, list(args))

    def get(self, name: str) -> Reply:
        """Read a property: ``await ref.current.get("scrollTop")``."""
        return self._root.command(self.id, "#get", [name])

    def set(self, name: str, value: Any) -> Reply:
        """Set a property: ``ref.current.set("scrollTop", 0)``."""
        return self._root.command(self.id, "#set", [name, value])

    def focus(self) -> Reply:
        return self.call("focus")

    def blur(self) -> Reply:
        return self.call("blur")

    def click(self) -> Reply:
        return self.call("click")

    def select(self) -> Reply:
        return self.call("select")

    def scroll_into_view(self, behavior: str = "auto", block: str = "start") -> Reply:
        return self.call("scrollIntoView", {"behavior": behavior, "block": block})

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


def _flatten_items(
    items: Iterable[Any], prefix: tuple[Any, ...], out: list[tuple[Any, Any]], unkeyed: list[int] | None = None
) -> None:
    """Children as (key, item) pairs. Keys are scoped to the list they came from, and
    None/False holes keep their index, so a conditional child never shifts its siblings.

    Elements without a key inside a list (not the element's direct arguments) are
    counted into ``unkeyed``, for the debug warning."""
    for i, item in enumerate(items):
        if item is None or item is True or item is False:
            continue
        if isinstance(item, Element):
            if item.key is not None:
                key = (prefix, "$", item.key)
            else:
                key = (*prefix, i)
                if prefix and unkeyed is not None:
                    unkeyed.append(i)
            out.append((key, item))
        elif isinstance(item, str):
            out.append(((*prefix, i), item))
        elif isinstance(item, (int, float)):
            out.append(((*prefix, i), str(item)))
        elif isinstance(item, (list, tuple)) or inspect.isgenerator(item):
            _flatten_items(item, (*prefix, i), out, unkeyed)
        else:
            out.append(((*prefix, i), str(item)))


class Root:
    """One live UI: the mounted tree, its pending updates, and the operations they produce.

    ``location`` is the page URL (path and query), ``connection`` the request or
    WebSocket it serves (read with ``use_request``), and ``debug`` sends errors to
    the browser as well as the log.

    With ``threaded`` (the default), synchronous event handlers and effects run in
    a worker thread, one at a time and in order, so a slow one (a database call)
    does not freeze every other session on the event loop. ``async def`` ones run
    on the loop as tasks.
    """

    def __init__(
        self,
        element: Element,
        *,
        location: str = "/",
        connection: Any = None,
        debug: bool = False,
        threaded: bool = True,
    ) -> None:
        self.container = HostNode(0, Element("#root"), None, None)
        self.hosts: dict[int, HostNode] = {}
        self.ops: list[list[Any]] = []
        self.location = Location(location)
        self.connection = connection
        self.debug = debug
        self.threaded = threaded
        # Called with (exc, where) for every error not caught by an ErrorBoundary.
        self.on_error: Callable[[BaseException, str], None] | None = None
        self.title: str | None = None
        # For the server's first response: the HTTP status, or a URL to redirect to.
        self.status = 200
        self.redirect: str | None = None
        self.ack = 0
        self._title_dirty = False
        # Tags each Head component puts into <head>, in the order they rendered.
        self._head: dict[ComponentNode, tuple[Element, ...]] = {}
        self._head_dirty = False
        # Where a list without keys was already reported (debug mode), once each.
        self._warned_unkeyed: set[Any] = set()
        self._nav: dict[str, Any] | None = None
        self._errors: list[str] = []
        self._location_subs: set[ComponentNode] = set()
        # Files received ahead of the event that refers to them, by upload id.
        self.uploads: dict[str, UploadedFile] = {}
        self._ids = itertools.count(1)
        # For use_id: handed out in render order, the same on the server and live.
        self.next_id = itertools.count(1)
        # Element calls waiting for the browser's answer, by reply id.
        self._replies: dict[int, concurrent.futures.Future[Any]] = {}
        self._reply_ids = itertools.count(1)
        self._dirty: list[tuple[int, int, ComponentNode]] = []
        self._seq = itertools.count()
        self._effects: list[EffectSlot] = []
        self._flush_scheduled = False
        self._flushing = False
        self._sink: Callable[[dict[str, Any]], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: int | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        # Work started by background(): what settle() waits for.
        self._pending: set[asyncio.Task[Any]] = set()
        # Events and effect batches, run in order by one worker once attached.
        self._queue: asyncio.Queue[tuple[str, Any]] | None = None
        self._busy = False
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
        self._queue = asyncio.Queue()
        self.spawn(self._work())
        # Operations so far only built the tree; the snapshot replaces them.
        # Element commands made before now still have to run.
        commands = [op for op in self.ops if op[0] == "m"]
        self.ops = []
        if self.title is not None:
            self._title_dirty = True
        if self._head:
            self._head_dirty = True
        message = self._message(self.snapshot() + commands)
        message["init"] = "hydrate" if hydrate else "replace"
        self._sink = sink
        sink(message)
        self._run_effects()
        if self._dirty or self.ops or self._nav or self._errors or self._title_dirty or self._head_dirty:
            self.flush()

    def close(self) -> None:
        """Unmount everything, running effect cleanups and cancelling tasks."""
        self._sink = None
        for child in self.container.children:
            self._unmount(child, emit=False)
        self.container.children = []
        for task in list(self._tasks):
            task.cancel()
        replies, self._replies = self._replies, {}
        for future in replies.values():
            if not future.done():
                future.set_exception(ConnectionError("the page closed"))

    def snapshot(self) -> list[list[Any]]:
        """Operations that build the current tree from nothing, in document order.

        Portals come last, after the page's own tree, since their content lives
        elsewhere in the document and is not part of the server's HTML.
        """
        ops: list[list[Any]] = []
        portals: list[HostNode] = []

        def walk(host: HostNode) -> None:
            for node in self._dom_nodes(host.children, portals):
                if isinstance(node, TextNode):
                    ops.append(["t", node.id, node.text])
                    continue
                if isinstance(node.type, JsComponent):
                    ops.append(self._js_create(node))
                else:
                    ops.append(self._create_op(node))
                walk(node)
                if node.dom:
                    ops.append(["A", node.id, node.dom])

        walk(self.container)
        ops.append(["A", 0, self.container.dom])
        while portals:
            node = portals.pop(0)
            ops.append(["P", node.id, node.attrs["target"]])
            walk(node)
            if node.dom:
                ops.append(["A", node.id, node.dom])
        return ops

    # ---- scheduling ------------------------------------------------------------

    def call(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on this root's event loop; state setters go through here."""
        if self._loop is not None and threading.get_ident() != self._thread:
            seq = getattr(_handling, "seq", None)
            if seq is None:
                self._loop.call_soon_threadsafe(fn)
            else:
                # From an event handler in a worker thread: acknowledge the event in
                # the same step, so the render this update causes carries the ack.
                def apply() -> None:
                    self._ack(seq)
                    fn()

                self._loop.call_soon_threadsafe(apply)
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
        if self._head_dirty:
            message["head"] = self.head_html()
            self._head_dirty = False
        return message

    def head_html(self) -> str:
        """The tags Head components asked for, as HTML for the page's <head>."""
        from .ssr import render_head

        return render_head([el for elements in self._head.values() for el in elements])

    def _run_effects(self) -> None:
        # Each effect runs the function from the render that queued it, even when
        # later renders replace it before a queued batch runs.
        effects = [(slot, slot.fn) for slot in self._effects]
        self._effects = []
        if not effects:
            return
        if self._queue is not None:
            self._queue.put_nowait(("effects", effects))
            return
        for slot, fn in effects:
            if not slot.node.mounted:
                continue
            self._cleanup(slot)
            try:
                result = fn()  # type: ignore[misc]
            except Exception as exc:
                self.report(exc, f"an effect in {slot.node.name}")
                continue
            self._keep_effect_result(slot, result)

    def _keep_effect_result(self, slot: EffectSlot, result: Any) -> None:
        if not slot.node.mounted:
            # Unmounted while the effect ran in a thread: clean up at once.
            if inspect.iscoroutine(result):
                result.close()
            elif callable(result):
                slot.cleanup = result
                self._cleanup(slot)
            return
        if inspect.iscoroutine(result):
            task = self.spawn(result)
            slot.cleanup = task.cancel
        elif callable(result):
            slot.cleanup = result

    async def _run_effects_async(self, effects: list[tuple[EffectSlot, Any]]) -> None:
        for slot, fn in effects:
            if not slot.node.mounted:
                continue
            self._cleanup(slot)
            try:
                if self.threaded and not inspect.iscoroutinefunction(fn):
                    result = await asyncio.to_thread(fn)
                else:
                    result = fn()
            except Exception as exc:
                self.report(exc, f"an effect in {slot.node.name}")
                continue
            self._keep_effect_result(slot, result)

    async def _work(self) -> None:
        """Run queued events and effect batches one at a time, in arrival order."""
        assert self._queue is not None
        while True:
            kind, job = await self._queue.get()
            self._busy = True
            try:
                if kind == "event":
                    await self._dispatch_async(*job)
                else:
                    await self._run_effects_async(job)
            except Exception as exc:  # never let one bad job stop the session
                self.report(exc, "the event worker")
            finally:
                self._busy = False

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

    def background(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...] = (),
        done: Callable[[Any, BaseException | None], None] | None = None,
        after: asyncio.Task[Any] | None = None,
    ) -> asyncio.Task[Any] | None:
        """Run ``fn(*args)`` without holding up rendering, then ``done(result, error)``
        on the event loop. ``async`` functions run as tasks; others in a worker thread
        when ``threaded``. ``after`` waits for an earlier job first, to keep order.

        Call on the root's loop (from a worker thread, go through :meth:`call`).
        Outside any running loop (a plain unit test) a sync ``fn`` runs at once.
        """
        try:
            running = self._loop or asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is None:
            error: BaseException | None
            try:
                result, error = fn(*args), None
                if inspect.iscoroutine(result):
                    result.close()
                    result, error = None, RuntimeError("async work needs a running event loop")
            except Exception as exc:
                result, error = None, exc
            if done is not None:
                done(result, error)
            return None

        async def job() -> Any:
            if after is not None and not after.done():
                await asyncio.wait([after])
            try:
                if inspect.iscoroutinefunction(fn) or not self.threaded:
                    result = fn(*args)
                else:
                    result = await asyncio.to_thread(fn, *args)
                if inspect.iscoroutine(result):
                    result = await result
                error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result, error = None, exc
            if done is not None:
                done(result, error)
            return result

        task = self.spawn(job())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def settle(self, timeout: float = 5.0) -> bool:
        """Wait until queued events, effects, background work (data loads, actions,
        async handlers) are done and their updates rendered. False if ``timeout``
        (seconds) ran out first. Long-running async effects are not waited for."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._dirty:
                self._render_dirty() if self._sink is None else self.flush()
            queued = self._queue is not None and (not self._queue.empty() or self._busy)
            if not self._pending and not queued:
                await asyncio.sleep(0)  # done-callbacks and thread hand-offs land
                if not self._dirty and not self._pending and not (
                    self._queue is not None and (not self._queue.empty() or self._busy)
                ):
                    return True
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if self._pending:
                await asyncio.wait(set(self._pending), timeout=min(remaining, 0.05))
            else:
                await asyncio.sleep(0.005)
            await asyncio.sleep(0)

    def report(self, exc: BaseException, where: str) -> None:
        """Log an error, pass it to ``on_error``, and in debug mode show it in the browser."""
        logger.error("Error in %s", where, exc_info=exc)
        if self.on_error is not None:
            try:
                self.on_error(exc, where)
            except Exception:
                logger.exception("on_error failed")
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

    def set_head(self, node: ComponentNode, elements: tuple[Element, ...]) -> None:
        if self._head.get(node) != elements:
            self._head[node] = elements
            self._head_dirty = True
            self.request_flush()

    def set_location(self, url: str) -> None:
        """The browser moved to ``url`` (a link or the back button): re-render what reads it."""
        location = Location(url)
        if location == self.location:
            return
        self.location = location
        for node in list(self._location_subs):
            self.schedule(node)

    def navigate(self, url: str, replace: bool = False, reload: bool = False) -> None:
        """Go to ``url`` without reloading the page; with ``reload``, load it afresh
        from the server (after login or logout, when cookies changed)."""

        def apply() -> None:
            if reload:
                self._nav = {"to": url, "replace": replace, "reload": True}
            else:
                self.set_location(url)
                self._nav = {"to": str(self.location), "replace": replace}
            self.request_flush()

        self.call(apply)

    def subscribe_location(self, node: ComponentNode) -> None:
        self._location_subs.add(node)

    def command(self, node_id: int, method: str, args: list[Any]) -> Reply:
        """Ask the browser to call ``method`` on an element; the reply carries its result."""
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        reply_id = next(self._reply_ids)

        def apply() -> None:
            if node_id not in self.hosts:
                future.set_exception(LookupError("the element is no longer on the page"))
                return
            self._replies[reply_id] = future
            self.ops.append(["m", node_id, method, args, reply_id])
            self.request_flush()

        self.call(apply)
        return Reply(future, self._thread)

    def _answer(self, data: dict[str, Any]) -> None:
        future = self._replies.pop(data.get("r"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if isinstance(data.get("error"), str):
            future.set_exception(RuntimeError(data["error"]))
        else:
            future.set_result(data.get("value"))

    # ---- events ----------------------------------------------------------------

    def dispatch(self, seq: int, node_id: int, name: str, data: dict[str, Any]) -> None:
        """Deliver a browser event. ``seq`` is acknowledged once the handler finishes,
        which tells the browser that edits it sent before then are reflected.

        Once attached, events queue and run one at a time, each seeing the page as
        the previous one left it; before that (tests, server rendering) they run now.
        """
        if node_id == 0 and name == "#reply":
            # Never queued: the handler waiting for this answer may be the one
            # holding the queue's worker.
            self._answer(data)
            return
        if self._queue is not None:
            self._queue.put_nowait(("event", (seq, node_id, name, data)))
            return
        call = self._resolve(seq, node_id, name, data)
        if call is None:
            return
        try:
            result = call()
        except Exception as exc:
            self.report(exc, f"the {name!r} handler")
            self._ack(seq)
            return
        self._after_handler(seq, result)

    async def _dispatch_async(self, seq: int, node_id: int, name: str, data: dict[str, Any]) -> None:
        call = self._resolve(seq, node_id, name, data)
        if call is None:
            return
        try:
            if self.threaded and not inspect.iscoroutinefunction(call.fn):  # type: ignore[attr-defined]
                loop = self._loop
                assert loop is not None

                def run() -> Any:
                    _handling.seq = seq
                    try:
                        result = call()
                    finally:
                        _handling.seq = None
                    if not inspect.iscoroutine(result):
                        loop.call_soon_threadsafe(self._ack, seq)
                    return result

                result = await asyncio.to_thread(run)
            else:
                result = call()
        except Exception as exc:
            self.report(exc, f"the {name!r} handler")
            self._ack(seq)
            return
        self._after_handler(seq, result)

    def _resolve(self, seq: int, node_id: int, name: str, data: dict[str, Any]) -> Callable[[], Any] | None:
        """The handler call for an event, or None when there is nothing to run."""
        if node_id == 0:
            self._special(name, data)
            self._ack(seq)
            return None
        files = data.get("files")
        if isinstance(files, list):
            # Taken out of storage now, whether or not a handler wants them.
            data["files"] = [self._take_upload(f) for f in files if isinstance(f, dict)]
        form = data.get("form")
        if isinstance(form, list):
            data["form"] = FormFields(form)
        node = self.hosts.get(node_id)
        handler = node.events.get(name) if node is not None else None
        fn = handler.fn if handler is not None else None
        if fn is None:
            self._ack(seq)
            return None
        event = Event(data)
        wants = _wants_event(fn)

        def call() -> Any:
            return fn(event) if wants else fn()

        call.fn = fn  # type: ignore[attr-defined]
        return call

    def _special(self, name: str, data: dict[str, Any]) -> None:
        """Messages from the client script itself, not from an element."""
        if name == "#navigate":
            to = data.get("to")
            if isinstance(to, str) and to.startswith("/") and not to.startswith("//"):
                self.set_location(to)
        elif name == "#mismatch":
            # The server's HTML differed from the live render, so the browser rebuilt
            # the page: usually a render reading the clock or a random number.
            message = f"the server's HTML for {self.location} did not match the live render; the page was rebuilt"
            logger.warning(message)
            if self.debug:
                self._errors.append("Warning: " + message + " (does a component read the time or a random number?)")
                self.request_flush()
        # "#ping" keeps proxies from closing an idle connection; nothing to do.

    def _after_handler(self, seq: int, result: Any) -> None:
        if inspect.iscoroutine(result):
            task = self.spawn(result)
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            task.add_done_callback(lambda _: self._ack(seq))
        else:
            self._ack(seq)

    def _ack(self, seq: int) -> None:
        if seq > self.ack:
            self.ack = seq

    def _take_upload(self, meta: dict[str, Any]) -> UploadedFile:
        name = str(meta.get("name", ""))
        type_ = str(meta.get("type", ""))
        size = meta.get("size") if isinstance(meta.get("size"), int) else 0
        upload_id = meta.get("id")
        stored = self.uploads.pop(upload_id, None) if isinstance(upload_id, str) else None
        if stored is not None:
            return stored
        error = meta.get("error") if isinstance(meta.get("error"), str) else "not received"
        return UploadedFile(name, type_, b"", error=error, size=size)

    # ---- reconciliation --------------------------------------------------------

    def _reconcile(self, parent: Any, old: list[Any], items: Iterable[Any]) -> list[Any]:
        pairs: list[tuple[Any, Any]] = []
        unkeyed: list[int] | None = [] if self.debug else None
        _flatten_items(items, (), pairs, unkeyed)
        if unkeyed and len(unkeyed) > 1:
            self._warn_unkeyed(parent)
        by_key = {child.key: child for child in old}
        result: list[Any] = []
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
        if isinstance(item.type, JsComponent) or item.type == PORTAL:
            host = HostNode(next(self._ids), item, key, parent)
            self.hosts[host.id] = host
            if item.type == PORTAL:
                host.ns = None
                self.ops.append(["P", host.id, item.attrs["target"]])
            else:
                self.ops.append(self._js_create(host))
            host.children = self._reconcile(host, [], item.children)
            host.dom = self._dom_ids(host.children)
            if host.dom:
                self.ops.append(["A", host.id, host.dom])
            if item.ref is not None:
                item.ref.current = ElementHandle(self, host.id)
            return host
        if isinstance(item.type, str) and item.type != FRAGMENT:
            host = HostNode(next(self._ids), item, key, parent)
            self.hosts[host.id] = host
            self.ops.append(self._create_op(host))
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
        if isinstance(node.type, JsComponent) or node.type == PORTAL:
            # A module gets all its props at once; callbacks are looked up on dispatch.
            if old_attrs != new_attrs:
                ops.append(["J", node.id, new_attrs])
            self._swap_ref(node, item)
            node.element = item
            node.attrs = new_attrs
            node.events = item.events
            node.children = self._reconcile(node, node.children, item.children)
            self._sync_dom(node)
            return
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
        self._swap_ref(node, item)
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
            except Suspended as waiting:
                # Waiting for data: show the nearest Suspense fallback meanwhile. The
                # hook re-renders this component when the data arrives.
                node.pending_effects.clear()
                boundary = self._boundary_for(node, "suspend")
                if boundary is not None:
                    boundary.suspend(waiting.task)  # type: ignore[misc]
                if node.children:
                    return
                out = None
            except Exception as exc:
                node.pending_effects.clear()
                boundary = self._boundary_for(node, "catch")
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
    def _boundary_for(node: ComponentNode, kind: str) -> ComponentNode | None:
        parent = node.parent
        while parent is not None:
            if isinstance(parent, ComponentNode) and getattr(parent, kind) is not None:
                return parent
            parent = parent.parent
        return None

    def _warn_unkeyed(self, parent: Any) -> None:
        owner = parent
        while owner is not None and not isinstance(owner, ComponentNode):
            owner = owner.parent
        name = owner.name if owner is not None else "the root"
        where = (name, getattr(parent, "type", None))
        if where in self._warned_unkeyed:
            return
        self._warned_unkeyed.add(where)
        logger.warning(
            "Items of a list in %s have no key: pass key= (an id), so reordering or removing "
            "one keeps the right state with the right item",
            name,
        )

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

    def _swap_ref(self, node: HostNode, item: Element) -> None:
        if node.element.ref is not item.ref:
            self._release_ref(node)
            if item.ref is not None:
                item.ref.current = ElementHandle(self, node.id)

    @staticmethod
    def _js_create(host: HostNode) -> list[Any]:
        js: JsComponent = host.type
        return ["j", host.id, js.tag, js.module, js.export, host.attrs, host.element.kwargs.get("fallback")]

    @staticmethod
    def _create_op(host: HostNode) -> list[Any]:
        op = ["c", host.id, host.type, host.attrs, {n: h.spec() for n, h in host.events.items()}]
        if host.ns is not None:
            op.append(host.ns)
        return op

    def _unmount(self, node: Any, emit: bool) -> None:
        if isinstance(node, TextNode):
            if emit:
                self.ops.append(["r", node.id])
            return
        if isinstance(node, HostNode):
            # A portal's content lives elsewhere in the page, so removing an
            # ancestor does not take it along: it is always removed itself.
            if emit or node.type == PORTAL:
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
            elif hasattr(slot, "dispose"):
                # Subscriptions and background work a hook started.
                try:
                    slot.dispose()
                except Exception as exc:
                    self.report(exc, f"unmounting {node.name}")
        for provider in node.subscriptions:
            provider.subscribers.discard(node)
        self._location_subs.discard(node)
        if self._head.pop(node, None) is not None:
            self._head_dirty = True

    # ---- tree helpers ----------------------------------------------------------

    @staticmethod
    def _dom_nodes(children: list[Any], portals: list[Any] | None = None) -> list[Any]:
        """The DOM-level nodes (hosts and texts) among ``children``, components
        flattened. Portals are left out (their DOM is elsewhere) and added to
        ``portals`` when given."""
        out: list[Any] = []
        stack = list(reversed(children))
        while stack:
            node = stack.pop()
            if isinstance(node, ComponentNode):
                stack.extend(reversed(node.children))
            elif isinstance(node, HostNode) and node.type == PORTAL:
                if portals is not None:
                    portals.append(node)
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
