# pyreaction

React-style UI components in pure Python. Components run on the server; the
browser gets server-rendered HTML for the first paint, then small DOM patches
over a WebSocket. No JavaScript to write and no build step.

```python
from pyreaction import component, html, run, use_state

@component
def Counter():
    count, set_count = use_state(0)
    return html.button(f"Clicked {count} times", on_click=lambda: set_count(count + 1))

run(Counter)  # http://127.0.0.1:8000
```

## Start a project

```sh
uvx pyreaction init my-app     # creates the project, runs uv sync and git init
cd my-app
uv run pyreaction dev          # http://127.0.0.1:8000, restarts on every save
```

```
my-app/
  pyproject.toml     settings under [tool.pyreaction]
  app.py             the App component
  components/        your components
  public/            served at /static; style.css is linked from the page
```

When you save a `.py` file, `pyreaction dev` restarts the server and open pages
reconnect and redraw by themselves; component state starts over. Changes to
files in `public/` show on the next refresh. `pyreaction run` serves without
reloading (`--host`, `--port`).

```toml
[tool.pyreaction]
app = "app:App"              # module:attribute of the root component
title = "my-app"
static = "public"
stylesheets = ["style.css"]  # files in the static folder to link
head = ""                    # extra raw HTML for <head>
```

`init` takes `--no-install` and `--no-git`.

## Add to an existing project

```sh
uv add pyreaction            # or: uv add "pyreaction[fast]" for orjson encoding
```

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

- `class_name` becomes `class`, `html_for` becomes `for`, and underscores become dashes (`aria_label`, `data_id`).
- `style` takes a dict with snake_case or camelCase keys.
- `True` sets an attribute, while `False` or `None` leaves it out.
- `value` and `checked` are set as live DOM properties, so inputs can be controlled.
- `fragment(*children)` groups children without a wrapper, and a component may also return a list.
- `h("div", {"class_name": "x"}, child)` is the call form, for tags chosen at runtime.

## Events

`on_click`, `on_change` (fires on every edit, as in React), `on_submit`,
`on_key_down` and so on. A handler takes no argument, or one `Event`:
`e.value`, `e.checked`, `e.key`, `e.target.name`, and for a submit `e.form` holds
the form's fields. Handlers may be `async`.

Submit events call `preventDefault` by default. For other options:

```python
from pyreaction import event
html.input(on_input=event(search, debounce=300))          # skipped events never reach the server
html.a("Open", href="#", on_click=event(open_, prevent_default=True))
```

## Hooks

| Hook | |
| --- | --- |
| `use_state(initial)` | `value, set_value`; the setter takes a value or `lambda prev: ...` |
| `use_reducer(reducer, initial)` | `state, dispatch` |
| `use_effect(fn, deps)` | runs after the update reaches the browser; return a cleanup, or make it `async` (cancelled on cleanup) |
| `use_memo(fn, deps)` / `use_callback(fn, deps)` | cached until `deps` change |
| `use_ref(initial)` | `ref.current` survives re-renders; pass it as `ref=` to an element to act on it |
| `create_context(default)` / `use_context(ctx)` | provide with `ctx(*children, value=...)` |
| `use_location()` | the page URL: `.path`, `.search`, `.query` (a dict); re-renders when it changes |
| `use_navigate()` | `navigate("/users/7")` or `navigate(url, replace=True)`, without reloading |
| `use_title(text)` | sets the page title, in the first HTML too |
| `use_request()` | the Starlette `Request`, then `WebSocket`, serving the page: `.headers`, `.cookies`, `.client` |

`@memo` instead of `@component` skips re-rendering while the arguments are unchanged.

Updates made in the same event-loop tick are batched into one render and one
message. Setters are safe to call from other threads.

## Routing

```python
from pyreaction import Link, Router

@component
def App():
    return html.main(
        html.nav(Link("Home", to="/"), Link("Users", to="/users")),
        Router({"/": Home, "/users": Users, "/users/{id}": User, "/files/{rest:path}": File},
               fallback=NotFound),
    )
```

`Router` renders the first matching route, passing the `{params}` as keyword
arguments (`User(id="7")`); `{name:path}` matches across slashes. `Link` changes
the URL without reloading the page, and Ctrl/Cmd-click still opens a new tab.
Back and Forward work, and every URL also renders on the server when loaded
directly.

## Acting on elements

```python
box = use_ref()
html.input(ref=box)
html.button("Edit", on_click=lambda: box.current.focus())
```

`ref.current` has `focus()`, `blur()`, `click()`, `select()`,
`scroll_into_view(behavior=, block=)`, and `call(method, *args)` for any other
element method. It is None once the element is removed.

## Errors

```python
ErrorBoundary(
    Router(routes),
    fallback=lambda error, reset: html.p(f"Failed: {error}", html.button("Retry", on_click=reset)),
    reset_keys=[use_location().path],   # leaving the broken page clears the error
)
```

An error while rendering a child shows the nearest boundary's fallback, in the
server's HTML as well. Without a boundary, the component keeps its last good
output, or renders nothing the first time. Errors in handlers and effects are
logged and not caught by boundaries, as in React.

With `debug=True`, which `pyreaction dev` sets, every error not caught by a
boundary also shows in the browser with its traceback.

## Serving

`run(App, host=, port=, ...)` starts uvicorn; other options go to `create_app`.
`create_app(App, ...)` returns the Starlette app, to mount in FastAPI or any ASGI
server. It takes `title`, `head`, `lang`, `debug`, and `static_dir`, `static_path`
and `stylesheets` for static files:

```python
api.mount("/ui", create_app(App, static_dir="public", stylesheets=["app.css"]))
```

Each page view is a live session on the server. If the connection drops, the
page dims, reconnects and starts a fresh session at the same URL; component
state starts over, which is also what a deploy or restart does.

WebSockets are accepted only from the page's own origin, so other websites
cannot drive a visitor's session. Behind a proxy that rewrites the `Host`
header, list the public origin: `create_app(App, allowed_origins=["https://app.example.com"])`
(or `allowed_origins = [...]` under `[tool.pyreaction]`).

Any number of worker processes can serve the app: the page load and its
WebSocket need not reach the same one.

## How it works

1. A GET renders the component tree at the requested URL and returns its HTML, keeping nothing in memory.
2. `client.js` (about 330 lines, no dependencies) opens a WebSocket for the same URL. The server renders again and sends the tree in document order; when it matches the page, the browser adopts the existing nodes in place, and otherwise rebuilds them (for a render that reads the clock or a random number). This is how React hydrates server HTML.
3. A state change re-renders only the dirty components, shallowest first. The reconciler matches children by key and sends operations such as "set text of node 12" or "move node 40 before node 9". Moves are kept to the minimum using a longest-increasing-subsequence pass.
4. While you type, the browser ignores server values older than your latest keystroke, so a slow round trip never undoes typing.
5. If the browser falls behind, pending updates merge into one message; past 100,000 pending operations the connection is closed and the browser starts over.

The operation format is documented at the top of `src/pyreaction/core/reconciler.py`.

## Layout

```
src/pyreaction/
  core/     elements, hooks, the reconciler and HTML rendering (no web dependencies)
  server/   the Starlette app and run()
  client/   client.js, the index.html page template and its style.css
  cli/      the pyreaction command: scaffold.py (init), serve.py (dev, run)
            and templates/, what init copies
```

`index.html` uses `$name` placeholders (`$title`, `$head`, `$body`, ...). The
contents of `style.css` are inlined into the page so they cost no extra request.

## Development

```sh
uv sync
uv run pytest
uv run python examples/todo.py
uv run python benchmarks/bench.py
```
