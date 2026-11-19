"""wrap(): pyreaction pages in front of Django (ASGI) and Flask (WSGI)."""

import json
import logging

import pytest
from starlette.testclient import TestClient

from reaction import Navigate, Router, component, html, use_location, use_request, use_state, wrap

from .fakedom import FakeDom

django = pytest.importorskip("django")
flask = pytest.importorskip("flask")
pytest.importorskip("a2wsgi")


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@component
def Counter():
    n, set_n = use_state(0)
    return html.button(f"n={n}", on_click=lambda: set_n(n + 1))


# ---- the two framework apps -------------------------------------------------------------


def django_app():
    from django.conf import settings

    if not settings.configured:
        from django.http import HttpResponse, JsonResponse
        from django.urls import path

        def hello(request):
            return JsonResponse({"from": "django", "user": request.COOKIES.get("user")})

        def submit(request):
            return HttpResponse(f"posted {request.method}")

        def shop(request):
            # A Django template with a pyreaction widget in it.
            from django.template import Context, Engine
            from django.utils.safestring import mark_safe

            from reaction import embed

            widget = mark_safe(embed(Counter, mount_path="/widget", location=request.path))
            template = Engine().from_string("<h1>{{ title }}</h1>{{ widget }}")
            return HttpResponse(template.render(Context({"title": "Shop", "widget": widget})))

        urls = type("urls", (), {"urlpatterns": [
            path("api/hello", hello), path("api/submit", submit), path("shop", shop),
        ]})
        settings.configure(
            DEBUG=False, ALLOWED_HOSTS=["*"], SECRET_KEY="test", ROOT_URLCONF=urls,
            MIDDLEWARE=[], INSTALLED_APPS=[],
        )
        django.setup()
    from django.core.asgi import get_asgi_application

    return get_asgi_application()


def flask_app():
    app = flask.Flask("flask_test_app")

    @app.get("/api/hello")
    def hello():
        return {"from": "flask", "user": flask.request.cookies.get("user")}

    @app.post("/api/submit")
    def submit():
        return f"posted {flask.request.form.get('x')}"

    return app


FRAMEWORKS = [("django", django_app), ("flask", flask_app)]


# ---- the pyreaction app -----------------------------------------------------------------


@component
def Home():
    user = use_request().cookies.get("user", "nobody")
    n, set_n = use_state(0)
    return html.div(
        html.p(f"{user} at {use_location().path}", id="where"),
        html.button(f"n={n}", on_click=lambda: set_n(n + 1)),
    )


@component
def App():
    return Router({"/": Home, "/old": lambda: Navigate("/")}, fallback=lambda: html.p("not here"))


def live(client, ws_path, p, b=""):
    """The first live render of path ``p``, then the page after one click."""
    with client.websocket_connect(f"{ws_path}?p={p}&b={b}") as ws:
        first = json.loads(ws.receive_text())
        dom = FakeDom()
        dom.apply(first["ops"])
        before = dom.html()
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        dom.apply(json.loads(ws.receive_text())["ops"])
        return before, dom.html()


@pytest.mark.parametrize("name, make", FRAMEWORKS)
def test_pages_under_a_path_and_the_framework_everywhere_else(name, make, tmp_path):
    (tmp_path / "app.css").write_text("body{}")
    with TestClient(wrap(make(), App, "/ui", static_dir=tmp_path), cookies={"user": "ada"}) as client:
        assert client.get("/api/hello").json() == {"from": name, "user": "ada"}
        assert client.post("/api/submit", data={"x": "1"}).text.startswith("posted")

        page = client.get("/ui/")
        assert page.status_code == 200 and '<p id="where">ada at /</p>' in page.text
        assert 'src="/ui/_pyreaction/client.js?v=' in page.text and 'data-base="/ui"' in page.text
        assert client.get("/ui/_pyreaction/client.js").status_code == 200
        assert client.get("/ui/static/app.css").text == "body{}"
        redirect = client.get("/ui/old", follow_redirects=False)
        assert (redirect.status_code, redirect.headers["location"]) == (307, "/ui/")
        assert client.get("/nope").status_code == 404, "outside /ui the framework answers"

        assert live(client, "/ui/_pyreaction/ws", "/", "/ui") == (
            '<div><p id="where">ada at /</p><button>n=0</button></div>',
            '<div><p id="where">ada at /</p><button>n=1</button></div>',
        )


@pytest.mark.parametrize("name, make", FRAMEWORKS)
def test_at_the_root_pages_take_what_the_framework_does_not_find(name, make):
    with TestClient(wrap(make(), App)) as client:
        assert client.get("/api/hello").json()["from"] == name, "the framework's routes win"
        page = client.get("/")
        assert page.status_code == 200 and '<p id="where">nobody at /</p>' in page.text
        assert "not here" in client.get("/missing").text, "a framework 404 becomes a pyreaction page"
        assert client.post("/missing").status_code in (404, 405), "only GETs fall through to pyreaction"
        assert client.get("/_pyreaction/client.js").status_code == 200
        before, after = live(client, "/_pyreaction/ws", "/")
        assert "n=0" in before and "n=1" in after


def test_a_wsgi_app_without_a2wsgi_explains_what_to_install(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_a2wsgi(name, *args, **kwargs):
        if name == "a2wsgi":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_a2wsgi)
    with pytest.raises(ImportError, match=r"pyreaction\[wsgi\]"):
        wrap(flask_app(), App)


# ---- embed() inside the framework's own templates ---------------------------------------


