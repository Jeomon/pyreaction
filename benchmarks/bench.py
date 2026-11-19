"""Where a render's time goes: component calls, reconciling, and encoding the ops.

    uv run python benchmarks/bench.py

The numbers decide whether any part is worth moving to Rust.
"""

import random
import time

from reaction import Root, component, html, memo, use_page_store, use_state, use_store
from reaction.server.app import _dumps  # what the server sends with: orjson when installed

ROWS = 10_000
setters = {}


@memo
def Row(i, label, selected):
    return html.tr(html.td(i), html.td(label), class_name="selected" if selected else None)


@component
def Table():
    rows, set_rows = use_state([(i, f"row {i}") for i in range(ROWS)])
    selected, set_selected = use_state(-1)
    setters.update(rows=set_rows, selected=set_selected)
    return html.table(html.tbody([Row(i, label, i == selected, key=i) for i, label in rows]))


@memo
def StoreRow(i, label, selection):
    selected = use_store(selection, lambda s: s == i)  # re-renders only when this row's answer changes
    return html.tr(html.td(i), html.td(label), class_name="selected" if selected else None)


@component
def StoreTable():
    rows, _ = use_state([(i, f"row {i}") for i in range(ROWS)])
    selection = use_page_store(-1)
    setters["selection"] = selection
    return html.table(html.tbody([StoreRow(i, label, selection, key=i) for i, label in rows]))


def timed(label, fn, repeat=5):
    best = float("inf")
    out = None
    for _ in range(repeat):
        start = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - start)
    print(f"{label:<40} {best * 1000:8.1f} ms")
    return out


def main():
    root = timed(f"mount {ROWS} rows", lambda: Root(Table()), repeat=3)
    ops = root.ops
    encoder = "orjson" if "orjson" in _dumps.__code__.co_names else "json"
    timed(f"encode {len(ops)} mount ops ({encoder})", lambda: _dumps({"ops": ops}))
    root.ops = []
    sent = []
    root._sink = sent.append  # stands in for the WebSocket

    def update(make):
        def run():
            sent.clear()
            make()
            root.flush()
            return sum(len(m["ops"]) for m in sent)

        return run

    n = timed("select one row (memo skips the rest)", update(lambda: setters["selected"](random.randrange(ROWS))))
    print(f"{'':<40} {n} ops")

    def every_tenth():
        setters["rows"](lambda rows: [(i, label + " !") if i % 10 == 0 else (i, label) for i, label in rows])

    n = timed("relabel every 10th row", update(every_tenth))
    print(f"{'':<40} {n} ops")

    def swap():
        def f(rows):
            rows = list(rows)
            rows[1], rows[-2] = rows[-2], rows[1]
            return rows

        setters["rows"](f)

    n = timed("swap two rows", update(swap))
    print(f"{'':<40} {n} ops")

    timed("shuffle all rows", update(lambda: setters["rows"](lambda r: random.sample(r, len(r)))), repeat=3)

    root = Root(StoreTable())
    root.ops = []
    root._sink = sent.append
    n = timed("select one row (rows read a page store)", update(lambda: setters["selection"].set(random.randrange(ROWS))))
    print(f"{'':<40} {n} ops")


if __name__ == "__main__":
    main()
