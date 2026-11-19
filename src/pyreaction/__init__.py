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
from .core.element import Component, Element, component, event, fragment, h, html, memo
from .core.hooks import (
    Context,
    Ref,
    create_context,
    use_callback,
    use_context,
    use_effect,
    use_location,
    use_memo,
    use_navigate,
    use_reducer,
    use_ref,
    use_request,
    use_state,
    use_title,
)
from .core.reconciler import ElementHandle, Event, Location, Root
from .core.router import Link, Router
from .core.ssr import render_to_string

__version__ = "0.1.0"

__all__ = [
    "Component",
    "Context",
    "Element",
    "ElementHandle",
    "ErrorBoundary",
    "Event",
    "Link",
    "Location",
    "Ref",
    "Root",
    "Router",
    "component",
    "create_app",
    "create_context",
    "event",
    "fragment",
    "h",
    "html",
    "memo",
    "render_to_string",
    "run",
    "use_callback",
    "use_context",
    "use_effect",
    "use_location",
    "use_memo",
    "use_navigate",
    "use_reducer",
    "use_ref",
    "use_request",
    "use_state",
    "use_title",
]


def __getattr__(name: str):
    # The server pulls in Starlette; load it only when it is asked for.
    if name in ("create_app", "run"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module 'pyreaction' has no attribute {name!r}")
