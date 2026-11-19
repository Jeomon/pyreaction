"""The virtual DOM: the reconciler keeps the mounted tree and turns re-renders into
DOM operations; :func:`to_vdom` shows the tree as plain dicts.

Each DOM node the browser holds has an integer id. A render produces a flat list
of operations on those ids, so an update costs what changed, not what exists:

    ["c", id, tag, attrs, events, ns]   create an element (events: {name: spec}; trailing
                                    empty attrs and events, and ns when None, are left out)
    ["t", id, text]                 create a text node
    ["A", parent, [ids]]            append children to a new element
    ["a", parent, id, before]       insert or move a child before ``before`` (null: at the end)
    ["r", id]                       remove a node and everything under it
    ["p", id, name, value]          set an attribute or property (null removes it); "#text"
                                    is the element's text, for an element whose only child
                                    is a string or number (no text node of its own)
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
import gc
import heapq
import inspect
import io
import itertools
import logging
import shutil
import threading
import time
import traceback
import zlib
from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from operator import is_
from typing import Any
from urllib.parse import parse_qs

from .element import (
    FRAGMENT, PORTAL, PROPERTY_PROPS, RAW_HTML, RAW_MARK, TEXT, Component, Element, JsComponent,
    style_changes,
)
from .hooks import ACTIVE_ROOT, BROWSER_ONLY, CURRENT, CURRENT_ACTION, BrowserOnly, Context, EffectSlot, Suspended, same

logger = logging.getLogger("reaction")

# Renders of one component within one pass before it counts as an endless loop
# (a component that sets state every time it renders), as React does.
MAX_RENDERS = 50


def _too_many_renders(name: str) -> RuntimeError:
    return RuntimeError(
        f"Too many re-renders in {name}: it sets state every time it renders. "
        "Set state in an event handler or effect; pass the handler itself "
        "(on_click=vote), not its result (on_click=vote())."
    )

__all__ = ["ElementHandle", "Event", "FormFields", "Location", "Reply", "Root", "UploadedFile"]

_SKIP = object()

# Attributes the browser sets as live properties, which the server's HTML cannot
# always carry (a <select>'s value, a textarea's): sent even when adopting the HTML.
_LIVE_PROPS = PROPERTY_PROPS | {"defaultValue", "defaultChecked"}

# The event a worker thread is handling, so state updates it makes carry its ack.
_handling = threading.local()

# A render creates many objects that all live on (nodes, ops, elements), so the
# cyclic garbage collector, triggered by allocation counts, rescans them again and
# again for nothing: over half of a large mount's time. It is paused while a render
# pass runs and resumes after it, where it runs as usual.
_gc_lock = threading.Lock()
_gc_pauses = 0
_gc_resume = False


@contextmanager
def _gc_paused() -> Iterator[None]:
    global _gc_pauses, _gc_resume
    with _gc_lock:
        if _gc_pauses == 0:
            _gc_resume = gc.isenabled()
            gc.disable()
        _gc_pauses += 1
    try:
        yield
    finally:
        with _gc_lock:
            _gc_pauses -= 1
            if _gc_pauses == 0 and _gc_resume:
                gc.enable()


class TextNode:
    __slots__ = ("id", "text", "key", "parent")
    type = str
    memo = False

    def __init__(self, id: int, text: str, key: Any, parent: Any) -> None:
        self.id = id
        self.text = text
        self.key = key
        self.parent = parent


class HostNode:
    __slots__ = (
        "id", "type", "key", "parent", "element", "attrs", "events", "children", "dom", "depth", "ns",
        "ref_cleanup", "text", "reshaped",
    )
    memo = False

    def __init__(self, id: int, element: Element, key: Any, parent: Any) -> None:
        self.id = id
        self.type = element.type
        self.key = key
        self.parent = parent
        self.element = element
        self.attrs = element.attrs
        self.events = element.events
        self.ref_cleanup: Callable[[], Any] | None = None
        self.children: Any = ()  # a list once it has child nodes
        self.dom: Any = ()
        self.depth = parent.depth + 1 if parent is not None else 0
        self.ns = _namespace(self.type, parent)
        # Its only child as text, kept here instead of as a TextNode (None: it has children).
        self.text: str | None = None
        # Its DOM children may have changed order or membership in this pass (a child
        # list under it, or under a component inside it, reshaped): see Root._mark_reshaped.
        self.reshaped = False


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
        "subscriptions", "catch", "suspend", "waiting", "hook_count", "strict", "profile", "view_transition",
        "memo",
    )

    def __init__(self, root: Root, element: Element, key: Any, parent: Any) -> None:
        self.type = element.type
        self.key = key
        self.parent = parent
        self.root = root
        self.element = element
        self.depth = parent.depth + 1
        # A memo component: a parent's render may skip it when its props are the same.
        self.memo = type(element.type) is Component and element.type.memo
        self.children: list[Any] = []
        self.hooks: Any = ()  # a list from the first hook on
        self.hook_index = 0
        self.pending_effects: Any = ()
        self.dirty = False
        self.mounted = True
        self.value: Any = element.kwargs.get("value") if isinstance(element.type, Context) else None
        # Context links, made on first use: most components have none.
        self.subscribers: set[ComponentNode] | None = None
        self.subscriptions: list[ComponentNode] | None = None
        # Set by an error boundary: receives errors thrown while rendering its descendants.
        self.catch: Callable[[BaseException], None] | None = None
        # Set by a Suspense boundary: told when a descendant waits for data, and
        # whether it is showing its fallback right now.
        self.suspend: Callable[[Any], None] | None = None
        self.waiting = False
        # Hooks the last complete render called: every render must call as many.
        self.hook_count: int | None = None
        # Set by StrictMode: its descendants render twice and set up effects twice.
        self.strict = False
        # Set by Profiler: (id, on_render).
        self.profile: tuple[Any, Callable[..., Any]] | None = None
        # Set by ViewTransition: updates under it are animated by the browser.
        self.view_transition = False

    @property
    def name(self) -> str:
        return getattr(self.type, "__name__", str(self.type))


class Event(dict):
    """Event data from the browser. Keys read as attributes: ``e.value``, ``e.target.checked``.

    A missing key reads as None, since which keys arrive depends on the event type.
    For anything that was not sent, ask the browser while the event is recent
    (about a minute): ``await e.read("dataTransfer.files")``,
    ``await e.target.read("scrollTop")``, ``await e.call("target.getBoundingClientRect")``.
    """

    def __init__(self, data: Any = (), root: Root | None = None, seq: int | None = None, path: str = "") -> None:
        super().__init__(data)
        self._root = root
        self._seq = seq
        self._path = path

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        value = self.get(name)
        if value is None and name == "target" and not self._path:
            value = {}  # always there, so e.target.read(...) works
        if type(value) is dict:
            return Event(value, self._root, self._seq, f"{self._path}{name}.")
        return value

    def read(self, path: str = "") -> Reply:
        """The value of ``path`` (dotted, from this event, or from ``e.target`` for
        ``e.target.read(...)``) in the browser: ``await e.read("dataTransfer.types")``
        in an ``async`` handler, ``.result()`` in a plain one. Values come back as
        JSON (a file as its name, type and size; a list as its items)."""
        return self._ask(path, [], False)

    def call(self, path: str, *args: Any) -> Reply:
        """Call the method at ``path`` with ``args`` and get its result:
        ``await e.call("target.getBoundingClientRect")``. The browser has finished
        with the event by then, so calls such as ``preventDefault`` do nothing; use
        :func:`event` options for those."""
        return self._ask(path, list(args), True)

    def _ask(self, path: str, args: list[Any], call: bool) -> Reply:
        if self._root is None or self._seq is None:
            raise RuntimeError("this event did not come from a browser")
        return self._root.command(0, "#event", [self._seq, (self._path + path).rstrip("."), args, call])


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
    """The page's path and query string, as the app sees them. When the app is
    mounted under a path (``api.mount("/ui", create_app(App))``), ``path`` leaves
    that prefix out and ``base`` holds it: the browser shows ``base + path``."""

    __slots__ = ("path", "search", "base")

    def __init__(self, url: str = "/", base: str = "") -> None:
        path, sep, query = url.partition("?")
        self.path = path or "/"
        self.search = sep + query if query else ""
        self.base = base

    def href(self, to: str) -> str:
        """The URL the browser needs for the app's own path ``to`` (``/static/logo.png``
        or ``/users/7``): with the mount prefix in front. Other URLs are returned as is."""
        if to.startswith("/") and not to.startswith("//"):
            return self.base + to
        return to

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


class PageSignal:
    """What :func:`cache_signal` returns: tells work started for a page that the page
    has closed, as React's ``AbortSignal``. ``.aborted``, ``on_abort(fn)`` (``fn``
    is called at once if already aborted), and ``await signal.wait()``."""

    __slots__ = ("aborted", "_callbacks", "_lock")

    def __init__(self) -> None:
        self.aborted = False
        self._callbacks: list[Callable[[], Any]] = []
        self._lock = threading.Lock()

    def on_abort(self, fn: Callable[[], Any]) -> None:
        with self._lock:
            if not self.aborted:
                self._callbacks.append(fn)
                return
        fn()

    def abort(self) -> None:
        with self._lock:
            if self.aborted:
                return
            self.aborted = True
            callbacks, self._callbacks = self._callbacks, []
        for fn in callbacks:
            try:
                fn()
            except Exception:
                logger.exception("A cache_signal listener failed")

    async def wait(self) -> None:
        """Return once the page has closed."""
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def finish() -> None:
            if not done.done():
                done.set_result(None)

        def wake() -> None:
            loop.call_soon_threadsafe(finish)

        self.on_abort(wake)
        await done

    def __repr__(self) -> str:
        return f"PageSignal(aborted={self.aborted})"


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
    """Whether a handler takes the event: it has a parameter without a default.

    Parameters with defaults are left alone, so the usual way to capture a loop
    variable, ``on_click=lambda r=r: vote(r.id)``, keeps its ``r``."""
    code = getattr(fn, "__code__", None)
    if code is not None:
        n = code.co_argcount - len(getattr(fn, "__defaults__", None) or ())
        if inspect.ismethod(fn):
            n -= 1
        return n > 0 or bool(code.co_flags & inspect.CO_VARARGS)
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        for p in params
    )


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


_NO_PROP: Any = object()


def _same_props(new: Element, old: Element) -> bool:
    """Whether a memo component can skip rendering: every prop ``same`` as before.

    Props passed through unchanged are the same objects, so one identity pass in C
    (``map(is_, ...)``) settles most calls; ``same`` runs only when it fails."""
    new_args, old_args = new.args, old.args
    if new_args is not old_args:
        if len(new_args) != len(old_args):
            return False
        if not all(map(is_, new_args, old_args)):
            for a, b in zip(new_args, old_args):
                if a is not b and not same(a, b):
                    return False
    new_kwargs, old_kwargs = new.kwargs, old.kwargs
    if new_kwargs is old_kwargs:  # both the shared empty dict: positional props only
        return True
    if len(new_kwargs) != len(old_kwargs):
        return False
    for name, value in new_kwargs.items():
        before = old_kwargs.get(name, _NO_PROP)
        if value is not before and (before is _NO_PROP or not same(value, before)):
            return False
    return True


def _only_text(item: Element) -> str | None:
    """The text of a tag whose only child is a string or number, else None.

    Such a tag holds its text itself (its textContent, as React does), which
    saves a text node, its id and two operations per element. A <textarea>'s
    text is its value, so it keeps the general path."""
    children = item.children
    if len(children) != 1 or item.type == "textarea":
        return None
    child = children[0]
    kind = type(child)
    if kind is str:
        return child
    if kind is int or kind is float:
        return str(child)
    return None

# Keys of unkeyed children in a top-level child list, made once: ("#", index).
# Keyed children there use their key itself (a tuple key is wrapped as ("$", key)),
# and children of nested lists keep tuple keys starting with ints or a tuple, so
# the three kinds never collide.
_POSITIONS = tuple(("#", i) for i in range(256))


def _flatten_items(
    items: Iterable[Any], prefix: tuple[Any, ...], out: list[tuple[Any, Any]], unkeyed: list[int] | None = None,
    in_list: bool = False,
) -> None:
    """Children as (key, item) pairs. Keys are scoped to the list they came from, and
    None/False holes keep their index, so a conditional child never shifts its siblings.

    Elements without a key inside a list (not the element's direct arguments) are
    counted into ``unkeyed``, for the debug warning."""
    # Exact types first (the common cases, and cheaper than isinstance); text always
    # comes out as a plain str, so later steps can test ``type(item) is str``.
    top = not prefix
    positions = _POSITIONS
    for i, item in enumerate(items):
        if item is None or item is True or item is False:
            continue
        kind = type(item)
        if top:
            pos = positions[i] if i < 256 else ("#", i)
        else:
            pos = (*prefix, i)
        if kind is Element or (kind is not str and isinstance(item, Element)):
            k = item.key
            if k is not None:
                if top:
                    out.append((k if type(k) is not tuple else ("$", k), item))
                else:
                    out.append(((prefix, "$", k), item))
                continue
            if (prefix or in_list) and unkeyed is not None:
                unkeyed.append(i)
            out.append((pos, item))
        elif kind is str:
            out.append((pos, item))
        elif isinstance(item, str):
            out.append((pos, str(item)))
        elif isinstance(item, (int, float)):
            out.append((pos, str(item)))
        elif isinstance(item, (list, tuple)) or inspect.isgenerator(item):
            _flatten_items(item, (*prefix, i), out, unkeyed)
        else:
            out.append((pos, str(item)))


class Root:
    """One live UI: the mounted tree, its pending updates, and the operations they produce.

    ``location`` is the page URL (path and query), ``connection`` the request or
    WebSocket it serves (read with ``use_request``), and ``debug`` sends errors to
    the browser as well as the log.

    With ``threaded`` (the default), synchronous event handlers and effects run in
    a worker thread, one at a time and in order, so a slow one (a database call)
    does not freeze every other session on the event loop. ``async def`` ones run
    on the loop as tasks.

    ``ops=False`` is for a render made before any browser is connected (the server's
    first HTML): mounting then builds no create operations, which nothing would
    read, since a browser that connects gets a snapshot of the tree. They start
    with :meth:`attach`.
    """

    def __init__(
        self,
        element: Element,
        *,
        location: str = "/",
        connection: Any = None,
        debug: bool = False,
        threaded: bool = True,
        on_error: Callable[[BaseException, str], None] | None = None,
        base: str = "",
        ops: bool = True,
    ) -> None:
        self.container = HostNode(0, Element("#root"), None, None)
        self.hosts: dict[int, HostNode] = {}
        self.ops: list[list[Any]] = []
        # Whether mounting records create operations (see ``ops`` above).
        self._emit = ops
        # Where the app is mounted ("/ui"), or "" at the root of the site.
        self.base = base.rstrip("/")
        self.location = Location(location, self.base)
        self.connection = connection
        self.debug = debug
        self.threaded = threaded
        # Called with (exc, where) for every error not caught by an ErrorBoundary.
        # Given here so errors in the first render reach it too.
        self.on_error = on_error
        self.title: str | None = None
        # For the server's first response: the HTTP status, or a URL to redirect to.
        self.status = 200
        # More headers for the first response (from an HTTPException's headers).
        self.headers: dict[str, str] = {}
        self.redirect: str | None = None
        # Navigate's paths are inside the app (the mount prefix goes first); an
        # HTTPException's Location is sent as written.
        self.redirect_as_is = False
        self.ack = 0
        self._title_dirty = False
        # Tags each Head component puts into <head>, in the order they rendered.
        self._head: dict[ComponentNode, tuple[Element, ...]] = {}
        self._head_dirty = False
        # Resource hints (preload, preinit, ...): added once each, kept for the page.
        self._resources: dict[tuple[str, str], Element] = {}
        # What cache() functions returned for this page, by (function, arguments).
        self.cache: dict[Any, Any] = {}
        # Profiler timings of the render pass under way: node -> [phase, ms, start].
        self._profiling = False
        # Whether this pass changed something under a ViewTransition, and the types
        # add_transition_type gave it.
        self._animate = False
        # Mounted ViewTransition components: while there are none, a dirty component
        # need not look up through its ancestors for one.
        self._view_transitions = 0
        # Mounted portals: while there are none, the page's HTML needs no portal boxes
        # and render_portals need not walk the tree looking for them.
        self.portals = 0
        self._transition_types: list[str] = []
        # Components that called browser() before the page was live, and how to make
        # the Suspense boundaries showing their fallbacks show them once it is.
        self._browser_only: set[ComponentNode] = set()
        self._browser_release: dict[ComponentNode, Callable[[], None]] = {}
        # Aborted when the page closes; what cache_signal() returns.
        self.signal = PageSignal()
        self._profiles: dict[ComponentNode, list[Any]] = {}
        # Where a list without keys was already reported (debug mode), once each.
        self._warned_unkeyed: set[Any] = set()
        self._nav: dict[str, Any] | None = None
        self._errors: list[str] = []
        self._location_subs: set[ComponentNode] = set()
        # Files received ahead of the event that refers to them, by upload id.
        self.uploads: dict[str, UploadedFile] = {}
        self._ids = itertools.count(1)
        # Bumped whenever a child list gains, loses or reorders a node. A pass that
        # Hosts whose DOM children reshaped (gained, lost or reordered a node) during
        # this pass and still need their DOM order synced; only those are recomputed.
        self._reshaped: list[HostNode] = []
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
        self._loop_thread: int | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        # Work started by background(): what settle() waits for.
        self._pending: set[asyncio.Task[Any]] = set()
        # The part of it a Suspense boundary waits for: streamable after the shell.
        self._suspense_tasks: set[asyncio.Task[Any]] = set()
        # Events and effect batches, run in order by one worker once attached.
        self._queue: asyncio.Queue[tuple[str, Any]] | None = None
        self.pending_events = 0  # browser events queued and not yet started
        self._busy = False
        # While the handlers of one native event run (several elements on its path),
        # renders wait, so the page renders once after all of them, as in React.
        self._holding = False
        self._flush_held = False
        # State updates from worker threads, waiting to run on the loop. A burst of them
        # wakes the loop once (see call), not once per update.
        self._from_threads: list[Callable[[], None]] = []
        self._from_threads_lock = threading.Lock()
        self._drain_scheduled = False
        # The last ack sent to the browser (see flush).
        self._ack_sent = 0
        # Set when this tree's HTML went to the browser unchanged (a handed-off GET
        # render): its first hydrate message can then leave out what the HTML holds.
        self._html_matches = False
        with _gc_paused():
            self.container.reshaped = True  # its order is computed whole below
            self.container.children = self._reconcile(self.container, [], (element,))
            self.container.dom = self._dom_ids(self.container.children)
            self.container.reshaped = False
        # An error boundary that caught during the first render re-renders now,
        # so the first paint already shows its fallback.
        self._render_dirty()
        self._report_profiles()

    # ---- lifecycle -------------------------------------------------------------

    def attach(self, sink: Callable[[dict[str, Any]], None], hydrate: bool = False) -> None:
        """Start sending: the whole tree goes out, then effects run and updates flow.

        Must be called from the event loop that will serve this root. After
        :meth:`detach` it can be called again, for a new connection to the same
        page: the components keep their state.
        """
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        if self._queue is None:
            self._queue = asyncio.Queue()
            self.spawn(self._work())
        self._render_dirty()
        self._sink = sink
        self._emit = True  # a browser from here on: it reads what changes
        self._send_tree("hydrate" if hydrate else "replace")
        self._run_effects()
        self._render_browser_only()
        if self._dirty or self.ops or self._nav or self._errors or self._title_dirty or self._head_dirty:
            self.flush()

    def _send_tree(self, init: str, rebuild: bool = False) -> None:
        """Send the whole tree: ``"replace"`` builds it in the browser; ``"hydrate"``
        lets the browser adopt the matching HTML it already has, so the structure
        (which node is inside which) is left out."""
        assert self._sink is not None
        # Operations so far only built the tree; the snapshot replaces them.
        # Element commands made before now still have to run, unless their caller
        # already gave up waiting for the answer.
        commands = [op for op in self.ops if op[0] == "m" and (op[4] is None or op[4] in self._replies)]
        # The browser's DOM is this tree's HTML when nothing has changed it since.
        compact = init == "hydrate" and self._html_matches and all(op[0] == "m" for op in self.ops)
        self._html_matches = False
        self.ops = []
        if self.title is not None:
            self._title_dirty = True
        if self._head or self._resources:
            self._head_dirty = True
        adopt = self._adopt_segments() if compact else None
        if adopt is not None:
            message = self._message(commands)
            message["adopt"] = adopt
        else:
            message = self._message(self.snapshot(structure=init != "hydrate") + commands)
        message["init"] = init
        if rebuild:
            message["rebuild"] = True  # the same session: the browser keeps its event numbers
        self._ack_sent = message["ack"]
        self._sink(message)

    def detach(self) -> None:
        """Stop sending: the connection dropped. State, queued events and background
        work carry on (effects wait), and :meth:`attach` resumes the page."""
        self._sink = None
        # The browser may never answer these: fail them so no handler waits forever.
        replies, self._replies = self._replies, {}
        for future in replies.values():
            if not future.done():
                future.set_exception(ConnectionError("the page disconnected"))

    def close(self) -> None:
        """Unmount everything, running effect cleanups and cancelling tasks."""
        self._sink = None
        self.signal.abort()
        for child in self.container.children:
            self._unmount(child, emit=False)
        self.container.children = []
        self.ops = []  # operations never sent, e.g. a first-HTML render's
        for task in list(self._tasks):
            task.cancel()
        replies, self._replies = self._replies, {}
        for future in replies.values():
            if not future.done():
                future.set_exception(ConnectionError("the page closed"))

    def snapshot(self, structure: bool = True) -> list[list[Any]]:
        """Operations that build the current tree from nothing, in document order.

        Portals come last, after the page's own tree, since their content lives
        elsewhere in the document and is not part of the server's HTML. Without
        ``structure`` the ``"A"`` operations that put nodes inside their parents are
        left out: enough for a browser adopting HTML that already has them.
        """
        with _gc_paused():  # allocates a list per node: collections here only slow it down
            ops = self._snapshot()
        return ops if structure else [op for op in ops if op[0] != "A"]

    def _snapshot(self) -> list[list[Any]]:
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

    def _adopt_segments(self) -> list[dict[str, Any]] | None:
        """A hydrate message for a browser whose HTML came from this very tree, as
        React's production hydration: only what the HTML cannot carry. One segment
        for the page, then one per portal (in the order the server numbered the
        portal boxes). Each has its nodes' ids in document order, each the difference
        from the one before times two, plus one for an element holding its own text
        (whose text node comes next and needs no id); a CRC-32 of the tag sequence
        ("#" for text) the browser checks its DOM against, catching HTML-parser
        fix-ups; and, only where present, event specs, properties the HTML cannot set
        (value, checked...) and js_component mounts.

        None when the browser could not line its DOM up with it: a <textarea> with
        child nodes, whose text the browser's walk does not enter."""
        if not self.portals:
            with _gc_paused():
                return self._adopt_page()
        segments: list[dict[str, Any]] = []
        portals: list[HostNode] = []
        unsupported = False

        def segment(top: HostNode) -> dict[str, Any]:
            ids: list[int] = []
            tags: list[str] = []
            events: list[list[Any]] = []
            props: list[list[Any]] = []
            mounts: list[list[Any]] = []
            prev = 0

            def walk(host: HostNode) -> None:
                nonlocal prev, unsupported
                for node in self._dom_nodes(host.children, portals):
                    step = (node.id - prev) * 2
                    prev = node.id
                    if type(node) is TextNode:
                        ids.append(step)
                        tags.append("#")
                        continue
                    if isinstance(node.type, JsComponent):
                        ids.append(step)
                        tags.append(node.type.tag)
                        mounts.append(self._js_create(node))
                        walk(node)
                        continue
                    tags.append(node.type)
                    if node.text:
                        ids.append(step + 1)
                        tags.append("#")
                    else:
                        ids.append(step)
                    if node.events:
                        events.append([node.id, {n: h.spec() for n, h in node.events.items()}])
                    set_live = {k: v for k, v in node.attrs.items() if k in _LIVE_PROPS}
                    if set_live:
                        props.append([node.id, set_live])
                    if node.type == "textarea":
                        unsupported = unsupported or bool(node.children)
                    else:
                        walk(node)

            walk(top)
            out: dict[str, Any] = {"ids": ids, "sig": zlib.crc32(",".join(tags).encode())}
            if events:
                out["ev"] = events
            if props:
                out["pr"] = props
            if mounts:
                out["js"] = mounts
            return out

        with _gc_paused():
            segments.append(segment(self.container))
            while portals:
                node = portals.pop(0)
                seg = segment(node)
                seg["box"] = node.id
                segments.append(seg)
        return None if unsupported else segments

    def _adopt_page(self) -> list[dict[str, Any]] | None:
        """``_adopt_segments`` for a page without portals (most pages): one loop in
        document order, with no list built per element. The same message."""
        ids: list[int] = []
        tags: list[str] = []
        events: list[list[Any]] = []
        props: list[list[Any]] = []
        mounts: list[list[Any]] = []
        add_id = ids.append
        add_tag = tags.append
        live_props = _LIVE_PROPS
        prev = 0
        stack: list[Any] = list(reversed(self.container.children))
        pop = stack.pop
        extend = stack.extend
        while stack:
            node = pop()
            kind = type(node)
            if kind is ComponentNode:
                extend(reversed(node.children))
                continue
            step = (node.id - prev) * 2
            prev = node.id
            if kind is TextNode:
                add_id(step)
                add_tag("#")
                continue
            tag = node.type
            if type(tag) is not str:  # a js_component: its box, mounted by its module
                add_id(step)
                add_tag(tag.tag)
                mounts.append(self._js_create(node))
                extend(reversed(node.children))
                continue
            add_tag(tag)
            if node.text:
                add_id(step + 1)
                add_tag("#")
            else:
                add_id(step)
            if node.events:
                events.append([node.id, {n: h.spec() for n, h in node.events.items()}])
            attrs = node.attrs
            if attrs and not live_props.isdisjoint(attrs):
                props.append([node.id, {k: v for k, v in attrs.items() if k in live_props}])
            if tag == "textarea":
                if node.children:
                    return None  # its text nodes are not in the browser's walk
            elif node.children:
                extend(reversed(node.children))
        out: dict[str, Any] = {"ids": ids, "sig": zlib.crc32(",".join(tags).encode())}
        if events:
            out["ev"] = events
        if props:
            out["pr"] = props
        if mounts:
            out["js"] = mounts
        return [out]

    # ---- scheduling ------------------------------------------------------------

    def call(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on this root's event loop; state setters go through here."""
        if self._loop is not None and threading.get_ident() != self._loop_thread:
            seq = getattr(_handling, "seq", None)
            if seq is not None:
                # From an event handler in a worker thread: acknowledge the event in
                # the same step, so the render this update causes carries the ack.
                update = fn

                def fn() -> None:
                    self._ack(seq)
                    update()

            # Queued, with one wake-up per burst. Order still holds: an update joins a
            # drain scheduled before it, or schedules one before anything this thread
            # sends the loop next (the ack, or _in_thread's "landed").
            with self._from_threads_lock:
                self._from_threads.append(fn)
                if self._drain_scheduled:
                    return
                self._drain_scheduled = True
            self._loop.call_soon_threadsafe(self._drain_from_threads)
        else:
            fn()

    def _drain_from_threads(self) -> None:
        with self._from_threads_lock:
            pending, self._from_threads = self._from_threads, []
            self._drain_scheduled = False
        for fn in pending:
            fn()

    def schedule(self, node: ComponentNode) -> None:
        if node.dirty or not node.mounted:
            return
        node.dirty = True
        heapq.heappush(self._dirty, (node.depth, next(self._seq), node))
        self.request_flush()

    def request_flush(self) -> None:
        """Send what is pending at the end of this event-loop tick."""
        if self._holding:
            self._flush_held = True
            return
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
        # An event handled with nothing to change still tells the browser so: its
        # controlled inputs then show the server's value again (a rejected keystroke).
        if ops or len(message) > 2 or message["ack"] > self._ack_sent:
            self._ack_sent = message["ack"]
            self._sink(message)
        self._run_effects()

    def _render_dirty(self) -> None:
        self._flushing = True
        try:
            if not self._dirty:
                return
            with _gc_paused():
                self._render_all_dirty()
        finally:
            self._flushing = False
        self._report_profiles()

    def _render_all_dirty(self) -> None:
        renders: dict[ComponentNode, int] = {}
        while self._dirty:
            _, _, node = heapq.heappop(self._dirty)
            if not node.dirty or not node.mounted:
                continue
            count = renders.get(node, 0) + 1
            renders[node] = count
            if count > MAX_RENDERS:
                # A component that sets state on every render would loop forever.
                node.dirty = False
                self._render_failed(node, _too_many_renders(node.name))
                continue
            if self._view_transitions and not self._animate:
                self._animate = self._under_view_transition(node)
            start = time.perf_counter() if self._profiling else 0.0
            self._render(node, node.element)
            if self._profiling:
                # The Profilers above it did not render, but their subtree did.
                elapsed = time.perf_counter() - start
                above = node.parent
                while above is not None:
                    if isinstance(above, ComponentNode) and above.profile is not None:
                        self._add_profile(above, "update", elapsed, start)
                    above = above.parent
            # Hosts diffed in this render synced themselves; what is left is the host
            # around this component, whose own diff did not run.
            if self._reshaped:
                for host in self._reshaped:
                    if host.reshaped:
                        host.reshaped = False
                        self._sync_dom(host)
                self._reshaped.clear()

    def _render_failed(self, node: ComponentNode, exc: BaseException) -> None:
        """An error while rendering ``node``: to the nearest error boundary, else logged."""
        boundary = self._boundary_for(node, "catch")
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and hasattr(exc, "detail"):
            # An HTTPException (FastAPI's or Starlette's): an expected answer, not a
            # crash. It sets the first response's status, headers and redirect.
            if self._sink is None:
                headers = dict(getattr(exc, "headers", None) or {})
                location = headers.pop("Location", None) or headers.pop("location", None)
                self.status = status
                self.headers.update(headers)
                if 300 <= status < 400 and location:
                    self.redirect = location
                    self.redirect_as_is = True  # a site URL, as FastAPI means it
            logger.info("%s raised HTTP %d: %s", node.name, status, getattr(exc, "detail", ""))
            if boundary is not None:
                boundary.catch(exc)  # type: ignore[misc]
            return
        if boundary is None:
            self.report(exc, f"rendering {node.name}")
        else:
            logger.error("Error rendering %s, caught by an error boundary", node.name, exc_info=exc)
            boundary.catch(exc)  # type: ignore[misc]

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
        if self._animate:
            if ops:
                # Apply inside document.startViewTransition, with the types if any.
                message["vt"] = self._transition_types or True
            self._animate = False
        if ops:
            self._transition_types = []
        return message

    def add_transition_type(self, name: str) -> None:
        def apply() -> None:
            if name not in self._transition_types:
                self._transition_types.append(name)

        self.call(apply)

    def flush_now(self) -> None:
        """Render and send pending updates at once (flush_sync). From a worker
        thread, waits until the loop has done it; the thread's own updates, posted
        to the loop before, are included."""
        if self._loop is None or threading.get_ident() == self._loop_thread:
            self.flush()
            return
        done: concurrent.futures.Future[None] = concurrent.futures.Future()

        def run() -> None:
            try:
                self.flush()
            finally:
                done.set_result(None)

        self._loop.call_soon_threadsafe(run)
        done.result(timeout=10)

    @property
    def live(self) -> bool:
        """Whether the page has been live in a browser (and is not just rendering
        HTML for a response). Stays True while a dropped connection may resume."""
        return self._queue is not None

    def on_live(self, boundary: ComponentNode, release: Callable[[], None]) -> None:
        """A Suspense ``boundary`` showing its fallback for browser() content:
        ``release`` shows the content once the page is live."""
        self._browser_release[boundary] = release

    def _render_browser_only(self) -> None:
        """Now the page is live, render what was left to the browser."""
        nodes, self._browser_only = self._browser_only, set()
        releases, self._browser_release = self._browser_release, {}
        for release in releases.values():
            release()
        for node in nodes:
            self.schedule(node)

    @staticmethod
    def _under_view_transition(node: Any) -> bool:
        while node is not None:
            if isinstance(node, ComponentNode) and node.view_transition:
                return True
            node = node.parent
        return False

    def head_html(self) -> str:
        """Resource hints, then the tags Head components asked for, as HTML for the
        page's <head>."""
        return "".join(self.head_parts())

    def head_parts(self) -> list[str]:
        """``head_html`` one tag at a time, so a streamed page can send the tags a
        boundary added after the shell went out."""
        from .ssr import render_head

        elements = [*self._resources.values(), *(el for elements in self._head.values() for el in elements)]
        return [render_head([el]) for el in elements]

    def defer(self, fn: Callable[[], None]) -> bool:
        """Run ``fn`` on the loop once what is being rendered now has been sent.
        False (and nothing scheduled) when the page is not live, as for its first HTML."""
        if self._loop is None:
            return False
        self._loop.call_soon(fn)
        return True

    def track(self, future: asyncio.Future[Any]) -> None:
        """Count ``future`` as work the page is waiting for (see :meth:`settle`)."""
        if not future.done():
            self._pending.add(future)  # type: ignore[arg-type]
            future.add_done_callback(self._pending.discard)

    def add_resource(self, key: tuple[str, str], element: Element) -> None:
        def apply() -> None:
            if key not in self._resources:
                self._resources[key] = element
                self._head_dirty = True
                self.request_flush()

        self.call(apply)

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
        token = ACTIVE_ROOT.set(self)
        try:
            self._run_effects_now(effects)
        finally:
            ACTIVE_ROOT.reset(token)

    def _run_effects_now(self, effects: list[tuple[EffectSlot, Any]]) -> None:
        for slot, fn in effects:
            if not slot.node.mounted:
                continue
            self._cleanup(slot)
            try:
                result = fn()
                if self._strict_rerun(slot, result):
                    result = fn()
            except Exception as exc:
                self.report(exc, f"an effect in {slot.node.name}")
                continue
            self._keep_effect_result(slot, result)

    def _strict_rerun(self, slot: EffectSlot, result: Any) -> bool:
        """Under StrictMode in debug mode, an effect's first run is set up, cleaned
        up and set up again, so an effect without the cleanup it needs shows it.
        Cleans up the first setup and says whether to run it again."""
        if slot.ran:
            return False
        slot.ran = True
        if not (self.debug and slot.node.mounted and self._is_strict(slot.node)):
            return False
        self._keep_effect_result(slot, result)
        self._cleanup(slot)
        return True

    @staticmethod
    def _is_strict(node: Any) -> bool:
        while node is not None:
            if isinstance(node, ComponentNode) and node.strict:
                return True
            node = node.parent
        return False

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
        i, n = 0, len(effects)
        while i < n:
            if self.threaded and not inspect.iscoroutinefunction(effects[i][1]):
                j = i + 1
                while j < n and not inspect.iscoroutinefunction(effects[j][1]):
                    j += 1
                await self._run_effects_in_thread(effects[i:j])
                i = j
                continue
            slot, fn = effects[i]
            i += 1
            if not slot.node.mounted:
                continue
            self._cleanup(slot)
            try:
                result = fn()
                if self._strict_rerun(slot, result):
                    result = fn()
            except Exception as exc:
                self.report(exc, f"an effect in {slot.node.name}")
                continue
            self._keep_effect_result(slot, result)

    async def _run_effects_in_thread(self, effects: list[tuple[EffectSlot, Any]]) -> None:
        """Plain effects in one worker-thread hop, not one each (a hop costs more than
        most effects). As React does, every cleanup runs first, then every setup;
        results are kept here on the loop, where a returned coroutine becomes a task."""
        live = [(slot, fn) for slot, fn in effects if slot.node.mounted]
        if not live:
            return
        for slot, _ in live:
            self._cleanup(slot)

        def run_all() -> list[tuple[Any, Exception | None]]:
            outcomes: list[tuple[Any, Exception | None]] = []
            for _, fn in live:
                try:
                    outcomes.append((fn(), None))
                except Exception as exc:
                    outcomes.append((None, exc))
            return outcomes

        for (slot, fn), (result, error) in zip(live, await self._in_thread(run_all)):
            try:
                if error is not None:
                    raise error
                if self._strict_rerun(slot, result):
                    result = await self._in_thread(fn)
            except Exception as exc:
                self.report(exc, f"an effect in {slot.node.name}")
                continue
            self._keep_effect_result(slot, result)

    async def _in_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` in a worker thread, and return only once every state update it
        sent to the loop has been applied, so the caller can render them."""
        loop = self._loop
        assert loop is not None
        applied = loop.create_future()

        def landed() -> None:
            if not applied.done():
                applied.set_result(None)

        def run() -> Any:
            try:
                return fn(*args)
            finally:
                # Updates reach the loop as callbacks in the order they were made, so
                # once this one runs, all of the function's updates have landed.
                loop.call_soon_threadsafe(landed)

        try:
            return await asyncio.to_thread(run)
        finally:
            await applied

    async def _work(self) -> None:
        """Run queued events and effect batches one at a time, in arrival order.

        After each one its updates are rendered before the next starts, so every
        handler sees the page as the previous event left it: two quick clicks on a
        counter count two, even when they arrive in the same network packet.
        """
        assert self._queue is not None
        ACTIVE_ROOT.set(self)  # this task's context: its handlers, effects and threads
        while True:
            kind, job = await self._queue.get()
            if kind == "event" or kind == "batch":
                self.pending_events -= 1
            self._busy = True
            try:
                if kind == "event":
                    await self._dispatch_async(*job)
                elif kind == "batch":
                    await self._dispatch_batch(*job)
                else:
                    await self._run_effects_async(job)
                if self._dirty:
                    self.flush()
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
            CURRENT_ACTION.set(asyncio.current_task())  # use_optimistic ties to this work
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

    async def settle(self, timeout: float = 5.0, suspense: bool = True) -> bool:
        """Wait until queued events, effects, background work (data loads, actions,
        async handlers) are done and their updates rendered. False if ``timeout``
        (seconds) ran out first. Long-running async effects are not waited for.

        With ``suspense=False``, data that a Suspense boundary is waiting for is not
        waited for: the page can be sent with those fallbacks and streamed later."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        def waiting_on() -> set[asyncio.Task[Any]]:
            return self._pending if suspense else self._pending - self._suspense_tasks

        def queued() -> bool:
            return self._queue is not None and (not self._queue.empty() or self._busy)

        while True:
            if self._dirty:
                self._render_dirty() if self._sink is None else self.flush()
            if not waiting_on() and not queued():
                await asyncio.sleep(0)  # done-callbacks and thread hand-offs land
                if not self._dirty and not waiting_on() and not queued():
                    return True
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            pending = waiting_on()
            if pending:
                await asyncio.wait(pending, timeout=min(remaining, 0.05))
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
        location = Location(url, self.base)
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
            if node_id != 0 and node_id not in self.hosts:  # 0: the page itself
                future.set_exception(LookupError("the element is no longer on the page"))
                return
            self._replies[reply_id] = future
            self.ops.append(["m", node_id, method, args, reply_id])
            self.request_flush()

        self.call(apply)
        return Reply(future, self._loop_thread)

    def _answer(self, data: dict[str, Any]) -> None:
        future = self._replies.pop(data.get("r"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if isinstance(data.get("error"), str):
            future.set_exception(RuntimeError(data["error"]))
        else:
            future.set_result(data.get("value"))

    # ---- events ----------------------------------------------------------------

    def dispatch(self, seq: int, node_id: int | list[int], name: str, data: dict[str, Any]) -> None:
        """Deliver a browser event. ``seq`` is acknowledged once the handler finishes,
        which tells the browser that edits it sent before then are reflected.

        ``node_id`` is a list when one native event reached several elements with
        handlers (the browser delegates from the root, as React does): they run in
        that order, and the page renders once after all of them, so each sees the
        state from before the event (``set_n(lambda n: n + 1)`` builds on another's).

        Once attached, events queue and run one at a time, each seeing the page as
        the previous one left it; before that (tests, server rendering) they run now.
        """
        if isinstance(node_id, list):
            if self._queue is not None:
                self.pending_events += 1
                self._queue.put_nowait(("batch", (seq, node_id, name, data)))
                return
            calls = self._resolve_batch(seq, node_id, name, data)
            token = ACTIVE_ROOT.set(self)
            try:
                for handler_call in calls:
                    try:
                        result = handler_call()
                    except Exception as exc:
                        self.report(exc, f"the {name!r} handler")
                        continue
                    if inspect.iscoroutine(result):
                        task = self.spawn(result)
                        self._pending.add(task)
                        task.add_done_callback(self._pending.discard)
            finally:
                ACTIVE_ROOT.reset(token)
            self._ack(seq)
            return
        if node_id == 0 and name == "#reply":
            # Never queued: the handler waiting for this answer may be the one
            # holding the queue's worker.
            self._answer(data)
            return
        if self._queue is not None:
            self.pending_events += 1
            self._queue.put_nowait(("event", (seq, node_id, name, data)))
            return
        call = self._resolve(seq, node_id, name, data)
        if call is None:
            return
        token = ACTIVE_ROOT.set(self)
        try:
            result = call()
        except Exception as exc:
            self.report(exc, f"the {name!r} handler")
            self._ack(seq)
            return
        finally:
            ACTIVE_ROOT.reset(token)
        self._after_handler(seq, result)

    async def _dispatch_async(self, seq: int, node_id: int, name: str, data: dict[str, Any]) -> None:
        call = self._resolve(seq, node_id, name, data)
        if call is None:
            return
        # Optimistic values a handler sets last until it (and any async work it
        # returned) is done.
        assert self._loop is not None
        action: asyncio.Future[None] = self._loop.create_future()
        token = CURRENT_ACTION.set(action)
        try:
            task = await self._dispatch_call(seq, name, call)
        finally:
            CURRENT_ACTION.reset(token)
        def finish(_: Any = None) -> None:
            if not action.done():
                action.set_result(None)

        if task is None:
            finish()
        else:
            task.add_done_callback(finish)

    async def _dispatch_call(self, seq: int, name: str, call: Callable[[], Any]) -> asyncio.Task[Any] | None:
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

                result = await self._in_thread(run)
            else:
                result = call()
        except Exception as exc:
            self.report(exc, f"the {name!r} handler")
            self._ack(seq)
            return None
        return self._after_handler(seq, result)

    async def _dispatch_batch(self, seq: int, node_ids: list[int], name: str, data: dict[str, Any]) -> None:
        """The handlers one native event reached, in order, with renders held until
        all have run. ``seq`` is acknowledged once they (and async work they returned)
        are done."""
        calls = self._resolve_batch(seq, node_ids, name, data)
        if not calls:
            self._ack(seq)
            return
        assert self._loop is not None
        loop = self._loop
        action: asyncio.Future[None] = loop.create_future()
        token = CURRENT_ACTION.set(action)
        tasks: list[asyncio.Task[Any]] = []
        self._holding = True
        try:
            for call in calls:
                try:
                    if self.threaded and not inspect.iscoroutinefunction(call.fn):  # type: ignore[attr-defined]

                        def run(call: Callable[[], Any] = call) -> Any:
                            _handling.seq = seq
                            try:
                                return call()
                            finally:
                                _handling.seq = None

                        result = await self._in_thread(run)
                    else:
                        result = call()
                except Exception as exc:
                    self.report(exc, f"the {name!r} handler")
                    continue
                if inspect.iscoroutine(result):
                    task = self.spawn(result)
                    self._pending.add(task)
                    task.add_done_callback(self._pending.discard)
                    tasks.append(task)
        finally:
            CURRENT_ACTION.reset(token)
            self._holding = False
            if self._flush_held:
                self._flush_held = False
                self.request_flush()

        def finish(_: Any = None) -> None:
            if all(task.done() for task in tasks):
                self._ack(seq)
                if not action.done():
                    action.set_result(None)

        if not tasks:
            finish()
        for task in tasks:
            task.add_done_callback(finish)

    def _prepare_event_data(self, data: dict[str, Any]) -> None:
        files = data.get("files")
        if isinstance(files, list):
            # Taken out of storage now, whether or not a handler wants them.
            data["files"] = [self._take_upload(f) for f in files if isinstance(f, dict)]
        form = data.get("form")
        if isinstance(form, list):
            data["form"] = FormFields(form)

    def _handler_call(self, seq: int, node_id: int, name: str, data: dict[str, Any]) -> Callable[[], Any] | None:
        node = self.hosts.get(node_id)
        handler = node.events.get(name) if node is not None else None
        fn = handler.fn if handler is not None else None
        if fn is None:
            return None
        event = Event(data, self, seq)
        wants = _wants_event(fn)

        def call() -> Any:
            return fn(event) if wants else fn()

        call.fn = fn  # type: ignore[attr-defined]
        return call

    def _resolve_batch(self, seq: int, node_ids: list[int], name: str, data: dict[str, Any]) -> list[Callable[[], Any]]:
        self._prepare_event_data(data)
        calls = [self._handler_call(seq, node_id, name, data) for node_id in node_ids if node_id != 0]
        return [call for call in calls if call is not None]

    def _resolve(self, seq: int, node_id: int, name: str, data: dict[str, Any]) -> Callable[[], Any] | None:
        """The handler call for an event, or None when there is nothing to run."""
        if node_id == 0:
            self._special(name, data)
            self._ack(seq)
            return None
        self._prepare_event_data(data)
        call = self._handler_call(seq, node_id, name, data)
        if call is None:
            self._ack(seq)
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
            if self._sink is not None:
                # The hydrate message had no structure to rebuild from: send all of it.
                self._render_dirty()
                self._send_tree("replace", rebuild=True)
        # "#ping" keeps proxies from closing an idle connection; nothing to do.

    def _after_handler(self, seq: int, result: Any) -> asyncio.Task[Any] | None:
        if inspect.iscoroutine(result):
            task = self.spawn(result)
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            task.add_done_callback(lambda _: self._ack(seq))
            return task
        self._ack(seq)
        return None

    def _ack(self, seq: int) -> None:
        if seq > self.ack:
            self.ack = seq
            if self._sink is not None:
                self.request_flush()  # sends the ack, alone when nothing changed

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
        # A lone list (html.ul([...])) is the child list itself, as React's
        # props.children is the array: its children take the cheap top-level keys.
        if type(items) is tuple and len(items) == 1 and type(items[0]) is list:
            _flatten_items(items[0], (), pairs, unkeyed, True)
        else:
            _flatten_items(items, (), pairs, unkeyed)
        if unkeyed and len(unkeyed) > 1:
            self._warn_unkeyed(parent)
        result: list[Any] = []
        if not old:
            # Mounting: every child is new. (Half the calls of a first render, so
            # kept to the minimum: no ends to match and no table of old keys.)
            seen: set[Any] = set()
            for i, (key, item) in enumerate(pairs):
                if key in seen:
                    logger.warning("Duplicate key %r under %s", key, getattr(parent, "name", parent.type))
                    key = (key, i)
                seen.add(key)
                result.append(self._mount(item, key, parent))
            if result:
                self._mark_reshaped(parent)
            return result
        n_old, n_new = len(old), len(pairs)
        # Children that kept their key and type at either end need no lookup table:
        # in a list that changed in place (most updates) that is all of them, and an
        # insertion or removal leaves only the part between the two ends. The front
        # is updated as it is matched; the back is matched first (the middle needs
        # its keys) and updated after the middle, so children update in their new
        # order. Tag names are compared by value: runtime strings are not interned.
        update = self._update
        start = 0
        limit = min(n_old, n_new)
        while start < limit:
            key, item = pairs[start]
            prev = old[start]
            item_type = str if type(item) is str else item.type
            if prev.key != key or not (prev.type is item_type or prev.type == item_type):
                break
            # A memo child with the same props, tested here rather than in _update:
            # one call fewer for each of a long list's unchanged rows.
            if prev.memo and not prev.dirty and (prev.element is item or _same_props(item, prev.element)):
                prev.element = item
            else:
                update(prev, item)
            start += 1
        result = old[:start]
        end_old, end_new = n_old, n_new
        while end_old > start and end_new > start:
            key, item = pairs[end_new - 1]
            prev = old[end_old - 1]
            item_type = str if type(item) is str else item.type
            if prev.key != key or not (prev.type is item_type or prev.type == item_type):
                break
            end_old -= 1
            end_new -= 1
        # A new node, or an old one in another place, reshapes the list: its host's DOM
        # order is synced. Removals alone need no sync: their "r" ops are the change.
        reshaped = False
        leftovers: list[Any] = []
        if start < end_new:
            by_index = {old[i].key: i for i in range(start, end_old)}
            # The ends' keys are the old children's there, so they cannot repeat each
            # other; only a key between them can repeat one of theirs.
            seen = set()
            if start:
                seen.update(pairs[i][0] for i in range(start))
            if end_new < n_new:
                seen.update(pairs[i][0] for i in range(end_new, n_new))
            last = -1
            for i in range(start, end_new):
                key, item = pairs[i]
                if key in seen:
                    logger.warning("Duplicate key %r under %s", key, getattr(parent, "name", parent.type))
                    key = (key, i)
                seen.add(key)
                j = by_index.pop(key, -1)
                prev = old[j] if j >= 0 else None
                item_type = str if type(item) is str else item.type
                if prev is not None and (prev.type is item_type or prev.type == item_type):
                    update(prev, item)
                    if j < last:
                        reshaped = True  # moved
                    last = j
                    result.append(prev)
                else:
                    if prev is not None:
                        self._unmount(prev, emit=True)
                    reshaped = True
                    result.append(self._mount(item, key, parent))
            leftovers = [old[j] for j in by_index.values()]
        elif start < end_old:
            leftovers = old[start:end_old]  # only removals between the ends
        shift = n_old - n_new
        for i in range(end_new, n_new):
            prev = old[i + shift]
            item = pairs[i][1]
            if prev.memo and not prev.dirty and (prev.element is item or _same_props(item, prev.element)):
                prev.element = item
            else:
                update(prev, item)
            result.append(prev)
        if leftovers:
            host = parent if type(parent) is HostNode else self._host_of(parent)
            if not reshaped and not host.reshaped:
                # Only removals: drop their DOM ids from the host's order, no sync.
                if host is parent and not result:
                    host.dom = []
                    gone = None
                else:
                    gone = set(self._dom_ids(leftovers))
                if gone:
                    host.dom = [node_id for node_id in host.dom if node_id not in gone]
            for leftover in leftovers:
                self._unmount(leftover, emit=True)
        if reshaped:
            self._mark_reshaped(parent)
        return result

    def _mount(self, item: Any, key: Any, parent: Any) -> Any:
        emit = self._emit
        if type(item) is str:
            node_id = next(self._ids)
            if emit:
                self.ops.append(["t", node_id, item])
            return TextNode(node_id, item, key, parent)
        if isinstance(item.type, JsComponent) or item.type == PORTAL:
            host = HostNode(next(self._ids), item, key, parent)
            self.hosts[host.id] = host
            if item.type == PORTAL:
                host.ns = None
                self.portals += 1
                if emit:
                    self.ops.append(["P", host.id, item.attrs["target"]])
            elif emit:
                self.ops.append(self._js_create(host))
            host.reshaped = True  # its order is computed whole below
            host.children = self._reconcile(host, [], item.children)
            host.dom = self._dom_ids(host.children)
            host.reshaped = False
            if host.dom and emit:
                self.ops.append(["A", host.id, host.dom])
            self._attach_ref(host, item.ref)
            return host
        if type(item.type) is str and item.type != FRAGMENT:
            host = HostNode(next(self._ids), item, key, parent)
            self.hosts[host.id] = host
            text = _only_text(item)
            if text is not None:
                host.text = text
                if emit:
                    self.ops.append(self._create_op(host))
            else:
                if emit:
                    self.ops.append(self._create_op(host))
                host.reshaped = True  # its order is computed whole below
                host.children = self._reconcile(host, [], item.children)
                host.dom = self._dom_ids(host.children)
                host.reshaped = False
                if host.dom and emit:
                    self.ops.append(["A", host.id, host.dom])
            self._attach_ref(host, item.ref)
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
            if isinstance(node.type, Component) and node.type.memo and _same_props(item, node.element):
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
            if node.reshaped:
                node.reshaped = False
                self._sync_dom(node)
            return
        if old_attrs != new_attrs:
            for name, value in new_attrs.items():
                if old_attrs.get(name, _SKIP) != value:
                    if name == "style":
                        changes = style_changes(old_attrs.get(name), value)
                        if changes is not None:
                            # Only the properties that changed: the browser keeps the
                            # rest, and any a script set on the element itself.
                            ops.append(["p", node.id, "style", changes])
                            continue
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
        text = _only_text(item)
        if text is not None:
            if node.text is None:
                # It had child nodes: they go, and its own text takes their place.
                for child in node.children:
                    self._unmount(child, emit=True)
                node.children = ()
                node.dom = ()
                ops.append(["p", node.id, TEXT, text])
            elif text != node.text:
                ops.append(["p", node.id, TEXT, text])
            node.text = text
            return
        if node.text is not None:
            # Its text gives way to child nodes, inserted by the reconcile below.
            if node.text:
                ops.append(["p", node.id, TEXT, None])
            node.text = None
        node.children = self._reconcile(node, node.children, item.children)
        if node.reshaped:
            node.reshaped = False
            self._sync_dom(node)

    def _render(self, node: ComponentNode, item: Element, timed: bool = False) -> None:
        kind = node.type
        if not timed and type(kind) is Component and kind.profiler:
            phase = "mount" if node.profile is None else "update"
            start = time.perf_counter()
            self._render(node, item, True)
            if node.profile is not None:
                self._add_profile(node, phase, time.perf_counter() - start, start)
            return
        node.dirty = False
        node.element = item
        kind = node.type
        if isinstance(kind, Component):
            node.hook_index = 0
            token = CURRENT.set(node)
            try:
                if self.debug and self._is_strict(node):
                    # StrictMode: a render whose result is thrown away, so a component
                    # that is not pure (changes something while rendering) shows it.
                    hooks = len(node.hooks)
                    effects = [(h, h.fn, h.deps) for h in node.hooks if isinstance(h, EffectSlot)]
                    kind.fn(*item.args, **item.kwargs)
                    # Undo what that render did to the hooks, so the real one decides.
                    if hooks < len(node.hooks):
                        del node.hooks[hooks:]
                    for slot, fn, deps in effects:
                        slot.fn, slot.deps = fn, deps
                    node.hook_index = 0
                    node.pending_effects = ()
                out = kind.fn(*item.args, **item.kwargs)
                self._check_hook_count(node)
                reruns = 0
                while node.dirty and node.mounted:
                    # It set its own state while rendering (deriving state from a
                    # prop, as React's docs show): run it again before diffing, so
                    # its children see only the final output.
                    reruns += 1
                    if reruns > MAX_RENDERS:
                        node.dirty = False  # reported once, not rendered again from the queue
                        raise _too_many_renders(node.name)
                    node.dirty = False
                    for slot in node.pending_effects:
                        slot.fn, slot.deps = slot.before
                    node.pending_effects = ()
                    node.hook_index = 0
                    out = kind.fn(*item.args, **item.kwargs)
                    self._check_hook_count(node)
            except Suspended as waiting:
                # Waiting for data: show the nearest Suspense fallback meanwhile. The
                # hook re-renders this component when the data arrives.
                node.pending_effects = ()
                boundary = self._boundary_for(node, "suspend")
                if isinstance(waiting, BrowserOnly):
                    # Left to the browser: rendered (empty) here, again once live.
                    self._browser_only.add(node)
                    if boundary is not None:
                        boundary.suspend(BROWSER_ONLY)  # type: ignore[misc]
                elif boundary is not None:
                    if waiting.task is not None:
                        self._suspense_tasks.add(waiting.task)
                        waiting.task.add_done_callback(self._suspense_tasks.discard)
                    boundary.suspend(waiting.task)  # type: ignore[misc]
                if node.children:
                    return
                out = None
            except Exception as exc:
                node.pending_effects = ()
                self._render_failed(node, exc)
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
                    for sub in list(node.subscribers or ()):
                        self.schedule(sub)
            children = item.children
        ops_before = len(self.ops)
        node.children = self._reconcile(node, node.children, children)
        if node.view_transition and len(self.ops) > ops_before and self._sink is not None:
            self._animate = True  # a parent re-rendered and changed what is inside
        if node.pending_effects:
            # Queued after the children's, so children's effects run first, as in React.
            self._effects.extend(node.pending_effects)
            node.pending_effects = ()

    def _add_profile(self, node: ComponentNode, phase: str, seconds: float, start: float) -> None:
        entry = self._profiles.get(node)
        if entry is None:
            self._profiles[node] = [phase, seconds * 1000, start * 1000]
        else:
            entry[1] += seconds * 1000
            if phase == "mount":
                entry[0] = "mount"

    def _report_profiles(self) -> None:
        """Tell each Profiler how long its subtree took to render in this pass."""
        if not self._profiles:
            return
        profiles, self._profiles = self._profiles, {}
        commit = time.perf_counter() * 1000
        for node, (phase, ms, start) in profiles.items():
            if node.profile is None or not node.mounted:
                continue
            profile_id, on_render = node.profile
            try:
                on_render(profile_id, phase, ms, start, commit)
            except Exception as exc:
                self.report(exc, f"the on_render of Profiler {profile_id!r}")

    @staticmethod
    def _check_hook_count(node: ComponentNode) -> None:
        """Hooks are matched to their slots by call order, so every render of a
        component must call the same hooks: never inside an if, a loop, or after an
        early return."""
        used = node.hook_index
        if node.hook_count is None:
            node.hook_count = used
        elif used != node.hook_count:
            expected = node.hook_count
            raise RuntimeError(
                f"Rendered {'fewer' if used < expected else 'more'} hooks than during the previous render "
                f"of {node.name} ({used} instead of {expected}). Call hooks at the top level of the "
                "component, the same ones on every render: not inside an if, a loop, or after an early return."
            )

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

    def _attach_ref(self, node: HostNode, ref: Any) -> None:
        """Give ``ref`` the element: a ref object gets it in ``.current``; a function
        is called with it, and may return a cleanup for when it is detached."""
        if ref is None:
            return
        handle = ElementHandle(self, node.id)
        if hasattr(ref, "current"):
            ref.current = handle
            return
        try:
            result = ref(handle)
        except Exception as exc:
            self.report(exc, "a callback ref")
            return
        node.ref_cleanup = result if callable(result) else None

    def _release_ref(self, node: HostNode) -> None:
        ref = node.element.ref
        if ref is None:
            return
        if hasattr(ref, "current"):
            if isinstance(ref.current, ElementHandle) and ref.current.id == node.id:
                ref.current = None
            return
        # A callback ref: its cleanup if it returned one, else it is called with None.
        cleanup, node.ref_cleanup = node.ref_cleanup, None
        try:
            cleanup() if cleanup is not None else ref(None)
        except Exception as exc:
            self.report(exc, "a callback ref")

    def _swap_ref(self, node: HostNode, item: Element) -> None:
        if node.element.ref is not item.ref:
            self._release_ref(node)
            self._attach_ref(node, item.ref)

    @staticmethod
    def _js_create(host: HostNode) -> list[Any]:
        js: JsComponent = host.type
        return ["j", host.id, js.tag, js.module, js.export, host.attrs, host.element.kwargs.get("fallback")]

    @staticmethod
    def _create_op(host: HostNode) -> list[Any]:
        # Trailing fields left empty are left out: most elements have no events,
        # many no attributes, and 10,000 rows of `{},{}` add up.
        attrs = {**host.attrs, TEXT: host.text} if host.text else host.attrs
        if host.ns is not None:
            return ["c", host.id, host.type, attrs, {n: h.spec() for n, h in host.events.items()}, host.ns]
        if host.events:
            return ["c", host.id, host.type, attrs, {n: h.spec() for n, h in host.events.items()}]
        if attrs:
            return ["c", host.id, host.type, attrs]
        return ["c", host.id, host.type]

    def _unmount(self, node: Any, emit: bool) -> None:
        kind = type(node)
        if kind is TextNode:
            if emit:
                self.ops.append(["r", node.id])
            return
        if kind is HostNode:
            # A portal's content lives elsewhere in the page, so removing an
            # ancestor does not take it along: it is always removed itself.
            if emit or node.type == PORTAL:
                self.ops.append(["r", node.id])
            self._drop_host(node)
            return
        self._unmount_component(node, emit)

    def _drop_host(self, node: HostNode) -> None:
        """A removed host and what is under it: no "r" ops (its own removal took them),
        except for portals, whose DOM lives elsewhere. Exact type tests and no call
        per text node or ref-less host: this runs for every node of a removed tree."""
        del self.hosts[node.id]
        node.reshaped = False
        if node.type == PORTAL:
            self.portals -= 1
        if node.element.ref is not None:
            self._release_ref(node)
        children = node.children
        if children:
            node.children = []  # no parent <-> child cycle left for the garbage collector
            for child in children:
                kind = type(child)
                if kind is HostNode:
                    if child.type == PORTAL:
                        self.ops.append(["r", child.id])
                    self._drop_host(child)
                elif kind is not TextNode:
                    self._unmount_component(child, False)

    def _unmount_component(self, node: ComponentNode, emit: bool) -> None:
        node.mounted = False
        if node.view_transition:
            node.view_transition = False
            self._view_transitions -= 1
        node.dirty = False
        children = node.children
        if children:
            node.children = []
            for child in children:
                self._unmount(child, emit)
        hooks = node.hooks
        if hooks:
            node.hooks = []
            for slot in hooks:
                if type(slot) is EffectSlot:
                    if slot.cleanup is not None:
                        self._cleanup(slot)
                elif hasattr(slot, "dispose"):
                    # Subscriptions and background work a hook started.
                    try:
                        slot.dispose()
                    except Exception as exc:
                        self.report(exc, f"unmounting {node.name}")
        if node.subscriptions is not None:
            for provider in node.subscriptions:
                if provider.subscribers is not None:
                    provider.subscribers.discard(node)
            node.subscriptions = None
        if self._location_subs:
            self._location_subs.discard(node)
        if self._head and self._head.pop(node, None) is not None:
            self._head_dirty = True
        node.subscribers = None

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
            kind = type(node)
            if kind is ComponentNode:
                stack.extend(reversed(node.children))
            elif kind is HostNode and node.type == PORTAL:
                if portals is not None:
                    portals.append(node)
            else:
                out.append(node)
        return out

    @classmethod
    def _dom_ids(cls, children: list[Any]) -> list[int]:
        # Elements and text only (a <tr> of <td>s, most elements): their own ids,
        # without the walk that flattens components and sets portals aside.
        ids = []
        for node in children:
            kind = type(node)
            if kind is ComponentNode or (kind is HostNode and node.type == PORTAL):
                return [node.id for node in cls._dom_nodes(children)]
            ids.append(node.id)
        return ids

    @staticmethod
    def _host_of(node: Any) -> HostNode:
        parent = node.parent
        while not isinstance(parent, HostNode):
            parent = parent.parent
        return parent

    def _mark_reshaped(self, parent: Any) -> None:
        """A child list under ``parent`` gained, lost or reordered a node: the nearest
        host's DOM order needs syncing. Only that host, not every host above it."""
        host = parent
        while not isinstance(host, HostNode):
            host = host.parent
        if not host.reshaped:
            host.reshaped = True
            self._reshaped.append(host)

# ---- the tree as plain dicts ---------------------------------------------------------
#
# pyreaction keeps its virtual DOM here, on the server, and sends the browser only
# changes. to_vdom turns the tree into JSON-ready data, using reactpy's key names:
# {"tagName": "button", "attributes": {...}, "eventHandlers": {...}, "children": [...]}


def to_vdom(target: Root | Element) -> dict[str, Any]:
    """The rendered tree of ``target`` as plain dicts and strings.

    ``target`` is an element (mounted just for this, as for the page's first HTML)
    or a live :class:`Root` (its current state, e.g. ``page.root`` in a test).
    Components are rendered: only tags and text remain. The result is a fragment,
    ``{"tagName": "", "children": [...]}``, whose children are what the page shows.

    Each tag is ``{"tagName": "div"}`` plus, when present:

    - ``"key"``: the element's key
    - ``"attributes"``: as written to the page (``class``, ``for``, ``data-id``,
      ``style`` as text); ``value`` and ``checked`` as given
    - ``"eventHandlers"``: one entry per DOM event (``"click"``; ``"input"`` for
      ``on_change``; ``"click:capture"`` for ``on_click_capture``), listing the
      options that are set (``preventDefault``, ``debounce``, ``keys``...); a
      :func:`~reaction.js` handler has ``"code"``
    - ``"dangerouslySetInnerHTML"``: ``{"__html": "..."}`` for raw HTML
    - ``"importSource"``: ``{"source": url, "export": name}`` for a
      :func:`~reaction.js_component`, whose props are its attributes
    - ``"portal"``: the target selector, for a :func:`~reaction.portal` (shown where
      it is written, though the browser puts it elsewhere)
    """
    root = target if isinstance(target, Root) else Root(target)
    try:
        return {"tagName": "", "children": _children(root.container.children)}
    finally:
        if root is not target:
            root.close()


def _children(nodes: list[Any]) -> list[Any]:
    out: list[Any] = []
    for node in nodes:
        if isinstance(node, ComponentNode):
            out.extend(_children(node.children))  # a component adds no tag of its own
        elif isinstance(node, TextNode):
            if node.text:
                out.append(node.text)
        else:
            out.append(_host(node))
    return out


_SPEC_NAMES = (
    "preventDefault", "stopPropagation", "debounce", "throttle", "keys", "code",
    "stopImmediatePropagation", "form",
)


def _host(node: HostNode) -> dict[str, Any]:
    if node.type == PORTAL:
        out: dict[str, Any] = {"tagName": "", "portal": node.attrs.get("target", "body")}
    elif isinstance(node.type, JsComponent):
        js = node.type
        out = {"tagName": js.tag, "importSource": {"source": js.module, "export": js.export}}
        if node.attrs:
            out["attributes"] = dict(node.attrs)
    else:
        out = {"tagName": node.type}
        attrs = {k: v for k, v in node.attrs.items() if k not in (RAW_HTML, RAW_MARK)}
        if attrs:
            out["attributes"] = attrs
        if RAW_HTML in node.attrs:
            out["dangerouslySetInnerHTML"] = {"__html": node.attrs[RAW_HTML]}
    if node.element.key is not None:
        out["key"] = node.element.key
    if node.events:
        out["eventHandlers"] = {
            name: {k: v for k, v in zip(_SPEC_NAMES, handler.spec()) if v not in (None, False)}
            for name, handler in node.events.items()
        }
    children = [node.text] if node.text else _children(node.children)
    if children:
        out["children"] = children
    return out
