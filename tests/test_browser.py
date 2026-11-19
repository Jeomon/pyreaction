"""Tests in a real browser (Chrome through Playwright): what the fake browser cannot run."""

import asyncio
import time
from pathlib import Path

import pytest

from reaction import Suspense, component, event, html, js, js_component, use_data, use_state
from reaction.testing import serve

pytestmark = pytest.mark.browser

STATIC = Path(__file__).resolve().parent.parent / "examples" / "static"


def open_page(page, server, path="/"):
    page.goto(server.url + path)
    page.wait_for_selector(server.READY)


def test_counter_hydrates_and_counts(page):
    @component
    def Counter():
        n, set_n = use_state(0)
        return html.button(f"clicked {n}", on_click=lambda: set_n(n + 1))

    with serve(Counter) as server:
        open_page(page, server)
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"
        for _ in range(3):
            page.click("button")
        page.wait_for_function("document.querySelector('button').textContent === 'clicked 3'")


def test_js_handler_runs_in_the_browser_without_a_round_trip(page):
    sent = []

    @component
    def App():
        return html.div(
            html.input(id="box", value="select me", on_click=js("element.select()")),
            html.button("count", id="count", on_click=js("element.textContent = 'clicked in JS'")),
            html.p("server", id="server", on_click=lambda: sent.append(1)),
        )

    with serve(App) as server:
        open_page(page, server)
        page.click("#box")
        assert page.evaluate("[box.selectionStart, box.selectionEnd]") == [0, 9]
        page.click("#count")
        assert page.text_content("#count") == "clicked in JS"
        page.click("#server")
        page.wait_for_timeout(200)
        assert sent == [1], "only the Python handler reached the server"


def test_js_component_module_mounts_updates_and_emits(page):
    Bars = js_component("/static/bars.js", "mount")

    @component
    def App():
        values, set_values = use_state([3, 7])
        picked, set_picked = use_state(None)
        return html.main(
            html.button("more", id="more", on_click=lambda: set_values(values + [5])),
            Bars(values=values, color="teal", on_pick=lambda e: set_picked(e.value), fallback=html.p("loading")),
            html.p(f"picked {picked}", id="picked"),
        )

    with serve(App, static_dir=STATIC) as server:
        open_page(page, server)
        page.wait_for_selector("main button[title]")
        assert page.eval_on_selector_all("main button[title]", "els => els.map(e => e.title)") == ["3", "7"]
        page.click("#more")
        page.wait_for_function("document.querySelectorAll('main button[title]').length === 3")
        page.click("main button[title='7']")
        page.wait_for_function("document.querySelector('#picked').textContent === 'picked 7'")


def test_streamed_suspense_content_is_swapped_in_then_adopted(page):
    @component
    def Slow():
        async def load():
            await asyncio.sleep(0.6)
            return "sales: 42"

        return html.p(use_data(load), id="sales")

    @component
    def App():
        n, set_n = use_state(0)
        return html.main(
            html.h1("Dashboard"),
            Suspense(Slow(), fallback=html.p("Loading sales...", id="fallback")),
            html.button(f"n={n}", id="inc", on_click=lambda: set_n(n + 1)),
        )

    with serve(App) as server:
        page.goto(server.url, wait_until="commit")
        page.wait_for_selector("#fallback", state="visible")  # the shell arrived before the data
        page.wait_for_selector("#sales")
        page.wait_for_selector(server.READY)
        assert page.text_content("#sales") == "sales: 42"
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"
        assert not page.is_visible("#fallback")
        page.click("#inc")
        page.wait_for_function("document.querySelector('#inc').textContent === 'n=1'")


def test_typing_under_server_lag_loses_nothing(page):
    """Each keystroke goes to a handler that takes 150 ms and uppercases the text,
    so the server answers well behind the typing. The browser must ignore its stale
    answers, and the input must end with every character, uppercased."""
    seen = []

    @component
    def App():
        text, set_text = use_state("")

        def change(e):
            time.sleep(0.15)
            seen.append(e.value)
            set_text(e.value.upper())

        return html.div(html.input(id="box", value=text, on_change=change), html.p(text, id="echo"))

    with serve(App) as server:
        open_page(page, server)
        page.click("#box")
        page.keyboard.type("hello world", delay=30)  # faster than the server keeps up
        page.wait_for_function("document.querySelector('#echo').textContent === 'HELLO WORLD'", timeout=10000)
        assert page.input_value("#box") == "HELLO WORLD"
        assert len(seen) == 11, "every keystroke reached the server, in order"
        assert seen[-1].lower() == "hello world"


