import asyncio

from reaction import (
    ErrorBoundary,
    Link,
    Router,
    component,
    html,
    render_to_string,
    use_location,
    use_navigate,
    use_ref,
    use_request,
    use_state,
    use_title,
)

from .test_vdom import Harness


def test_ref_commands_reach_the_browser_and_release_on_unmount():
    refs = {}
    setters = {}

    @component
    def App():
        ref = use_ref()
        show, set_show = use_state(True)
        refs["input"] = ref
        setters["show"] = set_show
        return html.div(html.input(ref=ref) if show else None)

    h = Harness(App())
    handle = refs["input"].current
    assert handle is not None
    handle.focus()
    handle.scroll_into_view(behavior="smooth")
    ops = h.flush()
    assert ops == [
        ["m", handle.id, "focus", [], 1],
        ["m", handle.id, "scrollIntoView", [{"behavior": "smooth", "block": "start"}], 2],
    ]
    setters["show"](False)
    h.flush()
    assert refs["input"].current is None
    handle.focus()  # a command for a removed element is dropped
    assert h.flush() == []


def test_commands_before_attach_run_after_hydration():
    refs = {}

    @component
    def App():
        refs["input"] = use_ref()
        return html.input(ref=refs["input"])

    sent = []

    async def go():
        from reaction import Root

        root = Root(App())
        refs["input"].current.focus()  # before the browser is connected
        root.attach(sent.append, hydrate=True)

    asyncio.run(go())
    ops = sent[0]["ops"]
    assert ops[0][0] == "c", "the snapshot comes first"
    assert ops[-1] == ["m", refs["input"].current.id, "focus", [], 1]


def test_router_matches_params_and_follows_navigation():
    @component
    def Home():
        return html.h1("home")

    @component
    def User(id):
        return html.h1(f"user {id}")

    @component
    def Files(rest):
        return html.h1(f"file {rest}")

    @component
    def App():
        return html.div(
            Link("Users", to="/users/7", class_name="nav"),
            Router(
                {"/": Home, "/users/{id}": User, "/files/{rest:path}": Files},
                fallback=html.h1("not found"),
            ),
        )

    h = Harness(App(), location="/")
    assert h.html() == '<div><a class="nav" data-pyreaction-link="push" href="/users/7">Users</a><h1>home</h1></div>'
    h.root.dispatch(1, 0, "#navigate", {"to": "/users/7"})
    h.flush()
    assert "<h1>user 7</h1>" in h.html()
    h.root.dispatch(2, 0, "#navigate", {"to": "/files/a/b.txt"})
    h.flush()
    assert "<h1>file a/b.txt</h1>" in h.html()
    h.root.dispatch(3, 0, "#navigate", {"to": "/nope"})
    h.flush()
    assert "<h1>not found</h1>" in h.html()
    h.root.dispatch(4, 0, "#navigate", {"to": "https://evil.example/"})
    assert h.flush() == [], "only same-site paths are accepted"


def test_navigate_from_python_tells_the_browser():
    nav = {}

    @component
    def App():
        nav["go"] = use_navigate()
        return html.p(use_location().path)

    h = Harness(App(), location="/start")
    nav["go"]("/next?x=1")
    h.flush()
    assert h.sent[-1]["nav"] == {"to": "/next?x=1", "replace": False}
    assert h.html() == "<p>/next</p>"


def test_title_follows_renders():
    setters = {}

    @component
    def App():
        n, set_n = use_state(0)
        setters["n"] = set_n
        use_title(f"{n} unread")
        return html.p(n)

    h = Harness(App())
    assert h.root.title == "0 unread"
    setters["n"](3)
    h.flush()
    assert h.sent[-1]["title"] == "3 unread"
    setters["n"](3)
    h.flush()
    assert h.sent == [], "no message when nothing changed"


def test_use_request_is_the_connection():
    seen = {}

    @component
    def App():
        seen["request"] = use_request()
        return html.p()

    marker = object()
    Harness(App(), connection=marker)
    assert seen["request"] is marker


def test_error_boundary_shows_fallback_and_resets(caplog):
    setters = {}
    resets = {}

    @component
    def Fragile():
        n, set_n = use_state(0)
        setters["n"] = set_n
        if n == 1:
            raise ValueError("bad row")
        return html.p(f"ok {n}")

    def fallback(error, reset):
        resets["reset"] = reset
        return html.p(f"failed: {error}")

    @component
    def App():
        return html.div(html.h1("title"), ErrorBoundary(Fragile(), fallback=fallback))

    h = Harness(App())
    setters["n"](1)
    h.flush()
    assert h.html() == "<div><h1>title</h1><p>failed: bad row</p></div>"
    assert "bad row" in caplog.text
    resets["reset"]()
    h.flush()
    assert h.html() == "<div><h1>title</h1><p>ok 0</p></div>", "reset mounts the children afresh"


def test_error_boundary_catches_during_first_render():
    @component
    def Broken():
        raise RuntimeError("no data")

    @component
    def App():
        return html.main(ErrorBoundary(Broken()), html.footer("f"))

    h = Harness(App())
    assert h.html() == '<main><div role="alert">Something went wrong.</div><footer>f</footer></main>'
    assert "Something went wrong" in render_to_string(h.root), "server HTML shows the fallback too"


def test_errors_without_a_boundary_are_reported_in_debug_mode():
    setters = {}

    @component
    def App():
        n, set_n = use_state(0)
        setters["n"] = set_n
        if n:
            raise KeyError("missing")
        return html.p(n)

    h = Harness(App(), debug=True)
    setters["n"](1)
    h.flush()
    assert "rendering App" in h.sent[-1]["errors"][0]
    assert "KeyError" in h.sent[-1]["errors"][0]
    assert h.html() == "<p>0</p>", "the last good output stays"


def test_error_boundary_resets_when_reset_keys_change():
    @component
    def Page():
        path = use_location().path
        if path == "/bad":
            raise ValueError("bad page")
        return html.p(path)

    @component
    def App():
        return ErrorBoundary(Page(), reset_keys=[use_location().path])

    h = Harness(App(), location="/bad")
    assert h.html() == '<div role="alert">Something went wrong.</div>'
    h.root.dispatch(1, 0, "#navigate", {"to": "/good"})
    h.flush()
    assert h.html() == "<p>/good</p>"
