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
import tempfile
from collections.abc import Callable, Sequence
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from ..core.element import Component, Element
from ..core.reconciler import Root, UploadedFile
from ..core.ssr import render_to_string

try:
    import orjson

    def _dumps(data: Any) -> str:
        return orjson.dumps(data).decode()

except ImportError:  # pragma: no cover - depends on the optional extra
    _encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False)

    def _dumps(data: Any) -> str:
        return _encoder.encode(data)

__all__ = ["create_app", "embed", "run"]

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
    return app


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


DEFAULT_MAX_UPLOAD = 10 * 1024 * 1024
_MAX_HEADER = 64 * 1024
# The browser sends files in chunks of this size; frames stay far below servers'
# WebSocket message limits (uvicorn: 16 MiB) whatever max_upload_size is.
UPLOAD_CHUNK = 256 * 1024
# Received files are kept in memory up to this size, then on disk.
_SPOOL_IN_MEMORY = 1024 * 1024
# Files a connection may have in transfer, or waiting for their event, at once.
_MAX_OPEN_UPLOADS = 16


def _parse_frame(frame: bytes) -> tuple[dict[str, Any], bytes] | None:
    """A binary frame from the browser: 4-byte header length, JSON header, bytes."""
    if len(frame) < 4:
        return None
    n = int.from_bytes(frame[:4], "big")
    if n > _MAX_HEADER or len(frame) < 4 + n:
        return None
    try:
        header = json.loads(frame[4 : 4 + n])
    except ValueError:
        return None
    if not isinstance(header, dict) or not isinstance(header.get("u"), str):
        return None
    data = frame[4 + n :]
    if len(data) > UPLOAD_CHUNK:
        return None
    return header, data


