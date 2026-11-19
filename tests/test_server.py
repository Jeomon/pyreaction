import asyncio
import gc
import json
import tracemalloc

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pyreaction import component, create_app, html, use_effect, use_location, use_state, use_title
from pyreaction.server.app import _Outbox

from .fakedom import FakeDom

effects = []


@component
def Counter():
    n, set_n = use_state(0)
    use_effect(lambda: effects.append(n), [n])
    return html.div(html.button("+", on_click=lambda: set_n(n + 1)), html.span(n))


def connect(client, path="/", hydrate=True, **headers):
    query = f"p={path}" + ("&h=1" if hydrate else "")
    return client.websocket_connect(f"/_pyreaction/ws?{query}", headers=headers)


def test_page_renders_html_and_client_script():
    client = TestClient(create_app(Counter, title="T<1>"))
    page = client.get("/any/path").text
    assert "<title>T&lt;1&gt;</title>" in page
    assert '<div id="pyreaction-root"><div><button>+</button><span>0</span></div></div>' in page
    js = client.get("/_pyreaction/client.js")
    assert js.status_code == 200 and "insertBefore" in js.text


def test_socket_renders_again_and_handles_events():
    effects.clear()
    client = TestClient(create_app(Counter))
    client.get("/")
    assert effects == [], "effects must wait for the browser"
    with connect(client) as ws:
        first = json.loads(ws.receive_text())
        assert first["init"] == "hydrate"
        dom = FakeDom()
        dom.apply(first["ops"])
        assert dom.html() == "<div><button>+</button><span>0</span></div>"
        assert effects == [0]
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        update = json.loads(ws.receive_text())
        dom.apply(update["ops"])
        assert dom.html() == "<div><button>+</button><span>1</span></div>"
        assert update["ack"] == 1
    assert effects == [0, 1]


def test_reconnect_without_markup_replaces():
    client = TestClient(create_app(Counter))
    with connect(client, hydrate=False) as ws:
        assert json.loads(ws.receive_text())["init"] == "replace"


def test_page_loads_hold_no_sessions():
    """Item 2: a GET used to park a live session for 30 s; now it keeps nothing."""

    @component
    def Big():
        return html.div([html.p(i) for i in range(200)])

    client = TestClient(create_app(Big))
    client.get("/")
    gc.collect()
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    for _ in range(200):
        client.get("/")
    # The finished tree is cyclic garbage (nodes point at their parents); collect it.
    gc.collect()
    after, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert (after - before) / 200 < 1_000, f"{(after - before) / 200:.0f} bytes kept per page load"


def test_cross_origin_socket_is_refused():
    client = TestClient(create_app(Counter))
    with pytest.raises(WebSocketDisconnect) as refused:
        with connect(client, origin="https://evil.example") as ws:
            ws.receive_text()
    assert refused.value.code == 1008
    with connect(client, origin="http://testserver") as ws:
        assert json.loads(ws.receive_text())["init"] == "hydrate"


def test_allowed_origins_extend_same_origin():
    client = TestClient(create_app(Counter, allowed_origins=["https://app.example"]))
    with connect(client, origin="https://app.example") as ws:
        assert "init" in json.loads(ws.receive_text())


def test_malformed_messages_are_ignored():
    client = TestClient(create_app(Counter))
    with connect(client) as ws:
        first = json.loads(ws.receive_text())
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        for bad in ["not json", "[]", '{"a": 1}', '["1", 2, "click", {}]', "[1, 2, 3, 4]"]:
            ws.send_text(bad)
        ws.send_bytes(b"\x00\x01")
        ws.send_text(json.dumps([1, button, "click", {}]))
        update = json.loads(ws.receive_text())
        assert ["x", 5, "1"] in update["ops"], "the socket still works after bad input"


def test_location_and_title_come_from_the_url():
    @component
    def Page():
        loc = use_location()
        use_title(f"Page {loc.path}")
        return html.p(loc.path, " ", loc.query.get("q", "-"))

    client = TestClient(create_app(Page, title="default"))
    page = client.get("/docs?q=hooks").text
    assert "<title>Page /docs</title>" in page
    assert "<p>/docs<!----> <!---->hooks</p>" in page
    with connect(client, "/docs?q=hooks") as ws:
        first = json.loads(ws.receive_text())
        assert first["title"] == "Page /docs"
        texts = [op[2] for op in first["ops"] if op[0] == "t"]
        assert texts == ["/docs", " ", "hooks"]
        ws.send_text(json.dumps([1, 0, "#navigate", {"to": "/about"}]))
        update = json.loads(ws.receive_text())
        assert update["title"] == "Page /about"
        assert ["x", 2, "/about"] in update["ops"]


def test_debug_mode_sends_errors_to_the_browser():
    @component
    def Broken():
        def fail():
            raise ValueError("handler blew up")

        return html.button("x", on_click=fail)

    client = TestClient(create_app(Broken, debug=True))
    with connect(client) as ws:
        first = json.loads(ws.receive_text())
        button = next(op[1] for op in first["ops"] if op[0] == "c")
        ws.send_text(json.dumps([1, button, "click", {}]))
        update = json.loads(ws.receive_text())
        assert "handler blew up" in update["errors"][0]

    client = TestClient(create_app(Broken))
    with connect(client) as ws:
        first = json.loads(ws.receive_text())
        assert "errors" not in first


def _run(coro):
    return asyncio.run(coro)


def test_outbox_merges_messages_for_a_slow_socket():
    async def go():
        box = _Outbox(limit=100)
        box.push({"ops": [["t", 1, "a"]], "ack": 0, "init": "hydrate"})
        box.push({"ops": [["x", 1, "b"]], "ack": 1, "nav": {"to": "/a", "replace": False}})
        box.push({"ops": [["x", 1, "c"]], "ack": 2, "title": "T", "nav": {"to": "/b", "replace": True}})
        first = await box.take()
        merged = await box.take()
        return first, merged

    first, merged = _run(go())
    assert first["init"] == "hydrate"
    assert merged == {
        "ops": [["x", 1, "b"], ["x", 1, "c"]],
        "ack": 2,
        "title": "T",
        "nav": {"to": "/b", "replace": False},
    }


def test_outbox_gives_up_past_its_limit():
    async def go():
        box = _Outbox(limit=3)
        for i in range(5):
            box.push({"ops": [["x", 1, str(i)]], "ack": i})
        return await box.take()

    assert _run(go()) is None
