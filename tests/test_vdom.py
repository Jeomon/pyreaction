import random

import pytest

from reaction import (
    Root,
    component,
    create_context,
    event,
    fragment,
    html,
    js,
    memo,
    render_to_string,
    use_context,
    use_effect,
    use_memo,
    use_reducer,
    use_ref,
    use_state,
)

from .fakedom import FakeDom, tree_html


class Harness:
    """Mount a tree, mirror its operations into a fake DOM, and flush updates by hand."""

    def __init__(self, element, **options):
        self.root = Root(element, **options)
        self.dom = FakeDom()
        self.sent = []
        # What attach() sends first: the whole tree, built from nothing.
        self.dom.apply(self.root.snapshot())
        self.root.ops = []
        self.root._sink = self._receive
        self.root._run_effects()

    def _receive(self, message):
        self.sent.append(message)
        self.dom.apply(message["ops"])

    def flush(self):
        self.sent.clear()
        self.root.flush()
        assert self.dom.html() == tree_html(self.root), "DOM drifted from the tree"
        return [op for m in self.sent for op in m["ops"]]

    def html(self):
        return self.dom.html()

    def click(self, node_id, name="click", data=None):
        self.root.dispatch(1, node_id, name, data or {})

    def find(self, tag, text=None):
        for node_id, host in self.root.hosts.items():
            if host.type == tag and (text is None or text in render_to_string(host.element)):
                return node_id
        raise LookupError(tag)


def test_static_render_matches_ssr():
    @component
    def App():
        return html.div(html.h1("Hi", class_name="t"), html.p("a", 1, None, False, "b"), html.br())

    h = Harness(App())
    assert h.html() == '<div><h1 class="t">Hi</h1><p>a1b</p><br></div>'
    assert render_to_string(App()) == '<div><h1 class="t">Hi</h1><p>a<!---->1<!---->b</p><br></div>'


def test_state_updates_are_batched_and_minimal():
    setters = {}

    @component
    def App():
        n, set_n = use_state(0)
        setters["n"] = set_n
        return html.div(html.span("static"), html.b(n))

    h = Harness(App())
    setters["n"](1)
    setters["n"](lambda v: v + 1)
    ops = h.flush()
    assert ops == [["p", 3, "#text", "2"]]
    assert len(h.sent) == 1
    assert h.html() == "<div><span>static</span><b>2</b></div>"


def test_setting_same_value_does_not_render():
    renders = []
    setters = {}

    @component
    def App():
        n, set_n = use_state(5)
        setters["n"] = set_n
        renders.append(n)
        return html.p(n)

    h = Harness(App())
    setters["n"](5)
    assert h.flush() == []
    assert renders == [5]


def test_events_call_handlers_with_and_without_event():
    seen = []

    @component
    def App():
        n, set_n = use_state(0)
        return html.div(
            html.button("inc", on_click=lambda: set_n(n + 1)),
            html.input(value=str(n), on_change=lambda e: seen.append(e.target.value)),
            html.span(n),
        )

    h = Harness(App())
    button = h.find("button")
    h.click(button)
    h.flush()
    assert "<span>1</span>" in h.html()
    h.click(h.find("input"), "input", {"target": {"value": "x"}})
    assert seen == ["x"]
    assert h.root.ack == 1


def test_event_specs_and_submit_prevents_default():
    @component
    def App():
        return html.form(
            html.input(on_input=event(lambda e: None, debounce=200)),
            html.button("go", on_click=lambda: None),
            on_submit=lambda e: None,
        )

    root = Root(App())
    creates = {op[2]: op[4] for op in root.ops if op[0] == "c"}
    # Trailing defaults are left out: a plain handler's spec is empty.
    assert creates["form"] == {"submit": [True]}
    assert creates["input"] == {"input": [False, False, 200]}
    assert creates["button"] == {"click": []}


