"""Hooks: per-component state kept in call order on the component being rendered."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from .vdom import ComponentNode

logger = logging.getLogger("reaction")

__all__ = [
    "AsyncState",
    "Context",
    "Ref",
    "Suspended",
    "create_context",
    "use_action_state",
    "use_async",
    "use_callback",
    "use_context",
    "use_data",
    "use_debug_value",
    "use_effect",
    "use_effect_event",
    "use_id",
    "use_imperative_handle",
    "use_location",
    "use_memo",
    "use_navigate",
    "use_reducer",
    "use_ref",
    "use_request",
    "use_state",
    "use_status",
    "use_sync_external_store",
    "use_title",
    "use_transition",
    "start_transition",
    "use_optimistic",
    "create_ref",
    "use",
    "use_deferred_value",
    "cache",
    "browser",
    "flush_sync",
    "request_form_reset",
    "add_transition_type",
    "cache_signal",
    "lazy",
]

T = TypeVar("T")

CURRENT: ContextVar[ComponentNode | None] = ContextVar("reaction_current", default=None)
# The page whose event handler, effect or background work is running (worker threads
# get it too: they run in a copy of the context that started them).
ACTIVE_ROOT: ContextVar[Any] = ContextVar("reaction_root", default=None)
# The action running now (a transition's task, or an event handler's completion
# future): optimistic values set during it last until it is done.
CURRENT_ACTION: ContextVar[Any] = ContextVar("reaction_action", default=None)


def active_root() -> Any:
    """The page being rendered, or whose handler, effect or background work runs."""
    node = CURRENT.get()
    if node is not None:
        return node.root
    root = ACTIVE_ROOT.get()
    if root is None:
        raise RuntimeError("only callable while a page renders, or from its event handlers and effects")
    return root

_PRIMITIVES = (int, float, str, bytes, bool, type(None), tuple, frozenset)


def same(a: Any, b: Any) -> bool:
    """Whether a state change can be skipped: identity, or equality for immutable values.

    Mutable containers compare by identity, as in React: mutate-and-set does not re-render.
    """
    if a is b:
        return True
    if type(a) is type(b) and isinstance(a, _PRIMITIVES):
        try:
            return bool(a == b)
        except Exception:
            return False
    return False


def _deps_changed(old: Sequence[Any] | None, new: Sequence[Any] | None) -> bool:
    if old is None or new is None or len(old) != len(new):
        return True
    return any(not same(a, b) for a, b in zip(old, new))


def _slot(kind: Callable[..., T], *args: Any) -> tuple[T, bool]:
    """The current component's next hook slot, created on first render."""
    node = CURRENT.get()
    if node is None:
        raise RuntimeError(
            "Invalid hook call: hooks can only be called while a component renders, at its top "
            "level, or from your own use_ functions called there; not from event handlers or effects."
        )
    i = node.hook_index
    node.hook_index = i + 1
    hooks = node.hooks
    if i < len(hooks):
        slot = hooks[i]
        if type(slot) is not kind:
            raise RuntimeError(
                f"Hook order changed in {node.name}: expected {type(slot).__name__}, "
                f"got {getattr(kind, '__name__', kind)}. Hooks must not be called conditionally."
            )
        return slot, False
    slot = kind(node, *args)
    hooks.append(slot)
    return slot, True


class _StateSlot:
    __slots__ = ("value", "set")

    def __init__(self, node: ComponentNode, initial: Any) -> None:
        self.value = initial() if callable(initial) else initial

        def set_state(new: Any) -> None:
            def apply() -> None:
                value = new(self.value) if callable(new) else new
                if not same(value, self.value):
                    self.value = value
                    node.root.schedule(node)

            node.root.call(apply)

        self.set = set_state


def use_state(initial: T | Callable[[], T]) -> tuple[T, Callable[[T | Callable[[T], T]], None]]:
    """``value, set_value = use_state(0)``. ``set_value`` takes a value or ``lambda prev: ...``."""
    slot, _ = _slot(_StateSlot, initial)
    return slot.value, slot.set


class _ReducerSlot:
    __slots__ = ("value", "reducer", "dispatch")

    def __init__(self, node: ComponentNode, reducer: Callable[[Any, Any], Any], initial: Any) -> None:
        self.value = initial
        self.reducer = reducer

        def dispatch(action: Any) -> None:
            def apply() -> None:
                value = self.reducer(self.value, action)
                if not same(value, self.value):
                    self.value = value
                    node.root.schedule(node)

            node.root.call(apply)

        self.dispatch = dispatch