def test_enter_only_key_filter_in_a_real_browser(page):
    submitted = []

    @component
    def App():
        draft, set_draft = use_state("")
        return html.textarea(
            id="msg",
            value=draft,
            on_change=lambda e: set_draft(e.value),
            on_key_down=event(lambda: submitted.append(draft), keys=["Enter"], prevent_default=True),
        )

    with serve(App) as server:
        open_page(page, server)
        page.click("#msg")
        page.keyboard.type("hi there")
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.input_value("#msg") == "hi there", "Enter's default (a newline) was prevented"
        assert submitted == ["hi there"]


def test_portal_from_server_html_is_adopted_not_rebuilt(page):
    from reaction import portal

    @component
    def App():
        n, set_n = use_state(0)
        return html.main(
            html.aside(id="side"),
            portal(html.button(f"modal {n}", id="modal-btn", on_click=lambda: set_n(n + 1))),
            portal(html.span("side note", id="note"), target="#side"),
        )

    with serve(App) as server:
        page.goto(server.url, wait_until="commit")
        page.wait_for_selector("#modal-btn")
        page.evaluate("window.__box = document.querySelector('#modal-btn').parentElement")
        assert page.evaluate("document.querySelector('#note').closest('#side') !== null"), "moved into its target"
        page.wait_for_selector(server.READY)
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"
        assert page.evaluate("document.querySelector('#modal-btn').parentElement === window.__box"), "same nodes"
        assert page.evaluate("document.querySelectorAll('[data-pyreaction-portal]').length") == 2
        page.click("#modal-btn")
        page.wait_for_function("document.querySelector('#modal-btn').textContent === 'modal 1'")


def test_js_select_on_focus_by_click_and_by_tab(page):
    @component
    def App():
        return html.div(
            html.input(id="first", value="before"),
            html.input(id="box", value="select me", on_focus=js("element.select()")),
        )

    with serve(App) as server:
        open_page(page, server)
        page.mouse.click(5, 300)  # somewhere else, so the click below focuses the input
        page.click("#box")
        assert page.evaluate("[box.selectionStart, box.selectionEnd]") == [0, 9]
        page.click("#first")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == "box"
        assert page.evaluate("[box.selectionStart, box.selectionEnd]") == [0, 9]


def test_state_survives_a_dropped_connection(page):
    """Playwright's offline mode leaves open WebSockets alone, so the socket is
    routed through Playwright: cut it, refuse reconnects for a while, then allow."""
    import re

    @component
    def App():
        n, set_n = use_state(0)
        return html.div(html.button(f"n={n}", id="inc", on_click=lambda: set_n(n + 1)))

    net = {"offline": False, "open": []}

    def route(ws):
        if net["offline"]:
            ws.close()
            return
        net["open"].append((ws, ws.connect_to_server()))

    page.route_web_socket(re.compile(r".*/_pyreaction/ws.*"), route)
    with serve(App) as server:
        open_page(page, server)
        page.click("#inc")
        page.click("#inc")
        page.wait_for_function("document.querySelector('#inc').textContent === 'n=2'")
        net["offline"] = True
        for client_side, server_side in net["open"]:
            server_side.close()
            client_side.close()
        page.wait_for_selector("html[data-pyreaction-offline]", state="attached")
        # The page blocks pointer input while offline, but the keyboard still works:
        # this click is queued and sent once the connection is back.
        page.focus("#inc")
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.text_content("#inc") == "n=2"
        net["offline"] = False
        page.wait_for_selector(server.READY, state="attached", timeout=15000)
        page.wait_for_function("document.querySelector('#inc').textContent === 'n=3'", timeout=5000)
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"


