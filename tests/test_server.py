import asyncio
import gc
import json
import time
import tracemalloc

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from reaction import component, create_app, html, use_effect, use_location, use_state, use_title
from reaction.server.app import _Outbox

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


def wait_for(condition, timeout=2.0):
    """Effects run in a worker after a message is sent; give them a moment."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_socket_renders_again_and_handles_events():
    effects.clear()
    client = TestClient(create_app(Counter))
    page = client.get("/").text
    assert effects == [], "effects must wait for the browser"
    with connect(client) as ws:
        first = json.loads(ws.receive_text())
        assert first["init"] == "hydrate"
        assert not [op for op in first["ops"] if op[0] == "A"], "the HTML already has the structure"
        dom = FakeDom()
        dom.adopt(page, first["ops"])
        assert dom.html() == "<div><button>+</button><span>0</span></div>"
        wait_for(lambda: effects == [0])
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        update = json.loads(ws.receive_text())
        dom.apply(update["ops"])
        assert dom.html() == "<div><button>+</button><span>1</span></div>"
        assert update["ack"] == 1
        wait_for(lambda: effects == [0, 1])


def test_reconnect_without_markup_replaces():
    client = TestClient(create_app(Counter))
    with connect(client, hydrate=False) as ws:
        assert json.loads(ws.receive_text())["init"] == "replace"


def test_page_loads_hold_a_bounded_number_of_renders():
    """A GET keeps its render for the page's live connection, but never more than
    max_handoffs of them: pages that never connect (bots) cannot add up."""

    @component
    def Big():
        return html.div([html.p(i) for i in range(200)])

    def kept_after(loads, **options):
        with TestClient(create_app(Big, **options)) as client:
            client.get("/")
            gc.collect()
            tracemalloc.start()
            before, _ = tracemalloc.get_traced_memory()
            for _ in range(loads):
                client.get("/")
            gc.collect()
            after, _ = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return after - before

    assert kept_after(200, handoff_timeout=0) / 200 < 1_000, "without handoffs a GET keeps nothing"
    few, many = kept_after(100, max_handoffs=8), kept_after(400, max_handoffs=8)
    assert many < few * 1.5 + 50_000, f"{few} bytes after 100 loads, {many} after 400: should not grow"


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


def _button(message):
    return next(op[1] for op in message["ops"] if op[0] == "c" and op[2] == "button")


def test_reconnect_with_the_session_id_keeps_state():
    # One TestClient context = one event loop, as one server process would have.
    with TestClient(create_app(Counter)) as client:
        page = client.get("/").text
        with connect(client) as ws:
            first = json.loads(ws.receive_text())
            sid = first["sid"]
            dom = FakeDom()
            dom.adopt(page, first["ops"])
            button = _button(first)
            ws.send_text(json.dumps([1, button, "click", {}]))
            dom.apply(json.loads(ws.receive_text())["ops"])
            assert dom.html() == "<div><button>+</button><span>1</span></div>"
        # The browser comes back with its id and an event it made while offline.
        with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&s={sid}") as ws:
            again = json.loads(ws.receive_text())
            assert again["resumed"] is True and again["init"] == "hydrate"
            assert again["sid"] != sid, "a fresh id each connection: an old one cannot be reused"
            resumed = FakeDom()
            resumed.adopt(dom.html(), again["ops"])  # the browser still shows the page
            assert resumed.html() == "<div><button>+</button><span>1</span></div>"
            assert _button(again) == button, "node ids are kept"
            ws.send_text(json.dumps([2, button, "click", {}]))
            update = json.loads(ws.receive_text())
            resumed.apply(update["ops"])
            assert resumed.html() == "<div><button>+</button><span>2</span></div>"
            assert update["ack"] == 2
        # The id is single use.
        with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&s={sid}") as ws:
            assert "resumed" not in json.loads(ws.receive_text())


def test_a_parked_session_expires_and_its_effects_clean_up():
    cleaned = []

    @component
    def App():
        n, set_n = use_state(0)
        use_effect(lambda: (lambda: cleaned.append(n)), [])
        return html.button(n, on_click=lambda: set_n(n + 1))

    with TestClient(create_app(App, resume_timeout=0.2)) as client:
        with connect(client) as ws:
            sid = json.loads(ws.receive_text())["sid"]
        time.sleep(0.1)
        assert cleaned == [], "kept while it may still come back"
        wait_for(lambda: cleaned == [0])
        with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&s={sid}") as ws:
            assert "resumed" not in json.loads(ws.receive_text()), "expired: rendered afresh"


def test_resume_timeout_zero_closes_at_once():
    cleaned = []

    @component
    def App():
        use_effect(lambda: (lambda: cleaned.append(1)), [])
        return html.p("x")

    with TestClient(create_app(App, resume_timeout=0)) as client:
        with connect(client) as ws:
            assert "sid" in json.loads(ws.receive_text())
        wait_for(lambda: cleaned == [1])


def test_a_page_that_floods_events_is_disconnected_and_resumes():
    from reaction.core import vdom

    roots = []
    most = [0]  # the most events ever waiting at once
    attach = vdom.Root.attach
    dispatch = vdom.Root.dispatch

    def count(self, *args):
        dispatch(self, *args)
        most[0] = max(most[0], self.pending_events)

    def spy(self, *args, **kwargs):
        roots.append(self)
        return attach(self, *args, **kwargs)

    @component
    def App():
        n, set_n = use_state(0)

        def slow():
            time.sleep(0.005)
            set_n(lambda v: v + 1)

        return html.button(f"n={n}", on_click=slow)

    app = create_app(App, max_pending_events=50)
    vdom.Root.attach = spy
    vdom.Root.dispatch = count
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/_pyreaction/ws?p=/") as ws:
                first = json.loads(ws.receive_text())
                sid = first["sid"]
                button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
                for i in range(500):
                    ws.send_text(json.dumps([i + 1, button, "click", {}]))
                with pytest.raises(WebSocketDisconnect) as closed:
                    while True:
                        ws.receive_text()
                assert closed.value.code == 1013
            root = roots[0]
            assert most[0] <= 50, "the server never queued past the limit"
            deadline = time.monotonic() + 5
            while (root.pending_events or root._busy) and time.monotonic() < deadline:
                time.sleep(0.01)
            # Only the events read before the limit ran; the page resumes with them.
            from reaction import to_vdom

            label = to_vdom(root)["children"][0]["children"][0]
            assert 1 <= int(label.removeprefix("n=")) < 500, label
            with client.websocket_connect(f"/_pyreaction/ws?p=/&s={sid}&h=1") as ws:
                assert json.loads(ws.receive_text())["resumed"]
                ws.send_text(json.dumps([1000, button, "click", {}]))
                deadline = time.monotonic() + 5
                while to_vdom(root)["children"][0]["children"][0] == label and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert to_vdom(root)["children"][0]["children"][0] == f"n={int(label[2:]) + 1}"
    finally:
        vdom.Root.attach = attach
        vdom.Root.dispatch = dispatch


# ---- settings from PYREACTION_* environment variables -------------------------------------------


def test_environment_variables_set_options_not_passed_in_code(monkeypatch):
    from reaction.server.settings import UNSET, resolve

    monkeypatch.setenv("PYREACTION_DEBUG", "yes")
    monkeypatch.setenv("PYREACTION_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("PYREACTION_MAX_PENDING_EVENTS", "20")
    monkeypatch.setenv("PYREACTION_RESUME_TIMEOUT", "2.5")
    assert resolve("debug", UNSET) is True
    assert resolve("debug", False) is False, "code wins over the environment"
    assert resolve("allowed_origins", UNSET) == ["https://a.example", "https://b.example"]
    assert resolve("max_pending_events", UNSET) == 20
    assert resolve("resume_timeout", UNSET) == 2.5
    assert resolve("stream", UNSET) is True, "unset variables take the default"
    monkeypatch.setenv("PYREACTION_MAX_UPLOAD_SIZE", "lots")
    with pytest.raises(ValueError, match="PYREACTION_MAX_UPLOAD_SIZE='lots'"):
        resolve("max_upload_size", UNSET)


def test_environment_settings_reach_the_page(monkeypatch):
    @component
    def App():
        return html.p("hi")

    monkeypatch.setenv("PYREACTION_RECONNECT_MAX", "3000")
    monkeypatch.setenv("PYREACTION_RECONNECT_ATTEMPTS", "12")
    with TestClient(create_app(App)) as client:
        page = client.get("/").text
    assert 'data-reconnect-max="3000" data-reconnect-attempts="12"' in page
    with TestClient(create_app(App, reconnect_attempts=4)) as client:
        assert 'data-reconnect-attempts="4"' in client.get("/").text


# ---- encoding updates for the browser -----------------------------------------------------------


def test_both_encoders_send_the_same_text_for_awkward_values(caplog):
    import dataclasses
    import datetime
    import decimal
    import enum
    import math

    from reaction.server import encoding

    class Color(enum.Enum):
        RED = "red"

    @dataclasses.dataclass
    class Point:
        x: int
        y: int

    data = {
        "text": "héllo ✓ 😀", "nums": [1, 0.5, 1e16, 2**70, math.nan, -math.inf],
        "keys": {1: "a", 2.5: "b", False: "c", None: "d"},
        "when": [datetime.date(2026, 1, 2), datetime.datetime(2026, 1, 2, 3, 4, 5), datetime.time(6, 7)],
        "enum": Color.RED, "point": Point(1, 2), "tags": {"x"}, "tuple": (1, 2),
        "decimal": decimal.Decimal("1.50"),
    }
    expected = (
        '{"text":"héllo ✓ 😀","nums":[1,0.5,1e+16,1180591620717411303424,null,null],'
        '"keys":{"1":"a","2.5":"b","false":"c","null":"d"},'
        '"when":["2026-01-02","2026-01-02T03:04:05","06:07:00"],'
        '"enum":"red","point":{"x":1,"y":2},"tags":["x"],"tuple":[1,2],"decimal":"1.50"}'
    )
    assert encoding._dumps_std(data) == expected
    assert encoding.dumps(data) == expected, "orjson gives the same text"
    assert "Sent a Decimal to the browser as text" in caplog.text
    loop = []
    loop.append(loop)
    with pytest.raises(ValueError, match="Circular"):
        encoding.dumps({"loop": loop})


def update_after_click(app_component, **options):
    """What a click sends back, or how the connection closed."""
    with TestClient(create_app(app_component, **options)) as client:
        with client.websocket_connect("/_pyreaction/ws?p=/") as ws:
            first = json.loads(ws.receive_text())
            button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
            ws.send_text(json.dumps([1, button, "click", {}]))
            try:
                return json.loads(ws.receive_text())
            except WebSocketDisconnect as closed:
                return closed.code


def test_props_that_are_not_json_still_reach_the_page():
    import datetime

    from reaction import js_component

    Chart = js_component("/static/chart.js")

    @component
    def App():
        n, set_n = use_state(0)
        chart = Chart(labels={1: "Jan"}, since=datetime.date(2026, 1, 1), big=2**70) if n else None
        return html.div(html.button(f"n={n}", on_click=lambda: set_n(n + 1)), chart)

    message = update_after_click(App)
    mounted = next(op for op in message["ops"] if op[0] == "j")
    assert mounted[5] == {"labels": {"1": "Jan"}, "since": "2026-01-01", "big": 2**70}


def test_an_update_that_cannot_be_sent_closes_the_page_and_reports_it():
    from reaction import js_component

    Chart = js_component("/static/chart.js")
    loop = []
    loop.append(loop)
    errors = []

    @component
    def App():
        n, set_n = use_state(0)
        return html.div(html.button(f"n={n}", on_click=lambda: set_n(n + 1)), Chart(data=loop) if n else None)

    assert update_after_click(App, on_error=lambda exc, where, conn: errors.append((type(exc), where))) == 1011
    assert errors == [(ValueError, "sending an update to the browser")]


def test_the_favicon_never_renders_the_app():
    renders = []

    @component
    def App():
        renders.append(use_location().path)
        return html.p("hi")

    with TestClient(create_app(App)) as client:
        page = client.get("/").text
        assert '<link rel="icon" href="data:,">' in page, "so the browser does not ask"
        icon = client.get("/favicon.ico")
        assert (icon.status_code, icon.content) == (404, b"")
    assert renders == ["/"]
    head = '<link rel="shortcut icon" href="/static/me.png">'
    with TestClient(create_app(App, head=head)) as client:
        page = client.get("/").text
        assert head in page and 'href="data:,"' not in page, "an app's own icon is kept"

    @component
    def WithIcon():
        return html.div(html.link(rel="icon", href="/static/logo.svg"), "hi")

    with TestClient(create_app(WithIcon)) as client:
        assert 'href="data:,"' not in client.get("/").text, "an icon from a component counts too"


def test_a_mismatch_gets_the_whole_tree_in_the_same_session():
    with TestClient(create_app(Counter)) as client:
        with connect(client) as ws:
            first = json.loads(ws.receive_text())
            assert first["init"] == "hydrate" and not [op for op in first["ops"] if op[0] == "A"]
            ws.send_text(json.dumps([0, 0, "#mismatch", {"path": "/"}]))
            again = json.loads(ws.receive_text())
            assert again["init"] == "replace" and again["rebuild"] is True
            dom = FakeDom()
            dom.apply(again["ops"])
            assert dom.html() == "<div><button>+</button><span>0</span></div>"
            assert _button(again) == _button(first), "the same session and node ids"
            ws.send_text(json.dumps([1, _button(again), "click", {}]))
            dom.apply(json.loads(ws.receive_text())["ops"])
            assert dom.html() == "<div><button>+</button><span>1</span></div>"


# ---- the live connection takes over the first-HTML render ---------------------------------------


def _handoff_token(page):
    import re

    return re.search(r'data-handoff="([^"]*)"', page).group(1)


def _counting_app():
    renders = []

    @component
    def App():
        n, set_n = use_state(0)
        renders.append(use_location().path)
        return html.div(html.button("+", on_click=lambda: set_n(n + 1)), html.span(n))

    return App, renders


def test_the_live_connection_takes_over_the_first_render():
    App, renders = _counting_app()
    with TestClient(create_app(App)) as client:
        page = client.get("/").text
        token = _handoff_token(page)
        assert token and renders == ["/"]
        with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&k={token}") as ws:
            first = json.loads(ws.receive_text())
            assert renders == ["/"], "not rendered again"
            dom = FakeDom()
            dom.adopt(page, first["ops"])
            ws.send_text(json.dumps([1, _button(first), "click", {}]))
            dom.apply(json.loads(ws.receive_text())["ops"])
            assert dom.html() == "<div><button>+</button><span>1</span></div>"
        # Single use: the same token again renders afresh.
        with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&k={token}") as ws:
            ws.receive_text()
        assert renders == ["/", "/", "/"]


def test_a_render_is_only_handed_to_the_same_browser_and_url():
    App, renders = _counting_app()
    with TestClient(create_app(App), cookies={"session": "ada"}) as client:
        token = _handoff_token(client.get("/").text)
        client.cookies.set("session", "eve")
        with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&k={token}") as ws:
            ws.receive_text()
        assert renders == ["/", "/"], "other cookies: rendered for this connection"
        client.cookies.set("session", "ada")
        token = _handoff_token(client.get("/").text)
        with client.websocket_connect(f"/_pyreaction/ws?p=/other&h=1&k={token}") as ws:
            ws.receive_text()
        assert renders[-1] == "/other", "another URL: rendered for it"


def test_handoffs_expire_and_are_limited_in_number():
    App, renders = _counting_app()
    with TestClient(create_app(App, handoff_timeout=0.1)) as client:
        token = _handoff_token(client.get("/").text)
        time.sleep(0.3)
        client.get("/_pyreaction/client.js")  # let the server's loop run the expiry
        renders.clear()
        with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&k={token}") as ws:
            ws.receive_text()
        assert renders == ["/"], "expired: rendered again"
    App, renders = _counting_app()
    with TestClient(create_app(App, max_handoffs=2)) as client:
        tokens = [_handoff_token(client.get("/").text) for _ in range(3)]
        renders.clear()
        for token in tokens:
            with client.websocket_connect(f"/_pyreaction/ws?p=/&h=1&k={token}") as ws:
                ws.receive_text()
        assert renders == ["/"], "the oldest made way for the newer two"
    App, renders = _counting_app()
    with TestClient(create_app(App, handoff_timeout=0)) as client:
        assert _handoff_token(client.get("/").text) == "", "turned off"
