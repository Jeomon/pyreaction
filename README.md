<div align="center">
  <h1>⚛️ pyreaction</h1>

  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/typing-mypy%20checked-informational" alt="Typed with mypy">
  <img src="https://img.shields.io/badge/status-pre--release%200.1-orange" alt="Status: pre-release 0.1">
</div>

**pyreaction** brings React's component model to Python. Components run on the
server; the browser gets server-rendered HTML for the first paint, then small DOM
updates over a WebSocket. No JavaScript to write, no build step, no Node.

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

## ✨ Key Features

- **The React model in Python**
  Components, `use_state`, `use_effect`, context, keys, error boundaries, Suspense,
  portals, refs, the rules of hooks, `StrictMode`, and React 19's form actions,
  `use_optimistic` and `ViewTransition`.

- **Server-rendered first**
  Every URL returns real HTML with its data in it, good for search engines and slow
  devices. Suspense content streams into the same response, and the live page
  adopts the HTML instead of redrawing it.

- **Small updates**
  The server diffs the tree and sends only DOM operations, such as "set the text of
  node 12". A click on a small page is a round trip of about a third of a
  millisecond on the server.

- **Resilient connections**
  Pages reconnect by themselves and resume with their state, and typing is never
  undone by a slow round trip.

- **Large pages stay fast**
  `VirtualList` renders only the rows in view, and per-page stores with selectors
  re-render only the components whose part changed.

- **Fits your stack**
  Run it alone, mount it in FastAPI (with `Depends`), put it in front of Django or
  Flask, or drop components into Jinja templates.

- **Escape hatches**
  Wrap any JavaScript or React widget, run code in the browser with `js()`, call
  element methods from Python, and read any event property on demand.

- **Testable and typed**
  A fake browser for unit tests, a live server helper for Playwright, and typed
  HTML tags that editors autocomplete.

## 🛠️ Installation

### Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

```shell
uv add pyreaction
# or
pip install pyreaction
```

Optional extras are added the same way, as in `pyreaction[redis,jinja]`:

| Extra | For |
| --- | --- |
| `redis` | stores shared between worker processes (`RedisBackend`) |
| `wsgi` | putting pages in front of a WSGI app such as Flask |
| `jinja` | the `{% component %}` template tag |
| `browser` | Playwright, for tests in a real browser |

### Start a project

```shell
uvx pyreaction init my-app
cd my-app
uv run dev        # http://127.0.0.1:8000, restarts on every save
```

```
my-app/
  pyproject.toml     settings under [tool.pyreaction]
  app.py             the App component
  components/        your components
  public/            served at /static; style.css is linked from the page
```

### Use it with your framework

<details>
  <summary>FastAPI</summary>

  Serve the pages next to your API with `mount`, or turn a route into a page with
  `page`, whose function can use `Depends`:

