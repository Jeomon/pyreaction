"""The ``reaction`` command (also installed as ``pyreaction``): ``init`` a project, then ``dev`` or ``run`` it.

With no command, ``reaction`` serves the project for production, as ``run``
does: ``reaction --port 8000`` is ``reaction run --port 8000``.
"""

from __future__ import annotations

import argparse
import os
import sys

from .. import __version__
from .scaffold import cmd_init, template_names
from .serve import cmd_dev, cmd_run

COMMANDS = ("init", "dev", "run")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reaction",
        description="React-style UIs in Python. With no command, serves the project for production (like `run`).",
    )
    parser.add_argument("--version", action="version", version=f"pyreaction {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a new project")
    init.add_argument("directory", help="folder to create (use . for the current, empty, folder)")
    init.add_argument(
        "--template",
        default="default",
        choices=template_names(),
        help="project template (default: %(default)s)",
    )
    init.add_argument("--no-install", action="store_true", help="skip `uv sync`")
    init.add_argument("--no-git", action="store_true", help="skip `git init`")
    init.set_defaults(func=cmd_init)

    for name, func, text in (
        ("dev", cmd_dev, "serve the project, restarting on every save"),
        ("run", cmd_run, "serve the project for production (the default)"),
    ):
        p = sub.add_parser(name, help=text)
        # Containers and hosting platforms set these; PORT is the common convention.
        p.add_argument(
            "--host", default=os.environ.get("PYREACTION_HOST", "127.0.0.1"),
            help="address to listen on (default: $PYREACTION_HOST, else 127.0.0.1)",
        )
        p.add_argument(
            "--port", type=int, default=int(os.environ.get("PYREACTION_PORT") or os.environ.get("PORT") or 8000),
            help="port to listen on (default: $PYREACTION_PORT, then $PORT, else 8000)",
        )
        if name == "run":
            p.add_argument("--log-level", default="info")
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    # No command (or only options such as --port): serve for production.
    if not args_in or (args_in[0] not in COMMANDS and args_in[0] not in ("-h", "--help", "--version")):
        args_in.insert(0, "run")
    args = build_parser().parse_args(args_in)
    return args.func(args)


# Entry points a project declares in its own [project.scripts], so that inside it
# `uv run dev` and `uv run pro` work (reaction init writes them).


def dev() -> int:
    """``uv run dev``: serve the project, restarting on every save, in debug mode."""
    return main(["dev", *sys.argv[1:]])


def pro() -> int:
    """``uv run pro``: serve the project for production."""
    return main(["run", *sys.argv[1:]])