def use_reducer(reducer: Callable[[T, Any], T], initial: T) -> tuple[T, Callable[[Any], None]]:
    """``state, dispatch = use_reducer(reducer, initial)``."""
    slot, _ = _slot(_ReducerSlot, reducer, initial)
    slot.reducer = reducer
    return slot.value, slot.dispatch


class Ref(Generic[T]):
    __slots__ = ("current",)

    def __init__(self, current: T) -> None:
        self.current = current

    def __repr__(self) -> str:
        return f"Ref({self.current!r})"


def create_ref(initial: T = None) -> Ref[T]:  # type: ignore[assignment]
    """A new ref object, for code outside components (in a component, ``use_ref``
    keeps the same one across renders)."""
    return Ref(initial)


class _RefSlot:
    __slots__ = ("ref",)

    def __init__(self, node: ComponentNode, initial: Any) -> None:
        self.ref = Ref(initial)


def use_ref(initial: T = None) -> Ref[T]:  # type: ignore[assignment]
    """A mutable box that survives re-renders; changing ``.current`` does not re-render."""
    slot, _ = _slot(_RefSlot, initial)
    return slot.ref


class _MemoSlot:
    __slots__ = ("value", "deps")

    def __init__(self, node: ComponentNode) -> None:
        self.value: Any = None
        self.deps: Sequence[Any] | None = None


def use_memo(fn: Callable[[], T], deps: Sequence[Any] | None = None) -> T:
    """Recompute ``fn()`` only when ``deps`` change (every render when ``deps`` is None)."""
    slot, new = _slot(_MemoSlot)
    if new or _deps_changed(slot.deps, deps):
        slot.value = fn()
        slot.deps = None if deps is None else tuple(deps)
    return slot.value


def use_callback(fn: T, deps: Sequence[Any] | None = None) -> T:
    """Keep the same function object while ``deps`` are unchanged."""
    return use_memo(lambda: fn, deps)


class EffectSlot:
    __slots__ = ("node", "fn", "deps", "cleanup", "ran")

    def __init__(self, node: ComponentNode) -> None:
        self.node = node
        self.fn: Callable[[], Any] | None = None
        self.deps: Sequence[Any] | None = None
        self.cleanup: Callable[[], Any] | None = None
        self.ran = False  # StrictMode sets up, cleans up and sets up again the first time


def use_effect(fn: Callable[[], Any], deps: Sequence[Any] | None = None) -> None:
    """Run ``fn`` after the update reaches the browser.

    ``deps=None`` runs it after every render, ``[]`` once on mount, otherwise when a
    dependency changes. ``fn`` may return a cleanup function, or be ``async``, in
    which case its task is cancelled on cleanup.
    """
    slot, new = _slot(EffectSlot)
    if new or deps is None or _deps_changed(slot.deps, deps):
        slot.fn = fn
        slot.deps = None if deps is None else tuple(deps)
        slot.node.pending_effects.append(slot)


def use_layout_effect(fn: Callable[[], Any], deps: Sequence[Any] | None = None) -> None:
    """The same as :func:`use_effect`, for code written for React. In React it runs
    before the browser paints, to measure the page; here components run on the
    server, so it runs once the update has reached the browser. To measure an
    element, ask the browser: ``await ref.current.call("getBoundingClientRect")``."""
    use_effect(fn, deps)


class Context(Generic[T]):
    """Created by :func:`create_context`. ``ctx(value, *children)`` provides a value."""

    def __init__(self, default: T, name: str = "Context") -> None:
        self.default = default
        self.__name__ = name

    def __call__(self, *children: Any, value: T, key: Any = None) -> Any:
        from .element import Element

        return Element(self, key, children=children, kwargs={"value": value})

    @property
    def Provider(self) -> Context[T]:
        """``ctx.Provider(*children, value=...)``, React's older spelling of ``ctx(...)``."""
        return self

    def __repr__(self) -> str:
        return f"<context {self.__name__}>"


def create_context(default: T = None, name: str = "Context") -> Context[T]:  # type: ignore[assignment]
    return Context(default, name)