def check_embedded(client, page_url):
    page = client.get(page_url).text
    assert "<h1>Shop</h1>" in page, "the framework's own template"
    assert '<div id="pyreaction-root"><button>n=0</button></div>' in page, "server-rendered widget"
    assert 'src="/widget/_pyreaction/client.js?v=' in page and 'data-ws="/widget/_pyreaction/ws"' in page
    assert client.get("/widget/_pyreaction/client.js").status_code == 200
    with client.websocket_connect(f"/widget/_pyreaction/ws?p={page_url}&h=1") as ws:
        first = json.loads(ws.receive_text())
        dom = FakeDom()
        dom.adopt(page, first["ops"])
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        dom.apply(json.loads(ws.receive_text())["ops"])
        assert dom.html() == "<button>n=1</button>"


def test_embed_in_a_django_template():
    with TestClient(wrap(django_app(), Counter, "/widget")) as client:
        check_embedded(client, "/shop")


def test_embed_in_a_flask_template():
    from reaction import embed

    app = flask.Flask("flask_embed_test")

    @app.get("/shop")
    def shop():
        widget = embed(Counter, mount_path="/widget", location=flask.request.path)
        return flask.render_template_string("<h1>{{ title }}</h1>{{ widget | safe }}", title="Shop", widget=widget)

    with TestClient(wrap(app, Counter, "/widget")) as client:
        check_embedded(client, "/shop")


# ---- the {% component %} Jinja tag ------------------------------------------------------


@component
def Greeter(name="you", start=0):
    n, set_n = use_state(start)
    return html.button(f"hi {name} {n} at {use_location().path}", on_click=lambda: set_n(n + 1))


GREETER = f"{__name__}.Greeter"


def tag_page(html_text):
    """The widget's box, its WebSocket path, and the box's id, from a rendered page."""
    import re

    box = re.search(r'<div id="(pyreaction-[0-9a-f]+)">(.*?)</div><script', html_text)
    ws = re.search(r'data-ws="([^"]+)"', html_text).group(1).replace("&amp;", "&")
    return box.group(1), box.group(2), ws


def click_once(client, ws_path, p, page):
    with client.websocket_connect(f"{ws_path}&p={p}&h=1") as ws:
        first = json.loads(ws.receive_text())
        dom = FakeDom()
        dom.adopt(page, first["ops"])
        before = dom.html()
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        dom.apply(json.loads(ws.receive_text())["ops"])
        return before, dom.html()


def test_component_tag_in_fastapi_jinja_templates():
    from fastapi import FastAPI, Request
    from jinja2 import DictLoader, Environment

    from reaction import jinja_components

    env = Environment(loader=DictLoader({"shop.html": (
        '<h1>Shop</h1>{% component "' + GREETER + '", name=who, start=5 %}'
    )}))
    api = FastAPI()
    api.mount("/components", jinja_components(env, [Greeter], secret="s3cret"))

    @api.get("/shop")
    def shop(request: Request):
        from fastapi.responses import HTMLResponse

        return HTMLResponse(env.get_template("shop.html").render(request=request, who="ada"))

    with TestClient(api) as client:
        page = client.get("/shop").text
        assert page.startswith("<h1>Shop</h1>")
        _, box, ws = tag_page(page)
        assert box == "<button>hi ada 5 at /shop</button>", "server-rendered with the template's props"
        assert ws.startswith("/components/_pyreaction/ws?c=")
        assert 'src="/components/_pyreaction/client.js?v=' in page
        assert click_once(client, ws, "/shop", page) == (
            "<button>hi ada 5 at /shop</button>", "<button>hi ada 6 at /shop</button>",
        )


def test_component_tag_in_a_flask_template_through_wrap():
    from reaction import jinja_components

    app = flask.Flask("flask_tag_test")
    components = jinja_components(app.jinja_env, [Greeter], secret="s3cret")

    @app.get("/shop")
    def shop():
        return flask.render_template_string('{% component "' + GREETER + '", name="bo" %}')

    with TestClient(wrap(app, components, "/components")) as client:
        page = client.get("/shop").text
        _, box, ws = tag_page(page)
        assert box == "<button>hi bo 0 at /shop</button>", "the location is Flask's request.path"
        assert click_once(client, ws, "/shop", page)[1] == "<button>hi bo 1 at /shop</button>"


def test_component_tag_only_names_listed_components_and_signed_props():
    import base64

    from jinja2 import Environment

    from reaction import jinja_components

    env = Environment()
    app = jinja_components(env, [Greeter], secret="s3cret")
    with pytest.raises(KeyError, match="not one of the components"):
        env.from_string('{% component "os.system" %}').render()
    with pytest.raises(TypeError):
        env.from_string('{% component "' + GREETER + '", name=bad %}').render(bad=object())

    _, _, ws = tag_page(env.from_string('{% component "' + GREETER + '", name="ada" %}').render())
    token = ws.split("?c=", 1)[1]
    payload, mac = token.rsplit(".", 1)
    forged = base64.urlsafe_b64encode(json.dumps([GREETER, {"name": "eve"}]).encode()).decode()
    with TestClient(app) as client:
        for bad in (f"{forged}.{mac}", f"{payload}.{'0' * 32}", "nonsense", ""):
            with client.websocket_connect(f"/_pyreaction/ws?c={bad}&p=/") as conn:
                ops = json.loads(conn.receive_text())["ops"]
                assert not [op for op in ops if op[0] in ("c", "t")], f"refused, nothing drawn: {bad[:20]}"
        with client.websocket_connect(f"/_pyreaction/ws?c={token}&p=/") as conn:
            assert any(op[0] == "t" and "hi ada" in str(op) for op in json.loads(conn.receive_text())["ops"])
