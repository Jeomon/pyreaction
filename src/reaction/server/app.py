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
import re
import secrets
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from ..core.element import Component, Element
from ..core.reconciler import Root, UploadedFile
from ..core.ssr import render_children, render_portals, render_to_string

try:
    import orjson

    def _dumps(data: Any) -> str:
        return orjson.dumps(data).decode()

except ImportError:  # pragma: no cover - depends on the optional extra
    _encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False)

    def _dumps(data: Any) -> str:
        return _encoder.encode(data)

__all__ = ["create_app", "embed", "mount", "prerender", "run"]

logger = logging.getLogger("reaction")

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


# Where the page template is cut for streaming: the shell goes first, Suspense
# chunks next, and the client script last, once the page is complete.
_STREAM_SPLIT = "<!--pyreaction-stream-->"

# Replaces the content between a boundary's <!--pr-s:N--> markers with its chunk.
_SWAP = (
    "<script>function __prSwap(n){var t=document.getElementById('pr-c'+n),"
    "w=document.createTreeWalker(document.body,128),s,e,c;"
    "while(c=w.nextNode()){if(c.data==='pr-s:'+n)s=c;else if(c.data==='/pr-s:'+n){e=c;break}}"
    "if(!t||!s||!e)return;while(s.nextSibling&&s.nextSibling!==e)s.nextSibling.remove();"
    "e.parentNode.insertBefore(t.content,e);t.remove()}</script>\n"
)


async def _stream_page(
    root: Root, parts: list[str], boundaries: dict[Any, int], waiting: set[int], deadline: float
) -> AsyncIterator[str]:
    """The page in pieces: the shell with Suspense fallbacks at once, then each
    boundary's content as its data arrives, then the client script."""
    loop = asyncio.get_running_loop()
    try:
        yield parts[0] + _SWAP
        while waiting:
            remaining = deadline - loop.time()
            pending = set(root._pending)
            if remaining <= 0 or not pending:
                break
            await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)  # let the loaded data's state updates land
            root._render_dirty()
            by_number = {n: node for node, n in boundaries.items()}
            ready = sorted(n for n in waiting if by_number[n].mounted and not by_number[n].waiting)
            done: set[Any] = set()
            chunks = []
            for n in ready:
                node = by_number[n]
                waiting.discard(n)
                parent = node.parent
                while parent is not None and parent not in done:
                    parent = parent.parent
                if parent is not None:
                    continue  # an outer boundary's chunk already carries this one
                done.add(node)
                html = render_children(node.children, boundaries)
                chunks.append(
                    f'<template id="pr-c{n}">{html}</template>'
                    f"<script>__prSwap({n});document.currentScript.remove()</script>\n"
                )
            # Boundaries that appear only inside the new content, still waiting.
            waiting |= {n for node, n in boundaries.items() if node.waiting and node.mounted} - {
                boundaries[d] for d in done
            }
            if chunks:
                yield "".join(chunks)
        yield parts[1]
    finally:
        root.close()


def _app_path(scope: Any) -> tuple[str, str]:
    """(where the app is mounted, the path within it). Under ``Mount("/ui", ...)``
    a request for /ui/users/7 is ("/ui", "/users/7")."""
    base = scope.get("root_path", "").rstrip("/")
    path = scope.get("path", "/")
    if base and (path == base or path.startswith(base + "/")):
        path = path[len(base):] or "/"
    return base, path


