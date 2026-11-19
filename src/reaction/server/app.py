"""ASGI app: server-rendered first paint, then a WebSocket per page view.

A GET renders the app to HTML and keeps nothing. The page's WebSocket renders it
again from the same URL; when the result matches, the browser adopts the HTML in
place, and otherwise rebuilds it (a render that uses the clock or randomness).
This is how React hydrates, and it means no memory is held between the two
requests and any worker process can serve either one.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import hashlib
import hmac
import inspect
import logging
import re
import secrets
import tempfile
from contextvars import ContextVar
from collections.abc import AsyncIterator, Callable, Sequence
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

import orjson
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from ..core.element import Component, Element
from ..core.vdom import Root, UploadedFile
from ..core.ssr import image_preloads, render_children, render_portals, render_to_string
from .encoding import dumps as _dumps
from .encoding import loads as _loads
from .settings import UNSET, resolve

__all__ = ["create_app", "embed", "jinja_components", "mount", "page", "prerender", "run", "wrap"]

logger = logging.getLogger("reaction")

CLIENT_DIR = Path(__file__).parent.parent / "client"
CLIENT_JS = CLIENT_DIR / "client.js"
# The client script, served from memory at a URL carrying a hash of its contents
# (client.js?v=...), so browsers cache it for good and fetch it again only when
# it changes.
_CLIENT = CLIENT_JS.read_bytes()
_CLIENT_GZIP = gzip.compress(_CLIENT, 9, mtime=0)
CLIENT_VERSION = hashlib.sha256(_CLIENT).hexdigest()[:12]


def _client_js(request: Request) -> Response:
    tag = f'"{CLIENT_VERSION}"'
    headers = {"ETag": tag, "Vary": "Accept-Encoding"}
    # Versioned: never changes. Unversioned (an old page, a hand-written tag): check.
    headers["Cache-Control"] = (
        "public, max-age=31536000, immutable" if request.query_params.get("v") == CLIENT_VERSION else "no-cache"
    )
    if tag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(_CLIENT_GZIP, media_type="text/javascript", headers={**headers, "Content-Encoding": "gzip"})
    return Response(_CLIENT, media_type="text/javascript", headers=headers)
# $-placeholders rather than str.format, so the HTML and CSS need no brace escaping.
PAGE = Template((CLIENT_DIR / "index.html").read_text(encoding="utf-8"))
STYLE = (CLIENT_DIR / "style.css").read_text(encoding="utf-8")

# An app that sets no icon gets an empty one, so the browser does not ask for
# /favicon.ico, which would render the whole app as a page.
_ICON = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']?[^"'>]*\bicon\b""", re.IGNORECASE)
_NO_ICON = '<link rel="icon" href="data:,">\n'

# Close code for "the browser is not keeping up": it reconnects with a fresh session.
TRY_AGAIN_LATER = 1013
# Close code for an update the server could not send: the page reconnects and renders afresh.
INTERNAL_ERROR = 1011
POLICY_VIOLATION = 1008


def _as_factory(app: Component | Element | Callable[[], Element]) -> Callable[[], Element]:
    if isinstance(app, Element):
        return lambda: app
    return app


# Where the page template is cut for streaming: the shell goes first, Suspense
# chunks next, and the client script last, once the page is complete.
_STREAM_SPLIT = "<!--pyreaction-stream-->"

# Replaces the content between a boundary's <!--pr-s:N--> markers with its chunk.
# The markers are found by one walk of the page, kept by name in __prM; a boundary
# whose markers came inside an earlier chunk (or were removed with one) walks again.
# __prHead moves the <head> tags a chunk brought (<template class="pr-h">) into
# <head>; a boundary is revealed once the stylesheets among them have loaded (at most
# 3 s), as React's Fizz waits, so its content never shows unstyled. Swaps stay in
# order behind a waiting one (__prL), and client.js waits for them before adopting.
_SWAP = (
    "<script>function __prHead(){var w=[];"
    "document.querySelectorAll('.pr-h').forEach(function(t){"
    "Array.from(t.content.children).forEach(function(el){var n=el,i;"
    "if(el.localName==='script'){n=document.createElement('script');"
    "for(i=0;i<el.attributes.length;i++)n.setAttribute(el.attributes[i].name,el.attributes[i].value);n.text=el.text}"
    "if(n.localName==='link'&&n.rel==='stylesheet')w.push(new Promise(function(r){n.onload=n.onerror=r}));"
    "document.head.appendChild(n)});t.remove()});"
    "if(!w.length)return null;"
    "var p=Promise.race([Promise.all(w),new Promise(function(r){setTimeout(r,3000)})]);"
    "window.__prL=Promise.all([window.__prL,p]);return p}"
    "function __prSwap(n){var p=__prHead(),l=window.__prL;"
    "if(p||l){window.__prL=Promise.all([l,p]).then(function(){__prDo(n)});return}__prDo(n)}"
    "function __prDo(n){var t=document.getElementById('pr-c'+n),m=window.__prM,s,e,c,w;"
    "function find(){m=window.__prM={};w=document.createTreeWalker(document.body,128);"
    "while(c=w.nextNode())if(/^\\/?pr-s:/.test(c.data))m[c.data]=c}"
    "s=m&&m['pr-s:'+n];e=m&&m['/pr-s:'+n];"
    "if(!s||!e||!s.isConnected||!e.isConnected){find();s=m['pr-s:'+n];e=m['/pr-s:'+n]}"
    "if(!t||!s||!e)return;while(s.nextSibling&&s.nextSibling!==e)s.nextSibling.remove();"
    "e.parentNode.insertBefore(t.content,e);t.remove()}</script>\n"
)


