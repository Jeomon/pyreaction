"""ASGI app: server-rendered first paint, then a WebSocket per page view.

A GET renders the app to HTML and keeps nothing. The page's WebSocket renders it
again from the same URL; when the result matches, the browser adopts the HTML in
place, and otherwise rebuilds it (a render that uses the clock or randomness).
This is how React hydrates, and it means no memory is held between the two
requests and any worker process can serve either one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, HTMLResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from ..core.element import Component, Element
from ..core.reconciler import Root
from ..core.ssr import render_to_string

try:
    import orjson

    def _dumps(data: Any) -> str:
        return orjson.dumps(data).decode()

except ImportError:  # pragma: no cover - depends on the optional extra
    _encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False)
    _dumps = _encoder.encode

__all__ = ["create_app", "run"]

logger = logging.getLogger("pyreaction")

CLIENT_DIR = Path(__file__).parent.parent / "client"
CLIENT_JS = CLIENT_DIR / "client.js"
# $-placeholders rather than str.format, so the HTML and CSS need no brace escaping.
PAGE = Template((CLIENT_DIR / "index.html").read_text(encoding="utf-8"))
STYLE = (CLIENT_DIR / "style.css").read_text(encoding="utf-8")

# Close code for "the browser is not keeping up": it reconnects with a fresh session.
TRY_AGAIN_LATER = 1013
POLICY_VIOLATION = 1008


def _as_factory(app: Component | Element | Callable[[], Element]) -> Callable[[], Element]:
    if isinstance(app, Element):
        return lambda: app
    return app  # type: ignore[return-value]


def _origin_allowed(conn: HTTPConnection, allowed: Sequence[str]) -> bool:
    """Browsers send Origin on every WebSocket; only the page's own site may connect.

    Without this, any website a visitor has open could drive their session.
    """
    origin = conn.headers.get("origin")
    if origin is None:
        return True  # not a browser; it could forge the header anyway
    if "*" in allowed or origin in allowed:
        return True
    return urlsplit(origin).netloc == conn.headers.get("host", "")


def _parse_event(text: str) -> tuple[int, int, str, dict[str, Any]] | None:
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if not (isinstance(message, list) and len(message) == 4):
        return None
    seq, node_id, name, data = message
    if (
        type(seq) is not int
        or type(node_id) is not int
        or not isinstance(name, str)
        or not isinstance(data, dict)
    ):
        return None
    return seq, node_id, name, data


class _Outbox:
    """Messages waiting for a slow socket are merged into one, up to a limit of
    operations; past it the connection is closed and the browser starts over."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.pending: list[dict[str, Any]] = []
        self.size = 0
        self.overflow = False
        self.ready = asyncio.Event()

    def push(self, message: dict[str, Any]) -> None:
        self.pending.append(message)
        self.size += len(message["ops"])
        if self.size > self.limit:
            self.overflow = True
        self.ready.set()

    async def take(self) -> dict[str, Any] | None:
        """The next message to send, or None when the connection must close."""
        await self.ready.wait()
        if self.overflow:
            return None
        if "init" in self.pending[0]:
            message = self.pending.pop(0)  # the first message is never merged
        else:
            message = _merge(self.pending)
            self.pending = []
        self.size -= len(message["ops"])
        if not self.pending:
            self.ready.clear()
        return message


def _merge(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if len(messages) == 1:
        return messages[0]
    merged: dict[str, Any] = {"ops": [op for m in messages for op in m["ops"]], "ack": messages[-1]["ack"]}
    for m in messages:
        if "title" in m:
            merged["title"] = m["title"]
        if "nav" in m:
            prev = merged.get("nav")
            # A push followed by a replace still has to push.
            replace = m["nav"]["replace"] and (prev is None or prev["replace"])
            merged["nav"] = {"to": m["nav"]["to"], "replace": replace}
        if "errors" in m:
            merged.setdefault("errors", []).extend(m["errors"])
    return merged


def create_app(
    app: Component | Element | Callable[[], Element],
    *,
    title: str = "pyreaction",
    head: str = "",
    lang: str = "en",
    static_dir: str | Path | None = None,
    static_path: str = "/static",
    stylesheets: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
    max_pending_ops: int = 100_000,
    prefix: str = "/_pyreaction",
    debug: bool = False,
) -> Starlette:
    """An ASGI app serving ``app`` on every GET path.

    ``app`` is a component (called with no arguments), an element, or a function
    returning one. ``head`` is raw HTML for the page's <head>. ``static_dir`` is
    served at ``static_path``, and each of ``stylesheets`` (paths inside it) is
    linked from the page.

    WebSockets are only accepted from the page's own origin plus
    ``allowed_origins`` (needed behind a proxy that rewrites the Host header;
    ``"*"`` allows any). ``debug`` shows errors in the browser. Mount the result
    inside another ASGI app, or serve it with :func:`run`.
    """
    make = _as_factory(app)

    async def page(request: Request) -> HTMLResponse:
        url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        root = Root(make(), location=url, connection=request, debug=debug)
        try:
            body = render_to_string(root)
            page_title = root.title if root.title is not None else title
        finally:
            root.close()
        root_path = request.scope.get("root_path", "")
        links = "".join(
            f'<link rel="stylesheet" href="{escape(root_path + static_path)}/{escape(name)}">\n'
            for name in stylesheets
        )
        return HTMLResponse(
            PAGE.substitute(
                lang=escape(lang),
                title=escape(page_title),
                style=STYLE,
                head=links + head,
                body=body,
                base=escape(root_path + prefix),
            )
        )

    async def client_js(request: Request) -> FileResponse:
        return FileResponse(CLIENT_JS, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    async def socket(ws: WebSocket) -> None:
        if not _origin_allowed(ws, allowed_origins):
            logger.warning("Refused a WebSocket from origin %s", ws.headers.get("origin"))
            await ws.close(code=POLICY_VIOLATION)
            return
        await ws.accept()
        url = ws.query_params.get("p", "/")
        if not url.startswith("/"):
            url = "/"
        outbox = _Outbox(max_pending_ops)
        root: Root | None = None

        async def writer() -> None:
            while (message := await outbox.take()) is not None:
                await ws.send_text(_dumps(message))
            logger.warning("Closing a WebSocket that fell %d operations behind", outbox.size)
            await ws.close(code=TRY_AGAIN_LATER)

        writing = asyncio.create_task(writer())
        try:
            root = Root(make(), location=url, connection=ws, debug=debug)
            root.attach(outbox.push, hydrate=ws.query_params.get("h") == "1")
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                event = _parse_event(text) if text is not None else None
                if event is None:
                    logger.warning("Ignored a malformed message on a WebSocket")
                    continue
                root.dispatch(*event)
        finally:
            if root is not None:
                root.close()
            writing.cancel()

    routes: list[Any] = [
        Route(prefix + "/client.js", client_js),
        WebSocketRoute(prefix + "/ws", socket),
    ]
    if static_dir is not None:
        routes.append(Mount(static_path, StaticFiles(directory=static_dir), name="static"))
    routes += [Route("/", page), Route("/{path:path}", page)]
    return Starlette(debug=debug, routes=routes)


def run(
    app: Component | Element | Callable[[], Element],
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "info",
    **options: Any,
) -> None:
    """Serve ``app`` with uvicorn until interrupted. ``options`` go to :func:`create_app`."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    uvicorn.run(create_app(app, **options), host=host, port=port, log_level=log_level)
