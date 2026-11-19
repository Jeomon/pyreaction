"""Support for compiling the core with mypyc (see the README's Development section).

``python_class`` marks a class that must stay an ordinary Python class in a
compiled build: one that sets attributes such as ``__module__`` on its instances,
or adds ``__getattr__`` to a dict subclass. Without mypyc it does nothing, and
``mypy_extensions`` is only needed when compiling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

C = TypeVar("C")

if TYPE_CHECKING:
    from mypy_extensions import mypyc_attr

    python_class = mypyc_attr(native_class=False)
else:
    try:
        from mypy_extensions import mypyc_attr
    except ImportError:

        def mypyc_attr(*args: Any, **kwargs: Any) -> Any:
            return lambda cls: cls

    python_class = mypyc_attr(native_class=False)

__all__ = ["python_class", "warn_if_stale"]


def warn_if_stale() -> None:
    """A compiled module is used instead of its .py file: say so when the source is
    newer, since edits to it would otherwise be silently ignored."""
    import importlib.machinery
    import logging
    from pathlib import Path

    here = Path(__file__).parent
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        for built in here.glob(f"*{suffix}"):
            source = here / (built.name[: -len(suffix)] + ".py")
            if source.exists() and source.stat().st_mtime > built.stat().st_mtime:
                logging.getLogger("reaction").warning(
                    "%s changed after it was compiled; run scripts/compile.py again (or --clean)", source.name
                )
