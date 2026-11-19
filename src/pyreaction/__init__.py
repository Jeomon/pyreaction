"""pyreaction: React-style components in Python, rendered on the server and
patched into the browser over a WebSocket.

    from pyreaction import component, html, use_state, run

    @component
    def Counter():
        count, set_count = use_state(0)
        return html.button(f"Clicked {count} times", on_click=lambda: set_count(count + 1))

    run(Counter)
"""

from .core.boundary import ErrorBoundary
from .core.components import Activity, Form, FormStatus, Head, Suspense, use_form_status
from .core.element import Component, Element, EventHandler, JsComponent, component, event, fragment, h, html, js, js_component, memo, portal
from .core.hooks import (
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
)
from .core.parse import parse_html
from .core.reconciler import ElementHandle, Event, FormFields, Location, Reply, Root, UploadedFile
from .core.router import Link, Navigate, Router
from .core.ssr import render_to_string
from .core.store import RedisBackend, Store, StoreBackend, use_store

__version__ = "0.1.0"

__all__ = [
    "Activity",
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
    "portal",
    "render_to_string",
    "run",
    "use_action_state",
    "use_async",
    "use_callback",
    "use_context",
    "use_data",
    "use_debug_value",
    "use_effect",
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
]


def __getattr__(name: str):
    # The server pulls in Starlette; load it only when it is asked for.
    if name in ("create_app", "embed", "run"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module 'pyreaction' has no attribute {name!r}")