async def _stream_page(
    root: Root,
    parts: list[str],
    boundaries: dict[Any, int],
    waiting: set[int],
    deadline: float,
    hand_off: tuple[Callable[[], None], Callable[[], None]] | None = None,
    head_sent: set[str] | None = None,
    early_socket: str = "",
) -> AsyncIterator[str]:
    """The page in pieces: the shell with Suspense fallbacks at once, then each
    boundary's content as its data arrives, then the client script.

    ``hand_off`` is (finish, abandon) for the render already registered for the
    page's live connection. ``early_socket`` is a script in the shell that opens that
    connection at once, so its handshake happens while the page streams; the server
    holds it until ``finish`` runs, at the end of the stream, when the render is the
    page the browser has. ``abandon`` runs instead when the stream stops early.

    ``head_sent`` is the shell's <head> tags. Tags a boundary adds later (a
    stylesheet it preinits, an image it preloads, Head tags) go out with its chunk,
    for the swap script to move into <head> (and wait for stylesheets) before the
    content shows, as React's Fizz does, instead of after the whole stream."""
    loop = asyncio.get_running_loop()
    handed_off = False
    try:
        yield parts[0] + _SWAP + early_socket
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
            if head_sent is not None:
                new = [tag for tag in root.head_parts() if tag not in head_sent]
                if new:
                    head_sent.update(new)
                    chunks.insert(0, f'<template class="pr-h">{"".join(new)}</template>')
                    if len(chunks) == 1:  # tags with no content this round: move them now
                        chunks.append("<script>__prHead();document.currentScript.remove()</script>\n")
            if chunks:
                yield "".join(chunks)
        if hand_off is not None:
            hand_off[0]()
            handed_off = True
        yield parts[1]
    finally:
        if not handed_off:
            if hand_off is not None:
                hand_off[1]()
            root.close()  # streaming stopped early, or nothing will connect


# The most a page's Link header may hold (React's default, maxHeadersLength): proxies
# and servers limit header sizes, and the first few hints matter most.
_LINK_HEADER_MAX = 2000


def _link_url(url: str) -> str | None:
    """A URL as a Link header carries it: non-ASCII and <> percent-encoded; None for
    one no header can hold (control characters)."""
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    return quote(url, safe="!#$&'()*+,/:;=?@[]~%")


def _link_header(script: str, resources: Any) -> str:
    """A Link header for the page's response: the client script and the resource
    hints in its <head> (preconnects, preloads, preinit stylesheets and scripts), so a
    CDN can send them as 103 Early Hints and the browser starts on them before the
    HTML arrives, as React's onHeaders gives frameworks."""
    links = [f"<{script}>; rel=preload; as=script"]
    for (kind, _), element in resources.items():
        attrs = element.attrs
        href = attrs.get("href") or attrs.get("src")
        if kind == "dns" and href:
            link = f"<{href}>; rel=dns-prefetch"
        elif kind == "connect" and href:
            link = f"<{href}>; rel=preconnect"
        elif kind == "preload" and attrs.get("rel") == "modulepreload" and href:
            link = f"<{href}>; rel=modulepreload"
        elif kind == "preload" and href:
            link = f"<{href}>; rel=preload; as={attrs.get('as', 'fetch')}"
        elif kind == "init" and element.type == "link" and href:
            link = f"<{href}>; rel=preload; as=style"
        elif kind == "init" and href:
            link = f"<{href}>; rel=" + ("modulepreload" if attrs.get("type") == "module" else "preload; as=script")
        else:
            continue  # an image known only by its srcset: left to the <head>
        url = _link_url(href)
        if url is None:
            continue
        link = link.replace(f"<{href}>", f"<{url}>", 1)
        if "crossorigin" in attrs:  # an empty value is the bare attribute: anonymous
            link += f'; crossorigin="{attrs["crossorigin"]}"' if attrs["crossorigin"] else "; crossorigin"
        if kind == "preload" and attrs.get("type"):
            link += f'; type="{attrs["type"]}"'
        if attrs.get("fetchpriority"):
            link += f'; fetchpriority="{attrs["fetchpriority"]}"'
        links.append(link)
    out = links[0]
    for link in links[1:]:
        if len(out) + 2 + len(link) > _LINK_HEADER_MAX:
            break
        out += ", " + link
    return out


