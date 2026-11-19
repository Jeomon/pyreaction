"""The ``pyreaction`` command: ``init`` a project, then ``dev`` or ``run`` it."""

from __future__ import annotations

import argparse

from .. import __version__
from .scaffold import cmd_init, template_names
from .serve import cmd_dev, cmd_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyreaction", description="React-style UIs in Python.")
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
        ("run", cmd_run, "serve the project"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8000)
        if name == "run":
            p.add_argument("--log-level", default="info")
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
