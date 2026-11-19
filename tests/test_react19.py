"""React 19 features found missing by the comparison with the React repository."""

import asyncio
import logging
import time

import pytest
from starlette.testclient import TestClient

from pyreaction import (
    Children,
    Profiler,
    Root,
    Suspense,
    ViewTransition,
    cache,
    clone_element,
    component,
    create_app,
    create_context,
    create_ref,
    html,
    is_valid_element,
    preconnect,
    preinit,
    preload,
    prerender,
    start_transition,
    use,
    use_deferred_value,
    use_memo,
    use_optimistic,
    use_state,
)
from pyreaction.testing import render

from .fakedom import FakeDom


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


class Live:
    """A root attached to a fake browser on a real loop, keeping every message."""

    def __init__(self, element):
        self.root = Root(element)
        self.dom = FakeDom()
        self.sent = []
        self.root.attach(self._receive)

    def _receive(self, message):
        self.sent.append(message)
        self.dom.apply(message["ops"])

    def node(self, tag):
        return next(i for i, n in self.dom.nodes.items() if getattr(n, "tag", None) == tag)

    async def settle(self):
        for _ in range(3):
            await self.root.settle(2)
            await asyncio.sleep(0.02)


# ---- refs, events, forms -----------------------------------------------------------


def test_callback_ref_gets_the_element_and_its_cleanup_runs_on_removal():
    log = []

    def attach(el):
        log.append(("attach", type(el).__name__))
        return lambda: log.append("cleanup")

    @component
    def App():
        on, set_on = use_state(True)
        return html.div(html.p("x", ref=attach) if on else None, html.button("hide", on_click=lambda: set_on(False)))

    with render(App()) as page:
        assert log == [("attach", "ElementHandle")]
        page.click("button")
    assert log == [("attach", "ElementHandle"), "cleanup"]


def test_callback_ref_without_cleanup_is_called_with_none():
    log = []

    @component
    def App():
        on, set_on = use_state(True)
        return html.div(html.p("x", ref=log.append) if on else None, html.button("hide", on_click=lambda: set_on(False)))

    with render(App()) as page:
        page.click("button")
    assert [type(x).__name__ for x in log] == ["ElementHandle", "NoneType"]


def test_capture_handlers_run_outside_in_before_bubbling_ones():
    order = []

    @component
    def App():
        return html.div(
            html.section(
                html.button("b", on_click=lambda: order.append("button")),
                on_click_capture=lambda: order.append("section capture"),
                on_click=lambda: order.append("section"),
            ),
            on_click_capture=lambda: order.append("div capture"),
            on_click=lambda: order.append("div"),
        )

    with render(App()) as page:
        page.click("button")
    assert order == ["div capture", "section capture", "button", "section", "div"]


def test_capture_prop_names():
    root = Root(html.div(on_click_capture=lambda: 0, onKeyDownCapture=lambda: 0, on_got_pointer_capture=lambda: 0))
    assert set(root.container.children[0].events) == {"click:capture", "keydown:capture", "gotpointercapture"}
    root.close()


def test_form_action_runs_with_the_fields_and_pending_status():
    from pyreaction import use_form_status

    got = []

    @component
    def Submit():
        return html.button("saving" if use_form_status().pending else "save")

    @component
    def App():
        return html.form(html.input(name="q", default_value="hi"), Submit(), action=lambda f: got.append(dict(f)))

    with render(App()) as page:
        page.submit("form")
    assert got == [{"q": "hi"}]


def test_button_form_action_overrides_the_form_with_its_own_value():
    saved, drafted = [], []

    @component
    def App():
        return html.form(
            html.input(name="title", default_value="post"),
            html.button("Publish", action=None),
            html.button("Save draft", name="kind", value="draft", form_action=lambda f: drafted.append(dict(f))),
            on_submit=lambda e: saved.append(dict(e.form)),
        )

    with render(App()) as page:
        page.click(page.find_all("button")[1])
    assert drafted == [{"title": "post", "kind": "draft"}] and saved == []


# ---- head ---------------------------------------------------------------------------


def test_title_meta_and_link_anywhere_go_to_head():
    @component
    def Post():
        return html.article(
            html.title("My post"),
            html.meta(name="description", content="about it"),
            html.link(rel="canonical", href="/post"),
            html.meta(itemprop="author", content="ada"),  # page metadata: stays put
            html.p("text"),
        )

    page = TestClient(create_app(Post)).get("/").text
    head, body = page.split("</head>")
    assert "<title>My post</title>" in head
    assert '<meta name="description" content="about it" data-pyreaction-head="">' in head
    assert '<link rel="canonical" href="/post" data-pyreaction-head="">' in head
    assert '<article><meta itemprop="author" content="ada"><p>text</p></article>' in body


def test_svg_title_is_a_tooltip_and_stays():
    with render(html.svg(html.title("tip"), html.circle(r=3))) as page:
        assert page.html() == "<svg><title>tip</title><circle r=\"3\"></circle></svg>"
        assert page.title is None


