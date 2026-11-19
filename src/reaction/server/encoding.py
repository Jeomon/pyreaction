"""JSON to and from the browser, with orjson.

Props and attributes should be JSON values, but a Python value that is not one
must not stop the page: dates and times are sent in ISO format, an Enum as its
value, a dataclass as a dict, a set as a list, dict keys as text, NaN and
infinity as null, and anything else as its ``str()``, with a warning logged
once per type.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import json
import logging
import math
from typing import Any

import orjson

__all__ = ["dumps", "loads"]

logger = logging.getLogger("reaction")

_warned: set[type] = set()


def _fallback(value: Any) -> Any:
    """A JSON stand-in for a value the encoder does not know."""
    kind = type(value)
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    if kind not in _warned:
        _warned.add(kind)
        logger.warning(
            "Sent a %s to the browser as text: props and attributes should be JSON values",
            kind.__name__,
        )
    return str(value)


def _finite(value: Any, stack: tuple[int, ...] = ()) -> Any:
    """``value`` with NaN and infinity replaced by None, as orjson sends them."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple, dict)):
        if id(value) in stack:
            raise ValueError("Circular reference detected")
        inner = (*stack, id(value))
        if isinstance(value, dict):
            return {k: _finite(v, inner) for k, v in value.items()}
        return [_finite(v, inner) for v in value]
    return value


_encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_fallback)


def _dumps_std(data: Any) -> str:
    """The standard library's encoder, for the values orjson refuses."""
    try:
        return _encoder.encode(data)
    except ValueError:  # NaN or infinity somewhere (or a cycle, raised again by _finite)
        return _encoder.encode(_finite(data))


_OPTIONS = orjson.OPT_NON_STR_KEYS


def dumps(data: Any) -> str:
    """``data`` as compact JSON text."""
    try:
        return orjson.dumps(data, default=_fallback, option=_OPTIONS).decode()
    except TypeError:  # what orjson cannot write at all, e.g. an int past 64 bits
        return _dumps_std(data)


loads = orjson.loads