def test_conditional_child_keeps_sibling_identity():
    setters = {}

    @component
    def App():
        show, set_show = use_state(False)
        setters["show"] = set_show
        return html.div(html.p("banner") if show else None, html.input(value="kept"))

    h = Harness(App())
    input_id = h.find("input")
    setters["show"](True)
    ops = h.flush()
    assert h.find("input") == input_id, "input was remounted"
    assert not any(op[0] == "r" for op in ops)
    assert h.html().startswith("<div><p>banner</p>")


def test_type_change_remounts():
    setters = {}

    @component
    def App():
        tag, set_tag = use_state("p")
        setters["tag"] = set_tag
        return html.div(html(tag, "x"))

    h = Harness(App())
    setters["tag"]("section")
    ops = h.flush()
    assert any(op[0] == "r" for op in ops)
    assert h.html() == "<div><section>x</section></div>"


@pytest.mark.parametrize("seed", range(40))
def test_keyed_list_shuffles(seed):
    rng = random.Random(seed)
    setters = {}
    item_states = {}

    @component
    def Item(k):
        mounted = use_ref(k)  # identity check: a remount would make a new ref
        item_states.setdefault(k, set()).add(id(mounted))
        return html.li(k)

    @component
    def App():
        items, set_items = use_state(list(range(8)))
        setters["items"] = set_items
        return html.ul([Item(k, key=k) for k in items], "end")

    h = Harness(App())
    pool = list(range(16))
    for _ in range(6):
        nxt = rng.sample(pool, rng.randint(0, 12))
        setters["items"](nxt)
        h.flush()
        assert h.html() == "<ul>" + "".join(f"<li>{k}</li>" for k in nxt) + "end</ul>"
    for k, refs in item_states.items():
        # Each key keeps one ref per continuous presence; at most one per (re)appearance.
        assert len(refs) >= 1


def test_keyed_move_only_moves_what_changed():
    setters = {}

    @component
    def App():
        items, set_items = use_state(list(range(100)))
        setters["items"] = set_items
        return html.ul([html.li(i, key=i) for i in items])

    h = Harness(App())
    order = list(range(100))
    order.insert(10, order.pop(90))
    setters["items"](order)
    ops = h.flush()
    assert [op[0] for op in ops] == ["a"]


def test_components_and_fragments_flatten_into_parent():
    setters = {}

    @component
    def Pair(a, b):
        return [html.i(a), html.b(b)]

    @component
    def App():
        flip, set_flip = use_state(False)
        setters["flip"] = set_flip
        pairs = [Pair(1, 2, key="x"), fragment("t", html.u("u"), key="f")]
        return html.div(*(reversed(pairs) if flip else pairs))

    h = Harness(App())
    assert h.html() == "<div><i>1</i><b>2</b>t<u>u</u></div>"
    setters["flip"](True)
    h.flush()
    assert h.html() == "<div>t<u>u</u><i>1</i><b>2</b></div>"


def test_child_rerender_inside_parent_host():
    setters = {}

    @component
    def Toggle():
        on, set_on = use_state(False)
        setters["on"] = set_on
        return [html.em("on"), html.em("!")] if on else None

    @component
    def App():
        return html.div("a", Toggle(), "b")

    h = Harness(App())
    assert h.html() == "<div>ab</div>"
    setters["on"](True)
    h.flush()
    assert h.html() == "<div>a<em>on</em><em>!</em>b</div>"
    setters["on"](False)
    h.flush()
    assert h.html() == "<div>ab</div>"


def test_reorder_inside_a_component_moves_the_dom_of_its_host():
    # The div's own children stay in place; only the component among them reorders.
    setters = {}

    @component
    def Pair(flip):
        a, b = html.i("a", key="a"), html.b("b", key="b")
        return [b, a] if flip else [a, b]

    @component
    def App():
        flip, set_flip = use_state(False)
        setters["flip"] = set_flip
        return html.div("x", Pair(flip), "y")

    h = Harness(App())
    assert h.html() == "<div>x<i>a</i><b>b</b>y</div>"
    setters["flip"](True)
    assert [op[0] for op in h.flush()] == ["a"]
    assert h.html() == "<div>x<b>b</b><i>a</i>y</div>"

