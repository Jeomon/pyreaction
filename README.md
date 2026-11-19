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
reloading (`--host`, `--port`). `dev` also turns on debug mode (errors shown in
the page, warnings for lists without keys).

```toml
[tool.pyreaction]
app = "app:App"              # module:attribute of the root component
title = "my-app"
static = "public"
stylesheets = ["style.css"]  # files in the static folder to link
head = ""                    # extra raw HTML for <head>
max_upload_size = 10485760   # bytes per uploaded file (default 10 MiB)
allowed_origins = []         # extra origins allowed to open the WebSocket
stream = true                # send Suspense content as it loads (see Data)
resume_timeout = 30          # seconds a dropped page's state is kept for a reconnect
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
- SVG and MathML work like any other tag (`html.svg(html.circle(r=5))`); the standard tags are typed, so editors autocomplete them. Any other name makes a custom element: `html.my_widget` is `<my-widget>`.

### Raw HTML

```python
html.article(dangerously_set_inner_html=markdown_to_html(text))   # inserted as is
html.article(parse_html(markdown_to_html(text)))                  # parsed into elements
```

`dangerously_set_inner_html` inserts the string as markup: only use it for HTML
you trust, since it can carry scripts. `parse_html` turns HTML into ordinary
elements that later renders patch like any others, and by default removes what
could run code (`<script>`, `<iframe>`, `on*` attributes, `javascript:` links).

## Events

`on_click`, `on_change` (fires on every edit, as in React), `on_submit`,
`on_key_down` and so on. A handler takes no argument, or one `Event`:
`e.value`, `e.checked`, `e.key`, `e.target.name`, `e.target.dataset`,
`e.target.selectionStart`. On a submit, `e.form` holds the fields:
`e.form["email"]`, and `e.form.getlist("tags")` for checkbox groups and
multi-selects.

Handlers may be `async`. Plain handlers run in a worker thread, one at a time
in arrival order, so a slow one (a database call) does not freeze other pages
(`create_app(threaded=False)` runs them on the event loop instead).

Submit events call `preventDefault` by default. For other options:

```python
from pyreaction import event, js
html.input(on_input=event(search, debounce=300))          # skipped events never reach the server
html.a("Open", href="#", on_click=event(open_, prevent_default=True))
html.textarea(on_key_down=event(send, keys=["Enter"], prevent_default=True))
html.input(on_focus=js("element.select()"))               # runs in the browser, no round trip
```

`keys` limits a keyboard event to those `KeyboardEvent.key` values. Other keys
stay in the browser: they are not sent, and their default is not prevented, so
the example above blocks only Enter and the textarea still takes other typing.
`event()` also takes `throttle` and `stop_propagation` /
`stop_immediate_propagation`. `js()` code sees `event` and `element`; it is sent
to the page as written, so never build it from user input.

### File uploads

```python
def save(e):
    for f in e.files:          # UploadedFile: .name, .type, .size, .data, .text(), .file, .save(path), .error
        if not f.error:
            f.save(Path("uploads") / f.name)

