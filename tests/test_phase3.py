"""JS components with children and a fallback, portals."""

from reaction import Root, component, html, js_component, portal, render_to_string, use_state
from reaction.testing import render

Map = js_component("/static/map.js", "mount")


def test_js_component_children_render_into_a_slot_with_fallback():
    @component
    def App():
        n, set_n = use_state(0)
        return Map(
            html.button(f"pins: {n}", on_click=lambda: set_n(n + 1)),
            center=[51.5, 0],
            fallback=html.p("Loading map..."),
        )

    root = Root(App())
    assert render_to_string(root) == (
        '<div><div data-pyreaction-fallback style="display:contents"><p>Loading map...</p></div>'
        '<div data-pyreaction-slot style="display:contents"><button>pins: 0</button></div></div>'
    )
    ops = root.snapshot()
    assert ops[0][:6] == ["j", 1, "div", "/static/map.js", "mount", {"center": [51.5, 0]}]
    assert ops[0][6] == "<p>Loading map...</p>"
    with render(App()) as page:
        page.click("button")
        assert page.find("button").text == "pins: 1", "children stay live Python components"


def test_portal_renders_outside_and_is_removed_with_its_owner():
    @component
    def App():
        open_, set_open = use_state(True)
        return html.main(
            html.button("close", on_click=lambda: set_open(False)),
            portal(html.div("modal", class_name="modal"), target="body") if open_ else None,
        )

    root = Root(App())
    assert render_to_string(root) == "<main><button>close</button></main>", "not in the server's HTML"
    ops = root.snapshot()
    a0 = ops.index(["A", 0, [1]])
    assert [op[0] for op in ops[a0 + 1 :]][:1] == ["P"], "portals come after the page's tree"
    assert ops[a0 + 1][2] == "body"

    with render(App()) as page:
        assert page.find(".modal").text == "modal"
        portal_id = next(i for i, n in page.root.hosts.items() if n.type == "#portal")
        page.click("button")
        assert page.find_all(".modal") == []
        assert portal_id not in page.root.hosts


def test_portal_children_keep_events_and_state():
    @component
    def App():
        n, set_n = use_state(0)
        return html.div(html.p(n, id="count"), portal(html.button("+", on_click=lambda: set_n(n + 1))))

    with render(App()) as page:
        page.click("button")
        page.click("button")
        assert page.find("#count").text == "2"


def test_on_error_hook_gets_uncaught_errors_with_their_connection():
    import json

    from starlette.testclient import TestClient

    from reaction import create_app

    seen = []

    @component
    def App():
        def boom():
            raise RuntimeError("payment failed")

        return html.button("pay", on_click=boom)

    client = TestClient(create_app(App, on_error=lambda exc, where, conn: seen.append((str(exc), where, conn.url.path))))
    with client.websocket_connect("/_pyreaction/ws?p=/") as ws:
        first = json.loads(ws.receive_text())
        button = next(op[1] for op in first["ops"] if op[0] == "c")
        ws.send_text(json.dumps([1, button, "click", {}]))
        ws.send_text(json.dumps([2, 0, "#ping", {}]))  # heartbeat: ignored
        ws.send_text(json.dumps([3, 0, "#mismatch", {"path": "/"}]))
        ws.send_text(json.dumps([4, 0, "#navigate", {"to": "/x"}]))  # one more round trip
    assert seen and seen[0][:2] == ("payment failed", "the 'click' handler")
    assert seen[0][2] == "/_pyreaction/ws"


def test_mismatch_report_shows_in_debug(caplog):
    with render(html.p("x")) as page:
        page.root.debug = True
        sent = []
        page.root._sink = sent.append
        page.root.dispatch(1, 0, "#mismatch", {})
        page.settle()
    assert "did not match" in caplog.text
    assert any("did not match" in e for m in sent for e in m.get("errors", []))