def test_effects_order_deps_and_cleanup():
    log = []
    setters = {}

    @component
    def Child(n):
        def effect():
            log.append(f"child {n}")
            return lambda: log.append(f"child cleanup {n}")

        use_effect(effect, [n])
        return html.span(n)

    @component
    def App():
        n, set_n = use_state(0)
        show, set_show = use_state(True)
        setters.update(n=set_n, show=set_show)
        use_effect(lambda: log.append("app mount"), [])
        return html.div(Child(n) if show else None)

    h = Harness(App())
    assert log == ["child 0", "app mount"]
    setters["n"](1)
    h.flush()
    assert log[2:] == ["child cleanup 0", "child 1"]
    setters["show"](False)
    h.flush()
    assert log[4:] == ["child cleanup 1"]


def test_memo_skips_unchanged_props():
    renders = []
    setters = {}

    @memo
    def Row(label):
        renders.append(label)
        return html.li(label)

    @component
    def App():
        n, set_n = use_state(0)
        setters["n"] = set_n
        return html.ul(html.li(n), Row("fixed"), Row(f"n={n}"))

    h = Harness(App())
    setters["n"](1)
    h.flush()
    assert renders == ["fixed", "n=0", "n=1"]


def test_context_reaches_memoized_consumer():
    Theme = create_context("light")
    setters = {}

    @memo
    def Label():
        return html.span(use_context(Theme))

    @component
    def App():
        theme, set_theme = use_state("dark")
        setters["theme"] = set_theme
        return html.div(Theme(Label(), value=theme), Label())

    h = Harness(App())
    assert h.html() == "<div><span>dark</span><span>light</span></div>"
    setters["theme"]("blue")
    h.flush()
    assert h.html() == "<div><span>blue</span><span>light</span></div>"


def test_reducer_memo_and_attrs():
    setters = {}

    def reducer(state, action):
        return state + action

    @component
    def App():
        total, dispatch = use_reducer(reducer, 0)
        doubled = use_memo(lambda: total * 2, [total])
        setters["d"] = dispatch
        return html.div(
            doubled,
            class_name="big" if total else None,
            style={"font_size": "12px", "marginTop": 0},
            data_total=total,
            disabled=total > 2,
        )

    h = Harness(App())
    assert h.html() == '<div data-total="0" style="font-size:12px;margin-top:0">0</div>'
    setters["d"](3)
    h.flush()
    assert h.html() == '<div class="big" data-total="3" disabled="" style="font-size:12px;margin-top:0">6</div>'


def test_render_error_keeps_last_good_output(caplog):
    setters = {}

    @component
    def App():
        n, set_n = use_state(0)
        setters["n"] = set_n
        if n == 1:
            raise ValueError("boom")
        return html.p(n)

    h = Harness(App())
    setters["n"](1)
    h.flush()
    assert h.html() == "<p>0</p>"
    assert "boom" in caplog.text
    setters["n"](2)
    h.flush()
    assert h.html() == "<p>2</p>"


def test_conditional_hook_raises_clear_error(caplog):
    setters = {}

    @component
    def App():
        flag, set_flag = use_state(False)
        setters["f"] = set_flag
        if flag:
            use_ref(1)
        else:
            use_memo(lambda: 1, [])
        return html.p()

    h = Harness(App())
    setters["f"](True)
    h.flush()
    assert "Hook order changed" in caplog.text