def test_uncontrolled_inputs_keep_what_is_typed_across_re_renders(page):
    sent = []

    @component
    def App():
        n, set_n = use_state(0)

        def submit(e):
            sent.append(dict(e.form.items()))
            set_n(n + 1)

        return html.form(
            html.input(name="q", id="q", default_value="hello"),
            html.input(type="checkbox", name="c", id="c", default_checked=True),
            html.textarea(name="t", id="t", default_value="notes"),
            html.select(html.option("a", value="a"), html.option("b", value="b"), name="s", id="s", default_value="b"),
            html.button(f"sent {n}", id="go"),
            on_submit=submit,
        )

    with serve(App) as server:
        open_page(page, server)
        assert page.input_value("#q") == "hello" and page.is_checked("#c")
        assert page.input_value("#t") == "notes"
        assert page.input_value("#s") == "b", "the select starts on its default"
        page.fill("#q", "typed by hand")
        page.uncheck("#c")
        page.select_option("#s", "a")
        page.click("#go")
        page.wait_for_function("document.querySelector('#go').textContent === 'sent 1'")
        assert sent == [{"q": "typed by hand", "t": "notes", "s": "a"}]
        # The re-render did not put the defaults back.
        assert page.input_value("#q") == "typed by hand"
        assert not page.is_checked("#c")
        assert page.input_value("#s") == "a"


def test_capture_events_and_button_form_action_in_chrome(page):
    order, drafts, posts = [], [], []

    @component
    def App():
        return html.div(
            html.form(
                html.input(name="title", id="title", required=True),
                html.button("Publish", id="publish"),
                html.button("Draft", id="draft", name="kind", value="draft",
                            form_action=lambda f: drafts.append(dict(f))),
                on_submit=lambda e: posts.append(dict(e.form)),
            ),
            html.button("inner", id="inner", on_click=lambda: order.append("inner")),
            on_click_capture=lambda: order.append("capture"),
        )

    with serve(App) as server:
        open_page(page, server)
        page.click("#inner")
        page.wait_for_timeout(300)
        assert order[-2:] == ["capture", "inner"]
        page.click("#draft")  # the title is required: the browser refuses first
        page.wait_for_timeout(300)
        assert drafts == []
        page.fill("#title", "hello")
        page.click("#draft")
        page.wait_for_timeout(300)
        assert drafts == [{"title": "hello", "kind": "draft"}] and posts == []


def test_head_tags_resources_and_callback_refs_in_chrome(page):
    from reaction import preinit

    refs = []

    @component
    def App():
        n, set_n = use_state(0)

        def go():
            preinit("data:text/javascript,window.__ran=(window.__ran||0)+1", as_="script")
            set_n(n + 1)

        return html.div(
            html.title(f"Page {n}"),
            html.meta(name="description", content=f"n={n}"),
            html.p("ref me", id="p", ref=lambda el: refs.append(el is not None)),
            html.button("go", id="go", on_click=go),
        )

    with serve(App) as server:
        open_page(page, server)
        assert page.title() == "Page 0"
        page.click("#go")
        page.wait_for_function("document.title === 'Page 1'")
        page.click("#go")
        page.wait_for_function("document.title === 'Page 2'")
        assert page.evaluate("document.querySelector('meta[name=description]').content") == "n=2"
        assert page.evaluate("document.querySelectorAll('meta[name=description]').length") == 1
        assert page.evaluate("window.__ran") == 1, "a preinit script runs once, not on every head update"
        assert True in refs


def test_view_transition_updates_in_chrome(page):
    from reaction import ViewTransition

    @component
    def App():
        n, set_n = use_state(0)
        return html.div(ViewTransition(html.p(f"n={n}", id="n"), name="count"),
                        html.button("+", id="inc", on_click=lambda: set_n(n + 1)))

    with serve(App) as server:
        open_page(page, server)
        page.evaluate("""window.__vt = 0; const o = document.startViewTransition.bind(document);
            document.startViewTransition = (cb) => { window.__vt++; return o(cb); }; true""")
        for _ in range(3):
            page.click("#inc")
        page.wait_for_function("document.querySelector('#n').textContent === 'n=3'")
        assert page.evaluate("window.__vt") >= 1
        assert page.evaluate("getComputedStyle(document.querySelector('#n').parentElement).viewTransitionName") == "count"


