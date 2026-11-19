# Elements and events

Describing the page, and reacting to what the user does.

## Elements

```python
html.div(
    html.h1("Title"),
    html.input(value=text, on_change=lambda e: set_text(e.value)),
    [Row(item, key=item.id) for item in items],   # lists need keys
    "plain text", 42, None,                       # None/False render nothing
    class_name="card", style={"margin_top": "1rem"}, data_id=7, disabled=False,
)
```

- `class_name` becomes `class`, `html_for` becomes `for`, and underscores become
  dashes (`aria_label`, `data_id`). `style` takes a dict with snake_case or
  camelCase keys.
- `True` sets an attribute; `False` or `None` leaves it out.
- `value` and `checked` are live DOM properties, so inputs can be controlled.
  `default_value` and `default_checked` make an uncontrolled input that only
  starts with that value (read it on submit from `e.form`, or through a `ref`).
- `fragment(*children)` groups children without a wrapper; a component may also
  return a list. `h("div", {"class_name": "x"}, child)` is the call form, for tags
  chosen at runtime.
- SVG and MathML work like any other tag (`html.svg(html.circle(r=5))`). Any other
  name makes a custom element: `html.my_widget` is `<my-widget>`.

**Raw HTML**, for Markdown or CMS content:

```python
html.article(dangerously_set_inner_html=markdown_to_html(text))   # inserted as is
html.article(parse_html(markdown_to_html(text)))                  # parsed into elements
```

`dangerously_set_inner_html` inserts the string as markup, so use it only for
HTML you trust. `parse_html` turns HTML into ordinary elements and removes what
could run code (`<script>`, `<iframe>`, `on*` attributes, `javascript:` links).

`image("png", data, alt=...)` shows image bytes held in memory, such as a
matplotlib chart, as an `<img>` with a `data:` URL (`"svg"` takes SVG text).
Keep such images small and serve large ones as files.

## Events

`on_click`, `on_change` (fires on every edit, as in React), `on_submit`,
`on_key_down` and so on. Pass the function itself (`on_click=vote`), not its
result. A handler takes no argument or one `Event`; parameters with defaults are
left alone, so `on_click=lambda r=r: vote(r.id)` works in a loop.

The event has `e.value`, `e.checked`, `e.key`, `e.target.name`,
`e.target.dataset` and `e.target.selectionStart`. On a submit, `e.form` holds the
fields: `e.form["email"]`, and `e.form.getlist("tags")` for checkbox groups and
multi-selects.

Handlers may be `async`. Plain handlers run in a worker thread, one at a time in
arrival order, so a slow one (a database call) does not freeze other pages.

As in React, one event that bubbles through several elements with handlers (a
button inside a clickable card) is one message: the handlers run in order, then
the page renders once. Each sees the state from before the event, so build on an
earlier handler's change with the function form, `set_n(lambda n: n + 1)`. A
handler with `debounce`, `throttle` or a form action sends its own message.

Submit events call `preventDefault` by default. Other options:

```python
from reaction import event, js

html.input(on_input=event(search, debounce=300))          # skipped events never reach the server
html.a("Open", href="#", on_click=event(open_, prevent_default=True))
html.textarea(on_key_down=event(send, keys=["Enter"], prevent_default=True))
html.input(on_focus=js("element.select()"))               # runs in the browser, no round trip
```

`keys` limits a keyboard event to those `KeyboardEvent.key` values; other keys
stay in the browser and keep their default, so the textarea above still takes
typing. `event()` also takes `throttle` (at most one event per period, always
including the last of a burst), `stop_propagation` and
`stop_immediate_propagation`. `js()` code sees `event` and `element`. A `_capture`
suffix (`on_click_capture`) listens on the way down, like React's `onClickCapture`.

For anything an event did not send, ask the browser while the event is recent
(the last 50 events, for up to a minute):

```python
async def dropped(e):
    files = await e.read("dataTransfer.files")            # [{"name", "type", "size", ...}]
    text = await e.call("dataTransfer.getData", "text/plain")
    top = await e.target.read("scrollTop")

def clicked(e):                                           # a plain handler waits with .result()
    shift = e.read("shiftKey").result()
```

Values come back as JSON (a file as its name, type and size; a list as its
items). The browser has finished with the event by then, so `preventDefault` and
the like are set with `event(...)` options, not called afterwards.

## File uploads

```python
def save(e):
    for f in e.files:          # UploadedFile: .name, .type, .size, .data, .text(), .file, .save(path), .error
        if not f.error:
            f.save(Path("uploads") / f.name)

html.input(type="file", multiple=True, on_change=save)
```

Files travel over the page's WebSocket in 256 KiB chunks, with no extra endpoint.
The server keeps a file in memory up to 1 MiB and on disk past that. A file over
`max_upload_size` (10 MiB by default) is refused in the browser and arrives with
`.error` set and no data.