def _early_socket(ws_path: str, url: str, base: str, token: str) -> str:
    """For a streamed page: open its WebSocket from the shell, so the handshake is done
    by the time the stream ends, and keep what arrives for client.js. The URL is the
    one client.js would open (``p`` the page inside the app, ``b`` the app's base, the
    hand-off token), with ``h=1``: the browser adopts the HTML it has."""
    config = json.dumps({"ws": ws_path, "p": url, "b": base, "k": token}).replace("</", "<\\/")
    return (
        "<script>(function(c){try{var u=new URL(c.ws,location.href),q=[],w;"
        "u.protocol=u.protocol==='https:'?'wss:':'ws:';u.searchParams.set('p',c.p);"
        "if(c.b)u.searchParams.set('b',c.b);u.searchParams.set('h','1');u.searchParams.set('k',c.k);"
        "w=new WebSocket(u);w.onmessage=function(e){q.push(e)};window.__prWS={ws:w,queue:q,k:c.k}}catch(e){}})"
        f"({config});document.currentScript.remove()</script>\n"
    )


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


def _warn_if_static_shadowed(scope: Any, ours: Any, static_path: str) -> None:
    """Warn when the app this one is mounted in has a route (often its own /static)
    that answers this app's static URLs first, so its stylesheets would 404."""
    from starlette.routing import Match

    host = scope.get("app")
    routes = getattr(getattr(host, "router", None), "routes", None)
    if host is None or host is ours or not routes:
        return
    base = scope.get("root_path", "").rstrip("/")
    host_root = scope.get("app_root_path", "").rstrip("/")
    probe = {
        "type": "http", "method": "GET", "headers": [], "root_path": host_root,
        "path": f"{base}{static_path}/__probe__", "query_string": b"",
    }
    for route in routes:
        if getattr(route, "app", None) is ours:
            return  # reached our own mount first
        try:
            match, _ = route.matches(probe)
        except Exception:
            continue
        if match == Match.FULL:
            logger.warning(
                "The app's route %r answers %s%s/... before pyreaction does, so its static files "
                "(stylesheets, scripts) are not served. Pass another static_path, e.g. "
                "mount(app, App, static_path=\"/assets\"), or mount the pages under a path.",
                getattr(route, "path", route), base, static_path,
            )
            return


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


def _parse_event(text: str) -> tuple[int, int | list[int], str, dict[str, Any]] | None:
    try:
        message = _loads(text)
    except ValueError:
        return None
    if not (isinstance(message, list) and len(message) == 4):
        return None
    seq, node_id, name, data = message
    if type(seq) is not int or not isinstance(name, str) or not isinstance(data, dict):
        return None
    if type(node_id) is list:
        # One native event that reached several elements with handlers (client.js
        # delegates from the root): their ids in order, run before one render.
        if not 0 < len(node_id) <= _MAX_BATCH or any(type(i) is not int for i in node_id):
            return None
    elif type(node_id) is not int:
        return None
    return seq, node_id, name, data


# Handlers one native event may reach: far more than any page nests.
_MAX_BATCH = 256


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
        header = _loads(frame[4 : 4 + n])
    except ValueError:
        return None
    if not isinstance(header, dict) or not isinstance(header.get("u"), str):
        return None
    data = frame[4 + n :]
    if len(data) > UPLOAD_CHUNK:
        return None
    return header, data


