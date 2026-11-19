# pyreaction

**React-style UI components in pure Python.** Components run on the server; the
browser gets server-rendered HTML for the first paint, then small DOM patches over
a WebSocket. No JavaScript to write, no build step, no Node.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Typed](https://img.shields.io/badge/typing-mypy%20checked-informational)
![Status](https://img.shields.io/badge/status-pre--release%200.1-orange)

```python
from reaction import component, html, run, use_state

@component
def Counter():
    count, set_count = use_state(0)
    return html.button(f"Clicked {count} times", on_click=lambda: set_count(count + 1))

run(Counter)  # http://127.0.0.1:8000
```

The package is `pyreaction`, the import is `reaction`, and the command is
`reaction` (`pyreaction` works too).

## Features

- **The React model in Python**: components, `use_state`, `use_effect`, context,
  keys, error boundaries, Suspense, portals, refs, the rules of hooks, `StrictMode`,
  and React 19's forms, actions, `use_optimistic` and `ViewTransition`.
- **Server-rendered first**: every URL returns real HTML with its data in it
  (good for search engines and slow devices), streamed as Suspense boundaries
  resolve; the live page adopts that HTML instead of redrawing it.
- **Small updates**: the server diffs the tree and sends only DOM operations, such
  as "set the text of node 12".
- **Resilient connections**: pages reconnect by themselves and resume with their
  state; typing is never undone by a slow round trip.
- **Fits your stack**: run it alone, mount it in FastAPI (with `Depends` support),
  put it in front of Django or Flask, or drop components into Jinja templates.
- **Escape hatches**: wrap any JavaScript or React widget, run code in the browser
  with `js()`, call element methods from Python, read any event property on demand.
- **Testable**: a fast fake browser for unit tests, and a live server helper for
  Playwright.
- **Typed**: the HTML tags and their attributes are typed, so editors autocomplete them.

## Contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [Guide](#guide): [elements](#elements), [events](#events), [hooks](#hooks),
  [data](#data), [forms and pending state](#forms-and-pending-state),
  [routing](#routing), [the page head](#the-page-head), [refs](#acting-on-elements),
  [portals](#portals-and-hidden-content), [long lists](#long-lists), [shared state](#shared-state),
  [errors](#errors), [file uploads](#file-uploads),
  [JavaScript components](#javascript-components)
- [Integrations](#integrations): [FastAPI](#fastapi), [Django and Flask](#django-flask-and-other-frameworks),
  [Jinja templates](#jinja-templates), [static prerendering](#static-prerendering)
- [Testing](#testing)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Security](#security)
- [How it works](#how-it-works)
- [Development](#development)
- [License](#license)

## Installation

Requires Python 3.11 or newer. pyreaction is not on PyPI yet; install it from
the Git repository:

```sh
uv add "pyreaction @ git+https://github.com/Jeomon/pyreaction"
# or
pip install "pyreaction @ git+https://github.com/Jeomon/pyreaction"
```

Optional extras, added the same way (`"pyreaction[redis,jinja] @ git+..."`):

| Extra | For |
| --- | --- |
| `redis` | stores shared between worker processes (`RedisBackend`) |
| `wsgi` | putting pages in front of a WSGI app such as Flask (`wrap`) |
| `jinja` | the `{% component %}` template tag (`jinja_components`) |
| `browser` | Playwright, for tests in a real browser |

## Quick start

### A single file

Save the counter above as `app.py` and run `python app.py`. `run()` starts
uvicorn; pass `host=`, `port=` and any [configuration option](#configuration).

### A project

```sh
uvx --from git+https://github.com/Jeomon/pyreaction reaction init my-app
cd my-app
uv run dev        # http://127.0.0.1:8000, restarts on every save
uv run pro        # production server
```

```
my-app/
  pyproject.toml     settings under [tool.pyreaction]
  app.py             the App component
  components/        your components
  public/            served at /static; style.css is linked from the page
```

`init` creates the project, runs `uv sync` and `git init` (`--no-install`,
`--no-git` skip them). `dev` and `pro` are commands the project declares in
`[project.scripts]`; they are the same as `uv run reaction dev` and
`uv run reaction run`, which work in any project.

`dev` restarts the server when a `.py` file changes, and open pages reconnect and
redraw by themselves (component state starts over). It also turns on debug mode:
errors appear in the page with their traceback, and lists without keys are
reported. `pro` serves without reloading or debug output.

## Guide

### Elements

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

### Events

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

### Hooks

| Hook | |
| --- | --- |
| `use_state(initial)` | `value, set_value`; the setter takes a value or `lambda prev: ...` |
| `use_reducer(reducer, initial)` | `state, dispatch` |
| `use_effect(fn, deps)` | runs after the update reaches the browser; return a cleanup, or make it `async` (cancelled on cleanup). `deps=None` runs after every render, `[]` once, a list when a value in it changes |
| `use_layout_effect(fn, deps)` | the same as `use_effect`, for code written for React |
| `use_memo(fn, deps)` / `use_callback(fn, deps)` | cached until `deps` change |
| `use_ref(initial)` | `ref.current` survives re-renders; pass it as `ref=` to an element to act on it |
| `create_context(default)` / `use_context(ctx)` | provide with `ctx(*children, value=...)` |
| `use(ctx_or_task)` | a context (allowed inside `if`), or the result of an `asyncio` task |
| `use_async(fn, deps)` / `use_data(fn, deps)` | load data without blocking; see [Data](#data) |
| `use_transition()` / `use_action_state(action, initial)` / `use_optimistic(state, update)` | pending and optimistic state; see [Forms](#forms-and-pending-state) |
| `use_deferred_value(value)` | `value` one render behind, so a slow part updates after the fast part |
| `use_location()` / `use_navigate()` | the page URL (`.path`, `.search`, `.query`), and `navigate(url, replace=, reload=)` |
| `use_title(text)` / `use_status(code)` | the page title, and the first response's HTTP status (e.g. 404) |
| `use_request()` | the Starlette `Request`, then `WebSocket`, serving the page: `.headers`, `.cookies`, `.client`, `.scope` |
| `use_id()` | an id that matches between the server's HTML and the live page |
| `use_store(store)` / `use_sync_external_store(subscribe, get)` | state shared between pages; see [Shared state](#shared-state) |
| `use_effect_event(fn)` | a stable function that always calls the latest `fn` |
| `use_imperative_handle(ref, create, deps)` | expose an API to a parent holding `ref` |
| `use_linked_inputs(attrs_list, on_change, cast)` | inputs that show one value (a slider and a number box) |
| `use_debug_value(value)` | logs the value when it changes, in debug mode |

`@memo` instead of `@component` skips re-rendering while the arguments are
unchanged. A function of your own whose name starts with `use_` and calls hooks
is a custom hook.

The rules of hooks are enforced as in React: a hook inside an `if` raises
"Rendered fewer (or more) hooks than during the previous render", a hook outside
rendering raises "Invalid hook call", and a component that sets state on every
render stops after 50 renders with "Too many re-renders".

Also available, as in React: `StrictMode` (double rendering in debug mode),
`Profiler`, `ViewTransition` with `add_transition_type`, `lazy`, `forward_ref`,
`Children.*`, `clone_element`, `is_valid_element`, `flush_sync` and
`request_form_reset`. Updates in the same event-loop tick are batched into one
render, and setters are safe to call from other threads.

### Data

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
`async def`) whenever `deps` change and returns `.loading`, `.value`, `.error`
and `.reload()`. `use_data(fn, deps)` returns the value itself: while it loads,
the nearest `Suspense(*children, fallback=...)` shows its fallback, and an error
goes to the nearest `ErrorBoundary`.

The server waits for data (up to `data_timeout`, 5 seconds by default) before
sending a page's first HTML, so the page arrives complete. Data inside a
`Suspense` boundary does not hold the page back: the HTML goes out with the
fallback, and the content follows in the same response when ready.

`@cache` memoises a function for one page, as React's `cache` does for one
request; `cache_signal()` says when the page has closed. `browser()` at the top
of a component leaves it to the browser: the first HTML shows the nearest
`Suspense` fallback, and the component renders once the page is live.

### Forms and pending state

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
fields on success (`reset=False` keeps them). `html.form(..., action=fn)` is the
same, as React 19 writes it, and `html.button("Draft", form_action=fn)` submits
its form to another action.

`use_transition()` returns `pending, start`; `start(fn, *args)` runs `fn` in the
background with `pending` true meanwhile. `use_action_state(action, initial)`
returns `state, run, pending`. `use_optimistic` shows a change at once, until the
action behind it finishes:

```python
shown, add = use_optimistic(messages, lambda cur, text: cur + [text + " (sending)"])

def send(fields):
    add(fields["text"])                         # shown at once
    set_messages(api.post(fields["text"]))      # the real list replaces it when done
```

### Routing

```python
from reaction import Link, Navigate, Router

@component
def App():
    return html.main(
        html.nav(Link("Home", to="/"), Link("Users", to="/users")),
        Router({"/": Home, "/users": Users, "/users/{id}": User, "/files/{rest:path}": File,
                "/old": lambda: Navigate("/users")},
               fallback=NotFound),
    )
```

`Router` renders the first matching route and passes the `{params}` as keyword
arguments (`User(id="7")`). When nothing matches it renders `fallback` and the
first response has status 404. `Navigate(to)` redirects: an HTTP 307 on first
load, a URL change once live. `Link` changes the URL without a reload, Back and
Forward work, and every URL also renders on the server when loaded directly.

### The page head

```python
html.title(post.title)                                   # anywhere in the tree
html.meta(name="description", content=post.summary)
html.link(rel="canonical", href=url)
preload("/fonts/inter.woff2", as_="font", cross_origin="anonymous")
```

As in React 19, `<title>`, `<meta>` and `<link>` written anywhere go into
`<head>`, in the first HTML too, and leave it when they unmount. `Head(...)` and
`use_title` do the same. The resource hints `prefetch_dns`, `preconnect`,
`preload`, `preload_module`, `preinit` and `preinit_module` add each resource once.

### Acting on elements

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

### Portals and hidden content

```python
portal(html.div("Saved!", class_name="toast"), target="body")   # rendered at the end of <body>
Activity(Settings(), mode="hidden" if tab != "settings" else "visible")
```

`portal` renders children elsewhere in the page (modals and toasts that must
escape `overflow: hidden`), with state, context and events working as if they
were in place, in the server's HTML too. `Activity` hides children without
unmounting them, so their state survives.

### Long lists

```python
VirtualList(users, lambda user, i: UserRow(user), row_height=36, height=600,
            key=lambda user: user.id, class_name="users")
```

`VirtualList` renders only the rows in view (plus `overscan` rows on each side), so
a list of 100,000 items costs what 30 do, in the first HTML and in every update.
Rows have a fixed `row_height`; the list is a scrolling box `height` pixels tall,
and other props go on it. Rows scrolled out are unmounted, so keep their state
above the list.

### Shared state

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

### Errors

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

### File uploads

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

### JavaScript components

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

## Integrations

### FastAPI

`mount` serves the pages inside a FastAPI (or Starlette) app, next to its API:

```python
from fastapi import FastAPI
from reaction import mount

api = FastAPI()

@api.get("/api/todos")
def todos(): ...

mount(api, App)                                # pages at /, the API keeps /api and /docs
mount(api, App, "/ui", static_dir="public")    # or pages under /ui
```

Run it as usual (`uvicorn main:api`). At `"/"` the pages answer every URL the
API does not. Under a path, components see paths inside it, and links,
redirects, static files and JavaScript modules get the prefix. Components reach
the FastAPI app through `use_request()`: `request.app.state`, the lifespan
state, `request.session` and `request.user` all work. `raise HTTPException(...)`
in a component sets the first response's status and headers, and the nearest
`ErrorBoundary` shows it.

**Pages with `Depends`.** `page` turns a FastAPI route into a pyreaction page, so
its function can use `Depends`, path and query parameters and `Cookie`:

```python
from fastapi import Depends
from starlette.requests import HTTPConnection
from reaction import page

def current_user(conn: HTTPConnection):        # HTTPConnection, not Request
    ...

@page(api, "/orders/{order_id}", title="Order")
def order(order_id: int, user=Depends(current_user), db=Depends(get_db)):
    return OrderPage(order=db.order(order_id, user))
```

FastAPI resolves the parameters when the page loads and again when its live
connection opens; a dependency with `yield` stays open while the page is live.
Type dependencies as `HTTPConnection`: the live connection is a WebSocket, where a
`Request` dependency fails, and browsers cannot add headers to a WebSocket, so
authenticate with cookies. Each `@page` shows one route; for an app with its own
routing, use `mount`. `examples/fastapi_app.py` is a complete app.

Checked against FastAPI 0.165, including routers, OpenAPI, `--root-path`, CORS,
TrustedHost, GZip, HTTP middleware, `AuthenticationMiddleware`, `fastapi run`
and `uvicorn --workers N`.

### Django, Flask and other frameworks

`wrap` puts pyreaction pages in front of another web app and returns an ASGI app
to run with uvicorn:

```python
# Django: asgi.py, then `uvicorn mysite.asgi:application`
from django.core.asgi import get_asgi_application
from reaction import wrap
application = wrap(get_asgi_application(), Dashboard, "/dashboard")

# Flask: a WSGI app (needs the `wsgi` extra), then `uvicorn main:app`
app = wrap(flask_app, Dashboard, "/dashboard")
```

Requests under the path go to pyreaction; everything else reaches the framework
unchanged. With `"/"`, the framework answers first and a GET it answers with 404
becomes a pyreaction page. Components read the request through `use_request()`
(cookies, headers); `flask.request` and Django's `request.user` are not available
in components. Checked with Django 6.1 and Flask 3.1.

To put a component inside a page the framework renders, wrap it under a path and
add `embed(Widget, mount_path="/widget", location=request.path)` to the template
(`|safe` in Jinja, `mark_safe` in Django).

### Jinja templates

`jinja_components` adds a `{% component %}` tag (needs the `jinja` extra):

```python
from reaction import jinja_components

# FastAPI with Jinja2Templates
api.mount("/components", jinja_components(templates.env, [Counter, Chart], secret=SECRET))

# Flask
app = wrap(flask_app, jinja_components(flask_app.jinja_env, [Counter, Chart], secret=SECRET), "/components")
```

```jinja
{% component "myapp.widgets.Counter" %}
{% component "myapp.widgets.Chart", values=[1, 2, 3], title=page_title %}
```

The tag writes the server-rendered HTML and the script. Only the listed
components can be named, by import path, and props must be JSON values. The
component and its props travel signed with `secret`, so a visitor cannot change
them; give every worker process the same `secret`.

### Static prerendering

`prerender(App, ["/", "/about"], "dist")` renders pages ahead of time for a
static host, as React's `prerender` does: each waits for its data and is written
as `dist/index.html`, `dist/about/index.html`. `live=True` keeps the client
script, for pages served in front of the running app. `render_to_string` and
`render_to_static_markup` render an element to HTML (an email, a snippet), and
`to_vdom` returns the rendered tree as plain dicts.

## Testing

```python
from reaction.testing import render

def test_todo():
    with render(Todo()) as page:
        page.type("input[name=item]", "milk")
        page.click("button.primary")
        assert [li.text for li in page.find_all("#list li")] == ["milk"]
```

`render` mounts a component on a real event loop with a small fake browser, so
handlers, effects, data loading and forms run as on a live page; each action
waits until the page has settled. `find` and `find_all` take CSS selectors and
`text=`; actions are `click`, `type`, `check`, `key`, `submit`, `navigate` and
`dispatch`. Errors raised by components fail the test (`raise_errors=False`
collects them in `page.errors`). `to_vdom(page.root)` gives the page as plain
dicts, for snapshot tests.

For a real browser, `serve` runs the app on a free local port, and `poll` waits
for a condition that the page reaches in its own time:

```python
from reaction.testing import poll, serve

def test_counter(page):                      # Playwright's page (pytest-playwright)
    with serve(Counter) as server:           # create_app options go here too
        page.goto(server.url)
        page.wait_for_selector(server.READY)
        page.click("button")
        poll(lambda: page.text_content("button") == "Clicked 1 times")
```

`poll(check, timeout=5)` returns the first truthy result of `check`, retrying
`AssertionError`s until the timeout. `serve(asgi_app, asgi=True)` serves an app
you built yourself.

## Configuration

Options are passed to `create_app` (and so to `run`, `mount`, `page`, `wrap` and
`serve`), set in a project's `[tool.pyreaction]` table, or read from environment
variables. Code wins over `[tool.pyreaction]`, which wins over the environment.

| Option | Default | Environment variable | |
| --- | --- | --- | --- |
| `title` | `"pyreaction"` | | the page title before `use_title` sets one |
| `head` | `""` | | extra raw HTML for `<head>` |
| `lang` | `"en"` | | the `<html lang>` attribute |
| `static_dir` / `static_path` | none / `"/static"` | | a folder served at a path |
| `stylesheets` | `()` | | files in the static folder linked from the page |
| `routes` | `()` | | extra Starlette routes, matched before the pages |
| `on_error` | none | | `fn(exc, where, connection)` for uncaught errors |
| `debug` | `False` | `PYREACTION_DEBUG` | errors and warnings shown in the page |
| `allowed_origins` | `()` | `PYREACTION_ALLOWED_ORIGINS` (comma-separated) | extra origins allowed to open the WebSocket |
| `max_upload_size` | 10 MiB | `PYREACTION_MAX_UPLOAD_SIZE` | bytes per uploaded file |
| `max_pending_events` | `1000` | `PYREACTION_MAX_PENDING_EVENTS` | events a page may queue before it is disconnected |
| `max_pending_ops` | `100000` | `PYREACTION_MAX_PENDING_OPS` | updates a slow browser may fall behind before it starts over |
| `data_timeout` | `5.0` | `PYREACTION_DATA_TIMEOUT` | seconds the first HTML waits for data |
| `stream` | `True` | `PYREACTION_STREAM` | stream Suspense content into the first response |
| `threaded` | `True` | `PYREACTION_THREADED` | run plain handlers and effects in worker threads |
| `reconnect_max` | `8000` | `PYREACTION_RECONNECT_MAX` | longest delay between reconnect attempts, in ms |
| `reconnect_attempts` | `0` (no limit) | `PYREACTION_RECONNECT_ATTEMPTS` | failed attempts in a row before the page gives up |
| `resume_timeout` | `30.0` | `PYREACTION_RESUME_TIMEOUT` | seconds a dropped page's state is kept for a reconnect |
| `handoff_timeout` | `5.0` | `PYREACTION_HANDOFF_TIMEOUT` | seconds a first-HTML render waits for its page's live connection (0: render again) |
| `max_handoffs` | `64` | `PYREACTION_MAX_HANDOFFS` | first-HTML renders that may wait at once; the oldest makes way |
| `compress` | `True` | `PYREACTION_COMPRESS` | gzip pages and static files for browsers that accept it |

A project's settings:

```toml
[tool.pyreaction]
app = "app:App"              # module:attribute of the root component
title = "my-app"
static = "public"            # served at /static
stylesheets = ["style.css"]
resume_timeout = 60          # any option from the table above
```

`reaction run` and `reaction dev` listen on `--host` and `--port`, which default
to `PYREACTION_HOST` (else `127.0.0.1`) and `PYREACTION_PORT`, then `PORT`
(else `8000`). Boolean variables accept `1`/`true`/`yes`/`on` and
`0`/`false`/`no`/`off`; an invalid value stops the app at startup.

## Deployment

**Running.** `uv run pro` (or `reaction run --host 0.0.0.0`) serves a project
with uvicorn; any ASGI server can serve `create_app(App)`. In a container, set
`PORT` and the `PYREACTION_*` variables instead of editing code.

**Compression and caching.** Pages and static files are gzipped (`compress`; turn
it off when a proxy already compresses), streamed pages included. The client
script is served at a URL with a hash of its contents (`client.js?v=...`), so
browsers keep it for a year and fetch it again only after an upgrade.

**Sessions and workers.** Each open page is a live session on the server. The
render that made a page's HTML waits up to `handoff_timeout` seconds for the
page's live connection, which takes it over instead of rendering again (only for
the same browser cookies and URL; at most `max_handoffs` wait at once). If the
connection drops, the page dims and reconnects with growing, randomised delays.
The server keeps the dropped session for `resume_timeout` seconds, so a reconnect
within that time continues with its state; after that, or on another worker
process, or after a deploy, the page renders afresh at the same URL. Any number
of workers can serve the app. To resume sessions or take over renders across
workers, route a page's connections to the worker that served it (sticky
sessions), and share stores with `RedisBackend`.

With `reconnect_attempts` set, a page that cannot reconnect stops after that many
attempts and shows "Connection to the server was lost." with a Reload button
(styled through `#pyreaction-lost`; `<html data-pyreaction-lost>` is set).

**Proxies.** The browser sends a small message every 25 seconds so proxies do not
close the WebSocket as idle. Nginx must pass the upgrade through and allow long
connections:

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

A proxy that rewrites the `Host` header needs the public origin in
`allowed_origins`. On Google Cloud Run a WebSocket counts as a request and is
closed at the service's request timeout (5 minutes by default); raise it to 60
minutes. The page reconnects by itself when that happens.

## Security

- **Origins.** WebSockets are accepted only from the page's own origin (plus
  `allowed_origins`), so other websites cannot drive a visitor's session.
- **Authentication.** A WebSocket cannot set cookies, and JavaScript must not read
  a session cookie. Log in and out with ordinary HTTP routes that set an
  `HttpOnly` cookie, read it with `use_request().cookies`, and use
  `navigate(url, reload=True)` afterwards so the next connection carries it.
  `examples/login.py` is a complete version.
- **Markup and code.** Text is always escaped. `dangerously_set_inner_html`
  inserts HTML as is; `parse_html` removes scripts and event attributes. `js()`
  code and `js_component_from_string` modules are sent as written: never build
  them from user input.
- **Limits.** Uploads are capped by `max_upload_size`, queued events per page by
  `max_pending_events`, and pending updates to a slow browser by
  `max_pending_ops`.
- **Templates.** `jinja_components` signs the component and props it puts in the
  page; tampered links render nothing.
- **Content-Security-Policy.** The page uses inline style and small inline
  scripts for streaming, and `js_component_from_string` needs `data:` scripts;
  allow them in a policy that restricts `script-src`.

## How it works

1. A GET renders the component tree at the requested URL, waits for its data, and
   returns the HTML. Content waiting in a `Suspense` boundary is streamed into the
   same response when ready. The render is kept for a few seconds for the page's
   live connection.
2. `client.js`, a single script with no dependencies, opens a WebSocket for the
   same URL. The server takes over the kept render (or renders again) and sends
   each node's id, attributes and events, but not the structure the HTML already
   has; the browser adopts the existing nodes in place, as React hydrates. If the
   HTML differs (a render that reads the clock), the browser asks for the whole
   tree and rebuilds the page.
3. A state change re-renders only the affected components. The diff in
   `core/vdom.py` matches children by key and sends operations such as "set the
   text of node 12" or "move node 40 before node 9", keeping moves to a minimum.
4. Events from one page run one at a time, in order; plain handlers and effects
   run in worker threads, so a slow one does not block other pages.
5. While you type, the browser ignores server values older than your latest
   keystroke, so a slow round trip never undoes typing.

The operation format is documented at the top of `src/reaction/core/vdom.py`.

## Development

```sh
uv sync
uv run pytest          # browser tests use the installed Chrome, or: uv run playwright install chromium
uv run mypy
uv run python examples/todo.py       # also: pages.py, widgets.py, login.py, lunch_roulette.py, fastapi_app.py
uv run python benchmarks/bench.py
```

The browser tests skip when no browser is available; the shared-store tests use
an in-process fake Redis server. The test suite also runs on Python 3.11.

**Compiling the core.** `uv run python scripts/compile.py` compiles the core
(`vdom`, `element`, `ssr`, `hooks`) with mypyc in place, which makes rendering and
updates 1.5-1.7x faster; `--clean` returns to plain Python. It needs a C compiler.
Compiled modules take precedence over their `.py` files, so compile again after
editing one; pyreaction logs a warning when a compiled module is older than its
source. The tests pass either way.

```
src/reaction/
  core/       elements, hooks, components, the virtual DOM (vdom.py) and HTML rendering
  server/     the ASGI app and integrations: run, mount, page, wrap, embed, jinja_components, prerender
  client/     client.js, the page template (index.html) and its style.css
  cli/        the reaction command: init, dev and run, and the project template
  testing.py  render() with a fake browser, serve() and poll() for real browsers
```

## License

No license has been chosen yet, so all rights are reserved by the author. The
project is not yet published; until a license is added, do not redistribute it.