def test_browser_only_part_is_adopted_then_rendered_in_chrome(page):
    from reaction import Suspense, browser

    @component
    def Width():
        browser("reads the window")
        return html.p("live part", id="live")

    @component
    def App():
        return html.main(html.h1("Page"), Suspense(Width(), fallback=html.p("server part", id="fb")))

    with serve(App) as server:
        first_html = page.request.get(server.url).text()
        assert "server part" in first_html and "live part" not in first_html
        page.goto(server.url)
        page.wait_for_selector(server.READY, state="attached")
        page.wait_for_selector("#live")
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted", "no mismatch rebuild"
        assert not page.is_visible("#fb")


def test_flush_sync_form_reset_and_transition_types_in_chrome(page):
    import time

    from reaction import ViewTransition, add_transition_type, flush_sync, request_form_reset, use_ref

    @component
    def App():
        status, set_status = use_state("idle")
        form = use_ref()

        def save():
            flush_sync(lambda: set_status("saving"))
            time.sleep(0.6)
            request_form_reset(form)
            add_transition_type("done")
            set_status("saved")

        return html.div(
            ViewTransition(html.p(status, id="status")),
            html.form(html.input(id="q", name="q", default_value="start"), ref=form),
            html.button("save", id="save", on_click=save),
        )

    with serve(App) as server:
        open_page(page, server)
        page.evaluate("""window.__types = []; const o = document.startViewTransition.bind(document);
            document.startViewTransition = (a) => { window.__types.push(a && typeof a === 'object' ? a.types : null); return o(a); }; true""")
        page.fill("#q", "edited")
        page.click("#save")
        page.wait_for_function("document.querySelector('#status').textContent === 'saving'", timeout=500)
        assert page.input_value("#q") == "edited", "not reset until the action is done"
        page.wait_for_function("document.querySelector('#status').textContent === 'saved'")
        page.wait_for_function("document.querySelector('#q').value === 'start'")
        assert ["done"] in page.evaluate("window.__types")


def test_fastapi_app_with_pages_under_ui_in_chrome(page):
    from fastapi import FastAPI

    from reaction import Link, Navigate, Router, js_component, mount, use_location

    Bars = js_component("/static/bars.js", "mount")

    @component
    def Home():
        return html.div(
            html.p(f"at {use_location().path}", id="at"),
            Link("user 7", to="/users/7", id="to-user"),
            Link("old link", to="/old", id="to-old"),
            Bars(values=[2, 4], color="teal"),
        )

    @component
    def User(id):
        n, set_n = use_state(0)
        return html.div(html.p(f"user {id}", id="user"), html.button(f"n={n}", id="inc", on_click=lambda: set_n(n + 1)))

    @component
    def App():
        return Router({"/": Home, "/users/{id}": User, "/old": lambda: Navigate("/users/1")})

    api = FastAPI()

    @api.get("/api/hello")
    def hello():
        return {"hello": "world"}

    mount(api, App, "/ui", static_dir=STATIC)
    with serve(api, asgi=True) as server:
        assert page.request.get(server.url + "/api/hello").json() == {"hello": "world"}
        page.goto(server.url + "/ui/")
        page.wait_for_selector(server.READY, state="attached")
        assert page.text_content("#at") == "at /"
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"
        page.wait_for_selector("main button[title], div button[title]")  # bars.js loaded from /ui/static
        page.click("#to-user")
        page.wait_for_selector("#user")
        assert page.text_content("#user") == "user 7"
        assert page.evaluate("location.pathname") == "/ui/users/7"
        page.click("#inc")
        page.wait_for_function("document.querySelector('#inc').textContent === 'n=1'")
        page.go_back()
        page.wait_for_selector("#at")
        assert page.evaluate("location.pathname") == "/ui/"
        page.click("#to-old")  # a live Navigate keeps the prefix
        page.wait_for_selector("#user")
        assert page.evaluate("location.pathname") == "/ui/users/1"
        page.reload()  # served directly at its own URL
        page.wait_for_selector(server.READY, state="attached")
        assert page.text_content("#user") == "user 1"
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"