class _ContextSlot:
    __slots__ = ("provider",)

    def __init__(self, node: ComponentNode, ctx: Context[Any]) -> None:
        provider = node.parent
        while provider is not None and provider.type is not ctx:
            provider = provider.parent
        self.provider = provider
        if provider is not None:
            provider.subscribers.add(node)
            node.subscriptions.append(provider)


def use_context(ctx: Context[T]) -> T:
    """The value of the nearest ``ctx(...)`` provider above, or the context's default."""
    slot, _ = _slot(_ContextSlot, ctx)
    return ctx.default if slot.provider is None else slot.provider.value


def _current() -> ComponentNode:
    node = CURRENT.get()
    if node is None:
        raise RuntimeError(
            "Invalid hook call: hooks can only be called while a component renders, at its top "
            "level, or from your own use_ functions called there; not from event handlers or effects."
        )
    return node


def use_location() -> Any:
    """The page's :class:`Location` (``.path``, ``.search``, ``.query``).

    The component re-renders when the browser moves to another URL.
    """
    node = _current()
    node.root.subscribe_location(node)
    return node.root.location


def use_navigate() -> Callable[..., None]:
    """``navigate = use_navigate()``, then ``navigate("/users/7")`` or ``navigate(url, replace=True)``."""
    return _current().root.navigate


def use_request() -> Any:
    """The connection serving this page: a Starlette ``Request`` while the first HTML
    is rendered, then the ``WebSocket``. Both have ``.headers``, ``.cookies`` and
    ``.client``; for the page URL use :func:`use_location`. None outside a server."""
    return _current().root.connection


def use_title(title: str) -> None:
    """Set the page's title. The deepest component that calls it wins, as rendered last."""
    _current().root.set_title(title)


def use_status(code: int) -> None:
    """Set the HTTP status of the page's first response, e.g. ``use_status(404)`` in a
    not-found page, so search engines and monitoring see it. No effect once live."""
    _current().root.status = code


# ---- pending state ------------------------------------------------------------


class _Counter:
    __slots__ = ("n", "last", "start")

    def __init__(self, node: ComponentNode) -> None:
        self.n = 0
        self.last: Any = None
        self.start: Any = None


def use_transition() -> tuple[bool, Callable[..., None]]:
    """``pending, start = use_transition()``; ``start(fn, *args)`` runs ``fn`` in the
    background (``async`` on the loop, otherwise in a worker thread) with ``pending``
    True until it finishes. Started work runs in order. Use it to show "saving..."
    while a slow action runs, since every event is a round trip to the server."""
    pending, set_pending = use_state(False)
    node = _current()
    slot, new = _slot(_Counter)
    if new:
        root = node.root

        def start(fn: Callable[..., Any], *args: Any) -> None:
            def go() -> None:
                slot.n += 1
                set_pending(True)

                def done(result: Any, error: BaseException | None) -> None:
                    slot.n -= 1
                    if error is not None:
                        root.report(error, f"a transition in {node.name}")
                    if slot.n == 0:
                        set_pending(False)

                slot.last = root.background(fn, args, done, after=slot.last)

            root.call(go)

        slot.start = start
    return pending, slot.start


def start_transition(fn: Callable[..., Any], *args: Any) -> None:
    """Run ``fn(*args)`` in the background, from a handler or effect, without holding
    up the page: like ``start`` from :func:`use_transition`, without a pending flag.
    ``async`` functions run on the loop, others in a worker thread; errors are reported."""
    root = active_root()

    def done(result: Any, error: BaseException | None) -> None:
        if error is not None:
            root.report(error, "a transition")

    root.call(lambda: root.background(fn, args, done))


class _OptimisticSlot:
    __slots__ = ("updates", "state", "add")

    def __init__(self, node: ComponentNode) -> None:
        # (the action it belongs to or None, its argument, the state when added)
        self.updates: list[tuple[Any, Any, Any]] = []
        self.state: Any = None
        root = node.root

        def add(arg: Any) -> None:
            owner = CURRENT_ACTION.get()

            def apply() -> None:
                entry = (owner, arg, self.state)
                self.updates.append(entry)
                root.schedule(node)
                if owner is not None:

                    def drop() -> None:
                        self.updates = [u for u in self.updates if u is not entry]
                        if node.mounted:
                            root.schedule(node)

                    owner.add_done_callback(lambda _: root.call(drop))

            root.call(apply)

        self.add = add