class _Handoff:
    """A first-HTML render kept for its page's live connection (see ``handoff_timeout``)."""

    __slots__ = ("root", "timer", "loop", "cookie", "url", "base", "ready")

    def __init__(
        self, root: Root, timer: asyncio.TimerHandle | None, loop: asyncio.AbstractEventLoop,
        cookie: str | None, url: str, base: str,
    ) -> None:
        self.root = root
        self.timer = timer
        self.loop = loop
        self.cookie = cookie
        self.url = url
        self.base = base
        # Done once the render matches the page the browser has: at once for a whole
        # page, at the end of the stream for a streamed one (True), or False when the
        # stream stopped early and the render is gone.
        self.ready: asyncio.Future[bool] = loop.create_future()


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
    operations; past it the connection is closed and the browser starts over.

    The message at the head of the queue does not count: it goes next, however big.
    The limit is for a backlog building up behind it, not for one large render (10,000
    new rows are 130,000 operations), which would otherwise close the socket, and
    again on every reconnect, since the reconnect's whole tree is as large."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.pending: list[dict[str, Any]] = []
        self.size = 0
        self.overflow = False
        self.ready = asyncio.Event()

    def push(self, message: dict[str, Any]) -> None:
        self.pending.append(message)
        self.size += len(message["ops"])
        if self.size - len(self.pending[0]["ops"]) > self.limit:
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
    allowed_origins: Sequence[str] = UNSET,
    max_pending_ops: int = UNSET,
    max_pending_events: int = UNSET,
    max_upload_size: int = UNSET,
    prefix: str = "/_pyreaction",
    data_timeout: float = UNSET,
    stream: bool = UNSET,
    routes: Sequence[Any] = (),
    on_error: Callable[[BaseException, str, Any], None] | None = None,
    threaded: bool = UNSET,
    reconnect_max: int = UNSET,
    reconnect_attempts: int = UNSET,
    resume_timeout: float = UNSET,
    handoff_timeout: float = UNSET,
    max_handoffs: int = UNSET,
    compress: bool = UNSET,
    debug: bool = UNSET,
) -> Starlette:
    """An ASGI app serving ``app`` on every GET path.

    ``app`` is a component (called with no arguments), an element, or a function
    returning one. ``head`` is raw HTML for the page's <head>. ``static_dir`` is
    served at ``static_path``, and each of ``stylesheets`` (paths inside it) is
    linked from the page.

    WebSockets are only accepted from the page's own origin plus
    ``allowed_origins`` (needed behind a proxy that rewrites the Host header;
    ``"*"`` allows any). ``max_upload_size`` (bytes) caps each uploaded file.
    A page that sends more than ``max_pending_events`` events its handlers have not
    started yet is disconnected (it reconnects and resumes), so a page cannot
    queue work on the server without limit.

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
    after a restart, the page renders afresh. With ``reconnect_attempts`` (0: no
    limit) the page stops retrying after that many failed attempts in a row and
    says the connection was lost, with a button to reload. ``debug`` shows errors
    in the browser. With ``compress`` (default) pages and static files are sent
    gzipped to browsers that accept it; turn it off when a proxy compresses.

    A page's first HTML is rendered once, and its live connection takes over that
    render instead of rendering again, when it connects within ``handoff_timeout``
    seconds (0: always render again) from the same browser (same cookies). At most
    ``max_handoffs`` renders wait like this at once; the oldest makes way, and a
    page whose render was not kept renders again when it connects.

    The options ``debug``, ``allowed_origins`` (comma-separated), ``max_upload_size``,
    ``max_pending_events``, ``max_pending_ops``, ``data_timeout``, ``stream``,
    ``threaded``, ``reconnect_max``, ``reconnect_attempts``, ``resume_timeout``,
    ``handoff_timeout``, ``max_handoffs`` and ``compress`` not passed here are read from ``PYREACTION_<OPTION>`` environment variables
    (``PYREACTION_DEBUG=1``), else take their defaults.
    """
    debug = resolve("debug", debug)
    allowed_origins = resolve("allowed_origins", allowed_origins)
    max_upload_size = resolve("max_upload_size", max_upload_size)
    max_pending_events = resolve("max_pending_events", max_pending_events)
    max_pending_ops = resolve("max_pending_ops", max_pending_ops)
    data_timeout = resolve("data_timeout", data_timeout)
    stream = resolve("stream", stream)
    threaded = resolve("threaded", threaded)
    reconnect_max = resolve("reconnect_max", reconnect_max)
    reconnect_attempts = resolve("reconnect_attempts", reconnect_attempts)
    resume_timeout = resolve("resume_timeout", resume_timeout)
    handoff_timeout = resolve("handoff_timeout", handoff_timeout)
    max_handoffs = resolve("max_handoffs", max_handoffs)
    compress = resolve("compress", compress)
    make = _as_factory(app)

    def new_root(url: str, connection: Any, base: str = "") -> Root:
        def report(exc: BaseException, where: str) -> None:
            if on_error is not None:
                on_error(exc, where, root.connection)

        root = Root(
            make(), location=url, connection=connection, debug=debug, threaded=threaded, on_error=report, base=base,
            ops=False,  # a browser that connects gets a snapshot, not the mount's operations
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

    # First-HTML renders waiting for their page's live connection, by token, oldest first.
    handoffs: dict[str, _Handoff] = {}

    def drop_handoff(token: str) -> None:
        entry = handoffs.pop(token, None)
        if entry is not None:
            if entry.timer is not None:
                entry.timer.cancel()
            entry.root.close()

    def register_handoff(token: str, root: Root, request: Request, url: str, base: str) -> _Handoff:
        """Keep ``root`` for the page's live connection under ``token``; it can be
        taken once ``finish_handoff`` says the render is the page's HTML."""
        while len(handoffs) >= max_handoffs:
            # The oldest waiting render makes way (a browser that never connected: a
            # bot, a link preview); renders still streaming are not evicted. Closing a
            # large tree takes milliseconds, so it happens after this response.
            oldest = next((k for k, e in handoffs.items() if e.ready.done()), None)
            if oldest is None:
                break
            evicted = handoffs.pop(oldest)
            if evicted.timer is not None:
                evicted.timer.cancel()
            asyncio.get_running_loop().call_soon(evicted.root.close)
        entry = _Handoff(root, None, asyncio.get_running_loop(), request.headers.get("cookie"), url, base)
        handoffs[token] = entry
        return entry

    def finish_handoff(token: str, entry: _Handoff) -> None:
        """The render now matches the page the browser has: it can be taken over."""
        root = entry.root
        # The mount operations were for a browser building the page; it adopts the
        # HTML instead. Element commands still have to run once it connects.
        root.ops = [op for op in root.ops if op[0] == "m"]
        # The browser's page is this tree's HTML: its first message can say less.
        root._html_matches = True
        entry.timer = entry.loop.call_later(handoff_timeout, drop_handoff, token)
        if not entry.ready.done():
            entry.ready.set_result(True)

    def abandon_handoff(token: str, entry: _Handoff) -> None:
        """The stream stopped early: the render goes, and a socket waiting for it
        renders the page itself."""
        handoffs.pop(token, None)
        if not entry.ready.done():
            entry.ready.set_result(False)

    def hand_off(token: str, root: Root, request: Request, url: str, base: str) -> None:
        finish_handoff(token, register_handoff(token, root, request, url, base))

    async def take_handoff(ws: WebSocket, url: str, base: str) -> Root | None:
        token = ws.query_params.get("k", "")
        entry = handoffs.get(token)
        if entry is None:
            return None
        if not entry.ready.done():
            # Opened while its page was still streaming (to save the handshake):
            # wait for the stream to end, when the render is the page's HTML.
            try:
                await asyncio.wait_for(asyncio.shield(entry.ready), data_timeout + 5)
            except TimeoutError:
                return None
        if not entry.ready.result() or handoffs.pop(token, None) is not entry:
            return None
        if entry.timer is not None:
            entry.timer.cancel()
        # Only the browser the page was rendered for (its cookies), for the same URL,
        # served by the same event loop the render's work runs on.
        same = (
            entry.loop is asyncio.get_running_loop()
            and entry.cookie == ws.headers.get("cookie")
            and (entry.url, entry.base) == (url, base)
        )
        if not same:
            entry.root.close()
            return None
        return entry.root

    checked_static = False
    this_app: list[Any] = [None]  # set below, once the app exists

    async def page(request: Request) -> Response:
        nonlocal checked_static
        if not checked_static:
            checked_static = True
            if static_dir is not None:
                _warn_if_static_shadowed(request.scope, this_app[0], static_path)
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
                code = root.status if 300 <= root.status < 400 else 307
                target = root.redirect if root.redirect_as_is else root.location.href(root.redirect)
                handed_off = True  # closed once the response is sent
                return RedirectResponse(
                    target, status_code=code, headers=root.headers, background=BackgroundTask(root.close)
                )
            boundaries: dict[Any, int] = {}
            images: list[Any] = []
            body = render_to_string(root, boundaries if stream else None, images)
            for key, link in image_preloads(images):
                root.add_resource(key, link)
            portals = render_portals(root, boundaries if stream else None)
            page_title = root.title if root.title is not None else title
            head_parts = root.head_parts()
            head_tags = "".join(head_parts)
            status = root.status
            root_path = request.scope.get("root_path", "")
            script_url = f"{request.scope.get(_SCRIPT_BASE, root_path + prefix)}/client.js?v={CLIENT_VERSION}"
            hints = _link_header(script_url, root._resources)
            existing = root.headers.get("Link") or root.headers.get("link")
            root.headers["Link"] = f"{existing}, {hints}" if existing else hints
            # Lets the page's live connection take over this render (see hand_off).
            token = secrets.token_urlsafe(18) if handoff_timeout > 0 and max_handoffs > 0 else ""
            links = "".join(
                f'<link rel="stylesheet" href="{escape(root_path + static_path)}/{escape(name)}">\n'
                for name in stylesheets
            )
            parts = PAGE.substitute(
                lang=escape(lang),
                title=escape(page_title),
                style=STYLE,
                head=links + head + head_tags + ("" if _ICON.search(head + head_tags) else _NO_ICON),
                body=body,
                portals=portals,
                stream=_STREAM_SPLIT,
                # @page routes serve the script and socket next to their own URL.
                base=escape(request.scope.get(_SCRIPT_BASE, root_path + prefix)),
                app_base=escape(base),
                page=escape(request.scope.get(_PAGE_PATH, "")),
                reconnect_max=int(reconnect_max),
                reconnect_attempts=int(reconnect_attempts),
                handoff=token,
                version=CLIENT_VERSION,
            ).split(_STREAM_SPLIT)
            waiting = {n for node, n in boundaries.items() if node.waiting}
            if not waiting:
                response = HTMLResponse(parts[0] + parts[1], status_code=status, headers=root.headers)
                if token:
                    hand_off(token, root, request, url, base)
                else:
                    # Unmounting a large tree takes milliseconds: after the response is
                    # sent, so the first byte does not wait for it.
                    response.background = BackgroundTask(root.close)
                handed_off = True
                return response
            handed_off = True  # _stream_page keeps or closes it from here
            stream_handoff = None
            early = ""
            if token:
                entry = register_handoff(token, root, request, url, base)
                stream_handoff = (
                    lambda: finish_handoff(token, entry),
                    lambda: abandon_handoff(token, entry),
                )
                early = _early_socket(request.scope.get(_SCRIPT_BASE, root_path + prefix) + "/ws", url, base, token)
            return StreamingResponse(
                _stream_page(root, parts, boundaries, waiting, deadline, stream_handoff, set(head_parts), early),
                media_type="text/html",
                status_code=status,
                headers=root.headers,
            )
        finally:
            if not handed_off:
                root.close()

    async def client_js(request: Request) -> Response:
        return _client_js(request)

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

        broken = False

        async def writer() -> None:
            nonlocal broken
            while (message := await outbox.take()) is not None:
                try:
                    text = _dumps(message)
                except Exception as exc:
                    # Without this the page would wait for an update that never comes.
                    broken = True
                    if root is not None:
                        root.report(exc, "sending an update to the browser")
                    else:
                        logger.error("Could not send an update to the browser", exc_info=exc)
                    await ws.close(code=INTERNAL_ERROR)
                    return
                await ws.send_text(text)
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
            elif (root := await take_handoff(ws, url, base)) is not None:
                # The render that made this page's HTML: no need to render it again.
                root.connection = ws
                root.attach(send, hydrate=ws.query_params.get("h") == "1")
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
                if root.pending_events >= max_pending_events:
                    # Handlers are not keeping up with this page: stop reading
                    # rather than queue without limit. The page reconnects and
                    # resumes once the queued events have run.
                    logger.warning("Closing a WebSocket with %d events waiting", root.pending_events)
                    await ws.close(code=TRY_AGAIN_LATER)
                    break
                root.dispatch(*event)
        finally:
            writing.cancel()
            if uploads is not None:
                uploads.close()  # files half-sent on this connection
            if root is not None:
                # A session whose update could not be sent is not resumed: it would
                # send the same update again.
                if resume_timeout > 0 and root._sink is not None and not broken:
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
    async def no_icon(request: Request) -> Response:
        return Response(status_code=404)  # asked for despite the empty icon: not a page

    table += [Route("/favicon.ico", no_icon), Route("/", page), Route("/{path:path}", page)]
    # Level 6, not Starlette's 9: on a 388 kB page 9 takes 20 ms (longer than the
    # render) for 1.4% fewer bytes than 6, which takes 2 ms.
    middleware = [Middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)] if compress else []
    this_app[0] = _MountableApp(debug=debug, routes=table, middleware=middleware)
    return this_app[0]


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