def test_embed_snippet_and_reconnect_setting():
    from starlette.testclient import TestClient

    from reaction import create_app, embed

    snippet = embed(html.p("hi"), mount_path="/ui", element_id="w1")
    assert snippet == (
        '<div id="w1"><p>hi</p></div>'
        '<script src="/ui/_pyreaction/client.js" data-ws="/ui/_pyreaction/ws" data-mount="w1"></script>'
    )
    assert embed(mount_path="/ui").startswith('<div id="pyreaction-root"></div>')
    page = TestClient(create_app(lambda: html.p("x"), reconnect_max=3000)).get("/").text
    assert 'data-reconnect-max="3000"' in page


def test_use_effect_event_sees_latest_values_without_rerunning_the_effect():
    from reaction import use_effect, use_effect_event

    runs, calls = [], []
    holder = {}

    @component
    def App():
        n, set_n = use_state(0)
        log = use_effect_event(lambda: calls.append(n))
        holder.update(log=log, inc=lambda: set_n(n + 1))
        use_effect(lambda: runs.append("effect"), [log])  # log never changes
        return html.button(n, on_click=lambda: set_n(n + 1))

    with render(App()) as page:
        page.click("button")
        page.click("button")
        holder["log"]()
    assert runs == ["effect"] and calls == [2]


def test_use_imperative_handle_exposes_an_api_to_the_parent():
    from reaction import use_imperative_handle, use_ref

    @component
    def Dialog(handle):
        open_, set_open = use_state(False)
        use_imperative_handle(handle, lambda: {"open": lambda: set_open(True)}, [])
        return html.dialog("hi", open=open_ or None)

    @component
    def App():
        handle = use_ref()
        return html.div(html.button("show", on_click=lambda: handle.current["open"]()), Dialog(handle))

    with render(App()) as page:
        assert "open" not in page.find("dialog").attrs
        page.click("button")
        assert page.find("dialog").attrs["open"] == ""


def test_debug_warns_once_about_lists_without_keys(caplog):
    from reaction import Root

    @component
    def List():
        return html.ul([html.li(i) for i in range(3)], html.li("last"))

    Root(List(), debug=True)
    Root(List())  # not in debug: silent
    warnings = [r for r in caplog.records if "have no key" in r.getMessage()]
    assert len(warnings) == 1 and "List" in warnings[0].getMessage()
    caplog.clear()

    @component
    def Keyed():
        return html.ul([html.li(i, key=i) for i in range(3)])

    Root(Keyed(), debug=True)
    assert not [r for r in caplog.records if "have no key" in r.getMessage()]


def test_use_debug_value_logs_changes_in_debug(caplog):
    import logging

    from reaction import Root, use_debug_value

    caplog.set_level(logging.INFO, logger="reaction")

    @component
    def Status():
        use_debug_value("online", lambda v: v.upper())
        return html.p()

    Root(Status(), debug=True)
    assert "Status: 'ONLINE'" in caplog.text


def test_portals_are_in_the_first_html_at_the_end_of_body():
    from starlette.testclient import TestClient

    from reaction import create_app

    @component
    def App():
        return html.main(
            html.p("page"),
            portal(html.div("toast", class_name="toast"), portal(html.b("nested")), target="body"),
            portal(html.span("in aside"), target="#side"),
            html.aside(id="side"),
        )

    page = TestClient(create_app(App)).get("/").text
    body = page.split('<div id="pyreaction-root">', 1)[1]
    root_html, after = body.split("</div>\n", 1)
    assert "toast" not in root_html, "portal content is not inside the page's own tree"
    assert after.index('data-pyreaction-portal="0"') < after.index('data-pyreaction-portal="1"') < after.index(
        'data-pyreaction-portal="2"'
    )
    assert '<div data-pyreaction-portal="0" style="display:contents"><div class="toast">toast</div></div>' in after
    assert 'data-pyreaction-portal="1" data-target="#side"' in after, "non-body targets are moved by a script"
    assert '<div data-pyreaction-portal="2" style="display:contents"><b>nested</b></div>' in after