def use_optimistic(
    state: T, update: Callable[[T, Any], T] | None = None
) -> tuple[T, Callable[[Any], None]]:
    """``shown, add = use_optimistic(state, update)``: show a change before the server
    work behind it finishes. ``add(x)`` makes ``shown`` ``update(shown, x)`` (or just
    ``x``) at once; when the action that called it (a transition, a form action or
    the event handler) is done, ``shown`` is ``state`` again, by then the real result.
    Outside an action, the change lasts until ``state`` itself changes."""
    slot, _ = _slot(_OptimisticSlot)
    slot.state = state
    slot.updates = [u for u in slot.updates if u[0] is not None or same(u[2], state)]
    shown = state
    for _, arg, _ in slot.updates:
        shown = update(shown, arg) if update is not None else arg
    return shown, slot.add


_MISSING: Any = object()


class _DeferredSlot:
    __slots__ = ("shown", "target", "waiting")

    def __init__(self, node: ComponentNode) -> None:
        self.shown: Any = None
        self.target: Any = None
        self.waiting = False


def use_deferred_value(value: T, initial_value: Any = _MISSING) -> T:
    """``value``, a step behind: when it changes, this render still gets the previous
    one, and a second render follows with the new one once the first update has
    reached the browser. Use it for the slow part of a page (search results) so the
    fast part (the search box) updates first. ``initial_value`` is shown on the
    first live render. The page's first HTML always has the latest value."""
    node = _current()
    slot, new = _slot(_DeferredSlot)
    if new:
        slot.shown = value if initial_value is _MISSING else initial_value
    slot.target = value
    if same(slot.shown, value):
        return value
    root = node.root

    def catch_up() -> None:
        slot.waiting = False
        if node.mounted and not same(slot.shown, slot.target):
            slot.shown = slot.target
            root.schedule(node)

    if not slot.waiting:
        if not root.defer(catch_up):
            slot.shown = value
            return value
        slot.waiting = True
    return slot.shown


def use(resource: Any) -> Any:
    """Read a context or wait for a future, as React's ``use``.

    ``use(SomeContext)`` is :func:`use_context`, but may be called inside an ``if``
    or a loop. ``use(task)``, with an ``asyncio`` task or future created outside this
    render (by :func:`cache` or ``asyncio.create_task`` in a handler), returns its
    result: until it is done the nearest :class:`Suspense` shows its fallback, and an
    error goes to the nearest :class:`ErrorBoundary`. A coroutine cannot be read
    twice, so it is refused: wrap the function with :func:`cache`, or use
    :func:`use_data`."""
    import asyncio

    node = _current()
    if isinstance(resource, Context):
        provider = node.parent
        while provider is not None and provider.type is not resource:
            provider = provider.parent
        if provider is None:
            return resource.default
        if node not in provider.subscribers:
            provider.subscribers.add(node)
            node.subscriptions.append(provider)
        return provider.value
    if isinstance(resource, asyncio.Future):
        if resource.done():
            return resource.result()
        root = node.root
        root.track(resource)

        def rerender() -> None:
            if node.mounted:
                root.schedule(node)

        def ready(_: Any) -> None:
            root.call(rerender)

        resource.add_done_callback(ready)
        raise Suspended(resource)
    if inspect.iscoroutine(resource):
        resource.close()
        raise TypeError("use() needs a task, not a coroutine: wrap the async function with cache(), or use use_data()")
    raise TypeError(f"use() takes a context or an asyncio task or future, not {type(resource).__name__}")


def _cache_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    def one(value: Any) -> Any:
        try:
            hash(value)
            return value
        except TypeError:
            return ("#id", id(value))  # unhashable: the same object, as React compares

    return tuple(one(a) for a in args), tuple(sorted((k, one(v)) for k, v in kwargs.items()))


