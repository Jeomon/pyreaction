# Built-in components and the DOM

Refs, portals, long lists, error boundaries and JavaScript widgets.

## Acting on elements

```python
box = use_ref()
html.input(ref=box)
html.button("Edit", on_click=lambda: box.current.focus())

async def measure():
    rect = await box.current.call("getBoundingClientRect")     # {"width": ..., ...}
    top = await box.current.get("scrollTop")
```

`ref.current` has `focus()`, `blur()`, `click()`, `select()`,
`scroll_into_view(behavior=, block=)`, `call(method, *args)`, and
`get(name)` / `set(name, value)`. Each returns the browser's answer: `await` it in
an `async` handler, or call `.result()` in a plain one.

## Portals and hidden content

```python
portal(html.div("Saved!", class_name="toast"), target="body")   # rendered at the end of <body>
Activity(Settings(), mode="hidden" if tab != "settings" else "visible")
```

`portal` renders children elsewhere in the page (modals and toasts that must
escape `overflow: hidden`), with state, context and events working as if they
were in place, in the server's HTML too. `Activity` hides children without
unmounting them, so their state survives.

## Long lists

```python
VirtualList(users, lambda user, i: UserRow(user), row_height=36, height=600,
            key=lambda user: user.id, class_name="users")
```

`VirtualList` renders only the rows in view (plus `overscan` rows on each side), so
a list of 100,000 items costs what 30 do, in the first HTML and in every update.
Rows have a fixed `row_height`; the list is a scrolling box `height` pixels tall,
and other props go on it. Rows scrolled out are unmounted, so keep their state
above the list.

## Errors

```python
ErrorBoundary(
    Router(routes),
    fallback=lambda error, reset: html.p(f"Failed: {error}", html.button("Retry", on_click=reset)),
    reset_keys=[use_location().path],   # leaving the broken page clears the error
)
```

An error while rendering shows the nearest boundary's fallback, in the server's
HTML as well. Without a boundary, the component keeps its last good output.
Errors in handlers and effects are logged and not caught by boundaries, as in
React. `create_app(on_error=fn)` calls `fn(exc, where, connection)` for every
uncaught error, for reporting to a service such as Sentry.

## JavaScript components

For what Python cannot draw in the browser (charts, maps, editors), wrap a
JavaScript module:

```python
from reaction import js_component

Chart = js_component("/static/chart.js", "mount")

Chart(values=values, color="teal", on_pick=lambda e: set_picked(e.index),
      fallback=html.p("Loading chart..."))
```

```js
// public/chart.js
export function mount(element, props, emit, slot) {
  // draw into element; emit("pick", {index: 2}) calls on_pick in Python
  return { update(props) { /* redraw */ }, unmount() { /* clean up */ } };
}
```

Props must be JSON values and are passed whole to `update` when any changes.
`on_<name>` props are Python callbacks that `emit("<name>", data)` calls.
`fallback` shows until the module loads, in the server's HTML too. Positional
children are rendered by pyreaction into `slot`, which the module places; they
stay live Python components.

React components wrap the same way: `examples/static/react_slider.js` renders one
with `createRoot` inside `mount`. A small module can also be written inline with
`js_component_from_string(code)`; the browser imports it from a `data:` URL, so it
can import other modules by full URL only.
