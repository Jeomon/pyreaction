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

        urls = type("urls", (), {"urlpatterns": [path("api/hello", hello), path("api/submit", submit)]})
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
        assert 'src="/ui/_pyreaction/client.js"' in page.text and 'data-base="/ui"' in page.text
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