def cache(fn: Callable[..., T]) -> Callable[..., T]:
    """Memoise ``fn`` for one page, as React's ``cache`` does for one server request:
    called with the same arguments while the same page renders or handles events,
    it runs once, so several components can ask for the same data without loading
    it twice. Errors are remembered too. An ``async`` function returns a task, to
    read with :func:`use` (``use(get_user(7))``) or to ``await``. Outside a page it
    just calls ``fn``. Each page starts empty; use a database or ``functools.lru_cache``
    for data shared between pages."""
    import asyncio
    import functools

    is_async = inspect.iscoroutinefunction(fn)

    @functools.wraps(fn)
    def cached(*args: Any, **kwargs: Any) -> Any:
        node = CURRENT.get()
        root = node.root if node is not None else ACTIVE_ROOT.get()
        if root is None:
            return fn(*args, **kwargs)
        key = (cached, _cache_key(args, kwargs))
        store = root.cache
        if key not in store:
            if is_async:
                coro: Any = fn(*args, **kwargs)
                task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(coro)
                root.track(task)
                root.signal.on_abort(task.cancel)  # the page closed: stop loading
                store[key] = (True, task)
            else:
                try:
                    store[key] = (True, fn(*args, **kwargs))
                except Exception as exc:
                    store[key] = (False, exc)
        ok, value = store[key]
        if not ok:
            raise value
        return value

    return cached


def flush_sync(fn: Callable[[], T] | None = None) -> T | None:
    """Call ``fn`` (if given), then render and send the resulting updates right away
    instead of at the end of the handler, as React's ``flushSync``: the page shows
    them before the handler goes on (a "Saving..." before a slow call). Returns what
    ``fn`` returned. Not callable while rendering."""
    if CURRENT.get() is not None:
        raise RuntimeError("flush_sync cannot be called while rendering; call it from a handler or effect")
    root = active_root()
    result = fn() if fn is not None else None
    root.flush_now()
    return result


def request_form_reset(form: Any) -> None:
    """Reset a ``<form>`` (a ref attached to it, or its element handle) to its
    initial values, as react-dom's ``requestFormReset``: inside an action (a
    handler, transition or form action) once that action is done, else now. Use it
    with forms whose inputs are uncontrolled (``default_value``)."""
    handle = getattr(form, "current", form)
    if handle is None or not hasattr(handle, "call"):
        raise ValueError("request_form_reset needs a ref attached to a <form>")
    root = active_root()
    action = CURRENT_ACTION.get()
    if action is None or action.done():
        handle.call("reset")
        return
    action.add_done_callback(lambda _: root.call(lambda: handle.call("reset")))


def add_transition_type(name: str) -> None:
    """Name the kind of change being made (``"forward"``, ``"back"``), as React's
    ``addTransitionType``: when the update animates through a :class:`ViewTransition`,
    the browser gets the types, and CSS can tell them apart with
    ``html:active-view-transition-type(forward)``. Call it in the handler or
    transition that makes the change."""
    active_root().add_transition_type(name)


def cache_signal() -> Any:
    """A signal that tells work done for this page that the page has closed, as
    React's ``cacheSignal``: ``.aborted``, ``.on_abort(fn)`` and ``await .wait()``.
    For work started by a :func:`cache` function that should stop early (a long
    query, a stream). None outside a page."""
    node = CURRENT.get()
    root = node.root if node is not None else ACTIVE_ROOT.get()
    return None if root is None else root.signal


def lazy(load: Callable[[], Any]) -> Any:
    """A component loaded on first use, as React's ``lazy``: ``load()`` returns the
    real component; it is called once, when the component first renders, and its
    result is kept. ``load`` may be ``async``: until it finishes the nearest
    :class:`Suspense` shows its fallback, and an error goes to the nearest
    ``ErrorBoundary`` (the next render tries again)::

        Settings = lazy(lambda: importlib.import_module("app.settings").Settings)
    """
    import asyncio

    from .element import Component

    state: dict[str, Any] = {}

    def render(*args: Any, **kwargs: Any) -> Any:
        target = state.get("component")
        if target is None:
            if inspect.iscoroutinefunction(load):
                task = state.get("task")
                if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
                    task = asyncio.ensure_future(load())
                    state["task"] = task
                target = use(task)
            else:
                target = load()
            state["component"] = target
        return target(*args, **kwargs)

    render.__name__ = f"lazy({getattr(load, '__name__', 'load')})"
    return Component(render)


