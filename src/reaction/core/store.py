"""State shared by every session: chat rooms, live dashboards, counters.

A plain :class:`Store` lives in one process. Give it a ``name`` and a backend such
as :class:`RedisBackend` and it is shared by every worker process using that
backend: a write anywhere re-renders the readers everywhere.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Generic, Protocol, TypeVar

import orjson

from .hooks import same, use_sync_external_store

__all__ = ["RedisBackend", "Store", "StoreBackend", "use_store"]

T = TypeVar("T")

logger = logging.getLogger("reaction")


class StoreBackend(Protocol):
    """Where shared stores keep their value and announce changes.

    Values travel as text (JSON) with a version number that grows with every write.
    """

    def load(self, name: str) -> tuple[int, str] | None:
        """The stored (version, text), or None if nothing was written yet."""

    def write(self, name: str, fn: Callable[[str | None], str | None]) -> tuple[int, str] | None:
        """Atomically replace the text with ``fn(old text)`` and tell every listener;
        ``fn`` may run more than once, and returning None leaves the value alone.
        Returns the new (version, text), or None when nothing was written."""

    def listen(self, name: str, callback: Callable[[int, str], None]) -> None:
        """Call ``callback(version, text)`` (from any thread) after each write."""


class Store(Generic[T]):
    """A value that components in any session can read with :func:`use_store`.

    ``store.set(value)`` or ``store.update(lambda old: new)`` re-renders every
    component reading it, in every open page. Safe from any thread.

    By itself a store lives in one process. With ``backend`` (and a ``name``
    unique among the app's stores) it is shared by every process using the same
    backend; its value must then be JSON (lists come back for tuples), and ``set``
    and ``update`` make a network round trip, so call them from handlers (which run
    in threads) rather than from async code that should not block::

        votes = Store({"yes": 0, "no": 0}, name="votes", backend=RedisBackend("redis://..."))
    """

    def __init__(self, value: T, *, name: str | None = None, backend: StoreBackend | None = None) -> None:
        self._value = value
        self._initial = value
        self._version = 0
        self._subscribers: set[Callable[[], None]] = set()
        self._lock = threading.Lock()
        self._name = name
        self._backend = backend
        if backend is not None:
            if not name:
                raise TypeError("a Store with a backend needs a name")
            # Listen first, then load: a write in between is seen either way, and
            # the version numbers keep the newer of the two.
            backend.listen(name, self._received)
            stored = backend.load(name)
            if stored is not None:
                self._received(*stored)

    def get(self) -> T:
        return self._value

    def set(self, value: T) -> None:
        if self._backend is not None:
            self.update(lambda _: value)
            return
        with self._lock:
            if same(value, self._value):
                return
            self._value = value
            subscribers = list(self._subscribers)
        self._notify(subscribers)

    def update(self, fn: Callable[[T], T]) -> T:
        """Replace the value with ``fn(old)``, atomically (across processes too, with
        a backend, where ``fn`` may run again if another write got in first);
        returns the new value."""
        if self._backend is not None:
            return self._update_shared(fn)
        with self._lock:
            value = fn(self._value)
            if same(value, self._value):
                return value
            self._value = value
            subscribers = list(self._subscribers)
        self._notify(subscribers)
        return value

    def _update_shared(self, fn: Callable[[T], T]) -> T:
        assert self._backend is not None and self._name is not None
        result: list[T] = []

        def step(old_text: str | None) -> str | None:
            old = self._initial if old_text is None else orjson.loads(old_text)
            new = fn(old)
            result[:] = [new]
            return None if same(new, old) else orjson.dumps(new, option=orjson.OPT_NON_STR_KEYS).decode()

        written = self._backend.write(self._name, step)
        if written is not None:
            self._apply(written[0], result[0])  # read your own write at once
        return result[0]

    def _received(self, version: int, text: str) -> None:
        if version > self._version:
            self._apply(version, orjson.loads(text))

    def _apply(self, version: int, value: Any) -> None:
        with self._lock:
            # Writes reach each process in order, but this process also applies its
            # own at once: an older version arriving later must not undo it.
            if version <= self._version:
                return
            self._version = version
            self._value = value
            subscribers = list(self._subscribers)
        self._notify(subscribers)

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


class RedisBackend:
    """Shares named stores through Redis (or Valkey), for apps with several worker
    processes or machines. Needs the ``redis`` package: ``pip install pyreaction[redis]``.

    Each store is one key, ``prefix + name``, holding ``"<version>:<json>"``; every
    write also publishes it on a channel of the same name. One background thread
    per backend listens for all its stores.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", *, prefix: str = "pyreaction:store:", client: Any = None) -> None:
        import redis

        self._redis = client if client is not None else redis.Redis.from_url(url)
        self._watch_error = redis.WatchError
        self.prefix = prefix
        self._callbacks: dict[str, list[Callable[[int, str], None]]] = {}
        self._lock = threading.Lock()
        self._listener: Any = None

    @staticmethod
    def _split(raw: bytes | str) -> tuple[int, str]:
        text = raw.decode() if isinstance(raw, bytes) else raw
        version, _, text = text.partition(":")
        return int(version), text

    def load(self, name: str) -> tuple[int, str] | None:
        raw = self._redis.get(self.prefix + name)
        return None if raw is None else self._split(raw)

    def write(self, name: str, fn: Callable[[str | None], str | None]) -> tuple[int, str] | None:
        key = self.prefix + name
        with self._redis.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    version, old = (0, None) if raw is None else self._split(raw)
                    new = fn(old)
                    if new is None:
                        pipe.unwatch()
                        return None
                    payload = f"{version + 1}:{new}"
                    pipe.multi()
                    pipe.set(key, payload)
                    pipe.publish(key, payload)
                    pipe.execute()
                    return version + 1, new
                except self._watch_error:
                    continue  # another process wrote first: run fn again on its value

    def listen(self, name: str, callback: Callable[[int, str], None]) -> None:
        with self._lock:
            self._callbacks.setdefault(self.prefix + name, []).append(callback)
            if self._listener is None:
                pubsub = self._redis.pubsub()
                pubsub.psubscribe(**{self.prefix + "*": self._message})
                # Wait until Redis confirms, so no write after this call is missed.
                while pubsub.get_message(timeout=1.0) is None:
                    pass
                self._listener = pubsub.run_in_thread(
                    sleep_time=0.2, daemon=True, exception_handler=self._listener_failed
                )

    @staticmethod
    def _listener_failed(exc: BaseException, pubsub: Any, thread: Any) -> None:
        logger.error("Lost the Redis connection for shared stores", exc_info=exc)
        thread.stop()

    def _message(self, message: dict[str, Any]) -> None:
        channel = message["channel"]
        channel = channel.decode() if isinstance(channel, bytes) else channel
        try:
            version, text = self._split(message["data"])
        except ValueError:
            logger.warning("Ignored a malformed store message on %s", channel)
            return
        for callback in list(self._callbacks.get(channel, ())):
            try:
                callback(version, text)
            except Exception:
                logger.exception("A shared store failed to apply an update")

    def close(self) -> None:
        """Stop listening and close the connection."""
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()
            # Let the thread finish its current read before its socket closes.
            listener.join(timeout=2)
            listener.pubsub.close()
        self._redis.close()
