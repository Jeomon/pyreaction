# Integrations

Running pyreaction inside FastAPI, Django, Flask and Jinja templates, or as static pages.

## FastAPI

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

## Django, Flask and other frameworks

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

## Jinja templates

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

## Static prerendering

`prerender(App, ["/", "/about"], "dist")` renders pages ahead of time for a
static host, as React's `prerender` does: each waits for its data and is written
as `dist/index.html`, `dist/about/index.html`. `live=True` keeps the client
script, for pages served in front of the running app. `render_to_string` and
`render_to_static_markup` render an element to HTML (an email, a snippet), and
`to_vdom` returns the rendered tree as plain dicts.