def use_action_state(
    action: Callable[[Any, Any], Any], initial: T
) -> tuple[T, Callable[..., None], bool]:
    """``state, run, pending = use_action_state(action, initial)``.

    ``run(payload)`` calls ``action(previous_state, payload)`` in the background and
    makes its result the new state; ``pending`` is True meanwhile. Passed straight
    to ``on_submit``, ``payload`` is the form's fields (``e.form``). ``action`` may
    be ``async``. If it raises, the state stays as it was and the error is reported.
    """
    state, set_state = use_state(initial)
    pending, start = use_transition()
    holder = use_ref(state)
    fn = use_ref(action)
    fn.current = action
    run = use_memo(lambda: _action_runner(holder, fn, set_state, start), [])
    return state, run, pending


def _action_runner(holder: Ref[Any], fn: Ref[Any], set_state: Any, start: Any) -> Callable[..., None]:
    from .vdom import Event

    # *args, not a default: as an event handler it must receive the event.
    def run(*args: Any) -> None:
        payload = args[0] if args else None
        if isinstance(payload, Event) and payload.get("form") is not None:
            payload = payload["form"]
        action = fn.current
        if inspect.iscoroutinefunction(action):

            async def step() -> None:
                holder.current = await action(holder.current, payload)
                set_state(holder.current)

        else:

            def step() -> None:  # type: ignore[misc]
                holder.current = action(holder.current, payload)
                set_state(holder.current)

        start(step)

    return run


# ---- async data ---------------------------------------------------------------


class Suspended(Exception):
    """Raised by :func:`use_data` while its data loads; caught by the reconciler."""

    def __init__(self, task: Any) -> None:
        super().__init__("waiting for data")
        self.task = task


class BrowserOnly(Suspended):
    """Raised by :func:`browser` while rendering HTML for a response."""

    def __init__(self, reason: str | None) -> None:
        super().__init__(None)
        self.reason = reason


# What a Suspense boundary is told when a component inside is left to the browser.
BROWSER_ONLY: Any = object()


def browser(reason: str | None = None) -> None:
    """Render this component only in the browser, as React's ``browser()``.

    Called at the top of a component, it stops the render while HTML is made for a
    response (the first page load, :func:`prerender`, ``embed``): the nearest
    :class:`Suspense` shows its fallback there, and once the page is live the
    component renders normally. For parts that depend on the browser (the window
    size, local settings) or are too costly to render for every request. Without a
    Suspense boundary the component renders nothing until the page is live.
    ``reason`` documents why. Once the page is live it does nothing."""
    if not _current().root.live:
        raise BrowserOnly(reason)


class AsyncState(Generic[T]):
    """What :func:`use_async` returns: ``.loading``, ``.value``, ``.error`` and ``.reload()``."""

    __slots__ = ("loading", "value", "error", "reload")

    def __init__(self, loading: bool, value: T | None, error: BaseException | None, reload: Callable[[], None]) -> None:
        self.loading = loading
        self.value = value
        self.error = error
        self.reload = reload

    def __repr__(self) -> str:
        state = "loading" if self.loading else f"error={self.error!r}" if self.error else f"value={self.value!r}"
        return f"AsyncState({state})"


class _AsyncSlot:
    __slots__ = ("node", "deps", "fn", "task", "value", "error", "loading", "version", "reload")

    def __init__(self, node: ComponentNode) -> None:
        self.node = node
        self.deps: tuple[Any, ...] | None = None
        self.fn: Callable[[], Any] | None = None
        self.task: Any = None
        self.value: Any = None
        self.error: BaseException | None = None
        self.loading = True
        self.version = 0
        self.reload = lambda: node.root.call(self.start)

    def start(self) -> None:
        self.version += 1
        version = self.version
        if self.task is not None:
            self.task.cancel()
        self.loading = True
        self.error = None
        node = self.node
        # True only while background() is running: without an event loop it calls
        # done() at once, inside this render, which then needs no second render.
        # (A context variable cannot tell: tasks copy the render's context.)
        starting = True

        def done(result: Any, error: BaseException | None) -> None:
            if version != self.version or not node.mounted:
                return
            self.value, self.error, self.loading = result, error, False
            if not starting:
                node.root.schedule(node)

        self.task = node.root.background(self.fn, (), done)  # type: ignore[arg-type]
        starting = False

    def dispose(self) -> None:
        self.version += 1
        if self.task is not None:
            self.task.cancel()


def _async_slot(fn: Callable[[], Any], deps: Sequence[Any]) -> _AsyncSlot:
    slot, new = _slot(_AsyncSlot)
    slot.fn = fn
    if new or _deps_changed(slot.deps, deps):
        slot.deps = tuple(deps)
        slot.start()
    return slot