def test_resource_hints_go_to_head_once():
    @component
    def Card(i):
        preload("/hero.jpg", as_="image")
        return html.p(i)

    @component
    def App():
        preconnect("https://api.example.com")
        preinit("/app.css", as_="style", precedence="high")
        return html.div([Card(i, key=i) for i in range(3)])

    head = TestClient(create_app(App)).get("/").text.split("</head>")[0]
    assert head.count('href="/hero.jpg"') == 1
    assert '<link rel="preconnect" href="https://api.example.com"' in head
    assert '<link rel="stylesheet" href="/app.css" data-precedence="high"' in head
    with pytest.raises(ValueError):
        with render(html.p()) as page:
            page.root.call(lambda: None)
            preinit("/x", as_="font")


def test_resource_hints_from_a_handler_reach_the_live_page():
    @component
    def App():
        return html.button("go", on_click=lambda: preload("/next.js", as_="script"))

    with render(App()) as page:
        assert page.head == ""
        page.click("button")
        assert 'rel="preload" href="/next.js" as="script"' in page.head


# ---- transitions and optimistic updates ---------------------------------------------


def test_start_transition_runs_in_the_background():
    @component
    def App():
        state, set_state = use_state("idle")

        def slow():
            time.sleep(0.05)
            set_state("done")

        return html.button(state, on_click=lambda: start_transition(slow))

    with render(App()) as page:
        page.click("button")
        assert page.html() == "<button>done</button>"


def test_start_transition_outside_a_page_is_an_error():
    with pytest.raises(RuntimeError, match="only callable"):
        start_transition(lambda: None)


def test_use_optimistic_shows_the_change_until_the_action_is_done():
    @component
    def App():
        items, set_items = use_state([])
        shown, add = use_optimistic(items, lambda cur, x: cur + [x + " (sending)"])

        def send():
            add("hi")
            time.sleep(0.2)
            set_items(items + ["hi"])

        return html.div(html.button("send", on_click=send), html.ul([html.li(t, key=t) for t in shown]))

    async def main():
        live = Live(App())
        live.root.dispatch(1, live.node("button"), "click", {})
        await asyncio.sleep(0.08)
        during = live.dom.html()
        await live.settle()
        return during, live.dom.html()

    during, after = asyncio.run(main())
    assert "<li>hi (sending)</li>" in during
    assert "<li>hi</li>" in after and "sending" not in after


def test_use_optimistic_reverts_when_the_action_fails():
    @component
    def App():
        liked, set_liked = use_state(False)
        shown, set_shown = use_optimistic(liked)

        async def like():
            set_shown(True)
            await asyncio.sleep(0.05)
            raise ConnectionError("server down")

        return html.button("liked" if shown else "like", on_click=like)

    async def main():
        live = Live(App())
        live.root.dispatch(1, live.node("button"), "click", {})
        await asyncio.sleep(0.02)
        during = live.dom.html()
        await live.settle()
        return during, live.dom.html()

    assert asyncio.run(main()) == ("<button>liked</button>", "<button>like</button>")


# ---- deferred values, use(), cache ----------------------------------------------------


def test_use_deferred_value_renders_the_new_value_in_a_second_message():
    @component
    def Search():
        q, set_q = use_state("")
        slow = use_deferred_value(q)
        return html.div(html.input(value=q, on_change=lambda e: set_q(e.value)), html.p(f"results for {slow}"))

    async def main():
        live = Live(Search())
        live.sent.clear()
        live.root.dispatch(1, live.node("input"), "input", {"value": "ab"})
        await live.settle()
        return live.sent, live.dom.html()

    sent, final = asyncio.run(main())
    texts = [[op for op in m["ops"] if op[0] == "x"] for m in sent if m["ops"]]
    assert texts[0] == [], "the input updates first, results unchanged"
    assert texts[1][0][2] == "results for ab"
    assert "results for ab" in final


def test_use_deferred_value_in_the_first_html_is_current():
    @component
    def App():
        return html.p(use_deferred_value("now", initial_value="before"))

    assert '<p>now</p>' in TestClient(create_app(App)).get("/").text


def test_use_reads_context_conditionally_and_waits_for_a_task():
    Theme = create_context("light")

    @component
    def Leaf(read):
        return html.span(use(Theme) if read else "-")

    @component
    def Loaded(task):
        return html.b(use(task))

    @component
    def App():
        task = use_memo(lambda: asyncio.ensure_future(asyncio.sleep(0.05, result="data")), [])
        return Theme(html.div(Leaf(True), Leaf(False), Suspense(Loaded(task), fallback=html.i("..."))), value="dark")

    with render(App()) as page:
        assert page.html() == '<div><span>dark</span><span>-</span><div style="display:contents"><b>data</b></div></div>'


def test_use_refuses_a_coroutine():
    async def load():
        return 1

    @component
    def App():
        return html.p(use(load()))

    with render(App(), raise_errors=False) as page:
        assert "needs a task" in str(page.errors[0])


