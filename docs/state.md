# Shared state

State shared between pages, between processes, and within one page.

```python
online = Store(0)          # one per process, shared by every open page

@component
def Badge():
    return html.span(f"{use_store(online)} online")

online.update(lambda n: n + 1)   # from any thread: every Badge re-renders
```

To share a store between worker processes or machines, name it and give it a
backend:

```python
votes = Store({"yes": 0, "no": 0}, name="votes", backend=RedisBackend("redis://localhost:6379/0"))
```

A write in any process re-renders the readers in all of them. The value must be
JSON, and `update` is atomic across processes. Valkey works too, and any other
broker can implement the `StoreBackend` protocol (`load`, `write`, `listen`).

**Stores for one page, read with selectors.** `use_page_store(initial)` makes a
store that belongs to one page. Components read it with `use_store(store,
selector)` and re-render only when their selected part changes, so moving a
selection through 10,000 rows re-renders two of them (about 1 ms instead of 15):

```python
@component
def Table(rows):
    selection = use_page_store(None)
    return html.tbody([Row(row, selection, key=row.id) for row in rows])

@memo
def Row(row, selection):
    selected = use_store(selection, lambda s: s == row.id)
    return html.tr(html.td(row.name), class_name="on" if selected else None,
                   on_click=lambda: selection.set(row.id))
```
