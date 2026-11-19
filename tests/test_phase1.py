"""Fixes from the React/ReactPy audit: threads, SVG, handler checks, forms, status codes."""

import asyncio
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from reaction import (
    FormFields,
    Navigate,
    Root,
    Router,
    component,
    create_app,
    event,
    html,
    js,
    use_effect,
    use_state,
    use_status,
)

from .fakedom import texts as op_texts
from .test_vdom import Harness


def test_slow_sync_handler_does_not_block_other_sessions():
    @component
    def Slow():
        return html.button("x", on_click=lambda: time.sleep(0.5))  # e.g. a database call

    async def main():
        root = Root(Slow())
        root.attach(lambda m: None)
        start = time.perf_counter()
        longest = 0.0

        async def other_session():
            # The longest the loop went without running this session: the handler's
            # 0.5 s sleep if it ran on the loop, scheduling noise otherwise.
            nonlocal longest
            last = time.perf_counter()
            while last - start < 0.6:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                longest = max(longest, now - last)
                last = now

        task = asyncio.create_task(other_session())
        await asyncio.sleep(0.05)
        root.dispatch(1, 1, "click", {})
        await task
        root.close()
        return longest

    assert asyncio.run(main()) < 0.3, "the event loop kept running while the handler slept"


def test_sync_handlers_and_effects_run_off_the_loop_in_order():
    order = []
    loop_thread = []

    @component
    def App():
        n, set_n = use_state(0)

        def click():
            order.append(("click", n, threading.get_ident() != loop_thread[0]))
            set_n(n + 1)

        use_effect(lambda: order.append(("effect", n)), [n])
        return html.button(n, on_click=click)

    async def main():
        loop_thread.append(threading.get_ident())
        sent = []
        root = Root(App())
        root.attach(sent.append)
        for seq in (1, 2, 3):
            root.dispatch(seq, 1, "click", {})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if root.ack == 3 and len(order) >= 7:
                break
        root.close()
        return sent

    sent = asyncio.run(main())
    clicks = [o for o in order if o[0] == "click"]
    assert clicks == [("click", 0, True), ("click", 1, True), ("click", 2, True)], (
        "each click saw the state the previous one left, from a worker thread"
    )
    assert [o for o in order if o[0] == "effect"] == [("effect", 0), ("effect", 1), ("effect", 2), ("effect", 3)]
    texts = op_texts([op for m in sent[1:] for op in m["ops"]])
    assert texts[-1] == "3"


def test_threaded_false_runs_handlers_on_the_loop():
    seen = []

    @component
    def App():
        return html.button("x", on_click=lambda: seen.append(threading.get_ident()))

    async def main():
        root = Root(App(), threaded=False)
        root.attach(lambda m: None)
        root.dispatch(1, 1, "click", {})
        await asyncio.sleep(0.05)
        root.close()
        return threading.get_ident()

    assert seen == [asyncio.run(main())]


def test_svg_elements_are_created_in_their_namespace():
    @component
    def App():
        return html.div(
            html.svg(html.circle(r=5), html.foreignObject(html.p("html again")), view_box="0 0 10 10"),
            html.math(html.mi("x")),
        )

    root = Root(App())
    ns = {op[2]: (op[5] if len(op) > 5 else None) for op in root.snapshot() if op[0] == "c"}
    assert ns == {"div": None, "svg": "svg", "circle": "svg", "foreignObject": "svg", "p": None, "math": "math", "mi": "math"}


def test_text_handlers_are_rejected_when_written():
    with pytest.raises(TypeError, match=r"use js\(\.\.\.\)"):
        html.button("x", on_click="alert(1)")
    with pytest.raises(TypeError, match="must be functions"):
        html.button("x", on_click=42)


def test_js_handlers_run_in_the_browser():
    el = html.input(on_focus=js("element.select()"), on_key_down=event(js("event.preventDefault()"), keys=["Tab"]))
    assert el.events["focus"].spec()[5] == "element.select()"
    assert el.events["keydown"].spec()[4:6] == [["Tab"], "event.preventDefault()"]
    with pytest.raises(TypeError):
        js(lambda: None)  # type: ignore[arg-type]


def test_stop_immediate_propagation_is_in_the_spec():
    el = html.div(on_click=event(lambda: None, stop_immediate_propagation=True))
    assert el.events["click"].spec()[6] is True


def test_form_fields_keep_repeated_names():
    got = {}

    @component
    def App():
        return html.form(on_submit=lambda e: got.update(form=e.form))

    h = Harness(App())
    h.root.dispatch(1, 1, "submit", {"form": [["name", "Ada"], ["tag", "a"], ["tag", "b"]]})
    form = got["form"]
    assert isinstance(form, FormFields)
    assert form["name"] == "Ada" and form.getlist("tag") == ["a", "b"] and form["tag"] == "b"
    assert form.getlist("missing") == []


def test_router_fallback_sends_404_and_use_status():
    @component
    def Home():
        return html.p("home")

    @component
    def Gone():
        use_status(410)
        return html.p("gone")

    @component
    def App():
        return Router({"/": Home, "/old": Gone}, fallback=html.p("not found"))

    client = TestClient(create_app(App))
    assert client.get("/").status_code == 200
    assert client.get("/old").status_code == 410
    missing = client.get("/nope")
    assert missing.status_code == 404 and "not found" in missing.text