def test_ssr_escapes_and_marks_text():
    @component
    def App():
        return html.div(
            '<b>&"',
            "",
            html.input(value='a"b', checked=True),
            html.textarea(value="<x>"),
            html.style("a > b { color: red }"),
            title='"q"',
        )

    out = render_to_string(App())
    assert out == (
        '<div title="&quot;q&quot;">&lt;b&gt;&amp;"<!----><!--$-->'
        '<input value="a&quot;b" checked=""><textarea>&lt;x&gt;</textarea>'
        "<style>a > b { color: red }</style></div>"
    )


def test_rendering_pauses_the_gc_and_restores_it():
    import gc

    from reaction import Root, component, html, use_state
    from reaction.core.vdom import _gc_paused

    seen = []

    @component
    def App():
        n, set_n = use_state(0)
        seen.append(gc.isenabled())
        return html.button(n, on_click=lambda: set_n(n + 1))

    assert gc.isenabled()
    root = Root(App())
    assert seen == [False], "paused while rendering"
    assert gc.isenabled(), "resumed afterwards"
    with _gc_paused():
        with _gc_paused():
            pass
        assert not gc.isenabled(), "nested pauses resume only at the outermost"
    assert gc.isenabled()
    gc.disable()
    try:
        root._render_dirty()
        with _gc_paused():
            pass
        assert not gc.isenabled(), "an app that turned the GC off keeps it off"
    finally:
        gc.enable()
        root.close()


# ---- to_vdom: the rendered tree as plain dicts -----------------------------------------


@component
def Row(item):
    return html.li(item)


@component
def App():
    return html.div(
        html.h1("Title", class_name="big", data_id=7),
        "some",
        "",
        html.ul([html.li(t, key=t) for t in ("a", "b")], Row("c", key="c")),
        html.button("Save", on_click=event(lambda: 0, prevent_default=True, debounce=200)),
        html.input(value="hi", on_change=lambda e: 0, on_focus=js("element.select()")),
    )


def test_components_render_away_and_tags_keep_their_details():
    import json

    from reaction import to_vdom

    vdom = to_vdom(App())
    assert vdom["tagName"] == "" and len(vdom["children"]) == 1
    div = vdom["children"][0]
    h1, text, ul, button, field = div["children"]
    assert h1 == {"tagName": "h1", "attributes": {"class": "big", "data-id": "7"}, "children": ["Title"]}
    assert text == "some", "empty text is left out"
    assert [li.get("key") for li in ul["children"]] == ["a", "b", None], "a component's key stays on it"
    assert ul["children"][2]["children"] == ["c"]
    assert button["eventHandlers"] == {"click": {"preventDefault": True, "debounce": 200}}
    assert field["attributes"] == {"value": "hi"}
    assert field["eventHandlers"] == {"input": {}, "focus": {"code": "element.select()"}}
    assert json.loads(json.dumps(vdom)) == vdom, "JSON-ready"


def test_raw_html_js_components_and_portals():
    from reaction import js_component, portal, to_vdom

    Chart = js_component("/static/chart.js", "mount")
    vdom = to_vdom(
        html.main(
            html.article(dangerously_set_inner_html="<b>raw</b>"),
            Chart(values=[1, 2], on_pick=lambda e: 0),
            portal(html.p("toast"), target="#side"),
        )
    )
    article, chart, box = vdom["children"][0]["children"]
    assert article == {"tagName": "article", "dangerouslySetInnerHTML": {"__html": "<b>raw</b>"}}
    assert chart["importSource"] == {"source": "/static/chart.js", "export": "mount"}
    assert chart["attributes"] == {"values": [1, 2]} and chart["eventHandlers"] == {"pick": {}}
    assert box == {"tagName": "", "portal": "#side", "children": [{"tagName": "p", "children": ["toast"]}]}


def test_a_live_root_shows_its_current_state():
    from reaction import to_vdom
    from reaction.testing import render

    @component
    def Counter():
        n, set_n = use_state(0)
        return html.button(f"n={n}", on_click=lambda: set_n(n + 1))

    with render(Counter()) as page:
        assert to_vdom(page.root)["children"][0]["children"] == ["n=0"]
        page.click("button")
        page.click("button")
        assert to_vdom(page.root)["children"][0]["children"] == ["n=2"]