# Scope keys a @page route sets for its pyreaction app: where the browser finds the
# script and socket (next to the page's own URL), and the page's path, outside which
# links load the page afresh.
_SCRIPT_BASE = "reaction.script_base"
_PAGE_PATH = "reaction.page_path"
# The element a @page function returned, for the pyreaction app to render.
_PAGE_ELEMENT: ContextVar[Element | None] = ContextVar("reaction_page_element", default=None)


def _page_element() -> Element:
    element = _PAGE_ELEMENT.get()
    if element is None:
        raise RuntimeError("a @page app was called outside its route")
    return element


class _PageResponse(Response):
    """What a @page GET route returns: renders the element as a pyreaction page."""

    def __init__(self, app: Any, element: Element) -> None:
        self.app = app
        self.element = element
        self.background = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        base, path = _app_path(scope)
        url = (base + path).rstrip("/") or ""
        inner = {**scope, _SCRIPT_BASE: url + "/_pyreaction", _PAGE_PATH: base + path}
        token = _PAGE_ELEMENT.set(self.element)
        try:
            await self.app(inner, receive, send)
        finally:
            _PAGE_ELEMENT.reset(token)


def page(host: Any, path: str, **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """A FastAPI route whose function returns a pyreaction page, with ``Depends``::

        @page(api, "/orders/{order_id}")
        def order(order_id: int, user: User = Depends(current_user), db=Depends(get_db)):
            return OrderPage(order=db.order(order_id, user))

    FastAPI resolves the function's parameters (path and query parameters,
    ``Depends``, ``Cookie``...) when the page is loaded, and again when its live
    connection opens, so both renders get the same values. A dependency with
    ``yield`` stays open while the page is live. The function may be ``async``;
    a plain one runs in a worker thread, as FastAPI runs routes.

    On the live connection there is no ``Request``: dependencies that need the
    connection should take ``HTTPConnection`` (cookies, headers, ``.state``,
    ``.session``, ``.user``), which FastAPI fills in for both. Browsers cannot add
    headers to a WebSocket, so use cookies for login, not ``Authorization``.

    Each @page is its own page: links to other URLs load them afresh. Within it,
    the query string can change without a reload. ``options`` go to
    :func:`create_app`, except ``static_dir``: serve static files from the host app
    (``mount`` or ``StaticFiles``). The page is left out of the OpenAPI schema.
    """
    from fastapi import WebSocket
    from fastapi.requests import Request as FastAPIRequest
    from starlette.concurrency import run_in_threadpool

    if "static_dir" in options:
        raise TypeError("@page does not serve static files; mount them on the app itself")
    # Dependencies with yield close after the GET's response, so the live page
    # renders with its own (handoff_timeout=0): never with the GET's render.
    sub_app = create_app(_page_element, **{**options, "handoff_timeout": 0})
    route = "/" + path.strip("/") if path.strip("/") else "/"
    under = route.rstrip("/")

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn, eval_str=True)
        params = [
            p.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            for p in signature.parameters.values()
            if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]

        async def element_of(values: dict[str, Any]) -> Element:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**values)
            else:
                result = await run_in_threadpool(fn, **values)
            if not isinstance(result, Element):
                raise TypeError(f"@page function {fn.__name__} must return an element, got {type(result).__name__}")
            return result

        async def http_endpoint(_reaction_conn: Any, **values: Any) -> Response:
            return _PageResponse(sub_app, await element_of(values))

        async def ws_endpoint(_reaction_conn: Any, **values: Any) -> None:
            element = await element_of(values)
            scope = dict(_reaction_conn.scope)
            root = scope.get("root_path", "")
            scope["path"] = root + "/_pyreaction/ws"
            scope["raw_path"] = scope["path"].encode()
            token = _PAGE_ELEMENT.set(element)
            try:
                await sub_app(scope, _reaction_conn._receive, _reaction_conn._send)
            finally:
                _PAGE_ELEMENT.reset(token)

        async def client_js(request: Request) -> Response:
            return _client_js(request)

        def with_conn(endpoint: Any, kind: Any) -> Any:
            conn = inspect.Parameter("_reaction_conn", inspect.Parameter.KEYWORD_ONLY, annotation=kind)
            endpoint.__signature__ = inspect.Signature([conn, *params])
            endpoint.__name__ = fn.__name__
            return endpoint

        host.add_api_route(route, with_conn(http_endpoint, FastAPIRequest), methods=["GET"],
                           include_in_schema=False, name=fn.__name__)
        host.add_api_websocket_route(under + "/_pyreaction/ws", with_conn(ws_endpoint, WebSocket))
        host.add_api_route(under + "/_pyreaction/client.js", client_js, methods=["GET"], include_in_schema=False)
        return fn

    return decorate


