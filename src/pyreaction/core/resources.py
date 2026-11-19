"""Resource hints, as in react-dom: tell the browser early about files and servers the
page will need. Callable while rendering or from handlers and effects; each resource
is added to the page's <head> once, however often it is asked for, and stays."""

from __future__ import annotations

from typing import Any

from .element import Element
from .hooks import active_root

__all__ = ["preconnect", "prefetch_dns", "preinit", "preinit_module", "preload", "preload_module"]


def _attrs(**values: Any) -> dict[str, str]:
    out = {}
    for name, value in values.items():
        if value is None or value is False:
            continue
        out[name.rstrip("_").replace("_", "-")] = "" if value is True else str(value)
    return out


def _add(key: tuple[str, str], tag: str, attrs: dict[str, str]) -> None:
    active_root().add_resource(key, Element(tag, None, attrs))


def prefetch_dns(href: str) -> None:
    """Look up the address of a server the page will fetch from."""
    _add(("dns", href), "link", _attrs(rel="dns-prefetch", href=href))


def preconnect(href: str, *, cross_origin: str | None = None) -> None:
    """Open a connection to a server the page will fetch from."""
    _add(("connect", href), "link", _attrs(rel="preconnect", href=href, crossorigin=cross_origin))


def preload(
    href: str,
    *,
    as_: str,
    type_: str | None = None,
    cross_origin: str | None = None,
    integrity: str | None = None,
    fetch_priority: str | None = None,
    image_srcset: str | None = None,
    image_sizes: str | None = None,
) -> None:
    """Download a file the page will use soon: ``preload("/font.woff2", as_="font")``.
    ``as_`` is "style", "script", "font", "image", "fetch" and so on."""
    _add(("preload", href), "link", _attrs(
        rel="preload", href=href, as_=as_, type_=type_, crossorigin=cross_origin, integrity=integrity,
        fetchpriority=fetch_priority, imagesrcset=image_srcset, imagesizes=image_sizes,
    ))


def preload_module(href: str, *, cross_origin: str | None = None, integrity: str | None = None) -> None:
    """Download a JavaScript module the page will import soon."""
    _add(("preload", href), "link", _attrs(
        rel="modulepreload", href=href, crossorigin=cross_origin, integrity=integrity,
    ))


def preinit(
    href: str,
    *,
    as_: str,
    precedence: str | None = None,
    cross_origin: str | None = None,
    integrity: str | None = None,
    fetch_priority: str | None = None,
) -> None:
    """Download and apply a stylesheet (``as_="style"``) or run a script
    (``as_="script"``, async) now, once for the page."""
    if as_ == "style":
        _add(("init", href), "link", _attrs(
            rel="stylesheet", href=href, crossorigin=cross_origin, integrity=integrity,
            fetchpriority=fetch_priority, data_precedence=precedence,
        ))
    elif as_ == "script":
        _add(("init", href), "script", _attrs(
            src=href, async_=True, crossorigin=cross_origin, integrity=integrity, fetchpriority=fetch_priority,
        ))
    else:
        raise ValueError(f'preinit takes as_="style" or as_="script", not {as_!r}')


def preinit_module(href: str, *, cross_origin: str | None = None, integrity: str | None = None) -> None:
    """Download and run a JavaScript module now, once for the page."""
    _add(("init", href), "script", _attrs(
        src=href, type_="module", async_=True, crossorigin=cross_origin, integrity=integrity,
    ))