def test_fastapi_page_routes_in_chrome(page):
    from fastapi import Depends, FastAPI
    from starlette.requests import HTTPConnection

    from reaction import Link, page as pyreaction_page, use_location

    def current_user(conn: HTTPConnection):
        return conn.cookies.get("user", "guest")

    api = FastAPI()

    @pyreaction_page(api, "/orders/{order_id}")
    def order(order_id: str, user: str = Depends(current_user)):
        return Order(order_id=order_id, user=user)

    @component
    def Order(order_id, user):
        n, set_n = use_state(0)
        return html.div(
            html.p(f"{user}: order {order_id} {use_location().search}", id="title"),
            html.button(f"n={n}", id="inc", on_click=lambda: set_n(n + 1)),
            Link("sorted", to=f"/orders/{order_id}?sort=price", id="same"),
            Link("next order", to="/orders/8", id="other"),
        )

    with serve(api, asgi=True) as server:
        page.context.add_cookies([{"name": "user", "value": "ada", "url": server.url}])
        open_page(page, server, "/orders/7")
        assert page.text_content("#title").strip() == "ada: order 7"
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"
        page.click("#inc")
        page.wait_for_function("document.querySelector('#inc').textContent === 'n=1'")
        page.evaluate("window.__same_document = true")
        page.click("#same")  # same path, new query: stays live, keeps state
        page.wait_for_function("document.querySelector('#title').textContent.includes('?sort=price')")
        assert page.evaluate("window.__same_document") is True
        assert page.text_content("#inc") == "n=1"
        page.click("#other")  # another @page URL: a full page load
        page.wait_for_function("location.pathname === '/orders/8'")
        page.wait_for_selector(server.READY, state="attached")
        assert page.text_content("#title").strip() == "ada: order 8"
        assert page.evaluate("window.__same_document === undefined")
        page.go_back()
        page.wait_for_function("location.pathname === '/orders/7'")
        page.wait_for_selector("#title")
        assert "order 7" in page.text_content("#title")


def test_event_properties_on_demand_in_chrome(page):
    seen = {}

    @component
    def App():
        async def dropped(e):
            seen["types"] = await e.read("dataTransfer.types")
            seen["files"] = await e.read("dataTransfer.files")
            seen["text"] = await e.call("dataTransfer.getData", "text/plain")

        async def scrolled(e):
            seen["scroll"] = await e.target.read("scrollTop")
            seen["rect"] = await e.call("target.getBoundingClientRect")

        def clicked(e):  # a plain handler waits with .result()
            seen["shift"] = e.read("shiftKey").result()
            seen["tag"] = e.target.read("tagName").result()

        return html.div(
            html.div("drop here", id="zone", style={"height": "60px"},
                     on_drop=event(dropped, prevent_default=True), on_drag_over=event(lambda: None, prevent_default=True)),
            html.div(html.div(style={"height": "2000px"}), id="box", style={"height": "100px", "overflow": "auto"},
                     on_scroll=event(scrolled, debounce=50)),
            html.button("go", id="go", on_click=clicked),
        )

    with serve(App) as server:
        open_page(page, server)
        page.evaluate("""() => {
            const dt = new DataTransfer();
            dt.setData('text/plain', 'hello');
            dt.items.add(new File(['abc'], 'notes.txt', {type: 'text/plain'}));
            document.querySelector('#zone').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true}));
        }""")
        page.evaluate("document.querySelector('#box').scrollTop = 250")
        page.click("#go", modifiers=["Shift"])
        page.wait_for_function("true", timeout=100)
        import time

        deadline = time.monotonic() + 5
        while len(seen) < 7 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert "text/plain" in seen["types"] and "Files" in seen["types"]
        assert seen["files"] == [{"name": "notes.txt", "type": "text/plain", "size": 3, "lastModified": seen["files"][0]["lastModified"]}]
        assert seen["text"] == "hello"
        assert seen["scroll"] == 250 and seen["rect"]["height"] == 100
        assert seen["shift"] is True and seen["tag"] == "BUTTON"