class _Uploads:
    """Files arriving in chunks on one connection, assembled in spooled temp files."""

    def __init__(self, root: Root, max_size: int) -> None:
        self.root = root
        self.max_size = max_size
        self.open: dict[str, tuple[Any, int, str, str]] = {}  # id -> (file, next part, name, type)

    def receive(self, frame: bytes) -> bool:
        """Take one frame; False when it is refused (and that file is dropped)."""
        parsed = _parse_frame(frame)
        if parsed is None:
            return False
        header, data = parsed
        upload_id = header["u"]
        part = header.get("part", 0)
        entry = self.open.get(upload_id)
        if entry is None:
            if part != 0 or len(self.open) + len(self.root.uploads) >= _MAX_OPEN_UPLOADS:
                return False
            name, type_ = header.get("name"), header.get("type")
            entry = (
                tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY),
                0,
                name if isinstance(name, str) else "",
                type_ if isinstance(type_, str) else "",
            )
        file, expected, name, type_ = entry
        if part != expected or file.tell() + len(data) > self.max_size:
            file.close()
            self.open.pop(upload_id, None)
            return False
        file.write(data)
        if header.get("last", True):
            self.open.pop(upload_id, None)
            size = file.tell()
            file.seek(0)
            self.root.uploads[upload_id] = UploadedFile(name, type_, size=size, file=file)
        else:
            self.open[upload_id] = (file, expected + 1, name, type_)
        return True

    def close(self) -> None:
        for file, *_ in self.open.values():
            file.close()
        self.open.clear()

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
    max_upload_size: int = DEFAULT_MAX_UPLOAD,
    prefix: str = "/_pyreaction",
    data_timeout: float = 5.0,
    routes: Sequence[Any] = (),
    on_error: Callable[[BaseException, str, Any], None] | None = None,
    threaded: bool = True,
    reconnect_max: int = 8000,
    debug: bool = False,
) -> Starlette:
    """An ASGI app serving ``app`` on every GET path.

    ``app`` is a component (called with no arguments), an element, or a function
    returning one. ``head`` is raw HTML for the page's <head>. ``static_dir`` is
    served at ``static_path``, and each of ``stylesheets`` (paths inside it) is
    linked from the page.

    WebSockets are only accepted from the page's own origin plus
    ``allowed_origins`` (needed behind a proxy that rewrites the Host header;
    ``"*"`` allows any). ``max_upload_size`` (bytes) caps each uploaded file.

    The first HTML waits up to ``data_timeout`` seconds for data that
    ``use_async``/``use_data`` load, so it arrives with the data in it.
    ``routes`` are extra Starlette routes (a login POST endpoint, an API), matched
    before the page. ``on_error(exc, where, connection)`` is called for every
    error not caught by an ErrorBoundary, for Sentry-style reporting. With
    ``threaded`` (default) synchronous handlers and effects run in worker
    threads. After a dropped connection the page retries with growing delays of
    at most ``reconnect_max`` milliseconds. ``debug`` shows errors in the browser.
    """
    make = _as_factory(app)

    def new_root(url: str, connection: Any) -> Root:
        root = Root(make(), location=url, connection=connection, debug=debug, threaded=threaded)
        if on_error is not None:
            root.on_error = lambda exc, where: on_error(exc, where, connection)
        return root

    async def page(request: Request) -> Response:
        url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        root = new_root(url, request)
        try:
            if not await root.settle(data_timeout):
                logger.warning("Rendering %s before its data loaded (data_timeout=%ss)", url, data_timeout)
            if root.redirect is not None:
                return RedirectResponse(root.redirect, status_code=307)
            body = render_to_string(root)
            page_title = root.title if root.title is not None else title
            head_tags = root.head_html()
            status = root.status
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
                head=links + head + head_tags,
                body=body,
                base=escape(root_path + prefix),
                reconnect_max=int(reconnect_max),
            ),
            status_code=status,
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
        uploads: _Uploads | None = None

        async def writer() -> None:
            while (message := await outbox.take()) is not None:
                await ws.send_text(_dumps(message))
            logger.warning("Closing a WebSocket that fell %d operations behind", outbox.size)
            await ws.close(code=TRY_AGAIN_LATER)

        writing = asyncio.create_task(writer())
        try:
            root = new_root(url, ws)

            def send(message: dict[str, Any]) -> None:
                if "init" in message:
                    message["maxUpload"] = max_upload_size  # the browser checks sizes first
                outbox.push(message)

            # Load data as the page's HTML did, so this render matches it and the
            # browser can adopt the HTML instead of rebuilding it.
            await root.settle(data_timeout)
            root.attach(send, hydrate=ws.query_params.get("h") == "1")
            uploads = _Uploads(root, max_upload_size)
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                frame = message.get("bytes")
                if frame is not None:
                    if not uploads.receive(frame):
                        logger.warning("Refused a file upload on a WebSocket")
                    continue
                text = message.get("text")
                event = _parse_event(text) if text is not None else None
                if event is None:
                    logger.warning("Ignored a malformed message on a WebSocket")
                    continue
                root.dispatch(*event)
        finally:
            if uploads is not None:
                uploads.close()
            if root is not None:
                for upload in root.uploads.values():
                    upload.file.close()
                root.close()
            writing.cancel()

    table: list[Any] = [
        Route(prefix + "/client.js", client_js),
        WebSocketRoute(prefix + "/ws", socket),
        *routes,
    ]
    if static_dir is not None:
        table.append(Mount(static_path, StaticFiles(directory=static_dir), name="static"))
    table += [Route("/", page), Route("/{path:path}", page)]
    return Starlette(debug=debug, routes=table)


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


def embed(
    app: Component | Element | Callable[[], Element] | None = None,
    *,
    mount_path: str = "",
    location: str = "/",
    element_id: str = "pyreaction-root",
    prefix: str = "/_pyreaction",
) -> str:
    """HTML that puts a pyreaction app inside an existing page (Jinja, Django, plain HTML).

    Mount the app's ASGI app at ``mount_path`` in your server
    (``api.mount("/ui", create_app(Widget))``), then place
    ``embed(Widget, mount_path="/ui", location=request.url.path)`` in the page,
    unescaped (Jinja: ``| safe``). With ``app`` the snippet carries the
    server-rendered HTML, adopted once live; without it the box fills in when the
    page connects. ``location`` is the URL the app sees, normally the host page's
    path. Use a different ``element_id`` for each app on a page.
    """
    body = ""
    if app is not None:
        root = Root(_as_factory(app)(), location=location)
        try:
            body = render_to_string(root)
        finally:
            root.close()
    base = escape(mount_path.rstrip("/") + prefix)
    return (
        f'<div id="{escape(element_id)}">{body}</div>'
        f'<script src="{base}/client.js" data-ws="{base}/ws" data-mount="{escape(element_id)}"></script>'
    )