def _as_asgi(app: Any) -> Any:
    """``app`` as an ASGI app: as is, or a WSGI app (Flask) through a2wsgi."""
    call = getattr(app, "__call__", None)
    if inspect.iscoroutinefunction(app) or inspect.iscoroutinefunction(call):
        return app
    try:
        from a2wsgi import WSGIMiddleware
    except ImportError:
        raise ImportError(
            'wrap() needs a2wsgi for a WSGI app such as Flask: pip install "pyreaction[wsgi]"'
        ) from None
    return WSGIMiddleware(app)


async def _empty_request(receive: Any) -> Any:
    """A receive that gives an empty request body, then waits for the disconnect."""
    sent = False

    async def again() -> Any:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await receive()

    return again


def wrap(
    app: Any,
    component: Component | Element | Callable[[], Element] | Starlette,
    path: str = "/",
    **options: Any,
) -> Any:
    """Serve pyreaction pages in front of another web app, such as Django or Flask.

    Returns an ASGI app to run with uvicorn (or any ASGI server). Requests at
    ``path`` go to pyreaction (its pages, script, WebSocket and ``static_dir``);
    everything else goes to ``app`` unchanged::

        # Django (asgi.py)
        application = wrap(get_asgi_application(), Dashboard, "/dashboard")

        # Flask: a WSGI app, run through a2wsgi (pip install "pyreaction[wsgi]")
        app = wrap(flask_app, Dashboard, "/dashboard")      # uvicorn main:app

    At ``"/"``, the other app answers first and the pages take what it does not
    find: a GET it answers with 404 is rendered by pyreaction instead.
    Components see the request through ``use_request()`` (``.cookies``,
    ``.headers``), not the framework's own request object. The app's lifespan
    events are answered here (Django and Flask have none), and WebSockets other
    than pyreaction's are passed on as they are. ``options`` go to
    :func:`create_app`. To put a component inside the framework's own templates,
    wrap it under a path and add :func:`embed` to the template.
    """
    inner = _as_asgi(app)
    # A pyreaction app already built (create_app, jinja_components) is used as is.
    ours = component if isinstance(component, Starlette) else create_app(component, **options)
    base = "/" + path.strip("/") if path.strip("/") else ""
    prefix = options.get("prefix", "/_pyreaction")
    own_paths = [prefix]
    if options.get("static_dir") is not None:
        own_paths.append(options.get("static_path", "/static"))

    def under(p: str, start: str) -> bool:
        return p == start or p.startswith(start.rstrip("/") + "/")

    async def ours_at(scope: Any, receive: Any, send: Any) -> None:
        if base:
            scope = {**scope, "root_path": scope.get("root_path", "") + base}
        await ours(scope, receive, send)

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await ours(scope, receive, send)  # Django and a2wsgi cannot handle these
            return
        root = scope.get("root_path", "")
        p = scope.get("path", "/")
        p = p[len(root):] if root and p.startswith(root) else p
        if base:
            if under(p, base):
                await ours_at(scope, receive, send)
            else:
                await inner(scope, receive, send)
            return
        if any(under(p, own) for own in own_paths):
            await ours(scope, receive, send)
            return
        if kind != "http" or scope.get("method") not in ("GET", "HEAD"):
            await inner(scope, receive, send)
            return
        # At "/": the other app first; a 404 for a GET becomes a pyreaction page.
        state = {"fallback": False}

        async def watch(message: Any) -> None:
            if message["type"] == "http.response.start":
                if message["status"] == 404:
                    state["fallback"] = True
                    return
                await send(message)
            elif not state["fallback"]:
                await send(message)

        await inner(scope, receive, watch)
        if state["fallback"]:
            await ours(scope, await _empty_request(receive), send)

    return wrapped


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
    return _embed_html(app, mount_path, location, element_id, prefix, "")


