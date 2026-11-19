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
    "js",
    "js_component",
    "memo",
    "portal",
]

FRAGMENT = "#fragment"

# Set as DOM properties rather than attributes, so they reflect live state
# (an attribute only sets the initial value of an <input>).
PROPERTY_PROPS = frozenset({"value", "checked", "selected", "indeterminate", "muted"})

# Where an element's raw inner HTML is kept among its attributes; not a valid
# attribute name, so it cannot collide with a real one.
RAW_HTML = "#html"
# Marks an element whose contents are raw HTML, so the browser does not try to
# match them against the tree when adopting server-rendered markup.
RAW_MARK = "data-pyreaction-raw"

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
    """A callback plus how the browser should treat the event before sending it.

    ``code`` makes it a browser-side handler (see :func:`js`): it runs in the page
    and nothing is sent to the server.
    """

    __slots__ = (
        "fn", "prevent_default", "stop_propagation", "debounce", "throttle", "keys", "code",
        "stop_immediate_propagation",
    )

    def __init__(
        self,
        fn: Callable[..., Any] | None,
        prevent_default: bool = False,
        stop_propagation: bool = False,
        debounce: int | None = None,
        throttle: int | None = None,
        keys: Sequence[str] | None = None,
        code: str | None = None,
        stop_immediate_propagation: bool = False,
    ) -> None:
        if fn is None and code is None:
            raise TypeError("an event handler needs a function or js() code")
        if fn is not None and not callable(fn):
            hint = " To run JavaScript in the browser, use js(...)." if isinstance(fn, str) else ""
            raise TypeError(f"event handlers must be functions, got {type(fn).__name__} {fn!r}.{hint}")
        self.fn = fn
        self.prevent_default = prevent_default
        self.stop_propagation = stop_propagation
        self.debounce = debounce
        self.throttle = throttle
        self.keys = list(keys) if keys is not None else None
        self.code = code
        self.stop_immediate_propagation = stop_immediate_propagation

    def spec(self) -> list[Any]:
        return [
            self.prevent_default, self.stop_propagation, self.debounce, self.throttle, self.keys,
            self.code, self.stop_immediate_propagation,
        ]