class _MountableApp(Starlette):
    """A Starlette app that, when mounted in another (FastAPI, Starlette), leaves
    ``request.app`` as that host app, so components reach its ``app.state``."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if "app" not in scope:
            scope["app"] = self
        if self.middleware_stack is None:
            self.middleware_stack = self.build_middleware_stack()
        await self.middleware_stack(scope, receive, send)


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
    stream: bool = True,
    routes: Sequence[Any] = (),
    on_error: Callable[[BaseException, str, Any], None] | None = None,
    threaded: bool = True,
    reconnect_max: int = 8000,
    resume_timeout: float = 30.0,
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
    ``use_async``/``use_data`` load, so it arrives with the data in it. With
    ``stream`` (the default), data a ``Suspense`` boundary waits for does not hold
    the page back: it is sent at once with the fallback, and the boundary's
    content follows in the same response when its data arrives.
    ``routes`` are extra Starlette routes (a login POST endpoint, an API), matched
    before the page. ``on_error(exc, where, connection)`` is called for every
    error not caught by an ErrorBoundary, for Sentry-style reporting. With
    ``threaded`` (default) synchronous handlers and effects run in worker
    threads. After a dropped connection the page retries with growing delays of
    at most ``reconnect_max`` milliseconds. The server keeps the dropped page's
    components for ``resume_timeout`` seconds (0: not at all), so a reconnect within
    that time continues with their state; later, or on another worker process or
    after a restart, the page renders afresh. ``debug`` shows errors in the browser.
    """
    make = _as_factory(app)

    def new_root(url: str, connection: Any, base: str = "") -> Root:
        def report(exc: BaseException, where: str) -> None:
            if on_error is not None:
                on_error(exc, where, root.connection)

        root = Root(
            make(), location=url, connection=connection, debug=debug, threaded=threaded, on_error=report, base=base
        )
        return root

    # Pages whose connection dropped, kept for a reconnect: id -> (root, expiry timer).
    parked: dict[str, tuple[Root, asyncio.TimerHandle]] = {}

    def discard(root: Root) -> None:
        for upload in root.uploads.values():
            upload.file.close()
        root.close()

    def expire(sid: str) -> None:
        entry = parked.pop(sid, None)
        if entry is not None:
            discard(entry[0])

    async def page(request: Request) -> Response:
        base, path = _app_path(request.scope)
        url = path + (f"?{request.url.query}" if request.url.query else "")
        root = new_root(url, request, base)
        handed_off = False
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + data_timeout
            # With streaming, data a Suspense boundary waits for is sent later.
            if not await root.settle(data_timeout, suspense=not stream):
                logger.warning("Rendering %s before its data loaded (data_timeout=%ss)", url, data_timeout)
            if root.redirect is not None:
                return RedirectResponse(root.location.href(root.redirect), status_code=307)
            boundaries: dict[Any, int] = {}
            body = render_to_string(root, boundaries if stream else None)
            portals = render_portals(root, boundaries if stream else None)
            page_title = root.title if root.title is not None else title
            head_tags = root.head_html()
            status = root.status
            root_path = request.scope.get("root_path", "")
            links = "".join(
                f'<link rel="stylesheet" href="{escape(root_path + static_path)}/{escape(name)}">\n'
                for name in stylesheets
            )
            parts = PAGE.substitute(
                lang=escape(lang),
                title=escape(page_title),
                style=STYLE,
                head=links + head + head_tags,
                body=body,
                portals=portals,
                stream=_STREAM_SPLIT,
                base=escape(root_path + prefix),
                app_base=escape(base),
                reconnect_max=int(reconnect_max),
            ).split(_STREAM_SPLIT)
            waiting = {n for node, n in boundaries.items() if node.waiting}
            if not waiting:
                return HTMLResponse(parts[0] + parts[1], status_code=status)
            handed_off = True
            return StreamingResponse(
                _stream_page(root, parts, boundaries, waiting, deadline),
                media_type="text/html",
                status_code=status,
            )
        finally:
            if not handed_off:
                root.close()

    async def client_js(request: Request) -> FileResponse:
        return FileResponse(CLIENT_JS, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    async def socket(ws: WebSocket) -> None:
        if not _origin_allowed(ws, allowed_origins):
            logger.warning("Refused a WebSocket from origin %s", ws.headers.get("origin"))
            await ws.close(code=POLICY_VIOLATION)
            return
        await ws.accept()
        url = ws.query_params.get("p", "/")
        if not url.startswith("/") or url.startswith("//"):
            url = "/"
        # The page says where the app is mounted (not the socket's own mount: an
        # embedded app sees the host page's URL).
        base = ws.query_params.get("b", "").rstrip("/")
        if base and (not base.startswith("/") or base.startswith("//")):
            base = ""
        if base and (url == base or url.startswith(base + "/") or url.startswith(base + "?")):
            url = url[len(base):] or "/"
            if url.startswith("?"):
                url = "/" + url
        outbox = _Outbox(max_pending_ops)
        root: Root | None = None
        uploads: _Uploads | None = None
        resumed = parked.pop(ws.query_params.get("s", ""), None)
        sid = secrets.token_urlsafe(18)

        async def writer() -> None:
            while (message := await outbox.take()) is not None:
                await ws.send_text(_dumps(message))
            logger.warning("Closing a WebSocket that fell %d operations behind", outbox.size)
            await ws.close(code=TRY_AGAIN_LATER)

        writing = asyncio.create_task(writer())
        try:

            def send(message: dict[str, Any]) -> None:
                if "init" in message:
                    message["maxUpload"] = max_upload_size  # the browser checks sizes first
                    message["sid"] = sid
                    if resumed is not None:
                        message["resumed"] = True
                outbox.push(message)

            if resumed is not None:
                root, timer = resumed
                timer.cancel()
                root.connection = ws
                root.set_location(url)
                root.attach(send, hydrate=True)
            else:
                root = new_root(url, ws, base)
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
            writing.cancel()
            if uploads is not None:
                uploads.close()  # files half-sent on this connection
            if root is not None:
                if resume_timeout > 0 and root._sink is not None:
                    root.detach()
                    timer = asyncio.get_running_loop().call_later(resume_timeout, expire, sid)
                    parked[sid] = (root, timer)
                else:
                    discard(root)

    table: list[Any] = [
        Route(prefix + "/client.js", client_js),
        WebSocketRoute(prefix + "/ws", socket),
        *routes,
    ]
    if static_dir is not None:
        table.append(Mount(static_path, StaticFiles(directory=static_dir), name="static"))
    table += [Route("/", page), Route("/{path:path}", page)]
    return _MountableApp(debug=debug, routes=table)


def mount(
    host: Any,
    app: Component | Element | Callable[[], Element],
    path: str = "/",
    **options: Any,
) -> Starlette:
    """Serve ``app`` inside an existing FastAPI or Starlette app ``host``, at ``path``.

    At ``"/"`` the pages take every URL the host's own routes do not, whatever order
    the routes are defined in, so ``/api/...`` keeps working. Elsewhere
    (``mount(api, App, "/ui")``) it is a normal sub-app: the components see paths
    inside it (``/users/7`` for ``/ui/users/7``) and links, redirects and static
    files get the prefix. Components reach the host through ``use_request()``:
    ``.app.state``, ``.state`` (lifespan state), ``.session``, ``.cookies``.
    ``options`` go to :func:`create_app`. Returns the pyreaction ASGI app.
    """
    asgi = create_app(app, **options)
    if path.rstrip("/") == "":
        router = getattr(host, "router", host)
        if not hasattr(router, "default"):
            raise TypeError("mount() needs a FastAPI or Starlette app")
        # Called when none of the host's routes match.
        router.default = asgi
    else:
        host.mount(path.rstrip("/"), asgi)
    return asgi


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


async def _get(asgi: Any, path: str) -> tuple[int, dict[str, str], str]:
    """One GET through an ASGI app, without a network: (status, headers, body)."""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": raw_path, "raw_path": raw_path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 0),
        "server": ("localhost", 80),
    }
    status = 500
    headers: dict[str, str] = {}
    chunks: list[bytes] = []
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # no disconnect until the response is done
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            headers.update({k.decode(): v.decode() for k, v in message.get("headers", [])})
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await asyncio.wait_for(asgi(scope, receive, send), timeout=None)
    return status, headers, b"".join(chunks).decode()


