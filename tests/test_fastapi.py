"""pyreaction inside FastAPI: at the root next to API routes, and under a path."""

import json
import logging
from contextlib import asynccontextmanager

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from reaction import (  # noqa: E402
    Link,
    Navigate,
    Router,
    component,
    create_app,
    html,
    mount,
    preload,
    use_location,
    use_request,
)

from .fakedom import FakeDom  # noqa: E402


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@component
def Home():
    preload("/static/hero.jpg", as_="image")
    return html.div(html.p(f"path={use_location().path}", id="path"), Link("user 7", to="/users/7"))


@component
def User(id):
    return html.p(f"user {id}")


@component
def App():
    return Router({"/": Home, "/users/{id}": User, "/old": lambda: Navigate("/users/1")})


def make_api(path, static_dir=None):
    api = FastAPI()

    @api.get("/api/hello")
    def hello():
        return {"hello": "world"}

    mount(api, App, path, static_dir=static_dir)
    return api


@pytest.mark.parametrize("base", ["", "/ui"])
def test_pages_routing_links_redirects_and_assets(base, tmp_path):
    (tmp_path / "hero.jpg").write_bytes(b"jpg")
    client = TestClient(make_api(base or "/", static_dir=tmp_path))
    assert client.get("/api/hello").json() == {"hello": "world"}, "API routes keep working"

    home = client.get(f"{base}/").text
    assert 'id="path">path=/<' in home, "components see paths inside the app"
    assert f'href="{base}/users/7"' in home, "links get the mount prefix"
    assert f'<link rel="preload" href="{base}/static/hero.jpg"' in home
    assert f'src="{base}/_pyreaction/client.js"' in home and f'data-ws="{base}/_pyreaction/ws"' in home
    assert f'data-base="{base}"' in home
    assert client.get(f"{base}/_pyreaction/client.js").status_code == 200
    assert client.get(f"{base}/static/hero.jpg").content == b"jpg"

    assert "user 7" in client.get(f"{base}/users/7").text
    redirect = client.get(f"{base}/old", follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"] == f"{base}/users/1"
    assert client.get(f"{base}/nope").status_code == 404


@pytest.mark.parametrize("base", ["", "/ui"])
def test_live_page_under_the_mount(base):
    client = TestClient(make_api(base or "/"))
    # The browser sends the path inside the app and the base; an older or embedded
    # page may send the full path, which works too.
    for p in ("/users/7", f"{base}/users/7"):
        with client.websocket_connect(f"{base}/_pyreaction/ws?p={p}&b={base}&h=1") as ws:
            dom = FakeDom()
            dom.apply(json.loads(ws.receive_text())["ops"])
            assert dom.html() == "<p>user 7</p>"

    with client.websocket_connect(f"{base}/_pyreaction/ws?p=/&b={base}") as ws:
        first = json.loads(ws.receive_text())
        ws.send_text(json.dumps([1, 0, "#navigate", {"to": "/users/3"}]))
        dom = FakeDom()
        dom.apply(first["ops"])
        dom.apply(json.loads(ws.receive_text())["ops"])
        assert dom.html() == "<p>user 3</p>"


def test_mount_at_root_works_before_or_after_the_routes():
    api = FastAPI()
    mount(api, App)  # before the route is defined

    @api.get("/api/hello")
    def hello():
        return {"hello": "world"}

    client = TestClient(api)
    assert client.get("/api/hello").json() == {"hello": "world"}
    assert client.get("/docs").status_code == 200, "FastAPI's own pages too"
    assert "path=/" in client.get("/").text


def test_components_reach_the_host_app_state_lifespan_and_session():
    @asynccontextmanager
    async def lifespan(app):
        yield {"pool": "the-pool"}

    api = FastAPI(lifespan=lifespan)
    api.state.db = "the-db"
    api.add_middleware(SessionMiddleware, secret_key="test")

    @api.post("/login")
    def login(request: Request):
        request.session["user"] = "ada"
        return {"ok": True}

    @component
    def Profile():
        r = use_request()
        return html.p(f"{type(r.app).__name__} {r.app.state.db} {r.state.pool} {r.session.get('user')}")

    mount(api, Profile, "/app")
    with TestClient(api) as client:
        client.post("/login")
        assert "<p>FastAPI the-db the-pool ada</p>" in client.get("/app/").text
        with client.websocket_connect("/app/_pyreaction/ws?p=/&b=/app") as ws:
            ops = json.loads(ws.receive_text())["ops"]
            assert ["t", ops[1][1], "FastAPI the-db the-pool ada"] in ops


def test_create_app_on_its_own_is_unchanged():
    client = TestClient(create_app(App))
    assert 'data-base=""' in client.get("/").text
    assert client.get("/old", follow_redirects=False).headers["location"] == "/users/1"