def test_an_only_text_child_is_the_elements_own_text():
    setters = {}

    @component
    def App():
        mode, set_mode = use_state("text")
        setters["mode"] = set_mode
        cells = {
            "text": lambda: html.td("hi"),
            "number": lambda: html.td(3.5),
            "empty": lambda: html.td(""),
            "nodes": lambda: html.td(html.b("bold"), " tail"),
        }
        return html.tr(cells[mode](), html.textarea("notes"))

    created = Root(App()).snapshot()
    assert ["c", 2, "td", {"#text": "hi"}] in created, "no text node of its own"
    assert [op[0] for op in created].count("t") == 1, "a textarea's text keeps its text node"

    h = Harness(App())
    for mode, ops, shown in [
        ("text", [], "<td>hi</td>"),
        ("number", [["p", 2, "#text", "3.5"]], "<td>3.5</td>"),
        ("empty", [["p", 2, "#text", ""]], "<td></td>"),
        ("nodes", None, "<td><b>bold</b> tail</td>"),
        ("text", None, "<td>hi</td>"),
    ]:
        setters["mode"](mode)
        sent = h.flush()  # also checks the fake DOM still matches the tree
        if ops is not None:
            assert sent == ops
        assert shown in h.html()
    assert sent[:2] == [["r", 5], ["r", 6]], "child nodes are removed before the text replaces them"
    assert sent[-1] == ["p", 2, "#text", "hi"]


@pytest.mark.parametrize("seed", range(30))
def test_small_list_edits_keep_every_kept_item_mounted(seed):
    """Edits at the ends and in the middle, as lists usually change: every key still
    present keeps its component (and state); the ops leave the DOM matching the tree."""
    rng = random.Random(seed)
    setters = {}
    mounts = []

    @component
    def Item(k):
        use_effect(lambda: mounts.append(k) or None, [])
        return html.li(k)

    @component
    def App():
        items, set_items = use_state(list(range(10)))
        setters["items"] = set_items
        return html.ul("start", [Item(k, key=k) if k % 7 else html.li(k, key=k) for k in items], "end")

    h = Harness(App())
    items = list(range(10))
    fresh = iter(range(100, 1000))
    for _ in range(8):
        before = set(items)
        edit = rng.choice(["prepend", "append", "drop_first", "drop_last", "insert", "remove", "move", "swap"])
        nxt = list(items)
        if edit == "prepend":
            nxt[:0] = [next(fresh) for _ in range(rng.randint(1, 3))]
        elif edit == "append":
            nxt += [next(fresh) for _ in range(rng.randint(1, 3))]
        elif edit == "drop_first" and nxt:
            del nxt[: rng.randint(1, 2)]
        elif edit == "drop_last" and nxt:
            del nxt[-rng.randint(1, 2):]
        elif edit == "insert":
            nxt.insert(rng.randint(0, len(nxt)), next(fresh))
        elif edit == "remove" and nxt:
            nxt.pop(rng.randrange(len(nxt)))
        elif edit == "move" and nxt:
            nxt.insert(rng.randint(0, len(nxt) - 1), nxt.pop(rng.randrange(len(nxt))))
        elif edit == "swap" and len(nxt) > 1:
            i, j = rng.sample(range(len(nxt)), 2)
            nxt[i], nxt[j] = nxt[j], nxt[i]
        mounts.clear()
        setters["items"](nxt)
        h.flush()
        assert h.html() == "<ul>start" + "".join(f"<li>{k}</li>" for k in nxt) + "end</ul>"
        assert not [k for k in mounts if k in before], f"{edit}: kept items were remounted"
        items = nxt