def test_navigate_redirects_on_first_load_and_live():
    @component
    def App():
        return Router({"/": lambda: html.p("home"), "/old": lambda: Navigate("/")})

    client = TestClient(create_app(App))
    response = client.get("/old", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/"

    with client.websocket_connect("/_pyreaction/ws?p=/old") as ws:
        first = json.loads(ws.receive_text())
        message = first if "nav" in first else json.loads(ws.receive_text())
        assert message["nav"] == {"to": "/", "replace": True}


def test_threaded_effects_run_as_one_batch_with_errors_reported_each(caplog):
    """Plain effects of one flush run together in a worker thread: every cleanup
    first, then every setup in order; an async effect splits the batch; a failing
    effect is reported on its own and the others still run and update state."""
    log = []
    setters = {}

    @component
    def Item(i, n):
        def effect():
            log.append(("setup", i, n, threading.get_ident() != loop_thread[0]))
            if i == 1 and n == 1:
                raise ValueError("effect 1 failed")
            return lambda: log.append(("cleanup", i, n))

        use_effect(effect, [n])
        return html.li(i)

    @component
    def Async(n):
        async def effect():
            log.append(("async", n))

        use_effect(effect, [n])

    @component
    def App():
        n, set_n = use_state(0)
        seen, set_seen = use_state(0)
        setters["n"] = set_n
        use_effect(lambda: set_seen(n), [n])  # an effect's state update renders
        return html.ul(Item(0, n), Item(1, n), Async(n), Item(2, n), html.p(f"seen {seen}"))

    loop_thread = []

    async def main():
        loop_thread.append(threading.get_ident())
        sent = []
        root = Root(App())
        root.attach(sent.append)
        await asyncio.sleep(0.2)
        log.clear()
        setters["n"](1)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any("seen 1" in t for m in sent for t in op_texts(m["ops"])):
                break
        root.close()
        return sent

    sent = asyncio.run(main())
    plain = [entry for entry in log if entry[0] != "async"]
    assert plain[:2] == [("cleanup", 0, 0), ("cleanup", 1, 0)], "cleanups of a batch run before its setups"
    assert ("setup", 0, 1, True) in plain and ("setup", 2, 1, True) in plain, "the rest ran, in a thread"
    assert ("async", 1) in log
    # The async effect (its body runs later, as a task) ends the first batch: the
    # item after it is cleaned up only once the batch before it has set up.
    assert plain.index(("cleanup", 2, 0)) > plain.index(("setup", 1, 1, True)), "the async effect split the batch"
    assert "effect 1 failed" in caplog.text and "an effect in Item" in caplog.text
    assert any("seen 1" in t for m in sent for t in op_texts(m["ops"]))


def test_updates_from_worker_threads_arrive_complete_and_in_order():
    """Updates from threads are queued and the loop drains them in bursts: each
    thread's updates still apply in the order it made them, and none are lost."""
    setters = {}

    @component
    def App():
        digits, set_digits = use_state("")
        count, set_count = use_state(0)
        setters.update(digits=set_digits, count=set_count)
        return html.p(f"{digits}|{count}")

    async def main():
        root = Root(App())
        root.attach(lambda m: None)

        def ordered():
            for d in "0123456789" * 30:
                setters["digits"](lambda s, d=d: s + d)

        def counted():
            for _ in range(500):
                setters["count"](lambda c: c + 1)

        await asyncio.gather(root._in_thread(ordered), root._in_thread(counted), root._in_thread(counted))
        root.flush()
        text = root.container.children[0].children[0].text
        root.close()
        return text

    digits, count = asyncio.run(main()).split("|")
    assert digits == "0123456789" * 30, "one thread's updates keep their order"
    assert count == "1000", "updates from two threads at once are all applied"


def test_one_event_reaching_several_handlers_renders_once():
    """A batch (one native event, several elements' handlers): they run in order, the
    page renders once after all of them, and each sees the state from before."""
    seen = []
    renders = []
    setters = {}

    @component
    def App():
        n, set_n = use_state(0)
        renders.append(n)
        setters["n"] = set_n

        def inner():
            seen.append(("inner", n))
            set_n(lambda v: v + 1)

        def outer():
            seen.append(("outer", n))
            set_n(lambda v: v + 10)

        return html.div(html.button(n, on_click=inner), on_click=outer)

    async def main():
        sent = []
        root = Root(App())
        root.attach(sent.append)
        await asyncio.sleep(0.05)
        renders.clear()
        button = next(node.id for node in root.hosts.values() if node.type == "button")
        div = next(node.id for node in root.hosts.values() if node.type == "div")
        root.dispatch(1, [button, div], "click", {})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if root.ack == 1 and renders:
                break
        await asyncio.sleep(0.05)
        root.close()
        return sent

    sent = asyncio.run(main())
    assert seen == [("inner", 0), ("outer", 0)], "both saw the state from before the event"
    assert renders == [11], "one render, after both handlers"
    assert [t for m in sent[1:] for t in op_texts(m["ops"])] == ["11"]


def test_batched_event_messages_are_checked():
    from reaction.server.app import _parse_event

    assert _parse_event('[1, [4, 9], "click", {}]') == (1, [4, 9], "click", {})
    assert _parse_event('[1, 4, "click", {}]') == (1, 4, "click", {})
    for bad in ('[1, [], "click", {}]', '[1, ["4"], "click", {}]', '[1, [4, 1.5], "click", {}]',
                '[1, ' + str(list(range(300))) + ', "click", {}]'):
        assert _parse_event(bad) is None, bad
