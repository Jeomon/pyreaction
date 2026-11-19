# pyreaction

**React-style UI components in pure Python.** Components run on the server; the
browser gets server-rendered HTML for the first paint, then small DOM patches over
a WebSocket. No JavaScript to write, no build step, no Node.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Typed](https://img.shields.io/badge/typing-mypy%20checked-informational)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
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
- **Large pages stay fast**: `VirtualList` renders only the rows in view, and
  per-page stores with selectors re-render only the components whose part changed.
- **Fits your stack**: run it alone, mount it in FastAPI (with `Depends` support),
  put it in front of Django or Flask, or drop components into Jinja templates.
- **Escape hatches**: wrap any JavaScript or React widget, run code in the browser
  with `js()`, call element methods from Python, read any event property on demand.
- **Testable**: a fast fake browser for unit tests, and a live server helper for
  Playwright.
- **Typed**: the HTML tags and their attributes are typed, so editors autocomplete them.

## Installation

Requires Python 3.11 or newer. pyreaction is not on PyPI yet; install it from the
Git repository:

```sh
uv add "pyreaction @ git+https://github.com/Jeomon/pyreaction"
# or
pip install "pyreaction @ git+https://github.com/Jeomon/pyreaction"
```

Optional extras: `redis` (stores shared between processes), `wsgi` (Flask and other
WSGI apps), `jinja` (the `{% component %}` template tag) and `browser` (Playwright,
for tests in a real browser), as in `"pyreaction[redis,jinja] @ git+..."`.

## Quick start

Save the counter above as `app.py` and run `python app.py`. For a project with
live reload:

```sh
uvx --from git+https://github.com/Jeomon/pyreaction reaction init my-app
cd my-app
uv run dev        # http://127.0.0.1:8000, restarts on every save
uv run pro        # production server
```

[Getting started](docs/getting-started.md) covers both in more detail.

## A slightly bigger example

```python
from reaction import Form, component, html, run, use_data, use_form_status, Suspense

@component
def Messages():
    messages = use_data(db.latest_messages, [])          # loads before the HTML is sent
    return html.ul([html.li(m.text, key=m.id) for m in messages])

@component
def SendButton():
    return html.button("Sending..." if use_form_status().pending else "Send", type="submit")

@component
def Chat():
    return html.main(
        Suspense(Messages(), fallback=html.p("Loading...")),
        Form(html.input(name="text", required=True), SendButton(), action=lambda f: db.add(f["text"])),
    )

run(Chat)
```

`db` stands for your own data layer; plain functions like these run in a worker
thread, so a slow query does not hold up other pages.

More complete apps are in [`examples/`](examples): a todo list, routed pages, a
login flow, JavaScript widgets, and a FastAPI app.

## Documentation

| | |
| --- | --- |
| [Getting started](docs/getting-started.md) | install, a single-file app, a project |
| [Elements and events](docs/elements-and-events.md) | describing the page, handlers, file uploads |
| [Hooks](docs/hooks.md) | state, effects and the rest |
| [Data, forms and pending state](docs/data-and-forms.md) | loading data, Suspense, form actions, optimistic updates |
| [Routing and the page head](docs/routing.md) | URLs, links, redirects, `<title>` and `<meta>` |
| [Built-in components and the DOM](docs/components.md) | refs, portals, long lists, error boundaries, JavaScript widgets |
| [Shared state](docs/state.md) | stores shared between pages, processes, or within one page |
| [Integrations](docs/integrations.md) | FastAPI, Django, Flask, Jinja templates, static pages |
| [Testing](docs/testing.md) | a fake browser for unit tests, a live server for Playwright |
| [Configuration](docs/configuration.md) | every option, in code, `pyproject.toml` or the environment |
| [Deployment and security](docs/deployment.md) | workers, proxies, compression, origins, limits |
| [API reference](docs/api.md) | everything `reaction` exports |
| [How it works](docs/how-it-works.md) | the request, the live connection, and the update protocol |

## Contributing

Bug reports and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md)
explains how to set up, run the tests and type checks, and what a change needs.

## References

pyreaction follows the design of these projects; their documentation is the best
explanation of the ideas it borrows.

- **[React](https://react.dev)** ([source](https://github.com/facebook/react)):
  the component model, hooks and their rules, keys and reconciliation, Suspense,
  error boundaries, portals, and React 19's form actions, `use_optimistic`,
  `use`, `cache`, resource hints and `ViewTransition`. Where pyreaction has a React
  API, it follows React's behaviour, checked against React 19.3's source.
- **[ReactPy](https://reactpy.dev)** ([source](https://github.com/reactive-python/reactpy)):
  React's model in Python, rendered over a WebSocket. pyreaction's hook names,
  `html` element factories, `to_vdom` key names and JavaScript component interop
  follow ReactPy's lead; it differs in diffing on the server and sending DOM
  operations rather than VDOM trees, and in rendering every page as HTML first.

## License

[MIT](LICENSE)
