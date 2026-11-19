"""Login with an HttpOnly session cookie.

A WebSocket cannot set cookies, and JavaScript must not be able to read a session
cookie, so login and logout are ordinary HTTP routes. Components only read the
cookie, through use_request(). After logging in or out the page reloads, so the
next WebSocket carries the new cookie.

    uv run python examples/login.py      # then sign in as ada / lovelace
"""

import hmac
import secrets
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import Route

from pyreaction import component, html, run, use_request

USERS = {"ada": "lovelace"}  # use a real user store and password hashing
SESSIONS: dict[str, str] = {}  # session token -> user name; use a database or Redis
COOKIE = "session"


async def form_fields(request: Request) -> dict[str, str]:
    # A plain HTML form posts URL-encoded fields; the standard library reads them,
    # so no extra package (python-multipart) is needed.
    fields = parse_qs((await request.body()).decode())
    return {k: v[0] for k, v in fields.items()}


async def login(request: Request) -> RedirectResponse:
    form = await form_fields(request)
    name, password = form.get("name", ""), form.get("password", "")
    expected = USERS.get(name)
    if expected is None or not hmac.compare_digest(expected, password):
        return RedirectResponse("/?error=1", status_code=303)
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = name
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return response


async def logout(request: Request) -> RedirectResponse:
    SESSIONS.pop(request.cookies.get(COOKIE, ""), None)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response


def current_user() -> str | None:
    """The signed-in user for this page, from its session cookie."""
    request = use_request()
    return SESSIONS.get(request.cookies.get(COOKIE, "")) if request is not None else None


@component
def App():
    user = current_user()
    if user is None:
        # A plain HTML form posting to the login route: no on_submit, so the browser
        # submits it normally and the response's Set-Cookie takes effect.
        return html.form(
            html.h1("Sign in"),
            html.input(name="name", placeholder="name", autocomplete="username"),
            html.input(name="password", type="password", placeholder="password", autocomplete="current-password"),
            html.button("Sign in", type="submit"),
            method="post",
            action="/login",
        )
    return html.main(
        html.h1(f"Hello, {user}"),
        html.form(html.button("Sign out", type="submit"), method="post", action="/logout"),
    )


routes = [Route("/login", login, methods=["POST"]), Route("/logout", logout, methods=["POST"])]

if __name__ == "__main__":
    run(App, title="pyreaction login", routes=routes)
