"""The last React APIs: flush_sync, request_form_reset, add_transition_type, browser(),
render_to_static_markup, cache_signal, forward_ref and lazy."""

import asyncio
import json
import logging
import time

import pytest
from starlette.testclient import TestClient

from reaction import (
    ErrorBoundary,
    Root,
    Suspense,
    ViewTransition,
    add_transition_type,
    browser,
    cache,
    cache_signal,
    component,
    create_app,
    flush_sync,
    forward_ref,
    html,
    js_component,
    lazy,
    prerender,
    render_to_static_markup,
    render_to_string,
    request_form_reset,
    use_ref,
    use_state,
)
from reaction.testing import render

from .fakedom import FakeDom


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


class Live:
    def __init__(self, element):
        self.root = Root(element)
        self.dom = FakeDom()
        self.sent = []
        self.root.attach(self._receive)

    def _receive(self, message):
        self.sent.append((time.monotonic(), message))
        self.dom.apply(message["ops"])

    def node(self, tag):
        return next(i for i, n in self.dom.nodes.items() if getattr(n, "tag", None) == tag)


# ---- flush_sync -----------------------------------------------------------------------


@pytest.mark.parametrize("use_fn", [True, False])
def test_flush_sync_sends_the_update_before_the_handler_goes_on(use_fn):
    @component
    def App():
        status, set_status = use_state("idle")

        def save():
            if use_fn:
                flush_sync(lambda: set_status("saving"))
            else:
                set_status("saving")
                flush_sync()
            time.sleep(0.3)  # the slow call
            set_status("saved")

        return html.button(status, on_click=save)

    async def main():
        live = Live(App())
        start = time.monotonic()
        live.root.dispatch(1, live.node("button"), "click", {})
        await asyncio.sleep(0.15)
        during = live.dom.html()
        await live.root.settle()
        await asyncio.sleep(0.02)
        return during, live.dom.html(), [t - start for t, m in live.sent if m["ops"]]

    during, after, times = asyncio.run(main())
    assert during == "<button>saving</button>", "shown while the slow call runs"
    assert after == "<button>saved</button>"
    assert times[0] < 0.15


def test_flush_sync_in_an_async_handler_and_errors():
    @component
    def App():
        status, set_status = use_state("idle")

        async def save():
            flush_sync(lambda: set_status("saving"))
            await asyncio.sleep(0.2)
            set_status("saved")

        return html.button(status, on_click=save)

    async def main():
        live = Live(App())
        live.root.dispatch(1, live.node("button"), "click", {})
        await asyncio.sleep(0.1)
        during = live.dom.html()
        await live.root.settle()
        return during, live.dom.html()

    assert asyncio.run(main()) == ("<button>saving</button>", "<button>saved</button>")

    @component
    def Bad():
        flush_sync()
        return html.p()

    with render(Bad(), raise_errors=False) as page:
        assert "cannot be called while rendering" in str(page.errors[0])


# ---- request_form_reset ------------------------------------------------------------------


def test_request_form_reset_resets_after_the_action_or_at_once():
    @component
    def App():
        form = use_ref()

        def later():
            request_form_reset(form)
            time.sleep(0.05)

        return html.form(
            html.input(name="q", default_value="x"),
            html.button("now", id="now", type="button", on_click=lambda: request_form_reset(form.current)),
            html.button("later", id="later", type="button", on_click=later),
            ref=form,
        )

    with render(App()) as page:
        page.click("#now")
        assert [(n.tag, m) for n, m, _ in page.calls] == [("form", "reset")]
        page.click("#later")
        assert [m for _, m, _ in page.calls] == ["reset", "reset"]

    @component
    def Wrong():
        return html.button("x", on_click=lambda: request_form_reset("not a ref"))

    with render(Wrong(), raise_errors=False) as page:
        page.click("button")
        assert "needs a ref" in str(page.errors[0])


# ---- add_transition_type --------------------------------------------------------------------


def test_transition_types_travel_with_the_animated_update():
    @component
    def App():
        page, set_page = use_state(1)

        def go(step):
            add_transition_type("forward" if step > 0 else "back")
            set_page(page + step)

        return html.div(
            ViewTransition(html.p(f"page {page}")),
            html.button("next", id="next", on_click=lambda: go(1)),
            html.button("prev", id="prev", on_click=lambda: go(-1)),
        )

    async def main():
        live = Live(App())
        buttons = [i for i, n in live.dom.nodes.items() if getattr(n, "tag", None) == "button"]
        live.sent.clear()
        live.root.dispatch(1, buttons[0], "click", {})
        await live.root.settle()
        live.root.dispatch(2, buttons[1], "click", {})
        await live.root.settle()
        return [m.get("vt") for _, m in live.sent if m["ops"]]

    assert asyncio.run(main()) == [["forward"], ["back"]]


# ---- browser() --------------------------------------------------------------------------------


@component
def WindowSize():
    browser("reads the window size")
    return html.p("measured", class_name="size")


@component
def Dashboard():
    n, set_n = use_state(0)
    return html.main(
        html.h1("Dashboard"),
        Suspense(WindowSize(), fallback=html.p("sizing...", class_name="fallback")),
        html.button(n, on_click=lambda: set_n(n + 1)),
    )