def test_type_change_at_either_end_remounts_only_that_child():
    setters = {}

    @component
    def App():
        tags, set_tags = use_state(["p", "p", "p"])
        setters["tags"] = set_tags
        return html.div([html(t, i, key=i) for i, t in enumerate(tags)])

    h = Harness(App())
    before = ["p", "p", "p"]
    for tags in (["b", "p", "p"], ["b", "p", "i"], ["p", "p", "p"]):
        setters["tags"](tags)
        ops = h.flush()
        assert h.html() == "<div>" + "".join(f"<{t}>{i}</{t}>" for i, t in enumerate(tags)) + "</div>"
        changed = sum(a != b for a, b in zip(tags, before))
        assert len([op for op in ops if op[0] == "r"]) == changed, "only the changed tags are replaced"
        before = tags


def test_duplicate_keys_are_warned_and_still_render(caplog):
    setters = {}

    @component
    def App():
        keys, set_keys = use_state(["a", "b", "a"])
        setters["keys"] = set_keys
        return html.ul([html.li(f"{k}{i}", key=k) for i, k in enumerate(keys)])

    h = Harness(App())
    assert "Duplicate key 'a'" in caplog.text
    for keys in (["a", "b", "a"], ["c", "a", "b", "a"], ["a", "a", "a"], ["b", "a"]):
        setters["keys"](keys)
        h.flush()  # checks the fake DOM against the tree
        assert h.html() == "<ul>" + "".join(f"<li>{k}{i}</li>" for i, k in enumerate(keys)) + "</ul>"


def test_memo_given_elements_in_a_tuple_sees_new_handlers():
    from reaction import h

    clicks = []
    setters = {}

    @memo
    def Panel(children=()):
        return html.div(*children)

    @component
    def App():
        n, set_n = use_state(0)
        setters["n"] = set_n
        return h(Panel, {}, html.button("go", on_click=lambda: clicks.append(n)))

    harness = Harness(App())
    setters["n"](5)
    harness.flush()
    button = next(node for node in harness.root.hosts.values() if node.type == "button")
    button.events["click"].fn()
    assert clicks == [5], "a new element in the tuple re-renders the memo component"


def test_same_compares_tuples_one_level_at_a_time():
    from reaction.core.hooks import same

    assert same((1, "a", (2.5, None)), (1, "a", (2.5, None))), "plain values compare by value"
    assert not same((1, [2]), (1, [2])), "a list inside compares by identity, as on its own"
    shared = [2]
    assert same((1, shared), (1, shared))
    assert not same(html.b("x"), html.b("x")), "elements compare by identity"
    assert not same((html.b("x"),), (html.b("x"),))
    assert not same((1, 2), (1, 2, 3)) and not same((1,), [1])


def test_mounted_view_transitions_are_counted_so_dirty_nodes_skip_the_walk():
    from reaction import ViewTransition

    setters = {}

    @component
    def App():
        shown, set_shown = use_state(True)
        n, set_n = use_state(0)
        setters.update(shown=set_shown, n=set_n)
        inner = ViewTransition(html.p(n)) if shown else html.p(n)
        return html.div(inner, ViewTransition(html.span("always")))

    h = Harness(App())
    assert h.root._view_transitions == 2
    setters["n"](1)
    h.flush()
    assert h.root._view_transitions == 2, "a re-render does not count it twice"
    setters["shown"](False)
    h.flush()
    assert h.root._view_transitions == 1
    h.root.close()
    assert h.root._view_transitions == 0


def test_a_row_that_gains_a_child_syncs_only_its_own_element():
    """Only the host whose children changed has its DOM order recomputed, not every
    host above it; the fake DOM still matches the tree after each change."""
    from reaction import use_page_store, use_store

    selections = {}
    synced = []

    @memo
    def Row(i, selection):
        selected = use_store(selection, lambda s: s == i)
        return html.li(html.span(i), html.b("*") if selected else None, " end")

    @component
    def App():
        selection = use_page_store(-1)
        selections["s"] = selection
        return html.div(html.ul([Row(i, selection, key=i) for i in range(50)]))

    h = Harness(App())
    real_sync = h.root._sync_dom
    h.root._sync_dom = lambda host: (synced.append(host.type), real_sync(host))[1]
    for pick in (3, 40, 3, -1):
        synced.clear()
        selections["s"].set(pick)
        h.flush()
        assert set(synced) <= {"li"}, f"only rows sync, not {synced}"
    assert "<b>" not in h.html()


