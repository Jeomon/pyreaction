"""Hooks: per-component state kept in call order on the component being rendered."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from .reconciler import ComponentNode

__all__ = [
    "Context",
    "Ref",
    "create_context",
    "use_callback",
    "use_context",
    "use_effect",
    "use_location",
    "use_memo",
    "use_navigate",
    "use_reducer",
    "use_ref",
    "use_request",
    "use_state",
    "use_title",
]

T = TypeVar("T")

CURRENT: ContextVar[ComponentNode | None] = ContextVar("pyreaction_current", default=None)

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


def _slot(kind: type[T], *args: Any) -> tuple[T, bool]:
    """The current component's next hook slot, created on first render."""
    node = CURRENT.get()
    if node is None:
        raise RuntimeError("Hooks can only be called while a component is rendering.")
    i = node.hook_index
    node.hook_index = i + 1
    hooks = node.hooks
    if i < len(hooks):
        slot = hooks[i]
        if type(slot) is not kind:
            raise RuntimeError(
                f"Hook order changed in {node.name}: expected {type(slot).__name__}, "
                f"got {kind.__name__}. Hooks must not be called conditionally."
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
        self.value = None
        self.deps: Sequence[Any] | None = None


def use_memo(fn: Callable[[], T], deps: Sequence[Any] | None = None) -> T:
    """Recompute ``fn()`` only when ``deps`` change (every render when ``deps`` is None)."""
    slot, new = _slot(_MemoSlot)
    if new or _deps_changed(slot.deps, deps):
        slot.value = fn()
        slot.deps = None if deps is None else tuple(deps)
    return slot.value  # type: ignore[return-value]


def use_callback(fn: T, deps: Sequence[Any] | None = None) -> T:
    """Keep the same function object while ``deps`` are unchanged."""
    return use_memo(lambda: fn, deps)


class EffectSlot:
    __slots__ = ("node", "fn", "deps", "cleanup")

    def __init__(self, node: ComponentNode) -> None:
        self.node = node
        self.fn: Callable[[], Any] | None = None
        self.deps: Sequence[Any] | None = None
        self.cleanup: Callable[[], Any] | None = None


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


class Context(Generic[T]):
    """Created by :func:`create_context`. ``ctx(value, *children)`` provides a value."""

    def __init__(self, default: T, name: str = "Context") -> None:
        self.default = default
        self.__name__ = name

    def __call__(self, *children: Any, value: T, key: Any = None) -> Any:
        from .element import Element

        return Element(self, key, children=children, kwargs={"value": value})

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
        raise RuntimeError("Hooks can only be called while a component is rendering.")
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
