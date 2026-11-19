"""Pending state, async data, Suspense, Head, raw HTML, use_id and shared stores."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from reaction import (
    Activity,
    Form,
    Head,
    Root,
    Store,
    Suspense,
    component,
    create_app,
    html,
    parse_html,
    render_to_string,
    use_action_state,
    use_async,
    use_data,
    use_form_status,
    use_id,
    use_state,
    use_store,
    use_transition,
)

from .fakedom import FakeDom, texts


class Live:
    """A root attached to a fake browser on a real event loop."""

    def __init__(self, element, **options):
        self.root = Root(element, **options)
        self.dom = FakeDom()
        self.sent = []
        self.root.attach(self._receive)

    def _receive(self, message):
        self.sent.append(message)
        self.dom.apply(message["ops"])

    async def settle(self):
        for _ in range(3):
            await self.root.settle(2)
            await asyncio.sleep(0.02)

    def node(self, tag):
        return next(i for i, n in self.root.hosts.items() if n.type == tag)


def run(coro):
    return asyncio.run(coro)


# ---- pending state ----------------------------------------------------------------


def test_use_transition_reports_pending_while_work_runs():
    gate = asyncio.Event

    @component
    def App():
        pending, start = use_transition()
        done, set_done = use_state(False)

        async def save():
            await asyncio.sleep(0.05)
            set_done(True)

        return html.button("saving" if pending else ("saved" if done else "save"), on_click=lambda: start(save))

    async def main():
        live = Live(App())
        live.root.dispatch(1, live.node("button"), "click", {})
        await asyncio.sleep(0.02)
        during = live.dom.html()
        await live.settle()
        return during, live.dom.html()

    during, after = run(main())
    assert during == "<button>saving</button>"
    assert after == "<button>saved</button>"


def test_use_action_state_runs_with_form_fields_and_keeps_state_on_error():
    def add(items, form):
        if form["item"] == "boom":
            raise ValueError("bad item")
        return items + [form["item"]]

    @component
    def App():
        items, submit, pending = use_action_state(add, [])
        return html.form(html.p(", ".join(items) or "-", " ", "..." if pending else ""), on_submit=submit)

    async def main():
        live = Live(App())
        form = live.node("form")
        for seq, item in enumerate(["milk", "boom", "eggs"], 1):
            live.root.dispatch(seq, form, "submit", {"form": [["item", item]]})
            await live.settle()
        return live.dom.html()

    assert run(main()) == "<form><p>milk, eggs </p></form>"


def test_form_component_status_and_reset():
    received = []

    @component
    def SaveButton():
        status = use_form_status()
        return html.button("Saving..." if status.pending else "Save", type="submit")

    @component
    def App():
        async def action(fields):
            await asyncio.sleep(0.05)
            received.append(dict(fields))

        return Form(html.input(name="email"), SaveButton(), action=action, class_name="f")

    async def main():
        live = Live(App())
        assert live.dom.html() == '<form class="f"><input name="email"><button type="submit">Save</button></form>'
        live.root.dispatch(1, live.node("form"), "submit", {"form": [["email", "a@b.c"]]})
        await asyncio.sleep(0.02)
        during = live.dom.html()
        await live.settle()
        calls = live.dom.calls
        return during, live.dom.html(), calls

    during, after, calls = run(main())
    assert "Saving..." in during
    assert "<button type=\"submit\">Save</button>" in after
    assert received == [{"email": "a@b.c"}]
    assert [c[1] for c in calls] == ["reset"], "the form was reset after success"


# ---- async data -------------------------------------------------------------------


def test_use_async_loads_then_renders_and_reloads_on_deps():
    loads = []

    @component
    def App():
        team, set_team = use_state("a")

        async def load():
            loads.append(team)
            await asyncio.sleep(0.01)
            return f"members of {team}"

        members = use_async(load, [team])
        text = "loading" if members.loading else members.value
        return html.button(text, on_click=lambda: set_team("b"))

    async def main():
        live = Live(App())
        first = live.dom.html()
        await live.settle()
        loaded = live.dom.html()
        live.root.dispatch(1, live.node("button"), "click", {})
        await live.settle()
        return first, loaded, live.dom.html()

    first, loaded, switched = run(main())
    assert first == "<button>loading</button>"
    assert loaded == "<button>members of a</button>"
    assert switched == "<button>members of b</button>"
    assert loads == ["a", "b"]


def test_use_async_error_and_sync_loader_in_a_thread():
    import threading

    threads = []

    @component
    def App():
        def load():
            threads.append(threading.current_thread().name)
            raise LookupError("no such team")

        result = use_async(load)
        return html.p("error: " + str(result.error) if result.error else "...")

    async def main():
        live = Live(App())
        await live.settle()
        return live.dom.html()

    assert run(main()) == "<p>error: no such team</p>"
    assert threads and threads[0] != "MainThread"


def test_suspense_shows_fallback_and_keeps_loaded_siblings():
    @component
    def Profile():
        async def load():
            await asyncio.sleep(0.03)
            return "Ada"

        return html.h2(use_data(load))

    @component
    def Counter():
        n, set_n = use_state(0)
        return html.button(n, on_click=lambda: set_n(n + 1))

    @component
    def App():
        return html.main(Suspense(Profile(), Counter(), fallback=html.p("Loading...")))

    async def main():
        live = Live(App())
        waiting = live.dom.html()
        await live.settle()
        return waiting, live.dom.html()

    waiting, loaded = run(main())
    assert "<p>Loading...</p>" in waiting and 'hidden=""' in waiting
    assert loaded == '<main><div style="display:contents"><h2>Ada</h2><button>0</button></div></main>'


def test_first_html_waits_for_data():
    @component
    def App():
        async def load():
            await asyncio.sleep(0.02)
            return ["Ada", "Grace"]

        users = use_async(load)
        return html.ul([html.li(u) for u in users.value or []]) if not users.loading else html.p("loading")

    client = TestClient(create_app(App))
    assert "<ul><li>Ada</li><li>Grace</li></ul>" in client.get("/").text
    slow = TestClient(create_app(App, data_timeout=0.001))
    assert "<p>loading</p>" in slow.get("/").text


# ---- head, raw HTML, ids ------------------------------------------------------------


def test_head_tags_in_first_html_and_live_updates():
    @component
    def Page():
        n, set_n = use_state(1)
        return html.div(
            Head(html.meta(name="description", content=f"page {n}"), html.link(rel="canonical", href="/p")),
            html.button("next", on_click=lambda: set_n(n + 1)),
        )

    client = TestClient(create_app(Page))
    page = client.get("/").text
    assert '<meta name="description" content="page 1" data-pyreaction-head="">' in page
    with client.websocket_connect("/_pyreaction/ws?p=/") as ws:
        first = json.loads(ws.receive_text())
        assert 'content="page 1"' in first["head"]
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        update = json.loads(ws.receive_text())
        assert 'content="page 2"' in update["head"]


def test_raw_html_renders_and_patches():
    @component
    def App():
        body, set_body = use_state("<b>one</b>")
        return html.article(dangerously_set_inner_html=body, on_click=lambda: set_body("<i>two</i>"))

    root = Root(App())
    assert render_to_string(root) == '<article data-pyreaction-raw=""><b>one</b></article>'
    with pytest.raises(TypeError, match="cannot also have children"):
        html.div("child", dangerously_set_inner_html="<b>x</b>")


def test_parse_html_sanitises_by_default():
    out = render_to_string(html.div(parse_html(
        '<p onclick="x()">a<img src=x onerror=alert(1)><a href="javascript:alert(1)">b</a></p>'
        '<script>alert(1)</script><iframe src="//evil"></iframe><ul><li>1<li>2</ul>'
    )))
    assert out == '<div><p>a<img src="x"><a>b</a></p><ul><li>1</li><li>2</li></ul></div>'
    unsafe = render_to_string(html.div(parse_html("<b onclick='f()'>x</b>", allow_unsafe=True)))
    assert unsafe == '<div><b onclick="f()">x</b></div>'


def test_use_id_matches_between_server_html_and_live_render():
    @component
    def Field(label):
        fid = use_id()
        return html.div(html.label(label, html_for=fid), html.input(id=fid))

    @component
    def App():
        return html.form(Field("Name"), Field("Email"))

    first = render_to_string(Root(App()))
    second = render_to_string(Root(App()))
    assert first == second
    assert 'for="pr-1"' in first and 'id="pr-2"' in first


def test_activity_hides_without_unmounting():
    @component
    def Tab():
        n, set_n = use_state(0)
        return html.button(n, on_click=lambda: set_n(n + 1))

    @component
    def App():
        open_, set_open = use_state(True)
        return html.div(
            html.a("toggle", on_click=lambda: set_open(not open_)),
            Activity(Tab(), mode="visible" if open_ else "hidden"),
        )

    async def main():
        live = Live(App())
        live.root.dispatch(1, live.node("button"), "click", {})
        await live.settle()
        live.root.dispatch(2, live.node("a"), "click", {})
        await live.settle()
        hidden = live.dom.html()
        live.root.dispatch(3, live.node("a"), "click", {})
        await live.settle()
        return hidden, live.dom.html()

    hidden, shown = run(main())
    assert '<div hidden=""><button>1</button></div>' in hidden
    assert '<div style="display:contents"><button>1</button></div>' in shown, "state survived hiding"


# ---- shared store -------------------------------------------------------------------


def test_store_updates_every_session():
    online = Store(0)

    @component
    def Badge():
        n = use_store(online)
        return html.span(f"{n} online")

    async def main():
        a, b = Live(Badge()), Live(Badge())
        online.update(lambda n: n + 2)
        await a.settle()
        await b.settle()
        a.root.close()  # unsubscribes
        online.set(5)
        await b.settle()
        return a.dom.html(), b.dom.html(), [len(readers) for readers in online._readers.values()]

    a_html, b_html, readers = run(main())
    assert a_html == "<span>2 online</span>"
    assert b_html == "<span>5 online</span>"
    assert readers == [1], "the closed page's reader is gone"


# ---- reading values from the browser ------------------------------------------------


def test_element_calls_return_the_browsers_answer():
    from reaction import use_ref

    seen = []

    @component
    def App():
        box = use_ref()

        async def measure():
            seen.append(await box.current.get("scrollTop"))
            try:
                await box.current.call("nope")
            except RuntimeError as err:
                seen.append(str(err))

        def measure_sync():
            # A plain handler runs in a worker thread, where blocking is fine.
            seen.append(box.current.call("getBoundingClientRect").result(2))

        return html.div(html.button("a", on_click=measure), html.button("b", on_click=measure_sync), ref=box)

    async def main():
        live = Live(App())
        buttons = [i for i, n in live.root.hosts.items() if n.type == "button"]

        async def browser(answers):
            # Answer each element call the way client.js does.
            for value in answers:
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    calls = [op for m in live.sent for op in m["ops"] if op[0] == "m" and op[4] not in done]
                    if calls:
                        break
                op = calls[0]
                done.add(op[4])
                reply = {"r": op[4], "error": value[1]} if isinstance(value, tuple) else {"r": op[4], "value": value}
                live.root.dispatch(0, 0, "#reply", reply)

        done = set()
        live.root.dispatch(1, buttons[0], "click", {})
        await browser([120, ("x", "the element has no method nope")])
        live.root.dispatch(2, buttons[1], "click", {})
        await browser([{"width": 10, "height": 4}])
        await live.settle()
        await asyncio.sleep(0.05)
        live.root.close()
        return seen

    assert run(main()) == [120, "the element has no method nope", {"width": 10, "height": 4}]


def test_reply_for_a_removed_element_fails_fast():
    from reaction import use_ref

    holder = {}

    @component
    def App():
        show, set_show = use_state(True)
        holder["ref"], holder["hide"] = use_ref(), lambda: set_show(False)
        return html.div(html.input(ref=holder["ref"]) if show else None)

    async def main():
        live = Live(App())
        handle = holder["ref"].current
        holder["hide"]()
        await live.settle()
        with pytest.raises(LookupError):
            await handle.focus()

    run(main())


def test_socket_waits_for_data_so_its_first_render_matches_the_html():
    @component
    def App():
        async def load():
            await asyncio.sleep(0.02)
            return "Ada"

        name = use_async(load)
        return html.p("..." if name.loading else name.value)

    client = TestClient(create_app(App))
    assert "<p>Ada</p>" in client.get("/").text
    with client.websocket_connect("/_pyreaction/ws?p=/&h=1") as ws:
        first = json.loads(ws.receive_text())
        assert texts(first["ops"]) == ["Ada"], "same as the server's HTML"


# ---- per-page stores with selectors --------------------------------------------------------


def test_page_store_selectors_rerender_only_the_rows_that_change():
    import gc

    from reaction import memo, use_page_store
    from reaction.testing import render

    renders = []

    @memo
    def Row(i, selection):
        selected = use_store(selection, lambda s: s == i)
        renders.append(i)
        return html.li(i, class_name="on" if selected else None, on_click=lambda: selection.set(i))

    @component
    def List():
        selection = use_page_store(-1)
        show, set_show = use_state(True)
        return html.div(
            html.button("hide", on_click=lambda: set_show(False)),
            html.ul([Row(i, selection, key=i) for i in range(100)]) if show else None,
        )

    with render(List()) as page:
        assert len(renders) == 100
        renders.clear()
        page.click(page.find_all("li")[7])
        assert renders == [7] and page.find(".on").text == "7"
        renders.clear()
        page.click(page.find_all("li")[42])
        assert sorted(renders) == [7, 42], "the old and the new selection only"
        assert [li.text for li in page.find_all(".on")] == ["42"]
        page.click("button")  # unmounts every row
        stores = [s for s in gc.get_objects() if isinstance(s, Store) and s._initial == -1]
        assert all(not s._readers for s in stores), "unmounted rows stop reading"


def test_use_store_follows_a_different_store_and_updates_from_threads():
    import threading

    from reaction.testing import render

    first, second = Store("a"), Store("x")

    @component
    def Show():
        which, set_which = use_state(0)
        value = use_store(first if which == 0 else second)
        return html.button(value, on_click=lambda: set_which(1))

    with render(Show()) as page:
        assert page.find("button").text == "a"
        page.click("button")
        assert page.find("button").text == "x" and not first._readers and second._readers
        worker = threading.Thread(target=lambda: second.set("from a thread"))
        worker.start()
        worker.join()
        page.settle()
        assert page.find("button").text == "from a thread"
