"""Write src/reaction/core/_html_types.py: typed call signatures for html.<tag>.

    uv run python scripts/gen_html_types.py

Each tag group gets a Protocol whose ``__call__`` names the attributes and events
it knows, with types, and ends in ``**extra: Any`` so any other attribute
(``data_*``, ``aria_*`` not listed, custom ones) still type-checks. The file is
only imported by type checkers, so it costs nothing at runtime.
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "reaction" / "core" / "_html_types.py"

TEXT = "Text"  # str | int | float
FLAG = "bool"
HANDLER = "Handler"

GLOBAL = {
    "key": "Any",
    "ref": "Any",
    "id": TEXT,
    "class_name": TEXT,
    "class_": TEXT,
    "style": "str | Mapping[str, Any]",
    "title": TEXT,
    "hidden": FLAG,
    "tabindex": "int",
    "role": "str",
    "lang": "str",
    "dir": "str",
    "draggable": "bool | str",
    "contenteditable": "bool | str",
    "spellcheck": "bool | str",
    "translate": "str",
    "accesskey": "str",
    "autofocus": FLAG,
    "inert": FLAG,
    "popover": "str",
    "slot": "str",
    "enterkeyhint": "str",
    "inputmode": "str",
    "aria_label": TEXT,
    "aria_labelledby": "str",
    "aria_describedby": "str",
    "aria_hidden": "bool | str",
    "aria_expanded": "bool | str",
    "aria_controls": "str",
    "aria_current": "bool | str",
    "aria_pressed": "bool | str",
    "aria_selected": "bool | str",
    "aria_checked": "bool | str",
    "aria_disabled": "bool | str",
    "aria_live": "str",
    "aria_busy": "bool | str",
    "aria_haspopup": "bool | str",
    "aria_invalid": "bool | str",
    "dangerously_set_inner_html": "str",
}

EVENTS = [
    "click", "double_click", "context_menu", "aux_click",
    "mouse_down", "mouse_up", "mouse_enter", "mouse_leave", "mouse_move", "mouse_over", "mouse_out",
    "pointer_down", "pointer_up", "pointer_move", "pointer_enter", "pointer_leave", "pointer_cancel",
    "touch_start", "touch_move", "touch_end", "touch_cancel",
    "key_down", "key_up",
    "focus", "blur", "focus_in", "focus_out",
    "input", "change", "submit", "reset", "invalid", "select",
    "scroll", "wheel", "copy", "cut", "paste",
    "drag", "drag_start", "drag_end", "drag_enter", "drag_leave", "drag_over", "drop",
    "animation_start", "animation_end", "transition_end",
    "load", "error", "toggle",
]

SPECIFIC: dict[str, dict[str, str]] = {
    "A": {"href": "str", "target": "str", "rel": "str", "download": "bool | str", "hreflang": "str",
          "type": "str", "referrerpolicy": "str", "ping": "str"},
    "Img": {"src": "str", "alt": "str", "width": TEXT, "height": TEXT, "loading": "str", "decoding": "str",
            "srcset": "str", "sizes": "str", "crossorigin": "str", "referrerpolicy": "str", "usemap": "str",
            "fetchpriority": "str"},
    "Input": {"type": "str", "name": "str", "value": TEXT, "checked": FLAG, "default_value": TEXT,
              "default_checked": FLAG, "placeholder": "str",
              "disabled": FLAG, "readonly": FLAG, "required": FLAG, "min": TEXT, "max": TEXT, "step": TEXT,
              "minlength": "int", "maxlength": "int", "pattern": "str", "multiple": FLAG, "accept": "str",
              "autocomplete": "str", "size": "int", "list": "str", "form": "str", "indeterminate": FLAG,
              "capture": "str", "src": "str", "alt": "str", "width": TEXT, "height": TEXT},
    "Button": {"type": "str", "name": "str", "value": TEXT, "disabled": FLAG, "form": "str",
               "formaction": "str", "formmethod": "str", "formnovalidate": FLAG, "popovertarget": "str"},
    "Form": {"action": "str", "method": "str", "enctype": "str", "novalidate": FLAG, "autocomplete": "str",
             "target": "str", "name": "str", "accept_charset": "str"},
    "Label": {"html_for": "str", "for_": "str", "form": "str"},
    "Select": {"name": "str", "value": TEXT, "default_value": TEXT, "multiple": FLAG, "disabled": FLAG, "required": FLAG,
               "size": "int", "autocomplete": "str", "form": "str"},
    "Option": {"value": TEXT, "selected": FLAG, "disabled": FLAG, "label": "str"},
    "Optgroup": {"label": "str", "disabled": FLAG},
    "Textarea": {"name": "str", "value": TEXT, "default_value": TEXT, "placeholder": "str", "rows": "int", "cols": "int",
                 "disabled": FLAG, "readonly": FLAG, "required": FLAG, "maxlength": "int", "minlength": "int",
                 "wrap": "str", "autocomplete": "str", "form": "str"},
    "Fieldset": {"disabled": FLAG, "name": "str", "form": "str"},
    "Cell": {"colspan": "int", "rowspan": "int", "headers": "str", "scope": "str", "abbr": "str"},
    "Col": {"span": "int"},
    "Media": {"src": "str", "controls": FLAG, "autoplay": FLAG, "loop": FLAG, "muted": FLAG,
              "preload": "str", "poster": "str", "width": TEXT, "height": TEXT, "playsinline": FLAG,
              "crossorigin": "str"},
    "Source": {"src": "str", "type": "str", "srcset": "str", "sizes": "str", "media": "str",
               "width": TEXT, "height": TEXT},
    "Track": {"src": "str", "kind": "str", "srclang": "str", "label": "str", "default": FLAG},
    "Iframe": {"src": "str", "srcdoc": "str", "width": TEXT, "height": TEXT, "allow": "str",
               "allowfullscreen": FLAG, "loading": "str", "name": "str", "sandbox": "str",
               "referrerpolicy": "str"},
    "Link": {"rel": "str", "href": "str", "type": "str", "media": "str", "sizes": "str",
             "crossorigin": "str", "as_": "str", "hreflang": "str", "integrity": "str", "disabled": FLAG},
    "Meta": {"name": "str", "content": TEXT, "charset": "str", "http_equiv": "str", "property": "str",
             "media": "str"},
    "Script": {"src": "str", "type": "str", "async_": FLAG, "defer": FLAG, "crossorigin": "str",
               "integrity": "str", "nomodule": FLAG, "referrerpolicy": "str"},
    "Style": {"media": "str"},
    "Open": {"open": FLAG, "name": "str"},
    "Range": {"value": TEXT, "max": TEXT, "min": TEXT, "low": TEXT, "high": TEXT, "optimum": TEXT, "form": "str"},
    "Ol": {"start": "int", "reversed": FLAG, "type": "str"},
    "Li": {"value": "int"},
    "Time": {"datetime": "str"},
    "Data": {"value": TEXT},
    "Output": {"html_for": "str", "for_": "str", "name": "str", "form": "str"},
    "Canvas": {"width": TEXT, "height": TEXT},
    "Object": {"data": "str", "type": "str", "width": TEXT, "height": TEXT, "name": "str", "src": "str"},
    "Area": {"alt": "str", "coords": "str", "shape": "str", "href": "str", "target": "str", "download": "bool | str",
             "rel": "str"},
    "Quote": {"cite": "str"},
    "Edit": {"cite": "str", "datetime": "str"},
    "Svg": {
        "xmlns": "str", "view_box": "str", "viewBox": "str", "width": TEXT, "height": TEXT,
        "preserve_aspect_ratio": "str", "fill": "str", "fill_opacity": TEXT, "fill_rule": "str",
        "stroke": "str", "stroke_width": TEXT, "stroke_linecap": "str", "stroke_linejoin": "str",
        "stroke_dasharray": TEXT, "stroke_dashoffset": TEXT, "stroke_opacity": TEXT, "opacity": TEXT,
        "transform": "str", "d": "str", "cx": TEXT, "cy": TEXT, "r": TEXT, "rx": TEXT, "ry": TEXT,
        "x": TEXT, "y": TEXT, "x1": TEXT, "y1": TEXT, "x2": TEXT, "y2": TEXT, "dx": TEXT, "dy": TEXT,
        "points": "str", "path_length": TEXT, "href": "str", "offset": TEXT, "stop_color": "str",
        "stop_opacity": TEXT, "gradient_units": "str", "gradient_transform": "str", "clip_path": "str",
        "mask": "str", "filter": "str", "marker_start": "str", "marker_mid": "str", "marker_end": "str",
        "text_anchor": "str", "dominant_baseline": "str", "font_size": TEXT, "font_family": "str",
        "font_weight": TEXT, "vector_effect": "str", "pointer_events": "str",
    },
}

# Which signature each html.<tag> uses; tags not listed use the global one.
TAGS: dict[str, str] = {
    "a": "A", "img": "Img", "input": "Input", "button": "Button", "form": "Form", "label": "Label",
    "select": "Select", "option": "Option", "optgroup": "Optgroup", "textarea": "Textarea",
    "fieldset": "Fieldset", "td": "Cell", "th": "Cell", "col": "Col", "colgroup": "Col",
    "video": "Media", "audio": "Media", "source": "Source", "track": "Track", "iframe": "Iframe",
    "embed": "Object", "object": "Object", "link": "Link", "meta": "Meta", "script": "Script",
    "style": "Style", "details": "Open", "dialog": "Open", "progress": "Range", "meter": "Range",
    "ol": "Ol", "li": "Li", "time": "Time", "data": "Data", "output": "Output", "canvas": "Canvas",
    "area": "Area", "blockquote": "Quote", "q": "Quote", "del_": "Edit", "ins": "Edit",
    **{t: "Svg" for t in (
        "svg", "g", "defs", "symbol", "use", "path", "circle", "ellipse", "line", "polyline", "polygon",
        "rect", "text", "tspan", "textPath", "image", "foreignObject", "linearGradient", "radialGradient",
        "stop", "clipPath", "mask", "pattern", "marker", "filter",
    )},
}


def signature(name: str, attrs: dict[str, str]) -> str:
    params = {**GLOBAL, **attrs}
    lines = [f"class {name}Tag(Protocol):", "    def __call__(", "        self,", "        *children: Any,"]
    for attr, kind in params.items():
        lines.append(f"        {attr}: {kind} | None = ...,")
    for ev in EVENTS:
        lines.append(f"        on_{ev}: {HANDLER} | None = ...,")
    lines += ["        **extra: Any,", "    ) -> Element: ..."]
    return "\n".join(lines)


def main() -> None:
    parts = [
        '"""Typed call signatures for ``html.<tag>``; only type checkers import this.',
        "",
        "Generated by scripts/gen_html_types.py: edit that and run it again.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from collections.abc import Callable, Mapping",
        "from typing import Any, Protocol",
        "",
        "from .element import Element, EventHandler",
        "",
        "Text = str | int | float",
        "Handler = Callable[..., Any] | EventHandler",
        "",
        "",
        signature("Global", {}),
    ]
    for group, attrs in SPECIFIC.items():
        parts += ["", "", signature(group, attrs)]
    parts += ["", "", "TAG_TYPES = {"]
    parts += [f'    "{tag}": "{group}Tag",' for tag, group in sorted(TAGS.items())]
    parts += ["}", ""]
    OUT.write_text("\n".join(parts))
    print(f"wrote {OUT} ({len(SPECIFIC) + 1} signatures, {len(TAGS)} tags mapped)")


if __name__ == "__main__":
    main()