def test_flask_app_with_pages_under_ui_in_chrome(page):
    flask = pytest.importorskip("flask")
    pytest.importorskip("a2wsgi")
    from reaction import Link, Router, use_location, wrap

    app = flask.Flask("flask_browser_test")

    @app.get("/")
    def home():
        return '<h1 id="flask">Flask home</h1><a id="to-ui" href="/ui/">pages</a>'

    @component
    def Index():
        n, set_n = use_state(0)
        return html.div(
            html.p(f"at {use_location().path}", id="at"),
            html.button(f"n={n}", id="inc", on_click=lambda: set_n(n + 1)),
            Link("about", to="/about", id="about"),
        )

    @component
    def About():
        return html.p("about page", id="about-page")

    @component
    def App():
        return Router({"/": Index, "/about": About})

    with serve(wrap(app, App, "/ui"), asgi=True) as server:
        page.goto(server.url + "/")
        assert page.text_content("#flask") == "Flask home"
        page.click("#to-ui")
        page.wait_for_selector(server.READY, state="attached")
        assert page.text_content("#at") == "at /"
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"
        page.click("#inc")
        page.wait_for_function("document.querySelector('#inc').textContent === 'n=1'")
        page.click("#about")
        page.wait_for_selector("#about-page")
        assert page.evaluate("location.pathname") == "/ui/about"
        page.go_back()
        page.wait_for_selector("#at")
        assert page.evaluate("location.pathname") == "/ui/"


def test_js_component_from_string_in_chrome(page):
    from reaction import js_component_from_string

    Counter = js_component_from_string("""
        export default function mount(element, props, emit, slot) {
          const button = document.createElement("button");
          button.id = "js-btn";
          const draw = (p) => (button.textContent = `${p.label}: ${p.count}`);
          button.onclick = () => emit("bump", { by: 2 });
          draw(props);
          element.append(button, slot);
          return { update: draw, unmount() { button.remove(); } };
        }
    """)

    @component
    def App():
        count, set_count = use_state(0)
        return html.main(
            Counter(html.p(f"python sees {count}", id="py"), label="clicks", count=count,
                    on_bump=lambda e: set_count(count + e.by)),
        )

    with serve(App) as server:
        open_page(page, server)
        page.wait_for_selector("#js-btn")
        assert page.text_content("#js-btn") == "clicks: 0"
        page.click("#js-btn")
        page.wait_for_function("document.querySelector('#js-btn').textContent === 'clicks: 2'")
        assert page.text_content("#py") == "python sees 2"


def test_page_gives_up_after_reconnect_attempts_in_chrome(page):
    @component
    def App():
        return html.p("hello", id="hello")

    sockets = []

    def opened(ws):
        sockets.append(ws.url)

    page.on("websocket", opened)
    server = serve(App, reconnect_attempts=2)
    try:
        open_page(page, server)
        assert page.query_selector("#pyreaction-lost") is None
    finally:
        server.close()  # the page loses its connection and cannot get it back
    page.wait_for_selector("#pyreaction-lost", timeout=10000)
    assert page.text_content("#pyreaction-lost span") == "Connection to the server was lost."
    assert page.text_content("#pyreaction-lost button") == "Reload"
    assert page.evaluate("document.documentElement.hasAttribute('data-pyreaction-lost')")
    # The first connection, then two failed attempts, then no more.
    attempts = len(sockets)
    page.wait_for_timeout(2000)
    page.remove_listener("websocket", opened)
    assert attempts == 3 and len(sockets) == attempts, sockets


def test_page_keeps_retrying_without_a_limit_in_chrome(page):
    @component
    def App():
        return html.p("hello", id="hello")

    server = serve(App)
    try:
        open_page(page, server)
    finally:
        server.close()
    page.wait_for_selector("html[data-pyreaction-offline]", state="attached", timeout=10000)
    page.wait_for_timeout(2000)
    assert page.query_selector("#pyreaction-lost") is None, "the default retries for as long as the page is open"


def test_markup_that_differs_is_rebuilt_from_the_server_in_chrome(page):
    import itertools

    renders = itertools.count()

    @component
    def App():
        n, set_n = use_state(0)
        # Differs between the page's HTML and the live render, like a clock would.
        return html.div(html.p(f"render {next(renders)}", id="which"),
                        html.button(f"n={n}", id="inc", on_click=lambda: set_n(n + 1)))

    # handoff_timeout=0: the live connection renders again, so the render differs.
    with serve(App, resume_timeout=0, handoff_timeout=0) as server:
        open_page(page, server)
        page.wait_for_function("document.querySelector('#pyreaction-root').dataset.hydration === 'rebuilt'")
        assert page.text_content("#which") != "render 0", "the live render, not the HTML"
        page.click("#inc")
        page.wait_for_function("document.querySelector('#inc').textContent === 'n=1'")