def use_async(fn: Callable[[], Any], deps: Sequence[Any] = ()) -> AsyncState[Any]:
    """Load data without blocking rendering: ``users = use_async(load_users, [team])``.

    ``fn`` is called with no arguments, again whenever ``deps`` change; it may be
    ``async`` (runs on the loop) or plain (runs in a worker thread). Returns an
    :class:`AsyncState`. The page's first HTML waits for it (up to the server's
    ``data_timeout``), so it arrives with the data in it.
    """
    slot = _async_slot(fn, deps)
    return AsyncState(slot.loading, slot.value, slot.error, slot.reload)


def use_data(fn: Callable[[], Any], deps: Sequence[Any] = ()) -> Any:
    """Like :func:`use_async`, but returns the value itself: while it loads, the
    nearest :class:`Suspense` shows its fallback, and an error goes to the nearest
    :class:`ErrorBoundary`."""
    slot = _async_slot(fn, deps)
    if slot.loading:
        raise Suspended(slot.task)
    if slot.error is not None:
        raise slot.error
    return slot.value


# ---- ids and outside state -------------------------------------------------------


class _IdSlot:
    __slots__ = ("value",)

    def __init__(self, node: ComponentNode) -> None:
        self.value = f"pr-{next(node.root.next_id)}"


def use_id() -> str:
    """An id unique on the page and the same in the server's HTML and the live page,
    for pairing ``<label html_for=...>`` with an input, or ``aria-*`` attributes."""
    slot, _ = _slot(_IdSlot)
    return slot.value


class _ExternalSlot:
    __slots__ = ("get_snapshot", "value", "unsubscribe")

    def __init__(self, node: ComponentNode, subscribe: Callable[..., Any], get_snapshot: Callable[[], Any]) -> None:
        self.get_snapshot = get_snapshot
        self.value = get_snapshot()

        def changed() -> None:
            def apply() -> None:
                value = self.get_snapshot()
                if not same(value, self.value) and node.mounted:
                    self.value = value
                    node.root.schedule(node)

            node.root.call(apply)

        self.unsubscribe = subscribe(changed)

    def dispose(self) -> None:
        if callable(self.unsubscribe):
            self.unsubscribe()


def use_sync_external_store(subscribe: Callable[[Callable[[], None]], Any], get_snapshot: Callable[[], T]) -> T:
    """Read state kept outside components, re-rendering when it changes.

    ``subscribe(callback)`` must call ``callback()`` on every change (from any
    thread) and return a function that unsubscribes; ``get_snapshot()`` returns the
    current value, a new object whenever it changed. :class:`Store` is a ready-made
    one, shared by every session in the process.
    """
    slot, _ = _slot(_ExternalSlot, subscribe, get_snapshot)
    slot.get_snapshot = get_snapshot
    slot.value = get_snapshot()
    return slot.value


# ---- smaller React hooks ---------------------------------------------------------


def use_effect_event(fn: Callable[..., T]) -> Callable[..., T]:
    """A function that always calls the latest ``fn``, but never changes itself, so it
    can be used inside an effect without being one of its dependencies."""
    latest = use_ref(fn)
    latest.current = fn
    return use_memo(lambda: lambda *args, **kwargs: latest.current(*args, **kwargs), [])


def use_imperative_handle(ref: Ref[Any] | None, create: Callable[[], Any], deps: Sequence[Any] | None = None) -> None:
    """Put ``create()`` into ``ref.current`` after rendering, so a parent holding ``ref``
    can call what this component exposes (``ref.current.open()``)."""

    def effect() -> Callable[[], None] | None:
        if ref is None:
            return None
        ref.current = create()

        def cleanup() -> None:
            ref.current = None

        return cleanup

    use_effect(effect, deps)


class _DebugSlot:
    __slots__ = ("last",)

    def __init__(self, node: ComponentNode) -> None:
        self.last: Any = _UNSET


_UNSET = object()


def use_debug_value(value: Any, format: Callable[[Any], Any] | None = None) -> None:
    """In debug mode, log ``value`` (through ``format``) whenever it changes."""
    node = _current()
    slot, _ = _slot(_DebugSlot)
    if node.root.debug and not same(value, slot.last):
        slot.last = value
        logger.info("%s: %r", node.name, format(value) if format is not None else value)
