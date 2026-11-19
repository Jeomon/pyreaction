"""React's rules, enforced: endless re-renders, hooks called conditionally, hooks
outside rendering."""

import logging

import pytest

from reaction import ErrorBoundary, component, html, use_effect, use_state
from reaction.testing import render


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def test_setting_state_while_rendering_stops_with_too_many_re_renders():
    @component
    def App():
        n, set_n = use_state(0)
        set_n(n + 1)  # what on_click=vote() does: the setter runs during render
        return html.p(n)

    with render(App(), raise_errors=False) as page:
        assert len(page.errors) == 1
        assert "Too many re-renders in App" in str(page.errors[0])
        assert "on_click=vote()" in str(page.errors[0])


def test_too_many_re_renders_goes_to_the_error_boundary():
    @component
    def Loop():
        n, set_n = use_state(0)
        set_n(n + 1)
        return html.p(n)

    @component
    def App():
        return ErrorBoundary(Loop(), fallback=lambda error, reset: html.p("broken", class_name="fallback"))

    with render(App()) as page:
        assert page.find(".fallback").text == "broken"


def test_a_few_renders_in_a_row_are_fine():
    @component
    def App():
        n, set_n = use_state(0)
        if n < 5:
            set_n(n + 1)  # adjusting state while rendering, a bounded number of times
        return html.p(n)

    with render(App()) as page:
        assert page.html() == "<p>5</p>"


@pytest.mark.parametrize("start, click_to, word", [(3, 0, "fewer"), (0, 1, "more")])
def test_a_hook_inside_an_if_is_an_error(start, click_to, word):
    @component
    def Countdown():
        seconds, set_seconds = use_state(start)
        if seconds > 0:
            use_effect(lambda: None, [])
        return html.button(seconds, on_click=lambda: set_seconds(click_to))

    with render(Countdown(), raise_errors=False) as page:
        page.click("button")
        assert len(page.errors) == 1
        assert f"Rendered {word} hooks than during the previous render of Countdown" in str(page.errors[0])
        assert page.html() == f"<button>{start}</button>", "the last good output stays"


def test_calling_a_hook_in_an_event_handler_is_an_invalid_hook_call():
    @component
    def App():
        def vote():
            use_state(None)

        return html.button("vote", on_click=vote)

    with render(App(), raise_errors=False) as page:
        page.click("button")
        assert "Invalid hook call" in str(page.errors[0])


def test_context_provider_is_the_older_spelling():
    from reaction import create_context, use_context

    Votes = create_context(None)

    @component
    def Banner():
        return html.p(f"pizza: {use_context(Votes)['pizza']}")

    @component
    def App():
        return Votes.Provider(html.div(Banner()), value={"pizza": 3})

    with render(App()) as page:
        assert page.html() == "<div><p>pizza: 3</p></div>"


def test_use_layout_effect_runs_like_use_effect():
    from reaction import use_layout_effect

    log = []

    @component
    def App():
        n, set_n = use_state(0)
        use_layout_effect(lambda: (log.append(f"set {n}"), lambda: log.append(f"clean {n}"))[1], [n])
        return html.button(n, on_click=lambda: set_n(n + 1))

    with render(App()) as page:
        page.click("button")
    assert log == ["set 0", "clean 0", "set 1", "clean 1"]


def _strict_app(log):
    from reaction import StrictMode

    @component
    def Countdown():
        log.append("render")

        def start():
            log.append("setup")
            return lambda: log.append("cleanup")

        use_effect(start, [])
        return html.p("ticking")

    @component
    def App():
        return StrictMode(Countdown())

    return App


def _root_log(debug):
    import asyncio

    from reaction import Root

    log = []

    async def main():
        root = Root(_strict_app(log)(), debug=debug, threaded=False)
        root.attach(lambda message: None)
        await root.settle()
        root.close()

    asyncio.run(main())
    return log


def test_strict_mode_in_debug_renders_twice_and_sets_effects_up_twice():
    assert _root_log(debug=True) == ["render", "render", "setup", "cleanup", "setup", "cleanup"]


def test_strict_mode_does_nothing_outside_debug_mode():
    assert _root_log(debug=False) == ["render", "setup", "cleanup"]


def test_handlers_with_default_arguments_keep_them():
    """on_click=lambda r=r: ... captures a loop variable; it must not get the event."""
    picked = []

    @component
    def App():
        return html.ul([html.li(r, on_click=lambda r=r: picked.append(r), key=r) for r in ("a", "b")])

    with render(App()) as page:
        page.click(page.find_all("li")[1])
    assert picked == ["b"]


def test_handlers_with_a_required_parameter_get_the_event():
    seen = []

    @component
    def App():
        return html.input(value="", on_change=lambda e, extra=1: seen.append((e.value, extra)))

    with render(App()) as page:
        page.type("input", "hi")
    assert seen == [("hi", 1)]
