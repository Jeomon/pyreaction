"""Stores shared across processes through Redis (a fakeredis TCP server here)."""

import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

pytest.importorskip("redis")
fakeredis = pytest.importorskip("fakeredis")

from reaction import RedisBackend, Store, component, html, use_store  # noqa: E402
from reaction.testing import render  # noqa: E402


@pytest.fixture
def redis_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = fakeredis.TcpFakeServer(("127.0.0.1", port), server_type="redis")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()
    server.server_close()


@pytest.fixture
def backends(redis_url):
    made = []

    def make():
        backend = RedisBackend(redis_url)
        made.append(backend)
        return backend

    yield make
    for backend in made:
        backend.close()


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_a_write_in_one_process_reaches_the_others(backends):
    # Each backend has its own connection and listener, as separate processes would.
    a = Store({"yes": 0}, name="votes", backend=backends())
    b = Store({"yes": 0}, name="votes", backend=backends())
    a.update(lambda v: {**v, "yes": v["yes"] + 1})
    assert a.get() == {"yes": 1}, "the writer reads its own write at once"
    wait_for(lambda: b.get() == {"yes": 1})
    b.set({"yes": 10})
    wait_for(lambda: a.get() == {"yes": 10})


def test_a_new_process_starts_from_the_stored_value(backends):
    Store(0, name="n", backend=backends()).set(5)
    late = Store(0, name="n", backend=backends())
    assert late.get() == 5


def test_concurrent_updates_from_several_processes_lose_nothing(backends):
    stores = [Store(0, name="count", backend=backends()) for _ in range(3)]

    def bump(store):
        for _ in range(40):
            store.update(lambda n: n + 1)

    threads = [threading.Thread(target=bump, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for store in stores:
        wait_for(lambda store=store: store.get() == 120)


def test_a_real_second_process_shares_the_store(backends, redis_url):
    mine = Store(0, name="hits", backend=backends())
    script = textwrap.dedent(f"""
        from reaction import RedisBackend, Store
        backend = RedisBackend({redis_url!r})
        store = Store(0, name="hits", backend=backend)
        for _ in range(50):
            store.update(lambda n: n + 1)
        backend.close()
    """)
    other = subprocess.Popen([sys.executable, "-c", script])
    for _ in range(50):
        mine.update(lambda n: n + 1)
    assert other.wait(timeout=30) == 0
    wait_for(lambda: mine.get() == 100)


def test_components_re_render_on_a_write_from_elsewhere(backends):
    shown = Store("hello", name="banner", backend=backends())
    elsewhere = Store("hello", name="banner", backend=backends())

    @component
    def Banner():
        return html.p(use_store(shown))

    with render(Banner()) as page:
        assert page.html() == "<p>hello</p>"
        elsewhere.set("changed in another process")
        wait_for(lambda: (page.settle(), page.html())[1] == "<p>changed in another process</p>")


def test_unchanged_writes_are_skipped_and_a_name_is_required(backends):
    store = Store([1, 2], name="list", backend=backends())
    seen = []
    store.subscribe(lambda: seen.append(store.get()))
    store.update(lambda v: v)  # same value: nothing written or announced
    time.sleep(0.1)
    assert seen == []
    with pytest.raises(TypeError):
        Store(0, backend=backends())
