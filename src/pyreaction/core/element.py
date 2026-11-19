"""Elements: the immutable description of what a component wants on screen.

Props are normalised once, when the element is created, into DOM attributes and
event handlers, so the reconciler only ever compares ready-to-send values.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import Any

__all__ = [
    "Component",
    "Element",
    "EventHandler",
    "FRAGMENT",
    "JsComponent",
    "component",
    "event",
    "fragment",
    "h",
    "html",
    "js_component",
    "memo",
]

FRAGMENT = "#fragment"

# Set as DOM properties rather than attributes, so they reflect live state
# (an attribute only sets the initial value of an <input>).
PROPERTY_PROPS = frozenset({"value", "checked", "selected", "indeterminate", "muted"})

_ATTR_ALIASES = {
    "class_name": "class",
    "className": "class",
    "class_": "class",
    "html_for": "for",
    "htmlFor": "for",
    "for_": "for",
}

# React's onChange fires on every edit, which is the DOM "input" event.
_EVENT_ALIASES = {"change": "input", "doubleclick": "dblclick"}


class EventHandler:
    """A callback plus how the browser should treat the event before sending it."""

    __slots__ = ("fn", "prevent_default", "stop_propagation", "debounce", "throttle", "keys")

    def __init__(
        self,
        fn: Callable[..., Any],
        prevent_default: bool = False,
        stop_propagation: bool = False,
        debounce: int | None = None,
        throttle: int | None = None,
        keys: Sequence[str] | None = None,
    ) -> None:
        self.fn = fn
        self.prevent_default = prevent_default
        self.stop_propagation = stop_propagation
        self.debounce = debounce
        self.throttle = throttle
        self.keys = list(keys) if keys is not None else None

    def spec(self) -> list[Any]:
        return [self.prevent_default, self.stop_propagation, self.debounce, self.throttle, self.keys]


def event(
    fn: Callable[..., Any] | None = None,
    *,
    prevent_default: bool = False,
    stop_propagation: bool = False,
    debounce: int | None = None,
    throttle: int | None = None,
    keys: Sequence[str] | None = None,
) -> Any:
    """Wrap an event callback with browser-side options.

    ``debounce`` and ``throttle`` are in milliseconds and are applied in the
    browser, so skipped events never cross the network. ``keys`` limits a
    keyboard event to those ``KeyboardEvent.key`` values, e.g. ``["Enter",
    "Escape"]``: other keys are neither sent nor have their default prevented,
    so ``event(save, keys=["Enter"], prevent_default=True)`` blocks only Enter.
    Usable as ``event(fn, debounce=200)`` or as a decorator ``@event(prevent_default=True)``.
    """

    def wrap(f: Callable[..., Any]) -> EventHandler:
        return EventHandler(f, prevent_default, stop_propagation, debounce, throttle, keys)

    return wrap if fn is None else wrap(fn)


class Element:
    """One node of a description tree: a DOM tag, a component call, or a fragment."""

    __slots__ = ("type", "key", "attrs", "events", "children", "args", "kwargs", "ref")

    def __init__(
        self,
        type: Any,
        key: Any = None,
        attrs: dict[str, Any] | None = None,
        events: dict[str, EventHandler] | None = None,
        children: tuple[Any, ...] = (),
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        ref: Any = None,
    ) -> None:
        self.type = type
        self.key = key
        self.attrs = attrs if attrs is not None else {}
        self.events = events if events is not None else {}
        self.children = children
        self.args = args
        self.kwargs = kwargs if kwargs is not None else {}
        self.ref = ref

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if not isinstance(other, Element):
            return NotImplemented
        return (
            self.type is other.type
            and self.key == other.key
            and self.attrs == other.attrs
            and self.children == other.children
            and self.args == other.args
            and self.kwargs == other.kwargs
            and self.events.keys() == other.events.keys()
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        name = self.type if isinstance(self.type, str) else getattr(self.type, "__name__", self.type)
        key = f" key={self.key!r}" if self.key is not None else ""
        return f"<{name}{key}>"


def _kebab(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("-")
            out.append(ch.lower())
        else:
            out.append("-" if ch == "_" else ch)
    return "".join(out)


def _style_to_str(style: dict[str, Any]) -> str:
    return ";".join(f"{_kebab(k)}:{v}" for k, v in style.items() if v is not None and v is not False)


def _event_name(prop: str) -> str | None:
    if prop.startswith("on_"):
        name = prop[3:].replace("_", "")
    elif len(prop) > 2 and prop.startswith("on") and prop[2].isupper():
        name = prop[2:].lower()
    else:
        return None
    return _EVENT_ALIASES.get(name, name)


def _split_props(props: dict[str, Any]) -> tuple[Any, Any, dict[str, Any], dict[str, EventHandler]]:
    key = None
    ref = None
    attrs: dict[str, Any] = {}
    events: dict[str, EventHandler] = {}
    for name, value in props.items():
        if name == "key":
            key = value
            continue
        if name == "ref":
            ref = value
            continue
        ev = _event_name(name)
        if ev is not None:
            if value is None:
                continue
            if not isinstance(value, EventHandler):
                # A form submit would otherwise navigate away from the live page.
                value = EventHandler(value, prevent_default=ev == "submit")
            events[ev] = value
            continue
        name = _ATTR_ALIASES.get(name, name)
        if name in PROPERTY_PROPS:
            attrs[name] = "" if value is None else value
            continue
        if name.endswith("_"):
            name = name[:-1]
        if "_" in name:
            name = name.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            attrs[name] = ""
        elif name == "style" and isinstance(value, dict):
            attrs[name] = _style_to_str(value)
        else:
            attrs[name] = str(value)
    return key, ref, attrs, events


def h(tag: Any, props: dict[str, Any] | None = None, *children: Any) -> Element:
    """Create an element, ``h("div", {"class_name": "box"}, "hello")``.

    ``tag`` may also be a component, in which case props become its keyword arguments.
    """
    if isinstance(tag, Component):
        props = dict(props or {})
        key = props.pop("key", None)
        if children:
            props["children"] = children
        return Element(tag, key, kwargs=props)
    key, ref, attrs, events = _split_props(props or {})
    return Element(tag, key, attrs, events, children, ref=ref)


def fragment(*children: Any, key: Any = None) -> Element:
    """Group children without adding a DOM node."""
    return Element(FRAGMENT, key, children=children)


class _TagFactory:
    __slots__ = ("tag",)

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def __call__(self, *children: Any, **props: Any) -> Element:
        # A leading dict is accepted as props, matching reactpy's html.div({...}, ...)
        if children and isinstance(children[0], dict):
            props = {**children[0], **props}
            children = children[1:]
        if not props:
            return Element(self.tag, None, {}, {}, children)
        key, ref, attrs, events = _split_props(props)
        return Element(self.tag, key, attrs, events, children, ref=ref)

    def __repr__(self) -> str:
        return f"html.{self.tag}"


class _Html:
    """``html.div(...)``, ``html.button(...)`` and every other tag, created on first use."""

    def __getattr__(self, tag: str) -> _TagFactory:
        if tag.startswith("__"):
            raise AttributeError(tag)
        factory = _TagFactory(tag.rstrip("_").replace("_", "-"))
        setattr(self, tag, factory)
        return factory

    def __call__(self, tag: str, *children: Any, **props: Any) -> Element:
        return _TagFactory(tag)(*children, **props)


html = _Html()


class Component:
    """A render function turned into an element factory by :func:`component`."""

    def __init__(self, fn: Callable[..., Any], memo: bool = False) -> None:
        self.fn = fn
        self.memo = memo
        functools.update_wrapper(self, fn)

    def __call__(self, *args: Any, key: Any = None, **kwargs: Any) -> Element:
        return Element(self, key, args=args, kwargs=kwargs)

    def __repr__(self) -> str:
        return f"<component {self.__name__}>"


def component(fn: Callable[..., Any]) -> Component:
    """Mark a function as a component. Calling it returns an element, not its output."""
    return Component(fn)


def memo(fn: Callable[..., Any] | Component) -> Component:
    """A component that skips re-rendering while its arguments are unchanged."""
    return Component(fn.fn if isinstance(fn, Component) else fn, memo=True)


class JsComponent:
    """A JavaScript module drawn inside an element; made by :func:`js_component`."""

    def __init__(self, module: str, export: str = "default", tag: str = "div") -> None:
        self.module = module
        self.export = export
        self.tag = tag
        self.__name__ = f"{module}:{export}"

    def __call__(self, *, key: Any = None, ref: Any = None, **props: Any) -> Element:
        attrs: dict[str, Any] = {}
        events: dict[str, EventHandler] = {}
        for name, value in props.items():
            if name.startswith("on_") and (callable(value) or isinstance(value, EventHandler)):
                events[name[3:]] = value if isinstance(value, EventHandler) else EventHandler(value)
            else:
                attrs[name] = value
        return Element(self, key, attrs, events, ref=ref)

    def __repr__(self) -> str:
        return f"<js_component {self.__name__}>"


def js_component(module: str, export: str = "default", tag: str = "div") -> JsComponent:
    """Use a JavaScript module as a component, for a chart, map or editor.

    ``module`` is a URL the browser can import, e.g. ``"/static/chart.js"`` or a
    CDN address. Its ``export`` is a function the browser calls as
    ``mount(element, props, emit)`` once a ``<tag>`` for it is on the page::

        export default function mount(element, props, emit) {
          // draw into element; emit("select", {id: 3}) calls on_select in Python
          return { update(props) { ... }, unmount() { ... } };
        }

    Props must be JSON values; ``on_<name>`` props are Python callbacks that
    ``emit("<name>", data)`` calls with ``data`` as the event. ``update`` runs
    when props change, ``unmount`` when the element is removed.
    """
    return JsComponent(module, export, tag)