def event(
    fn: Callable[..., Any] | EventHandler | None = None,
    *,
    prevent_default: bool = False,
    stop_propagation: bool = False,
    stop_immediate_propagation: bool = False,
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
    ``fn`` may also be :func:`js` code. Usable as ``event(fn, debounce=200)`` or as
    a decorator ``@event(prevent_default=True)``.
    """

    def wrap(f: Callable[..., Any] | EventHandler) -> EventHandler:
        code = f.code if isinstance(f, EventHandler) else None
        target = f.fn if isinstance(f, EventHandler) else f
        return EventHandler(
            target, prevent_default, stop_propagation, debounce, throttle, keys, code,
            stop_immediate_propagation,
        )

    return wrap if fn is None else wrap(fn)


def js(code: str) -> EventHandler:
    """An event handler that runs ``code`` in the browser, with no round trip.

    The code sees ``event`` and ``element``: ``on_click=js("element.select()")``.
    It is sent to the page as written, so never build it from user input.
    """
    if not isinstance(code, str):
        raise TypeError("js() takes JavaScript source as a string")
    return EventHandler(None, code=code)


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
        if name in ("dangerously_set_inner_html", "dangerouslySetInnerHTML"):
            if value is not None:
                attrs[RAW_HTML] = str(value)
                attrs[RAW_MARK] = ""
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
    _check_raw(attrs, children)
    return Element(tag, key, attrs, events, children, ref=ref)


def _check_raw(attrs: dict[str, Any], children: tuple[Any, ...]) -> None:
    if RAW_HTML in attrs and any(c is not None and c is not False for c in children):
        raise TypeError("an element with dangerously_set_inner_html cannot also have children")


def fragment(*children: Any, key: Any = None) -> Element:
    """Group children without adding a DOM node."""
    return Element(FRAGMENT, key, children=children)


PORTAL = "#portal"


def portal(*children: Any, target: str = "body", key: Any = None) -> Element:
    """Render ``children`` at the end of another element of the page (``target`` is a
    CSS selector, ``body`` by default), outside this one's layout: for modals,
    toasts and menus that must escape ``overflow: hidden`` or a stacking context.
    State, context and events work as if they were here. Portals appear once the
    page is live; they are not part of the server's first HTML."""
    return Element(PORTAL, key, {"target": target}, children=children)


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
        _check_raw(attrs, children)
        return Element(self.tag, key, attrs, events, children, ref=ref)

    def __repr__(self) -> str:
        return f"html.{self.tag}"


class _Html:
    """``html.div(...)``, ``html.button(...)`` and every other tag, created on first use.

    The standard HTML, SVG and MathML tags are declared below so editors and type
    checkers know them; any other name (a custom element, ``html.my_widget`` for
    ``<my-widget>``) works too.
    """

    # Document and sections
    html: _TagFactory
    head: _TagFactory
    title: _TagFactory
    base: _TagFactory
    link: _TagFactory
    meta: _TagFactory
    style: _TagFactory
    body: _TagFactory
    address: _TagFactory
    article: _TagFactory
    aside: _TagFactory
    footer: _TagFactory
    header: _TagFactory
    h1: _TagFactory
    h2: _TagFactory
    h3: _TagFactory
    h4: _TagFactory
    h5: _TagFactory
    h6: _TagFactory
    hgroup: _TagFactory
    main: _TagFactory
    nav: _TagFactory
    section: _TagFactory
    search: _TagFactory
    # Text content
    blockquote: _TagFactory
    dd: _TagFactory
    div: _TagFactory
    dl: _TagFactory
    dt: _TagFactory
    figcaption: _TagFactory
    figure: _TagFactory
    hr: _TagFactory
    li: _TagFactory
    menu: _TagFactory
    ol: _TagFactory
    p: _TagFactory
    pre: _TagFactory
    ul: _TagFactory
    # Inline text
    a: _TagFactory
    abbr: _TagFactory
    b: _TagFactory
    bdi: _TagFactory
    bdo: _TagFactory
    br: _TagFactory
    cite: _TagFactory
    code: _TagFactory
    data: _TagFactory
    dfn: _TagFactory
    em: _TagFactory
    i: _TagFactory
    kbd: _TagFactory
    mark: _TagFactory
    q: _TagFactory
    rp: _TagFactory
    rt: _TagFactory
    ruby: _TagFactory
    s: _TagFactory
    samp: _TagFactory
    small: _TagFactory
    span: _TagFactory
    strong: _TagFactory
    sub: _TagFactory
    sup: _TagFactory
    time: _TagFactory
    u: _TagFactory
    var: _TagFactory
    wbr: _TagFactory
    # Media and embedded
    area: _TagFactory
    audio: _TagFactory
    img: _TagFactory
    map: _TagFactory
    track: _TagFactory
    video: _TagFactory
    embed: _TagFactory
    iframe: _TagFactory
    object: _TagFactory
    picture: _TagFactory
    source: _TagFactory
    canvas: _TagFactory
    noscript: _TagFactory
    script: _TagFactory
    # Edits and tables
    del_: _TagFactory
    ins: _TagFactory
    caption: _TagFactory
    col: _TagFactory
    colgroup: _TagFactory
    table: _TagFactory
    tbody: _TagFactory
    td: _TagFactory
    tfoot: _TagFactory
    th: _TagFactory
    thead: _TagFactory
    tr: _TagFactory
    # Forms
    button: _TagFactory
    datalist: _TagFactory
    fieldset: _TagFactory
    form: _TagFactory
    input: _TagFactory
    label: _TagFactory
    legend: _TagFactory
    meter: _TagFactory
    optgroup: _TagFactory
    option: _TagFactory
    output: _TagFactory
    progress: _TagFactory
    select: _TagFactory
    textarea: _TagFactory
    # Interactive and components
    details: _TagFactory
    dialog: _TagFactory
    summary: _TagFactory
    slot: _TagFactory
    template: _TagFactory
    # SVG
    svg: _TagFactory
    g: _TagFactory
    defs: _TagFactory
    symbol: _TagFactory
    use: _TagFactory
    path: _TagFactory
    circle: _TagFactory
    ellipse: _TagFactory
    line: _TagFactory
    polyline: _TagFactory
    polygon: _TagFactory
    rect: _TagFactory
    text: _TagFactory
    tspan: _TagFactory
    textPath: _TagFactory
    image: _TagFactory
    foreignObject: _TagFactory
    linearGradient: _TagFactory
    radialGradient: _TagFactory
    stop: _TagFactory
    clipPath: _TagFactory
    mask: _TagFactory
    pattern: _TagFactory
    marker: _TagFactory
    filter: _TagFactory
    # MathML
    math: _TagFactory
    mi: _TagFactory
    mn: _TagFactory
    mo: _TagFactory
    mrow: _TagFactory
    msup: _TagFactory
    msub: _TagFactory
    mfrac: _TagFactory
    msqrt: _TagFactory

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

    __name__: str

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

    def __call__(
        self, *children: Any, key: Any = None, ref: Any = None, fallback: Any = None, **props: Any
    ) -> Element:
        attrs: dict[str, Any] = {}
        events: dict[str, EventHandler] = {}
        for name, value in props.items():
            if name.startswith("on_") and (callable(value) or isinstance(value, EventHandler)):
                events[name[3:]] = value if isinstance(value, EventHandler) else EventHandler(value)
            else:
                attrs[name] = value
        if fallback is not None and not isinstance(fallback, str):
            from .ssr import render_to_string

            fallback = render_to_string(fallback)
        return Element(self, key, attrs, events, children, kwargs={"fallback": fallback}, ref=ref)

    def __repr__(self) -> str:
        return f"<js_component {self.__name__}>"


def js_component(module: str, export: str = "default", tag: str = "div") -> JsComponent:
    """Use a JavaScript module as a component, for a chart, map or editor.

    ``module`` is a URL the browser can import, e.g. ``"/static/chart.js"`` or a
    CDN address. Its ``export`` is a function the browser calls as
    ``mount(element, props, emit, slot)`` once a ``<tag>`` for it is on the page::

        export default function mount(element, props, emit, slot) {
          // draw into element; emit("select", {id: 3}) calls on_select in Python
          return { update(props) { ... }, unmount() { ... } };
        }

    Props must be JSON values; ``on_<name>`` props are Python callbacks that
    ``emit("<name>", data)`` calls with ``data`` as the event. ``update`` runs
    when props change, ``unmount`` when the element is removed.

    Positional children are rendered by pyreaction into ``slot``, an element the
    module places where they belong (``element.append(slot)``, or inside its own
    markup); they stay live Python components. ``fallback`` (an element or HTML
    string) shows until the module has loaded, in the server's HTML too.
    """
    return JsComponent(module, export, tag)
