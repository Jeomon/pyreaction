"""Settings from ``PYREACTION_*`` environment variables, for deployments that configure
apps that way (containers, platforms that set the port).

An option passed to :func:`~reaction.create_app` in code wins; otherwise its
environment variable; otherwise the built-in default.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

__all__ = ["DEFAULT_MAX_UPLOAD", "ENV_OPTIONS", "UNSET", "env_name", "resolve"]

DEFAULT_MAX_UPLOAD = 10 * 1024 * 1024  # bytes per uploaded file


class _Unset:
    """A create_app default: take the value from the environment or the default."""

    def __repr__(self) -> str:
        return "<from PYREACTION_* or default>"


UNSET: Any = _Unset()


def _bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    raise ValueError("expected true or false")


def _list(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


# option -> (how to read its variable, built-in default)
ENV_OPTIONS: dict[str, tuple[Callable[[str], Any], Any]] = {
    "debug": (_bool, False),
    "allowed_origins": (_list, ()),
    "max_upload_size": (int, DEFAULT_MAX_UPLOAD),
    "max_pending_events": (int, 1000),
    "max_pending_ops": (int, 100_000),
    "data_timeout": (float, 5.0),
    "stream": (_bool, True),
    "threaded": (_bool, True),
    "reconnect_max": (int, 8000),
    "reconnect_attempts": (int, 0),
    "resume_timeout": (float, 30.0),
    "compress": (_bool, True),
    "handoff_timeout": (float, 5.0),
    "max_handoffs": (int, 64),
}


def env_name(option: str) -> str:
    return "PYREACTION_" + option.upper()


def resolve(option: str, given: Any) -> Any:
    """``given`` if it was passed, else the option's environment variable, else its default."""
    if given is not UNSET:
        return given
    read, default = ENV_OPTIONS[option]
    text = os.environ.get(env_name(option))
    if text is None:
        return default
    try:
        return read(text)
    except ValueError as exc:
        raise ValueError(f"{env_name(option)}={text!r}: {exc}") from None