def test_browser_only_shows_the_fallback_in_server_html():
    client = TestClient(create_app(Dashboard))
    page = client.get("/").text
    assert "sizing..." in page and "measured" not in page
    assert "measured" not in prerender(Dashboard, ["/"])["/"]
    assert "sizing..." in render_to_string(Dashboard())


def test_browser_only_renders_once_live_and_the_page_is_adopted():
    client = TestClient(create_app(Dashboard))
    page = client.get("/").text
    body = page.split('<div id="pyreaction-root">')[1].split("</div>\n")[0]
    with client.websocket_connect("/_pyreaction/ws?p=/&h=1") as ws:
        first = json.loads(ws.receive_text())
        dom = FakeDom()
        dom.apply(first["ops"])
        assert "sizing..." in dom.html(), "the first live tree matches the server's HTML"
        assert dom.html().replace('<div hidden="">', '<div hidden="">') == body.replace("<!---->", "").replace(
            "<!--pr-s:1-->", "").replace("<!--/pr-s:1-->", "")
        update = json.loads(ws.receive_text())
        dom.apply(update["ops"])
        assert '<p class="size">measured</p>' in dom.html() and "sizing..." not in dom.html()


def test_browser_only_in_the_test_page_and_without_a_boundary():
    with render(Dashboard()) as page:
        assert page.find(".size").text == "measured"
        assert page.find_all(".fallback") == []

    @component
    def Bare():
        return html.div(WindowSize(), html.p("rest"))

    assert render_to_string(Bare()) == "<div><p>rest</p></div>"
    with render(Bare()) as page:
        assert page.html() == '<div><p class="size">measured</p><p>rest</p></div>'


# ---- render_to_static_markup --------------------------------------------------------------------


def test_static_markup_leaves_out_what_hydration_needs():
    Chart = js_component("/static/chart.js", "mount")

    @component
    def Mail(name):
        return html.div(
            "Hello ", name, "!", "",
            html.article(dangerously_set_inner_html="<b>raw</b>"),
            Chart(values=[1], fallback="[chart]"),
        )

    hydratable = render_to_string(Mail("ada"))
    assert "<!---->" in hydratable and "<!--$-->" in hydratable and "data-pyreaction" in hydratable
    assert render_to_static_markup(Mail("ada")) == (
        "<div>Hello ada!<article><b>raw</b></article><div>[chart]</div></div>"
    )
    assert render_to_static_markup(html.script("if (a</b) {}")) == "<script>if (a<\\/b) {}</script>"


# ---- cache_signal ---------------------------------------------------------------------------------


def test_cache_signal_aborts_when_the_page_closes_and_cancels_cached_work():
    seen = {}

    @cache
    async def stream(topic):
        signal = cache_signal()
        seen["signal"] = signal
        signal.on_abort(lambda: seen.setdefault("aborted", True))
        await asyncio.sleep(10)
        return "never"

    @component
    def App():
        return Suspense(html.p(stream_value()), fallback=html.p("loading"))

    def stream_value():
        from reaction import use

        return use(stream("news"))

    page = render(App(), timeout=0.2)
    task = next(iter(page.root.cache.values()))[1]
    assert cache_signal() is None, "outside a page"
    assert not seen["signal"].aborted
    page.close()
    assert seen["signal"].aborted and seen["aborted"] is True
    assert task.cancelled() or task.done()

    async def wait_on_closed():
        await seen["signal"].wait()  # already aborted: returns at once
        return "ok"

    assert asyncio.run(wait_on_closed()) == "ok"


# ---- forward_ref and lazy ----------------------------------------------------------------------------


def test_forward_ref_passes_the_ref_through():
    @forward_ref
    def Field(label, ref=None):
        return html.label(label, html.input(ref=ref))

    @component
    def App():
        box = use_ref()
        return html.div(Field("Name", ref=box), html.button("focus", on_click=lambda: box.current.focus()))

    with render(App()) as page:
        page.click("button")
        assert [(n.tag, m) for n, m, _ in page.calls] == [("input", "focus")]
    assert forward_ref(Field) is Field


def test_lazy_loads_once_sync_and_async():
    loads = []

    def load_sync():
        loads.append("sync")

        @component
        def Settings(user):
            return html.p(f"settings for {user}")

        return Settings

    async def load_async():
        loads.append("async")
        await asyncio.sleep(0.05)

        @component
        def Chart(n):
            return html.b(f"chart {n}")

        return Chart

    Settings = lazy(load_sync)
    Chart = lazy(load_async)

    @component
    def App():
        return html.div(Settings("ada"), Settings("bob"), Suspense(Chart(1), Chart(2), fallback=html.i("...")))

    with render(App()) as page:
        assert page.html() == (
            '<div><p>settings for ada</p><p>settings for bob</p>'
            '<div style="display:contents"><b>chart 1</b><b>chart 2</b></div></div>'
        )
    with render(App()):
        pass
    assert loads == ["sync", "async"], "each loader runs once"


def test_lazy_error_goes_to_the_boundary_and_is_retried():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ImportError("not yet")
        return component(lambda: html.p("loaded"))

    Panel = lazy(flaky)

    @component
    def App():
        return ErrorBoundary(Panel(), fallback=lambda e, reset: html.button(f"retry: {e}", on_click=reset))

    with render(App()) as page:
        assert page.html() == "<button>retry: not yet</button>"
        page.click("button")
        assert page.html() == "<p>loaded</p>"