def _embed_html(
    app: Component | Element | Callable[[], Element] | None,
    mount_path: str,
    location: str,
    element_id: str,
    prefix: str,
    ws_query: str,
) -> str:
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
        f'<script src="{base}/client.js?v={CLIENT_VERSION}" data-ws="{base}/ws{escape(ws_query)}" data-mount="{escape(element_id)}"></script>'
    )


def _dotted(component: Any) -> str:
    return f"{component.__module__}.{getattr(component, '__qualname__', component.__name__)}"


def jinja_components(
    env: Any,
    components: Sequence[Component | Callable[..., Element]],
    *,
    mount_path: str = "/components",
    secret: str | bytes | None = None,
    **options: Any,
) -> Any:
    """Add a ``{% component %}`` tag to a Jinja ``Environment`` and return the ASGI
    app that runs those components, to mount at ``mount_path``::

        templates = Jinja2Templates("templates")
        api.mount("/components", jinja_components(templates.env, [Counter, Chart]))

        {# in a template #}
        {% component "myapp.widgets.Counter" %}
        {% component "myapp.widgets.Chart", values=[1, 2, 3], title=page_title %}

    Only the listed components can be named, by import path
    (``module.qualname``). The tag writes the server-rendered HTML and the script;
    the component and its props (JSON values) travel in the WebSocket URL, signed
    with ``secret``, so a visitor cannot choose another component or change the
    props. Give the same ``secret`` to every worker process; the default is a
    random one, which works for a single process only. The location the component
    sees is the ``request`` in the template's context (Starlette, Flask or Django),
    else ``/``. For Flask or Django, put the returned app in front with
    :func:`wrap`. ``options`` go to :func:`create_app`.
    """
    try:
        from jinja2 import nodes
        from jinja2.ext import Extension
        from markupsafe import Markup
    except ImportError:
        raise ImportError('jinja_components() needs Jinja: pip install "pyreaction[jinja]"') from None
    from ..core.element import component as as_component
    from ..core.hooks import use_request

    registry = {_dotted(c): c for c in components}
    key = secret.encode() if isinstance(secret, str) else secret or secrets.token_bytes(32)
    prefix = options.get("prefix", "/_pyreaction")

    def sign(name: str, props: dict[str, Any]) -> str:
        payload = base64.urlsafe_b64encode(orjson.dumps([name, props], option=orjson.OPT_NON_STR_KEYS)).decode()
        mac = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{payload}.{mac}"

    def unsign(token: str) -> tuple[str, dict[str, Any]] | None:
        payload, _, mac = token.rpartition(".")
        good = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not payload or not hmac.compare_digest(mac, good):
            return None
        name, props = _loads(base64.urlsafe_b64decode(payload))
        return (name, props) if name in registry else None

    @as_component
    def Chosen() -> Any:
        conn = use_request()
        found = unsign(conn.query_params.get("c", "")) if conn is not None else None
        if found is None:
            return None  # an unsigned or altered request: show nothing
        name, props = found
        return registry[name](**props)

    def render_tag(context: Any, name: str, /, **props: Any) -> Markup:
        if name not in registry:
            raise KeyError(f"{name!r} is not one of the components given to jinja_components()")
        orjson.dumps(props, option=orjson.OPT_NON_STR_KEYS)  # props travel as JSON; fail here, in the template, if they cannot
        request = context.get("request")
        path = getattr(request, "path", None) or getattr(getattr(request, "url", None), "path", None) or "/"
        query = "?c=" + sign(name, props)
        element_id = "pyreaction-" + secrets.token_hex(4)
        return Markup(_embed_html(registry[name](**props), mount_path, path, element_id, prefix, query))

    class ComponentTag(Extension):
        tags = {"component"}

        def parse(self, parser: Any) -> Any:
            lineno = next(parser.stream).lineno
            name = parser.parse_expression()
            kwargs = []
            while parser.stream.skip_if("comma"):
                target = parser.stream.expect("name")
                parser.stream.expect("assign")
                kwargs.append(nodes.Keyword(target.value, parser.parse_expression()))
            call = self.call_method("_render", [nodes.ContextReference(), name], kwargs)
            return nodes.Output([call], lineno=lineno)

        def _render(self, context: Any, name: str, /, **props: Any) -> Markup:
            return render_tag(context, name, **props)

    env.add_extension(ComponentTag)
    return create_app(Chosen, **options)


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
    asgi = create_app(app, stream=False, **{"handoff_timeout": 0, **options})
    # The client script, and its preload in <head>: a static page has neither.
    script = re.compile(
        r'<script src="[^"]*/client\.js(\?[^"]*)?"[^>]*></script>\n?'
        r'|<link rel="preload" as="script" href="[^"]*/client\.js(\?[^"]*)?">\n?'
    )

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