html.input(type="file", multiple=True, on_change=save)
```

Files travel over the page's WebSocket in 256 KiB chunks ahead of the event, so
no extra endpoint or dependency is needed. The server keeps a file in memory up
to 1 MiB and on disk past that; `.save()` and `.file` copy it without loading it
whole. A file over `max_upload_size` (10 MiB by default) is refused in the
browser and arrives with `.error` set and no data.

## Hooks

| Hook | |
| --- | --- |
| `use_state(initial)` | `value, set_value`; the setter takes a value or `lambda prev: ...` |
| `use_reducer(reducer, initial)` | `state, dispatch` |
| `use_effect(fn, deps)` | runs after the update reaches the browser; return a cleanup, or make it `async` (cancelled on cleanup). Plain effects run in a worker thread |
| `use_memo(fn, deps)` / `use_callback(fn, deps)` | cached until `deps` change |
| `use_ref(initial)` | `ref.current` survives re-renders; pass it as `ref=` to an element to act on it |
| `create_context(default)` / `use_context(ctx)` | provide with `ctx(*children, value=...)` |
| `use_async(fn, deps)` / `use_data(fn, deps)` | load data without blocking; see [Data](#data) |
| `use_transition()` / `use_action_state(action, initial)` | pending state for slow work; see [Pending state](#pending-state-and-forms) |
| `use_location()` | the page URL: `.path`, `.search`, `.query` (a dict); re-renders when it changes |
| `use_navigate()` | `navigate("/users/7")`, `navigate(url, replace=True)`, or `navigate(url, reload=True)` for a full load |
| `use_title(text)` | sets the page title, in the first HTML too |
| `use_status(code)` | the HTTP status of the first response, e.g. 404 or 410 |
| `use_request()` | the Starlette `Request`, then `WebSocket`, serving the page: `.headers`, `.cookies`, `.client` |
| `use_id()` | an id that is the same in the server's HTML and the live page, for `label`/`aria-*` pairs |
| `use_store(store)` / `use_sync_external_store(subscribe, get)` | state shared between pages; see [Shared state](#shared-state) |
| `use_effect_event(fn)` | a stable function that always calls the latest `fn` |
| `use_imperative_handle(ref, create, deps)` | expose an API to a parent holding `ref` |
| `use_debug_value(value)` | logs the value when it changes, in debug mode |

`@memo` instead of `@component` skips re-rendering while the arguments are unchanged.

Updates made in the same event-loop tick are batched into one render and one
message. Setters are safe to call from other threads.

## Data

```python
@component
def Team(team_id):
    members = use_async(lambda: db.members(team_id), [team_id])   # sync: runs in a thread
    if members.loading:
        return html.p("Loading...")
    if members.error:
        return html.p(f"Failed: {members.error}", html.button("Retry", on_click=members.reload))
    return html.ul([html.li(m.name, key=m.id) for m in members.value])
```

`use_async(fn, deps)` calls `fn` (a plain function in a worker thread, or an
`async def`) again whenever `deps` change, and returns `.loading`, `.value`,
`.error` and `.reload()`. `use_data(fn, deps)` returns the value itself; while it
loads, the nearest `Suspense(*children, fallback=...)` shows its fallback, and an
error goes to the nearest `ErrorBoundary`.

The server waits for this data (up to `data_timeout`, 5 seconds by default)
before sending a page's first HTML, so the page arrives with the data in it,
for search engines and without a loading flash.

Data inside a `Suspense` boundary does not hold the page back: the HTML goes out
at once with the fallback, and the boundary's content follows in the same
response when its data arrives, swapped in by a small inline script before the
page goes live. `create_app(stream=False)` waits for all of it instead.

## Pending state and forms

Every event is a round trip, so slow actions should say they are working:

```python
@component
def SaveButton():
    return html.button("Saving..." if use_form_status().pending else "Save", type="submit")

@component
def Contact():
    def send(fields):                 # FormFields; plain functions run in a thread
        mail.send(fields["email"], fields["message"])
    return Form(html.input(name="email"), html.textarea(name="message"), SaveButton(), action=send)
```

`Form(*children, action=...)` runs `action(fields)` in the background, gives
components inside it `use_form_status()` (`.pending`, `.data`), and clears the
fields on success (`reset=False` keeps them).

`use_transition()` returns `pending, start`: `start(fn, *args)` runs `fn` in the
background with `pending` True meanwhile. `use_action_state(action, initial)`
returns `state, run, pending`, where `run(payload)` sets the state to
`action(state, payload)`; pass `run` to `on_submit` to get the form's fields.

## Routing

```python
from pyreaction import Link, Navigate, Router

@component
def App():
    return html.main(
        html.nav(Link("Home", to="/"), Link("Users", to="/users")),
        Router({"/": Home, "/users": Users, "/users/{id}": User, "/files/{rest:path}": File,
                "/old": lambda: Navigate("/users")},
               fallback=NotFound),
    )
