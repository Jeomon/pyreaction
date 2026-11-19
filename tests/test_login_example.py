"""The login example: HttpOnly cookie set by a plain route, read by components."""

import json
import runpy
from pathlib import Path

from starlette.testclient import TestClient

from pyreaction import create_app

EXAMPLE = Path(__file__).parent.parent / "examples" / "login.py"


def test_login_sets_httponly_cookie_that_components_read():
    m = runpy.run_path(str(EXAMPLE))
    client = TestClient(create_app(m["App"], routes=m["routes"]))
    assert "Sign in" in client.get("/").text

    bad = client.post("/login", data={"name": "ada", "password": "nope"}, follow_redirects=False)
    assert bad.status_code == 303 and "set-cookie" not in bad.headers

    good = client.post("/login", data={"name": "ada", "password": "lovelace"}, follow_redirects=False)
    assert good.status_code == 303 and "httponly" in good.headers["set-cookie"].lower()
    assert "Hello, ada" in client.get("/").text
    with client.websocket_connect("/_pyreaction/ws?p=/") as ws:
        texts = [op[2] for op in json.loads(ws.receive_text())["ops"] if op[0] == "t"]
        assert texts[0] == "Hello, ada"

    client.post("/logout")
    assert "Sign in" in client.get("/").text


def test_navigate_reload_reaches_the_browser():
    from pyreaction import component, html, use_navigate
    from pyreaction.testing import render

    @component
    def App():
        navigate = use_navigate()
        return html.button("out", on_click=lambda: navigate("/logged-out", reload=True))

    with render(App()) as page:
        sent = []
        page.root._sink = lambda m: sent.append(m)
        page.click("button")
        assert sent[-1]["nav"] == {"to": "/logged-out", "replace": False, "reload": True}