def prerender(
    app: Component | Element | Callable[[], Element],
    paths: Sequence[str] = ("/",),
    out_dir: str | Path | None = None,
    *,
    live: bool = False,
    **options: Any,
) -> dict[str, str]:
    """Render ``paths`` to complete HTML pages ahead of time, for a static host.

    Each page waits for all its data (``use_data``, ``use_async``, Suspense), as
    React's ``prerender`` does, and gets its status, title and head tags like a
    served page. Returns ``{path: html}``; with ``out_dir`` also writes them, ``/``
    as ``index.html`` and ``/about`` as ``about/index.html`` (copy your static
    folder next to them yourself). Without ``live`` the pages have no client
    script: plain HTML, no WebSocket. With ``live=True`` they keep it, to be served
    in front of the running app. Redirecting paths are skipped. ``options`` go to
    :func:`create_app` (``title``, ``head``, ``stylesheets``, ``data_timeout``...).
    Call it outside an event loop (in async code, run it in a thread).
    """
    asgi = create_app(app, stream=False, **options)
    script = re.compile(r'<script src="[^"]*/client\.js"[^>]*></script>\n?')

    async def render_all() -> dict[str, str]:
        pages: dict[str, str] = {}
        for path in paths:
            if not path.startswith("/"):
                raise ValueError(f"paths start with '/': {path!r}")
            status, headers, body = await _get(asgi, path)
            if 300 <= status < 400:
                logger.warning("Not prerendering %s: it redirects to %s", path, headers.get("location"))
                continue
            if status >= 400:
                logger.warning("Prerendered %s with status %d", path, status)
            pages[path] = body if live else script.sub("", body)
        return pages

    pages = asyncio.run(render_all())
    if out_dir is not None:
        root = Path(out_dir)
        for path, html in pages.items():
            clean = path.partition("?")[0].strip("/")
            target = root / clean / "index.html" if clean else root / "index.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(html, encoding="utf-8")
    return pages