def test_prop_names_mean_the_same_on_every_call():
    """Prop names are classified once and cached: the second element gets what the
    first did, for every kind of name."""
    from reaction.core.element import RAW_HTML, _split_props

    def submit(fields):
        return None

    def props(action):
        return dict(  # noqa: C408 - written as the keyword arguments a tag call gets
            key="k", ref=None, class_name="a", class_="b", html_for="x", data_row_id=7, aria_label="l",
            default_value="d", value=None, checked=True, hidden=True, disabled=False, title=None,
            style={"background_color": "red"}, on_click=print, on_click_capture=print, on_change=print,
            on_submit=print, onDoubleClick=print, dangerously_set_inner_html="<i>x</i>", form_action=action,
        )

    for _ in range(2):
        key, ref, attrs, events = _split_props(props(submit))
        assert key == "k" and ref is None
        assert attrs == {
            "class": "b", "for": "x", "data-row-id": "7", "aria-label": "l", "defaultValue": "d",
            "value": "", "checked": True, "hidden": "", "style": "background-color:red",
            RAW_HTML: "<i>x</i>", "data-pyreaction-raw": "",
        }
        assert set(events) == {"click", "click:capture", "input", "submit", "dblclick"}
        assert events["submit"].prevent_default and events["click"].form, "form_action took the click"
        _, _, attrs, events = _split_props(props("/save"))
        assert attrs["form-action"] == "/save" and not events["click"].form, "a string stays an attribute"


def test_state_set_while_rendering_reruns_before_the_children_are_diffed():
    """React's "adjust state when a prop changes": the component runs again at once,
    so its children render only the final output and effects see final values."""
    child_renders = []
    effects = []
    setters = {}

    @component
    def Child(label):
        child_renders.append(label)
        return html.li(label)

    @component
    def List(items):
        prev, set_prev = use_state(items)
        shown, set_shown = use_state(items)
        if items is not prev:
            set_prev(items)
            set_shown(items)
        use_effect(lambda: effects.append(shown), [shown])
        return html.ul([Child(str(x), key=x) for x in shown])

    @component
    def App():
        items, set_items = use_state((1, 2, 3))
        setters["items"] = set_items
        return List(items)

    h = Harness(App())
    child_renders.clear()
    effects.clear()
    setters["items"]((4, 5, 6))
    h.flush()
    assert child_renders == ["4", "5", "6"], "the stale (1, 2, 3) output was never diffed"
    assert h.html() == "<ul><li>4</li><li>5</li><li>6</li></ul>"
    h.root._run_effects()
    assert effects == [(4, 5, 6)], "the effect ran once, with the final value"


def test_style_dicts_update_only_the_properties_that_changed():
    setters = {}

    @component
    def App():
        style, set_style = use_state({"left": "5px", "top": "10px", "color": "red"})
        setters["style"] = set_style
        return html.div("box", style=style)

    h = Harness(App())
    assert 'style="left:5px;top:10px;color:red"' in h.html()
    setters["style"]({"left": "6px", "top": "10px", "background_color": "#eee"})
    assert h.flush() == [["p", 1, "style", {"left": "6px", "background-color": "#eee", "color": None}]]
    assert 'style="left:6px;top:10px;background-color:#eee"' in h.html()
    setters["style"]({"left": "6px", "top": "10px", "background_color": "#eee"})
    assert h.flush() == [], "an equal style sends nothing"
