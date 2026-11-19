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
    assert f'src="{base}/_pyreaction/client.js?v=' in home and f'data-ws="{base}/_pyreaction/ws"' in home
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
        page = client.get(f"{base}/users/7").text
        with client.websocket_connect(f"{base}/_pyreaction/ws?p={p}&b={base}&h=1") as ws:
            dom = FakeDom()
            dom.adopt(page, json.loads(ws.receive_text())["ops"])
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


# ---- HTTPException in components ------------------------------------------------------


def test_http_exception_sets_status_headers_and_the_boundary_shows_it():
    from fastapi import HTTPException

    from reaction import ErrorBoundary

    @component
    def Guarded():
        if "user" not in use_request().cookies:
            raise HTTPException(401, "sign in first", headers={"WWW-Authenticate": "Cookie"})
        return html.p("secret")

    @component
    def Shop():
        return html.main(
            html.h1("Shop"),
            ErrorBoundary(Guarded(), fallback=lambda e, reset: html.p(f"{e.status_code}: {e.detail}", id="err")),
        )

    api = FastAPI()
    mount(api, Shop, "/shop")
    mount(api, Guarded, "/bare")
    client = TestClient(api)

    page = client.get("/shop/")
    assert page.status_code == 401 and page.headers["www-authenticate"] == "Cookie"
    assert "<h1>Shop</h1>" in page.text and 'id="err">401: sign in first<' in page.text
    assert client.get("/bare/").status_code == 401, "without a boundary too"
    assert "secret" in TestClient(api, cookies={"user": "ada"}).get("/bare/").text


def test_http_exception_redirect_is_sent_as_written():
    from fastapi import HTTPException

    @component
    def ToLogin():
        raise HTTPException(303, headers={"Location": "/login"})

    @component
    def Old():
        return Navigate("/new")

    api = FastAPI()
    mount(api, ToLogin, "/a")
    mount(api, Old, "/b")
    client = TestClient(api)
    to_login = client.get("/a/", follow_redirects=False)
    assert (to_login.status_code, to_login.headers["location"]) == (303, "/login"), "a site URL"
    old = client.get("/b/", follow_redirects=False)
    assert (old.status_code, old.headers["location"]) == (307, "/b/new"), "Navigate stays inside the app"


def test_http_exception_once_live_goes_to_the_boundary_only():
    from fastapi import HTTPException

    from reaction import ErrorBoundary, use_state

    @component
    def Item():
        n, set_n = use_state(0)
        if n:
            raise HTTPException(404, "gone")
        return html.button("open", on_click=lambda: set_n(1))

    @component
    def Page():
        return ErrorBoundary(Item(), fallback=lambda e, reset: html.p(f"{e.status_code} {e.detail}"))

    api = FastAPI()
    mount(api, Page)
    client = TestClient(api)
    with client.websocket_connect("/_pyreaction/ws?p=/") as ws:
        first = json.loads(ws.receive_text())
        dom = FakeDom()
        dom.apply(first["ops"])
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        while "404 gone" not in dom.html():
            dom.apply(json.loads(ws.receive_text())["ops"])
        assert dom.html() == "<p>404 gone</p>"


# ---- static files shadowed by the host ----------------------------------------------------


def test_warns_when_the_host_serves_the_same_static_path(tmp_path, caplog):
    from fastapi.staticfiles import StaticFiles

    (tmp_path / "mine").mkdir()
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "app.css").write_text("y")

    def build(**options):
        api = FastAPI()
        api.mount("/static", StaticFiles(directory=tmp_path / "mine"))
        mount(api, App, static_dir=tmp_path / "pub", **options)
        return TestClient(api)

    logging.disable(logging.NOTSET)
    with caplog.at_level(logging.WARNING, logger="reaction"):
        client = build()
        client.get("/")
        client.get("/again")
    warnings = [r.getMessage() for r in caplog.records if "answers /static/" in r.getMessage()]
    assert len(warnings) == 1 and 'static_path="/assets"' in warnings[0]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="reaction"):
        fixed = build(static_path="/assets")
        assert fixed.get("/assets/app.css").text == "y"
    assert not [r for r in caplog.records if "answers" in r.getMessage()]


