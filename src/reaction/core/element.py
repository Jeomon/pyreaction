"""Elements: the immutable description of what a component wants on screen.

Props are normalised once, when the element is created, into DOM attributes and
event handlers, so the reconciler only ever compares ready-to-send values.
"""

from __future__ import annotations

import base64
import functools
import inspect
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._html_types import (
        ATag,
        AreaTag,
        ButtonTag,
        CanvasTag,
        CellTag,
        ColTag,
        DataTag,
        EditTag,
        FieldsetTag,
        FormTag,
        GlobalTag,
        IframeTag,
        ImgTag,
        InputTag,
        LabelTag,
        LiTag,
        LinkTag,
        MediaTag,
        MetaTag,
        ObjectTag,
        OlTag,
        OpenTag,
        OptgroupTag,
        OptionTag,
        OutputTag,
        QuoteTag,
        RangeTag,
        ScriptTag,
        SelectTag,
        SourceTag,
        StyleTag,
        SvgTag,
        TextareaTag,
        TimeTag,
        TrackTag,
    )

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
    # Uncontrolled inputs: only the starting value, the browser keeps what is typed.
    "default_value": "defaultValue",
    "defaultValue": "defaultValue",
    "default_checked": "defaultChecked",
    "defaultChecked": "defaultChecked",
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
        "stop_immediate_propagation", "form",
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
        form: bool = False,
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
        # Send the fields of the element's form, checked first (a button's form_action).
        self.form = form

    def spec(self) -> list[Any]:
        return [
            self.prevent_default, self.stop_propagation, self.debounce, self.throttle, self.keys,
            self.code, self.stop_immediate_propagation, self.form,
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
    browser, so skipped events never cross the network. ``throttle`` sends at most
    one event per period and always the last one of a burst. ``keys`` limits a
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

    The code sees ``event`` and ``element``: ``on_focus=js("element.select()")``.
    It is sent to the page as written, so never build it from user input.
    """
    if not isinstance(code, str):
        raise TypeError("js() takes JavaScript source as a string")
    return EventHandler(None, code=code)


class _EmptyDict(dict):
    """The one empty dict shared by every element without attributes, events or
    keyword arguments, so 10,000 rows do not hold 30,000 empty dicts. Elements are
    never changed in place; this refuses writes so a mistake shows at once."""

    __slots__ = ()

    def _read_only(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("an element's props are read-only: build a new dict instead")

    __setitem__ = _read_only
    __delitem__ = _read_only
    setdefault = _read_only
    update = _read_only
    pop = _read_only
    popitem = _read_only
    clear = _read_only
    __ior__ = _read_only


_EMPTY: dict[str, Any] = _EmptyDict()


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
        self.attrs = attrs if attrs else _EMPTY
        self.events = events if events else _EMPTY
        self.children = children
        self.args = args
        self.kwargs = kwargs if kwargs else _EMPTY
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
    """The DOM event a prop listens to; ``on_click_capture`` gives ``click:capture``,
    a listener that runs on the way down, before the target's own handlers."""
    if prop.startswith("on_"):
        name = prop[3:].replace("_", "")
    elif len(prop) > 2 and prop.startswith("on") and prop[2].isupper():
        name = prop[2:].lower()
    else:
        return None
    if name.endswith("capture") and name not in _CAPTURE_NAMED:
        base = name[: -len("capture")]
        return _EVENT_ALIASES.get(base, base) + ":capture"
    return _EVENT_ALIASES.get(name, name)


# Events whose own name ends in "capture" (onGotPointerCaptureCapture captures them).
_CAPTURE_NAMED = frozenset({"gotpointercapture", "lostpointercapture"})


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
        if name in ("form_action", "formAction") and callable(value):
            events["click"] = _form_action(value)
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


def _form_action(fn: Callable[..., Any]) -> EventHandler:
    """``html.button(form_action=fn)``: this button submits its form to ``fn(fields)``
    instead of the form's own action, after the browser checks required fields."""
    if inspect.iscoroutinefunction(fn):

        async def run_async(e: Any) -> Any:
            return await fn(e.form)

        return EventHandler(run_async, prevent_default=True, form=True)

    def run(e: Any) -> Any:
        return fn(e.form)

    return EventHandler(run, prevent_default=True, form=True)


# Written anywhere, these go into the page's <head>, as in React 19.
HOISTED = frozenset({"title", "meta", "link"})


def _hoist(el: Element) -> Element:
    from .components import Hoisted

    return Hoisted(el, key=el.key)


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
    el = Element(tag, key, attrs, events, children, ref=ref)
    return _hoist(el) if tag in HOISTED else el


def _check_raw(attrs: dict[str, Any], children: tuple[Any, ...]) -> None:
    if RAW_HTML in attrs and any(c is not None and c is not False for c in children):
        raise TypeError("an element with dangerously_set_inner_html cannot also have children")


def is_valid_element(value: Any) -> bool:
    """Whether ``value`` is an element (what ``html.div(...)`` or a component call makes)."""
    return isinstance(value, Element)


def clone_element(element: Element, *children: Any, **props: Any) -> Element:
    """A copy of ``element`` with ``props`` merged over its own, and ``children``
    replacing its children when given. ``key`` and ``ref`` are kept unless passed."""
    if not isinstance(element, Element):
        raise TypeError(f"clone_element takes an element, got {type(element).__name__}")
    key = props.pop("key", element.key)
    if isinstance(element.type, str) and element.type not in (FRAGMENT, PORTAL):
        ref = props.pop("ref", element.ref)
        _, _, attrs, events = _split_props(props)
        new_children = children if children else element.children
        merged = {**element.attrs, **attrs}
        _check_raw(merged, new_children)
        return Element(element.type, key, merged, {**element.events, **events}, new_children, ref=ref)
    if element.type in (FRAGMENT, PORTAL):
        return Element(element.type, key, dict(element.attrs), children=children or element.children)
    kwargs = {**element.kwargs, **props}
    args = element.args
    if children:
        if "children" in element.kwargs:
            kwargs["children"] = children
        else:
            args = children
    return Element(element.type, key, args=args, kwargs=kwargs)


def _child_list(children: Any) -> list[Any]:
    """Children flattened out of nested lists and tuples (not out of fragments)."""
    if children is None:
        return []
    if not isinstance(children, (list, tuple)):
        return [children]
    out: list[Any] = []
    for child in children:
        if isinstance(child, (list, tuple)):
            out.extend(_child_list(child))
        else:
            out.append(child)
    return out


def _empty(child: Any) -> bool:
    return child is None or isinstance(child, bool)


class Children:
    """React's ``Children`` helpers, for components that work on the children they
    are given (``*children``, a list, or one child). Nested lists are flattened;
    fragments count as one child, as in React."""

    @staticmethod
    def map(children: Any, fn: Callable[..., Any]) -> list[Any]:
        """``fn(child, index)`` for each child (``fn(child)`` works too); empty
        results are dropped."""
        two = _wants_two(fn)
        out = [fn(c, i) if two else fn(c) for i, c in enumerate(_child_list(children))]
        return [c for c in _child_list(out) if not _empty(c)]

    @staticmethod
    def for_each(children: Any, fn: Callable[..., Any]) -> None:
        two = _wants_two(fn)
        for i, child in enumerate(_child_list(children)):
            fn(child, i) if two else fn(child)

    @staticmethod
    def count(children: Any) -> int:
        """How many children there are, empty ones (None, True, False) included."""
        return len(_child_list(children))

    @staticmethod
    def to_array(children: Any) -> list[Any]:
        """The children as a flat list, without empty ones."""
        return [c for c in _child_list(children) if not _empty(c)]

    @staticmethod
    def only(children: Any) -> Element:
        """The one child, which must be an element."""
        items = Children.to_array(children)
        if len(items) != 1 or not isinstance(items[0], Element):
            raise ValueError(f"expected exactly one element child, got {len(items)} child(ren)")
        return items[0]


def _wants_two(fn: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2 or any(p.kind is p.VAR_POSITIONAL for p in params)


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
            el = Element(self.tag, None, None, None, children)
        else:
            if self.tag == "form" and callable(props.get("action")):
                # React 19's <form action={fn}>: the Form component, with pending state.
                from .components import Form

                return Form(*children, **props)
            key, ref, attrs, events = _split_props(props)
            _check_raw(attrs, children)
            el = Element(self.tag, key, attrs, events, children, ref=ref)
        return _hoist(el) if self.tag in HOISTED else el

    def __repr__(self) -> str:
        return f"html.{self.tag}"


class _Html:
    """``html.div(...)``, ``html.button(...)`` and every other tag, created on first use.

    The standard HTML, SVG and MathML tags are declared below so editors and type
    checkers know them; any other name (a custom element, ``html.my_widget`` for
    ``<my-widget>``) works too.
    """

    # Document and sections
    html: GlobalTag
    head: GlobalTag
    title: GlobalTag
    base: GlobalTag
    link: LinkTag
    meta: MetaTag
    style: StyleTag
    body: GlobalTag
    address: GlobalTag
    article: GlobalTag
    aside: GlobalTag
    footer: GlobalTag
    header: GlobalTag
    h1: GlobalTag
    h2: GlobalTag
    h3: GlobalTag
    h4: GlobalTag
    h5: GlobalTag
    h6: GlobalTag
    hgroup: GlobalTag
    main: GlobalTag
    nav: GlobalTag
    section: GlobalTag
    search: GlobalTag
    # Text content
    blockquote: QuoteTag
    dd: GlobalTag
    div: GlobalTag
    dl: GlobalTag
    dt: GlobalTag
    figcaption: GlobalTag
    figure: GlobalTag
    hr: GlobalTag
    li: LiTag
    menu: GlobalTag
    ol: OlTag
    p: GlobalTag
    pre: GlobalTag
    ul: GlobalTag
    # Inline text
    a: ATag
    abbr: GlobalTag
    b: GlobalTag
    bdi: GlobalTag
    bdo: GlobalTag
    br: GlobalTag
    cite: GlobalTag
    code: GlobalTag
    data: DataTag
    dfn: GlobalTag
    em: GlobalTag
    i: GlobalTag
    kbd: GlobalTag
    mark: GlobalTag
    q: QuoteTag
    rp: GlobalTag
    rt: GlobalTag
    ruby: GlobalTag
    s: GlobalTag
    samp: GlobalTag
    small: GlobalTag
    span: GlobalTag
    strong: GlobalTag
    sub: GlobalTag
    sup: GlobalTag
    time: TimeTag
    u: GlobalTag
    var: GlobalTag
    wbr: GlobalTag
    # Media and embedded
    area: AreaTag
    audio: MediaTag
    img: ImgTag
    map: GlobalTag
    track: TrackTag
    video: MediaTag
    embed: ObjectTag
    iframe: IframeTag
    object: ObjectTag
    picture: GlobalTag
    source: SourceTag
    canvas: CanvasTag
    noscript: GlobalTag
    script: ScriptTag
    # Edits and tables
    del_: EditTag
    ins: EditTag
    caption: GlobalTag
    col: ColTag
    colgroup: ColTag
    table: GlobalTag
    tbody: GlobalTag
    td: CellTag
    tfoot: GlobalTag
    th: CellTag
    thead: GlobalTag
    tr: GlobalTag
    # Forms
    button: ButtonTag
    datalist: GlobalTag
    fieldset: FieldsetTag
    form: FormTag
    input: InputTag
    label: LabelTag
    legend: GlobalTag
    meter: RangeTag
    optgroup: OptgroupTag
    option: OptionTag
    output: OutputTag
    progress: RangeTag
    select: SelectTag
    textarea: TextareaTag
    # Interactive and components
    details: OpenTag
    dialog: OpenTag
    summary: GlobalTag
    slot: GlobalTag
    template: GlobalTag
    # SVG
    svg: SvgTag
    g: SvgTag
    defs: SvgTag
    symbol: SvgTag
    use: SvgTag
    path: SvgTag
    circle: SvgTag
    ellipse: SvgTag
    line: SvgTag
    polyline: SvgTag
    polygon: SvgTag
    rect: SvgTag
    text: SvgTag
    tspan: SvgTag
    textPath: SvgTag
    image: SvgTag
    foreignObject: SvgTag
    linearGradient: SvgTag
    radialGradient: SvgTag
    stop: SvgTag
    clipPath: SvgTag
    mask: SvgTag
    pattern: SvgTag
    marker: SvgTag
    filter: SvgTag
    # MathML
    math: GlobalTag
    mi: GlobalTag
    mn: GlobalTag
    mo: GlobalTag
    mrow: GlobalTag
    msup: GlobalTag
    msub: GlobalTag
    mfrac: GlobalTag
    msqrt: GlobalTag

    def __getattr__(self, tag: str) -> GlobalTag:
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
        self.profiler = False  # set on Profiler: the reconciler times its renders
        functools.update_wrapper(self, fn)

    def __call__(self, *args: Any, key: Any = None, **kwargs: Any) -> Element:
        return Element(self, key, args=args, kwargs=kwargs)

    def __repr__(self) -> str:
        return f"<component {self.__name__}>"


def component(fn: Callable[..., Any]) -> Component:
    """Mark a function as a component. Calling it returns an element, not its output."""
    return Component(fn)


def forward_ref(fn: Callable[..., Any] | Component) -> Component:
    """For code ported from React: a component that receives ``ref``. In pyreaction
    (as in React 19) any component can take ``ref=`` as an ordinary argument and
    pass it to an element, so this is the same as :func:`component`."""
    return fn if isinstance(fn, Component) else Component(fn)


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


def js_component_from_string(code: str, export: str = "default", tag: str = "div") -> JsComponent:
    """A :func:`js_component` whose module is written inline, for a small widget
    that does not deserve its own file::

        Clock = js_component_from_string('''
            export default function mount(element, props, emit) {
              const tick = () => (element.textContent = new Date().toLocaleTimeString(props.locale));
              tick();
              const timer = setInterval(tick, 1000);
              return { update(next) { props = next; tick(); }, unmount() { clearInterval(timer); } };
            }
        ''')

    The browser imports ``code`` as a module from a ``data:`` URL, so it can import
    other modules by full URL (``https://...``) but not by relative path. Pages
    whose Content-Security-Policy limits ``script-src`` need ``data:`` allowed.
    """
    module = "data:text/javascript;base64," + base64.b64encode(code.encode()).decode("ascii")
    return JsComponent(module, export, tag)
