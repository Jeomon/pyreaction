"""to_vdom: the rendered tree as plain dicts."""

import json

from reaction import component, event, html, js, js_component, portal, to_vdom, use_state
from reaction.testing import render


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
    @component
    def Counter():
        n, set_n = use_state(0)
        return html.button(f"n={n}", on_click=lambda: set_n(n + 1))

    with render(Counter()) as page:
        assert to_vdom(page.root)["children"][0]["children"] == ["n=0"]
        page.click("button")
        page.click("button")
        assert to_vdom(page.root)["children"][0]["children"] == ["n=2"]