```

`Router` renders the first matching route, passing the `{params}` as keyword
arguments (`User(id="7")`); `{name:path}` matches across slashes. When nothing
matches it renders `fallback` and the page's first response has status 404.
`Navigate(to)` redirects: an HTTP 307 on first load, a URL change once live.
`Link` changes the URL without reloading the page, and Ctrl/Cmd-click still
opens a new tab. Back and Forward work, and every URL also renders on the
server when loaded directly.

## The page's head

```python
Head(html.meta(name="description", content=post.summary), html.link(rel="canonical", href=url))
```

`Head` puts tags into `<head>` from anywhere in the tree, in the first HTML too,
and removes them when it unmounts. `use_title` sets the title.

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
`scroll_into_view(behavior=, block=)`, `call(method, *args)` for any other
element method, and `get(name)` / `set(name, value)` for properties. Each
returns the browser's answer: `await` it in an `async` handler, or call
`.result()` in a plain one. It is None once the element is removed.

## Portals and hidden content

```python
portal(html.div("Saved!", class_name="toast"), target="body")   # rendered at the end of <body>
Activity(Settings(), mode="hidden" if tab != "settings" else "visible")
```

`portal` renders children elsewhere in the page (for modals and toasts that must
escape `overflow: hidden`), with state, context and events working as if they
were in place. They are in the server's HTML too, at the end of `<body>` (moved
into their `target` by a one-line script), and the live page adopts them.
`Activity` hides children without unmounting them, so their state survives.

## Shared state

```python
online = Store(0)          # one per process, shared by every open page

@component
def Badge():
    return html.span(f"{use_store(online)} online")

online.update(lambda n: n + 1)   # from any thread: every Badge re-renders
```

A plain `Store` holds a value in one process. To share it between worker
processes or machines, give it a name and a backend:

```python
votes = Store({"yes": 0, "no": 0}, name="votes", backend=RedisBackend("redis://localhost:6379/0"))
```

A write in any process re-renders the readers in all of them. The value must be
JSON, `update` is atomic across processes (its function may run again if another
write got in first), and a process that starts later loads the current value.
It needs `pip install "pyreaction[redis]"`; Valkey works too, and any other
broker can implement the `StoreBackend` protocol (`load`, `write`, `listen`).
`use_sync_external_store(subscribe, get_snapshot)` connects any other source.

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

`create_app(on_error=fn)` calls `fn(exc, where, connection)` for every error not
caught by a boundary, to send them to a monitoring service. With `debug=True`,
which `pyreaction dev` sets, they also show in the browser with their traceback,
as does a warning when the server's HTML did not match the live render.

## JavaScript components

For what Python cannot draw in the browser (charts, maps, editors), wrap a
JavaScript module:

```python
from pyreaction import js_component

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

Props must be JSON values and are passed whole to `update` when any changes;
`on_<name>` props are Python callbacks that `emit("<name>", data)` calls. The
module URL can be a file in your static folder or a CDN address. `fallback`
shows until the module has loaded, in the server's HTML too. Positional
children are rendered by pyreaction into `slot`, which the module places where
they belong; they stay live Python components. `ref=` works too.

React components wrap the same way: `examples/static/react_slider.js` renders a
React component with `createRoot` inside `mount`, passes props through in
`update`, and calls `root.unmount()` in `unmount`. See `examples/widgets.py`.

## Testing

```python
from pyreaction.testing import render

def test_todo():
    with render(Todo()) as page:
        page.type("input[name=item]", "milk")
        page.click("button.primary")
        assert [li.text for li in page.find_all("#list li")] == ["milk"]
```

`render` mounts a component on a real event loop with a small fake browser, so
handlers, effects, data loading and forms run exactly as on a live page; each
action waits until the page has settled. `find`/`find_all` take CSS selectors
(tags, `#id`, `.class`, `[attr=value]`, descendants) and `text=`; actions are
`click`, `type`, `check`, `key`, `submit`, `navigate` and `dispatch`. Errors
raised by your components fail the test (`raise_errors=False` collects them in
`page.errors`). `on_call=` answers element calls such as `getBoundingClientRect`.

For a real browser, `serve` runs the app on a free local port in a background
thread; the page marks `<html data-pyreaction-ready>` once it is live:

```python
from pyreaction.testing import serve

def test_counter(page):                      # Playwright's page (pytest-playwright)
    with serve(Counter) as server:           # create_app options go here too
        page.goto(server.url)
        page.wait_for_selector(server.READY)
        page.click("button")
```

`serve(asgi_app, asgi=True)` serves an app you built yourself. Install the
browser driver with `pip install "pyreaction[browser]"`.

## Authentication

A WebSocket cannot set cookies, and JavaScript must not be able to read a
session cookie. So log in and out with ordinary HTTP routes that set an
`HttpOnly` cookie, and read it in components with `use_request().cookies`:

```python
run(App, routes=[Route("/login", login, methods=["POST"]), Route("/logout", logout, methods=["POST"])])
```