```python
from fastapi import Depends, FastAPI
from reaction import mount, page

api = FastAPI()
mount(api, App, "/ui")                          # a whole app under /ui

@page(api, "/orders/{order_id}", title="Order")
def order(order_id: int, user=Depends(current_user)):
    return OrderPage(order_id=order_id, user=user)
```

  Run it as usual with `uvicorn main:api`. See [Integrations](https://github.com/Jeomon/pyreaction/blob/main/docs/integrations.md#fastapi).
</details>

<details>
  <summary>Django</summary>

  Put the pages in front of Django's ASGI app, in `asgi.py`:

```python
from django.core.asgi import get_asgi_application
from reaction import wrap

application = wrap(get_asgi_application(), Dashboard, "/dashboard")
```

  Run it with `uvicorn mysite.asgi:application`. See [Integrations](https://github.com/Jeomon/pyreaction/blob/main/docs/integrations.md#django-flask-and-other-frameworks).
</details>

<details>
  <summary>Flask</summary>

  Install the `wsgi` extra, then wrap the Flask app:

```python
from reaction import wrap

app = wrap(flask_app, Dashboard, "/dashboard")
```

  Run it with `uvicorn main:app`. See [Integrations](https://github.com/Jeomon/pyreaction/blob/main/docs/integrations.md#django-flask-and-other-frameworks).
</details>

<details>
  <summary>Jinja templates</summary>

  Install the `jinja` extra, then add a `{% component %}` tag to your templates:

```python
from reaction import jinja_components

api.mount("/components", jinja_components(templates.env, [Counter, Chart], secret=SECRET))
```

```jinja
{% component "myapp.widgets.Counter" %}
{% component "myapp.widgets.Chart", values=[1, 2, 3] %}
```

  See [Integrations](https://github.com/Jeomon/pyreaction/blob/main/docs/integrations.md#jinja-templates).
</details>

## 🖥️ Running pyreaction

| Command | |
| --- | --- |
| `python app.py` | a single file that calls `run(App)` |
| `uv run dev` | a project, with reload on save and errors shown in the page |
| `uv run pro` | a project, for production |
| `uvicorn main:app` | any app built with `create_app`, `mount` or `wrap` |

`reaction run` and `reaction dev` take `--host` and `--port`, which default to
`PYREACTION_HOST`/`127.0.0.1` and `PYREACTION_PORT`/`PORT`/`8000`. Behind a proxy,
pass the WebSocket upgrade through; [Deployment](https://github.com/Jeomon/pyreaction/blob/main/docs/deployment.md) has an Nginx
example and notes on workers and sticky sessions.

## ⚙️ Configuration

Options go to `create_app` (and `run`, `mount`, `page`, `wrap`), into a project's
`[tool.pyreaction]` table, or into `PYREACTION_*` environment variables. The ones
most often changed:

| Option | Default | Environment variable | |
| --- | --- | --- | --- |
| `debug` | `False` | `PYREACTION_DEBUG` | errors and warnings shown in the page |
| `allowed_origins` | `()` | `PYREACTION_ALLOWED_ORIGINS` | extra origins allowed to open the WebSocket |
| `compress` | `True` | `PYREACTION_COMPRESS` | gzip pages; turn off when a proxy compresses |
| `resume_timeout` | `30.0` | `PYREACTION_RESUME_TIMEOUT` | seconds a dropped page's state is kept |
| `max_upload_size` | 10 MiB | `PYREACTION_MAX_UPLOAD_SIZE` | bytes per uploaded file |
| `reconnect_attempts` | `0` (no limit) | `PYREACTION_RECONNECT_ATTEMPTS` | failed attempts before a page gives up |

[Configuration](https://github.com/Jeomon/pyreaction/blob/main/docs/configuration.md) lists all of them.

## 📚 Documentation

| | |
| --- | --- |
| [Getting started](https://github.com/Jeomon/pyreaction/blob/main/docs/getting-started.md) | install, a single-file app, a project |
| [Elements and events](https://github.com/Jeomon/pyreaction/blob/main/docs/elements-and-events.md) | describing the page, handlers, file uploads |
| [Hooks](https://github.com/Jeomon/pyreaction/blob/main/docs/hooks.md) | state, effects and the rest |
| [Data, forms and pending state](https://github.com/Jeomon/pyreaction/blob/main/docs/data-and-forms.md) | loading data, Suspense, form actions, optimistic updates |
| [Routing and the page head](https://github.com/Jeomon/pyreaction/blob/main/docs/routing.md) | URLs, links, redirects, `<title>` and `<meta>` |
| [Built-in components and the DOM](https://github.com/Jeomon/pyreaction/blob/main/docs/components.md) | refs, portals, long lists, error boundaries, JavaScript widgets |
| [Shared state](https://github.com/Jeomon/pyreaction/blob/main/docs/state.md) | stores shared between pages, processes, or within one page |
| [Integrations](https://github.com/Jeomon/pyreaction/blob/main/docs/integrations.md) | FastAPI, Django, Flask, Jinja templates, static pages |
| [Testing](https://github.com/Jeomon/pyreaction/blob/main/docs/testing.md) | a fake browser for unit tests, a live server for Playwright |
| [Configuration](https://github.com/Jeomon/pyreaction/blob/main/docs/configuration.md) | every option, in code, `pyproject.toml` or the environment |
| [Deployment and security](https://github.com/Jeomon/pyreaction/blob/main/docs/deployment.md) | workers, proxies, compression, origins, limits |
| [API reference](https://github.com/Jeomon/pyreaction/blob/main/docs/api.md) | everything `reaction` exports |
| [How it works](https://github.com/Jeomon/pyreaction/blob/main/docs/how-it-works.md) | the request, the live connection, and the update protocol |

Runnable apps are in [`examples/`](https://github.com/Jeomon/pyreaction/tree/main/examples): a todo list, routed pages, a login
flow, JavaScript widgets, and a FastAPI app.

## 🔒 Security

- WebSockets are accepted only from the page's own origin, plus `allowed_origins`.
- Log in with ordinary HTTP routes that set an `HttpOnly` cookie; a WebSocket cannot
  set cookies. `examples/login.py` is a complete version.
- Text is always escaped. `dangerously_set_inner_html`, `js()` code and
  `js_component_from_string` are sent as written: never build them from user input.
- Uploads, queued events and pending updates are capped per page.

[Deployment and security](https://github.com/Jeomon/pyreaction/blob/main/docs/deployment.md#security) has the details. To report a
vulnerability, see [SECURITY.md](https://github.com/Jeomon/pyreaction/blob/main/SECURITY.md); please do not open a public issue.

## 📝 Limitations

- **Every interaction is a round trip.** Handlers run on the server, so latency
  follows the network; use `js()` for what must react instantly, such as selecting
  an input's text.
- **Each open page holds state on the server.** Memory grows with open tabs, and a
  10,000-row page holds about 20 MB. Long lists should use `VirtualList`.
- **Rendering runs on the server's event loop.** A very large page (10,000 rows)
  takes about 80 ms to serve, during which other pages on that process wait.
- **Resuming after a drop needs the same process.** With several workers, route a
  page's connections to the worker that served it (sticky sessions).
- **Before 1.0**, the API may still change between minor versions; see the
  [changelog](https://github.com/Jeomon/pyreaction/blob/main/CHANGELOG.md).
- **The import name is `reaction`**, which an unrelated, unmaintained PyPI package
  (`reaction`, 2020) also uses. Do not install both in one environment.

## 🤝 Contributing

Bug reports and pull requests are welcome. [CONTRIBUTING.md](https://github.com/Jeomon/pyreaction/blob/main/CONTRIBUTING.md)
explains the setup, the tests and type checks, and what a change needs.
[CHANGELOG.md](https://github.com/Jeomon/pyreaction/blob/main/CHANGELOG.md) lists what changed in each version.

## 🙏 References

pyreaction follows the design of these projects; their documentation is the best
explanation of the ideas it borrows.

- **[React](https://react.dev)** ([source](https://github.com/facebook/react)):
  the component model, hooks and their rules, keys and reconciliation, Suspense,
  error boundaries, portals, and React 19's form actions, `use_optimistic`, `use`,
  `cache`, resource hints and `ViewTransition`. Where pyreaction has a React API,
  it follows React's behaviour, checked against React 19.3's source.
- **[ReactPy](https://reactpy.dev)** ([source](https://github.com/reactive-python/reactpy)):
  React's model in Python, rendered over a WebSocket. pyreaction's hook names,
  `html` element factories, `to_vdom` key names and JavaScript component interop
  follow ReactPy's lead; it differs in diffing on the server and sending DOM
  operations rather than VDOM trees, and in rendering every page as HTML first.

It is built on [Starlette](https://www.starlette.io), [uvicorn](https://www.uvicorn.org)
and [orjson](https://github.com/ijl/orjson).

pyreaction is an independent project. It is not affiliated with, endorsed by or
sponsored by Meta, the React project or the ReactPy project. React is a trademark of
Meta Platforms, Inc.; the name is used only to describe what pyreaction follows.

## 🪪 License

pyreaction is free and open-source software, released under the
[MIT License](https://github.com/Jeomon/pyreaction/blob/main/LICENSE). You may use, copy, modify, merge, publish, distribute,
sublicense and sell it, in personal and commercial projects alike, provided the
copyright notice and the license text are included in all copies or substantial
portions of the software.

The software is provided "as is", without warranty of any kind; the authors are
not liable for any claim or damages arising from its use. Contributions to
pyreaction are accepted under the same license (see [CONTRIBUTING.md](https://github.com/Jeomon/pyreaction/blob/main/CONTRIBUTING.md)).

## 📄 Citation

```bibtex
@software{pyreaction,
  author    = {jeomon},
  title     = {pyreaction: React-style UI components in pure Python},
  year      = {2026},
  publisher = {GitHub},
  url       = {https://github.com/Jeomon/pyreaction}
}
```