def test_cache_runs_once_per_page_and_errors_are_remembered():
    calls = []

    @cache
    def user(uid):
        calls.append(uid)
        if uid < 0:
            raise ValueError("no such user")
        return f"user{uid}"

    @cache
    async def posts(uid):
        calls.append(("posts", uid))
        await asyncio.sleep(0.02)
        return ["p1"]

    @component
    def Name(uid):
        try:
            return html.b(user(uid))
        except ValueError:
            return html.b("?")

    @component
    def Posts(uid):
        return html.i(use(posts(uid))[0])

    @component
    def App():
        return html.div(Name(7), Name(7), Name(-1), Name(-1), Suspense(Posts(7), Posts(7)))

    with render(App()) as page:
        assert page.html() == '<div><b>user7</b><b>user7</b><b>?</b><b>?</b><div style="display:contents"><i>p1</i><i>p1</i></div></div>'
    assert calls == [7, -1, ("posts", 7)]
    with render(App()):
        pass
    assert calls.count(7) == 2, "a new page starts empty"
    assert user(3) == "user3", "outside a page it just calls"


# ---- small utilities ------------------------------------------------------------------


def test_children_helpers_and_clone_element():
    kids = ("a", None, [html.li("b"), [html.li("c")]], False)
    assert Children.count(kids) == 5
    assert Children.to_array(kids)[0] == "a" and len(Children.to_array(kids)) == 3
    assert Children.map(kids, lambda c: None if c is None or c is False else c) == Children.to_array(kids)
    seen = []
    Children.for_each(["x", "y"], lambda c, i: seen.append((i, c)))
    assert seen == [(0, "x"), (1, "y")]
    assert Children.only(html.p("one")).type == "p"
    with pytest.raises(ValueError):
        Children.only(["a", "b"])

    clicked = []
    original = html.button("go", id="b", class_name="x", on_click=lambda: clicked.append(1), key="k")
    copy = clone_element(original, "stop", disabled=True)
    assert copy.attrs == {"id": "b", "class": "x", "disabled": ""} and copy.children == ("stop",)
    assert copy.key == "k" and list(copy.events) == ["click"]
    assert original.attrs == {"id": "b", "class": "x"}, "the original is untouched"

    @component
    def Greeting(name, punct="!"):
        return html.p(f"hi {name}{punct}")

    with render(clone_element(Greeting("ada"), punct="?")) as page:
        assert page.html() == "<p>hi ada?</p>"
    assert is_valid_element(html.p()) and is_valid_element(Greeting("x")) and not is_valid_element("x")
    ref = create_ref()
    assert ref.current is None


# ---- Profiler, ViewTransition, prerender ----------------------------------------------


def test_profiler_reports_mount_and_updates_of_its_subtree():
    log = []

    @component
    def Counter():
        n, set_n = use_state(0)
        return html.button(n, on_click=lambda: set_n(n + 1))

    @component
    def App():
        return html.div(Profiler(Counter(), id="list", on_render=lambda *a: log.append(a)), html.p("outside"))

    with render(App()) as page:
        page.click("button")
    assert [(a[0], a[1]) for a in log] == [("list", "mount"), ("list", "update")]
    assert all(a[2] >= 0 and a[4] >= a[3] for a in log)


def test_view_transition_marks_messages_that_change_its_children():
    @component
    def App():
        n, set_n = use_state(0)
        return html.div(
            ViewTransition(html.p(n), name="count"),
            html.button("+", on_click=lambda: set_n(n + 1)),
        )

    async def main():
        live = Live(App())
        live.sent.clear()
        live.root.dispatch(1, live.node("button"), "click", {})
        await live.settle()
        return live.sent, live.dom.html()

    sent, html_ = asyncio.run(main())
    assert any(m.get("vt") for m in sent if m["ops"])
    assert 'style="view-transition-name:count"' in html_


def test_view_transition_leaves_other_updates_alone():
    @component
    def App():
        n, set_n = use_state(0)
        return html.div(ViewTransition(html.p("still")), html.button(n, on_click=lambda: set_n(n + 1)))

    async def main():
        live = Live(App())
        live.sent.clear()
        live.root.dispatch(1, live.node("button"), "click", {})
        await live.settle()
        return live.sent

    assert not any(m.get("vt") for m in asyncio.run(main()))


def test_prerender_writes_complete_static_pages(tmp_path):
    @component
    def Slow():
        async def load():
            await asyncio.sleep(0.05)
            return "loaded"

        from pyreaction import use_data

        return html.p(use_data(load))

    @component
    def App():
        return html.main(html.title("Static"), Suspense(Slow(), fallback=html.p("loading")))

    pages = prerender(App, ["/", "/docs/intro"], tmp_path)
    for body in pages.values():
        assert "<p>loaded</p>" in body and "loading" not in body
        assert "<title>Static</title>" in body and "client.js" not in body and "<template" not in body
    assert (tmp_path / "index.html").exists() and (tmp_path / "docs" / "intro" / "index.html").exists()
    assert "client.js" in prerender(App, ["/"], live=True)["/"]
