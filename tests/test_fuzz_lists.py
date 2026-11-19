"""Random list edits (insert, remove, swap, shuffle, relabel, drop a prefix) on keyed
lists of memo rows, components with several DOM nodes, elements and fragments, some
under a component with no element of its own. After each flush the fake DOM must equal
the tree, and a fresh snapshot must rebuild the same page."""
import random

from reaction import component, fragment, html, memo, use_state
from .fakedom import FakeDom
from .test_vdom import Harness

setters = {}


@memo
def Row(k, label):
    return html.li(label, class_name=f"r{k}")


@component
def Pair(k):  # a component with two DOM nodes
    return [html.b(k), html.i(k)]


def item(k, label):
    kind = k % 4
    if kind == 0:
        return Row(k, label, key=k)
    if kind == 1:
        return Pair(k, key=k)
    if kind == 2:
        return html.span(label, key=k)
    return fragment(html.u(k), key=k)


@component
def Group(items, nested):
    # a component whose child list sits under the host of its parent (no host of its own)
    return [item(k, lab) for k, lab in items] if not nested else [[item(k, lab) for k, lab in items], "tail"]


@component
def App():
    items, set_items = use_state([(k, f"x{k}") for k in range(12)])
    other, set_other = use_state([(k, f"y{k}") for k in range(100, 106)])
    setters.update(items=set_items, other=set_other)
    return html.div(
        html.ul([item(k, lab) for k, lab in items]),
        html.ol("head", Group(other, False), "mid", Group(items[:4], True), "end"),
    )


def mutate(rng, items, base):
    items = list(items)
    while len(items) < 8:
        k = base + rng.randrange(1000)
        if all(k != x for x, _ in items):
            items.insert(rng.randint(0, len(items)), (k, f"n{k}"))
    for _ in range(rng.randint(1, 4)):
        op = rng.randrange(6)
        if op == 0 and items:
            del items[rng.randrange(len(items))]
        elif op == 1:
            k = base + rng.randrange(1000)
            if all(k != x for x, _ in items):
                items.insert(rng.randint(0, len(items)), (k, f"n{k}"))
        elif op == 2 and len(items) > 1:
            a, b = rng.randrange(len(items)), rng.randrange(len(items))
            items[a], items[b] = items[b], items[a]
        elif op == 3:
            rng.shuffle(items)
        elif op == 4 and items:
            i = rng.randrange(len(items))
            items[i] = (items[i][0], items[i][1] + "!")
        elif op == 5:
            items = items[rng.randrange(len(items) + 1):]
    return items


def test_fuzz_lists():
    rng = random.Random(1234)
    h = Harness(App())
    for step in range(4000):
        setters["items"](lambda it: mutate(rng, it, 0))
        if step % 3 == 0:
            setters["other"](lambda it: mutate(rng, it, 2000))
        h.flush()  # asserts the fake DOM equals the tree
        if step % 50 == 0:
            fresh = FakeDom()
            fresh.apply(h.root.snapshot())
            assert fresh.html() == h.dom.html()