def test_no_warning_when_mounted_under_a_path(tmp_path, caplog):
    from fastapi.staticfiles import StaticFiles

    (tmp_path / "mine").mkdir()
    (tmp_path / "pub").mkdir()
    api = FastAPI()
    api.mount("/static", StaticFiles(directory=tmp_path / "mine"))
    mount(api, App, "/ui", static_dir=tmp_path / "pub")
    logging.disable(logging.NOTSET)
    with caplog.at_level(logging.WARNING, logger="reaction"):
        TestClient(api).get("/ui/")
    assert not [r for r in caplog.records if "answers" in r.getMessage()]


# ---- @page: FastAPI routes with Depends ---------------------------------------------------


def _page_api(log):
    from typing import Annotated

    from fastapi import Cookie, Depends, HTTPException
    from starlette.requests import HTTPConnection

    from reaction import page, use_state

    def current_user(conn: HTTPConnection):
        user = conn.cookies.get("user")
        if not user:
            raise HTTPException(401, "sign in")
        return user

    def get_db():
        log.append("open")
        yield {"7": "Pizza", "8": "Sushi"}
        log.append("close")

    @component
    def OrderPage(order, user):
        n, set_n = use_state(0)
        return html.div(
            html.p(f"{user} ordered {order} at {use_location().path}"),
            html.button(f"n={n}", on_click=lambda: set_n(n + 1)),
        )

    api = FastAPI()

    @api.get("/api/ping")
    def ping():
        return {"ok": True}

    @page(api, "/orders/{order_id}", title="Order")
    def order(order_id: str, user: str = Depends(current_user), db=Depends(get_db)):
        if order_id not in db:
            raise HTTPException(404, "no such order")
        return OrderPage(order=db[order_id], user=user)

    @page(api, "/hello")
    async def hello(name: Annotated[str | None, Cookie()] = None):
        return html.p(f"hello {name}")

    return api


def test_page_renders_with_dependencies_and_path_params():
    log = []
    api = _page_api(log)
    client = TestClient(api, cookies={"user": "ada", "name": "bob"})
    body = client.get("/orders/7").text
    assert "<p>ada ordered Pizza at /orders/7</p>" in body and "<title>Order</title>" in body
    assert 'src="/orders/7/_pyreaction/client.js?v=' in body and 'data-ws="/orders/7/_pyreaction/ws"' in body
    assert 'data-page="/orders/7"' in body
    assert client.get("/orders/7/_pyreaction/client.js").status_code == 200
    assert log == ["open", "close"], "a yield dependency closes after the response"
    assert "<p>hello bob</p>" in client.get("/hello").text, "async function, Cookie parameter"
    assert list(client.get("/openapi.json").json()["paths"]) == ["/api/ping"], "pages stay out of the schema"


def test_page_errors_come_from_dependencies_and_the_function():
    api = _page_api([])
    assert TestClient(api).get("/orders/7").status_code == 401
    assert TestClient(api, cookies={"user": "ada"}).get("/orders/9").status_code == 404


def test_page_live_connection_resolves_the_dependencies_again():
    log = []
    api = _page_api(log)
    client = TestClient(api, cookies={"user": "ada"})
    with client.websocket_connect("/orders/7/_pyreaction/ws?p=/orders/7") as ws:
        first = json.loads(ws.receive_text())
        dom = FakeDom()
        dom.apply(first["ops"])
        assert dom.html() == "<div><p>ada ordered Pizza at /orders/7</p><button>n=0</button></div>"
        assert log == ["open"], "the yield dependency stays open while the page is live"
        button = next(op[1] for op in first["ops"] if op[0] == "c" and op[2] == "button")
        ws.send_text(json.dumps([1, button, "click", {}]))
        dom.apply(json.loads(ws.receive_text())["ops"])
        assert "<button>n=1</button>" in dom.html()
    assert log == ["open", "close"]
    with pytest.raises(Exception):
        with TestClient(api).websocket_connect("/orders/7/_pyreaction/ws?p=/orders/7") as ws:
            ws.receive_text()


def test_page_rejects_static_dir_and_non_elements():
    from reaction import page

    api = FastAPI()
    with pytest.raises(TypeError, match="static"):
        page(api, "/x", static_dir="public")

    @page(api, "/bad")
    def bad():
        return "not an element"

    with pytest.raises(TypeError, match="must return an element"):
        TestClient(api).get("/bad")
