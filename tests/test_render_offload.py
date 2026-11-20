"""The server renders a page's first HTML in a worker thread, so a large page does
not hold up the other pages on the event loop (React yields every 5 ms instead)."""

import asyncio
import threading
import time

from starlette.testclient import TestClient

from reaction import (
    Root,
    Suspense,
    cache,
    component,
    create_app,
    html,
    lazy,
    use,
    use_async,
    use_data,
    use_state,
)
from reaction.core.ssr import render_to_string


async def asgi_get(app, path="/"):
    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"host", b"testserver")], "scheme": "http", "server": ("testserver", 80),
        "client": ("c", 1), "root_path": "", "http_version": "1.1", "asgi": {"version": "3.0"},
    }
    body = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            body.append(message.get("body", b""))

    await app(scope, receive, send)
    return b"".join(body).decode()


def on_the_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_other_pages_are_served_while_a_large_page_renders():
    @component
    def Large():
        end = time.perf_counter() + 0.3  # a render that takes 300 ms
        while time.perf_counter() < end:
            pass
        return html.p("large")

    @component
    def Small():
        return html.p("small")

    large = create_app(Large, handoff_timeout=0, compress=False)
    small = create_app(Small, handoff_timeout=0, compress=False)
    finished = []

    async def fetch(app, name, after=0.0):
        await asyncio.sleep(after)
        start = time.perf_counter()
        page = await asgi_get(app)
        assert f"<p>{name}</p>" in page
        finished.append((name, time.perf_counter() - start))

    async def main():
        await asgi_get(small)
        await asyncio.gather(fetch(large, "large"), fetch(small, "small", after=0.05))

    asyncio.run(main())
    assert [name for name, _ in finished] == ["small", "large"]
    assert finished[0][1] < 0.15, "the small page did not wait for the large render"


def test_a_path_that_renders_quickly_renders_on_the_loop_next_time():
    where = []

    @component
    def Quick():
        where.append(on_the_loop())
        return html.p("quick")

    @component
    def Slow():
        where.append(on_the_loop())
        time.sleep(0.01)
        return html.p("slow")

    @component
    def App():
        return Quick() if use_path() == "/quick" else Slow()

    client = TestClient(create_app(App, handoff_timeout=0))
    for _ in range(2):
        client.get("/quick")
    for _ in range(2):
        client.get("/slow")
    # A path not seen before renders in a thread; one known to take under 5 ms does
    # not (a thread costs about 0.2 ms); one that took longer keeps doing so.
    assert where == [False, True, False, False]


def use_path():
    from reaction import use_location

    return use_location().path


def test_data_loading_works_from_a_render_in_a_worker_thread():
    where = []

    async def load_async():
        await asyncio.sleep(0.01)
        return "async-data"

    def load_sync():
        return "sync-data"

    @cache
    async def user(n):
        await asyncio.sleep(0.01)
        return f"user-{n}"

    async def load_widget():
        await asyncio.sleep(0.01)

        @component
        def Widget():
            return html.b("lazy-widget")

        return Widget

    Widget = lazy(load_widget)

    @component
    def Waits():
        return html.p(use_data(load_async), " ", use(user(7)))

    @component
    def App():
        where.append(on_the_loop())
        state = use_async(load_sync)
        return html.div(
            html.span(state.value or "loading"),
            Suspense(Waits(), fallback=html.i("...")),
            Suspense(Widget(), fallback=html.i("...")),
        )

    for stream in (False, True):
        where.clear()
        page = TestClient(create_app(App, stream=stream, handoff_timeout=0)).get("/").text
        assert where[0] is False, "the first render ran in a worker thread"
        for text in ("sync-data", "async-data", "user-7", "lazy-widget"):
            assert text in page, (stream, text)


def test_updates_from_other_threads_wait_until_an_off_loop_render_is_done():
    setters = []
    dirty_during = []

    @component
    def App():
        n, set_n = use_state(0)
        setters.append(set_n)
        return html.p(n)

    async def main():
        root = Root(App(), ops=False, mount=False)
        await root.mount_async()

        def render():
            other = threading.Thread(target=lambda: setters[0](5))
            other.start()
            other.join()
            dirty_during.append(bool(root._dirty))  # the update did not land under us

        await root.off_loop(render)
        assert await root.settle(1)
        html_out = render_to_string(root)
        root.close()
        return html_out

    assert asyncio.run(main()) == "<p>5</p>"
    assert dirty_during == [False]


def test_an_error_in_the_first_render_reaches_on_error():
    seen = []

    @component
    def App():
        raise ValueError("boom")

    client = TestClient(
        create_app(App, on_error=lambda exc, where, conn: seen.append((type(exc), conn is not None))),
        raise_server_exceptions=False,
    )
    client.get("/")
    assert (ValueError, True) in seen
