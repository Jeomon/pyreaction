"""pyreaction: React-style components in Python, rendered on the server and
patched into the browser over a WebSocket.

    from reaction import component, html, use_state, run

    @component
    def Counter():
        count, set_count = use_state(0)
        return html.button(f"Clicked {count} times", on_click=lambda: set_count(count + 1))

    run(Counter)
"""

from .core.boundary import ErrorBoundary
from .core.components import Activity, Form, FormStatus, Head, Profiler, StrictMode, ViewTransition, Suspense, use_form_status
from .core.element import (
    Children,
    Component,
    Element,
    EventHandler,
    JsComponent,
    clone_element,
    component,
    event,
    forward_ref,
    fragment,
    h,
    html,
    is_valid_element,
    js,
    js_component,
    memo,
    portal,
)
from .core.hooks import (
    add_transition_type,
    browser,
    cache_signal,
    flush_sync,
    lazy,
    request_form_reset,
    cache,
    use,
    use_deferred_value,
    create_ref,
    AsyncState,
    Context,
    Ref,
    create_context,
    use_action_state,
    use_async,
    use_callback,
    use_context,
    use_data,
    use_debug_value,
    use_effect,
    use_layout_effect,
    use_effect_event,
    use_id,
    use_imperative_handle,
    use_location,
    use_memo,
    use_navigate,
    use_reducer,
    use_ref,
    use_request,
    use_state,
    use_status,
    use_sync_external_store,
    use_title,
    use_transition,
    start_transition,
    use_optimistic,
)
from .core.parse import parse_html
from .core.vdom import ElementHandle, Event, FormFields, Location, Reply, Root, UploadedFile, to_vdom
from .core.router import Link, Navigate, Router
from .core.ssr import render_to_static_markup, render_to_string
from .core.resources import preconnect, prefetch_dns, preinit, preinit_module, preload, preload_module
from .core.store import RedisBackend, Store, StoreBackend, use_store

__version__ = "0.1.0"

__all__ = [
    "forward_ref",
    "request_form_reset",
    "lazy",
    "flush_sync",
    "cache_signal",
    "browser",
    "add_transition_type",
    "ViewTransition",
    "cache",
    "Profiler",
    "use_deferred_value",
    "use",
    "create_ref",
    "is_valid_element",
    "clone_element",
    "Children",
    "Activity",
    "StrictMode",
    "AsyncState",
    "Component",
    "Context",
    "Element",
    "ElementHandle",
    "ErrorBoundary",
    "Event",
    "EventHandler",
    "Form",
    "FormFields",
    "FormStatus",
    "Head",
    "JsComponent",
    "Link",
    "Location",
    "Navigate",
    "Ref",
    "Reply",
    "Root",
    "Router",
    "RedisBackend",
    "Store",
    "StoreBackend",
    "Suspense",
    "UploadedFile",
    "component",
    "create_app",
    "mount",
    "page",
    "prerender",
    "create_context",
    "embed",
    "event",
    "fragment",
    "h",
    "html",
    "js",
    "js_component",
    "memo",
    "parse_html",
    "preload_module",
    "preload",
    "preinit_module",
    "preinit",
    "prefetch_dns",
    "preconnect",
    "portal",
    "render_to_static_markup",
    "render_to_string",
    "to_vdom",
    "run",
    "use_action_state",
    "use_async",
    "use_callback",
    "use_context",
    "use_data",
    "use_debug_value",
    "use_effect",
    "use_layout_effect",
    "use_effect_event",
    "use_form_status",
    "use_id",
    "use_imperative_handle",
    "use_location",
    "use_memo",
    "use_navigate",
    "use_reducer",
    "use_ref",
    "use_request",
    "use_state",
    "use_status",
    "use_store",
    "use_sync_external_store",
    "use_title",
    "use_transition",
    "start_transition",
    "use_optimistic",
]


def __getattr__(name: str):
    # The server pulls in Starlette; load it only when it is asked for.
    if name in ("create_app", "embed", "mount", "page", "prerender", "run"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module 'reaction' has no attribute {name!r}")
