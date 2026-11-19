"""The one-loop HTML renderer must write exactly what the general one does: the
browser adopts the page by counting its nodes, so a byte of difference matters."""

import random

import pytest

from reaction import Root, Suspense, component, fragment, html, js_component, portal
from reaction.core import ssr
from reaction.core.ssr import _children_fast, _children_general

TEXTS = ["", "plain", "a & b", "<tag>", "x > y", '"quoted"', "it's", "&amp;", "  ", "Ünïcödé"]
ATTRS = [
    {}, {"class_name": "row"}, {"id": "a&b"}, {"title": '"q" <x>'}, {"data_n": 7}, {"hidden": True},
    {"disabled": False}, {"style": {"color": "red", "left": 3}}, {"href": "/a?b=1&c='2'"},
    {"title": 'say "hi"'}, {"alt": "it's"}, {"data_x": "a>b"},
]
TAGS = ["div", "span", "p", "td", "li", "b", "section", "br", "img", "input", "hr"]
Widget = js_component("x-widget", "/w.js")


@component
def Wrap(*children):
    return list(children)


def tree(rng: random.Random, depth: int):
    if depth == 0 or rng.random() < 0.25:
        return rng.choice(TEXTS) if rng.random() < 0.7 else rng.randint(-5, 50)
    kind = rng.random()
    children = [tree(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    if kind < 0.08:
        return Wrap(*children)
    if kind < 0.12:
        return fragment(*children)
    if kind < 0.15:
        return Suspense(html.div(*children), fallback=html.p("loading"))
    if kind < 0.17:
        return html.textarea(rng.choice(TEXTS), name="t")
    if kind < 0.19:
        return html.select(html.option("a", value="a"), html.option("b", value="b"), value="b")
    if kind < 0.21:
        return html.input(value=rng.choice(TEXTS), checked=rng.random() < 0.5, type="checkbox")
    if kind < 0.23:
        return html.script("if (a < b && c) {}")
    if kind < 0.25:
        return html.div(dangerously_set_inner_html="<b>raw</b>")
    if kind < 0.27:
        return html.svg(html.circle(r=3), html.text("t & u"))
    if kind < 0.29:
        return Widget(*children, fallback="<i>wait</i>")
    if kind < 0.31:
        return portal(html.div(*children))
    tag = rng.choice(TAGS)
    props = dict(rng.choice(ATTRS))
    if tag in ("br", "img", "input", "hr"):
        return html(tag, **props)
    if rng.random() < 0.3:  # a lone text child: the element holds it itself
        return html(tag, rng.choice(TEXTS), **props)
    return html(tag, *children, **props)


def general_everywhere(children, out, raw, boundaries=None, static=False):
    """The old renderer all the way down: _host renders an element's children
    through ssr._children, which would otherwise go back to the fast loop."""
    _children_general(children, out, raw, boundaries, static)


@pytest.mark.parametrize("seed", range(300))
def test_fast_renderer_writes_the_same_html(seed, monkeypatch):
    rng = random.Random(seed)

    @component
    def App():
        return html.main(*[tree(rng, 4) for _ in range(rng.randint(1, 5))])

    root = Root(App())
    for marked in (False, True):  # without and with Suspense boundary markers
        slow: list[str] = []
        fast: list[str] = []
        with monkeypatch.context() as m:
            m.setattr(ssr, "_children", general_everywhere)
            _children_general(root.container.children, slow, raw=False, boundaries={} if marked else None)
        _children_fast(root.container.children, fast, {} if marked else None)
        assert "".join(fast) == "".join(slow)
    root.close()


@pytest.mark.parametrize("seed", range(300))
def test_fast_adopt_message_is_the_general_one(seed):
    """The compact hydrate message's one-loop path (pages without portals) gives the
    message the general, portal-aware walk gives."""
    rng = random.Random(seed)

    @component
    def App():
        return html.main(*[tree(rng, 4) for _ in range(rng.randint(1, 5))])

    root = Root(App())
    if root.portals:
        root.close()
        return  # the fast path is only for pages without portals
    fast = root._adopt_segments()
    root.portals = 1  # none in the tree: the general walk, which looks for them
    general = root._adopt_segments()
    root.portals = 0
    assert fast == general
    root.close()
