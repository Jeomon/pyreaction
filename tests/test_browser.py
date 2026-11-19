"""Tests in a real browser (Chrome through Playwright): what the fake browser cannot run."""

import asyncio
import time
from pathlib import Path

import pytest

from pyreaction import Suspense, component, event, html, js, js_component, use_data, use_state
from pyreaction.testing import serve

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
    from pyreaction import portal

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
