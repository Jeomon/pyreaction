"""State shared by every session in the process: chat rooms, live dashboards, counters."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from .hooks import same, use_sync_external_store

__all__ = ["Store", "use_store"]

T = TypeVar("T")

logger = logging.getLogger("pyreaction")


class Store(Generic[T]):
    """A value that components in any session can read with :func:`use_store`.

    ``store.set(value)`` or ``store.update(lambda old: new)`` re-renders every
    component reading it, in every open page. Safe from any thread. It lives in
    one process: with several worker processes, keep the source of truth in a
    database or Redis and update the store from it.
    """

    def __init__(self, value: T) -> None:
        self._value = value
        self._subscribers: set[Callable[[], None]] = set()
        self._lock = threading.Lock()

    def get(self) -> T:
        return self._value

    def set(self, value: T) -> None:
        with self._lock:
            if same(value, self._value):
                return
            self._value = value
            subscribers = list(self._subscribers)
        self._notify(subscribers)

    def update(self, fn: Callable[[T], T]) -> T:
        """Replace the value with ``fn(old)``, atomically; returns the new value."""
        with self._lock:
            value = fn(self._value)
            if same(value, self._value):
                return value
            self._value = value
            subscribers = list(self._subscribers)
        self._notify(subscribers)
        return value

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.add(callback)

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.discard(callback)

        return unsubscribe

    @staticmethod
    def _notify(subscribers: list[Callable[[], None]]) -> None:
        for callback in subscribers:
            try:
                callback()
            except Exception:
                logger.exception("A store subscriber failed")

    def __repr__(self) -> str:
        return f"Store({self._value!r})"


def use_store(store: Store[T], selector: Callable[[T], Any] | None = None) -> Any:
    """The store's value (or ``selector(value)``); re-renders when it changes."""
    if selector is None:
        return use_sync_external_store(store.subscribe, store.get)
    return use_sync_external_store(store.subscribe, lambda: selector(store.get()))