The login form is a plain `<form method="post" action="/login">`. After logging
in or out from Python code, `navigate(url, reload=True)` loads the page afresh
so the next connection carries the new cookie. `examples/login.py` is a
complete version.

## Serving

`run(App, host=, port=, ...)` starts uvicorn; other options go to `create_app`.
`create_app(App, ...)` returns the Starlette app, to mount in FastAPI or any ASGI
server:

```python
api.mount("/ui", create_app(App, static_dir="public", stylesheets=["app.css"]))
```

Options: `title`, `head`, `lang`, `static_dir`, `static_path`, `stylesheets`,
`routes` (extra Starlette routes), `data_timeout`, `stream`, `max_upload_size`,
`allowed_origins`, `on_error`, `threaded`, `reconnect_max` (ms),
`resume_timeout` (s) and `debug`.

To put an app inside a page your own server renders (Jinja, Django), mount it
as above and add `embed(App, mount_path="/ui", location=request.url.path)` to
the page, unescaped (`| safe` in Jinja).

Each page view is a live session on the server. If the connection drops, the
page dims and reconnects (with growing, randomised delays). The server keeps the
dropped session for `resume_timeout` seconds (30 by default; 0 turns this off),
so a reconnect within that time continues where it left off: component state is
kept, and clicks made while offline are sent and applied. After that, or when
the reconnect reaches another worker process, or after a deploy or restart, the
page renders afresh at the same URL and component state starts over.

WebSockets are accepted only from the page's own origin, so other websites
cannot drive a visitor's session. Behind a proxy that rewrites the `Host`
header, list the public origin in `allowed_origins`.

Any number of worker processes can serve the app: the page load and its
WebSocket need not reach the same one. To resume sessions after a dropped
connection with several workers, route reconnects to the same worker (sticky
sessions); otherwise they start afresh. Share stores between workers with a
`RedisBackend`.

### Deploying

The page keeps a WebSocket open, and the browser sends a small message every 25
seconds so proxies do not close it as idle.

Nginx must pass the WebSocket upgrade through and allow long connections:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
}
```

On Google Cloud Run a WebSocket counts as a request, so it is closed at the
service's request timeout (5 minutes by default, 60 at most); raise it to 60
minutes. The page reconnects by itself when that happens and, on the same
instance, keeps its state.

## How it works

1. A GET renders the component tree at the requested URL, waits for its data, and returns its HTML, keeping nothing in memory. Content waiting in a `Suspense` boundary is streamed into the same response when it is ready.
2. `client.js` (about 590 lines, no dependencies) opens a WebSocket for the same URL. The server renders again and sends the tree in document order; when it matches the page, the browser adopts the existing nodes in place, and otherwise rebuilds them (for a render that reads the clock or a random number). This is how React hydrates server HTML.
3. A state change re-renders only the dirty components, shallowest first. The reconciler matches children by key and sends operations such as "set text of node 12" or "move node 40 before node 9". Moves are kept to the minimum using a longest-increasing-subsequence pass.
4. Events from one page run one at a time, in order, each seeing the page as the previous one left it; plain handlers and effects run in worker threads.
5. While you type, the browser ignores server values older than your latest keystroke, so a slow round trip never undoes typing.
6. If the browser falls behind, pending updates merge into one message; past 100,000 pending operations the connection is closed and the browser starts over.

The operation format is documented at the top of `src/pyreaction/core/reconciler.py`.

## Layout

```
src/pyreaction/
  core/       elements, hooks, components, the reconciler and HTML rendering (no web dependencies)
  server/     the Starlette app, run() and embed()
  client/     client.js, the index.html page template and its style.css
  cli/        the pyreaction command: scaffold.py (init), serve.py (dev, run)
              and templates/, what init copies
  testing.py  render() and the fake browser for tests, serve() for real browsers
```

`index.html` uses `$name` placeholders (`$title`, `$head`, `$body`, ...). The
contents of `style.css` are inlined into the page so they cost no extra request.

## Development

```sh
uv sync
uv run pytest          # browser tests use the installed Chrome, or: uv run playwright install chromium
uv run mypy
uv run python examples/todo.py       # also: pages.py, widgets.py, login.py
uv run python benchmarks/bench.py
```

The browser tests (`tests/test_browser.py`) skip when no browser is available, and
the shared-store tests run against an in-process fake Redis server.