def test_virtual_list_scrolls_in_chrome(page):
    from reaction import VirtualList

    items = list(range(100_000))

    @component
    def App():
        return VirtualList(items, lambda n, i: html.span(f"row {n}", class_name="row"),
                           row_height=24, height=240, class_name="list")

    with serve(App) as server:
        open_page(page, server)
        assert page.locator(".row").count() < 30
        page.evaluate("document.querySelector('.list').scrollTop = 24 * 70000")
        page.wait_for_function("[...document.querySelectorAll('.row')].some(r => r.textContent === 'row 70000')")
        first_visible = page.evaluate("""() => {
            const box = document.querySelector('.list').getBoundingClientRect();
            return [...document.querySelectorAll('.row')].find(r => r.getBoundingClientRect().top >= box.top - 1).textContent;
        }""")
        assert first_visible == "row 70000", "the rows on screen are the ones scrolled to"
        # A quick burst: throttling still sends where the scrolling stopped.
        page.evaluate("""() => { const box = document.querySelector('.list');
            for (let k = 1; k <= 20; k++) box.scrollTop = 24 * (1000 + k * 100); }""")
        page.wait_for_function("[...document.querySelectorAll('.row')].some(r => r.textContent === 'row 3000')")
        assert page.locator(".row").count() < 30


def test_elements_holding_their_own_text_hydrate_and_update_in_chrome(page):
    @component
    def App():
        n, set_n = use_state(0)
        cell = html.td(html.b(f"bold {n}"), " tail") if n % 2 else html.td(n)
        return html.table(html.tbody(html.tr(cell, html.td(""), html.td("x < y")),
                                     html.tr(html.td(html.button("+", id="inc", on_click=lambda: set_n(n + 1))))))

    with serve(App) as server:
        open_page(page, server)
        assert page.get_attribute("#pyreaction-root", "data-hydration") == "adopted"
        cell = "document.querySelector('td')"
        assert page.evaluate(f"{cell}.innerHTML") == "0"
        page.click("#inc")
        page.wait_for_function(f"{cell}.innerHTML === '<b>bold 1</b> tail'")
        page.click("#inc")
        page.wait_for_function(f"{cell}.innerHTML === '2'")
        page.click("#inc")
        page.wait_for_function(f"{cell}.innerHTML === '<b>bold 3</b> tail'")
        assert page.evaluate("document.querySelectorAll('td')[2].textContent") == "x < y"


def test_scroll_listeners_are_passive_unless_they_may_cancel_in_chrome(page):
    @component
    def App():
        return html.div(
            html.div("plain", id="plain", on_wheel=lambda: None, on_touch_move=lambda: None),
            html.div("cancels", id="cancels", on_wheel=event(lambda: None, prevent_default=True)),
            html.div("js", id="js", on_wheel=js("element.dataset.seen = 1")),
            html.button("click", id="click", on_click=lambda: None),
        )

    # Record each listener's options as the client script adds it.
    page.add_init_script("""
        window.__listeners = [];
        const add = EventTarget.prototype.addEventListener;
        EventTarget.prototype.addEventListener = function (type, fn, options) {
          if (this.id) window.__listeners.push([this.id, type, typeof options === "object" && !!options.passive]);
          return add.call(this, type, fn, options);
        };
    """)
    with serve(App) as server:
        open_page(page, server)
        listeners = page.evaluate("window.__listeners")
        assert ["plain", "wheel", True] in listeners and ["plain", "touchmove", True] in listeners
        assert ["cancels", "wheel", False] in listeners, "prevent_default needs an active listener"
        assert ["js", "wheel", False] in listeners, "js() code might call preventDefault"
        assert ["click", "click", False] in listeners


def test_js_handlers_with_the_same_code_compile_once_in_chrome(page):
    @component
    def App():
        return html.ul([html.li(str(i), id=f"li{i}", on_click=js("element.dataset.hit = 1")) for i in range(50)])

    page.add_init_script("""
        window.__compiles = 0;
        const Real = Function;
        window.Function = function (...args) { window.__compiles++; return Real(...args); };
    """)
    with serve(App) as server:
        open_page(page, server)
        assert page.evaluate("window.__compiles") == 1
        page.click("#li7")
        assert page.get_attribute("#li7", "data-hit") == "1"
