"""Fixes from the React/ReactPy audit: threads, SVG, handler checks, forms, status codes."""

import asyncio
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from pyreaction import (
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

from .test_reconciler import Harness


def test_slow_sync_handler_does_not_block_other_sessions():
    @component
    def Slow():
        return html.button("x", on_click=lambda: time.sleep(0.5))  # e.g. a database call

    async def main():
        root = Root(Slow())
        root.attach(lambda m: None)
        start = time.perf_counter()
        ticks = 0

        async def other_session():
            nonlocal ticks
            while time.perf_counter() - start < 0.6:
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(other_session())
        await asyncio.sleep(0.05)
        root.dispatch(1, 1, "click", {})
        await task
        root.close()
        return ticks

    assert asyncio.run(main()) > 40, "the event loop kept running while the handler slept"


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
    texts = [op[2] for m in sent[1:] for op in m["ops"] if op[0] == "x"]
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
