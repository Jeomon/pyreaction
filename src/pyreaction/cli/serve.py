"""``pyreaction dev`` and ``pyreaction run``: serve the project in the current folder.

A project is any folder whose ``pyproject.toml`` has a ``[tool.pyreaction]`` table:

    [tool.pyreaction]
    app = "app:App"              # module:attribute of the root component
    title = "My app"
    static = "public"            # served at /static
    stylesheets = ["style.css"]  # files in the static folder linked from the page
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ENV = "PYREACTION_PROJECT"


def find_project(start: Path) -> tuple[Path, dict[str, Any]]:
    """The nearest folder at or above ``start`` with a ``[tool.pyreaction]`` table."""
    for folder in (start, *start.parents):
        pyproject = folder / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
            config = data.get("tool", {}).get("pyreaction")
            if config is not None:
                config.setdefault("title", data.get("project", {}).get("name", "pyreaction"))
                return folder, config
    raise SystemExit(
        "error: no pyproject.toml with a [tool.pyreaction] table here or above.\n"
        "Run `pyreaction init my-app` to create a project."
    )


def load_app(root: Path, config: dict[str, Any], debug: bool = False) -> Any:
    """Import the project's root component and build the ASGI app for it.

    ``debug`` shows errors in the browser; ``pyreaction dev`` turns it on.
    """
    from ..server.app import DEFAULT_MAX_UPLOAD, create_app

    target = config.get("app", "app:App")
    module_name, _, attr = target.partition(":")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module = importlib.import_module(module_name)
    try:
        component = getattr(module, attr or "App")
    except AttributeError:
        raise SystemExit(f"error: {module_name} has no {attr or 'App'!r} (set by app = {target!r})")

    static = config.get("static")
    static_dir = root / static if static else None
    if static_dir is not None and not static_dir.is_dir():
        static_dir = None
    return create_app(
        component,
        title=config.get("title", "pyreaction"),
        head=config.get("head", ""),
        lang=config.get("lang", "en"),
        static_dir=static_dir,
        stylesheets=config.get("stylesheets", []) if static_dir is not None else [],
        allowed_origins=config.get("allowed_origins", []),
        max_upload_size=config.get("max_upload_size", DEFAULT_MAX_UPLOAD),
        debug=debug,
    )


def app_factory() -> Any:
    """Entry point for uvicorn's reloader in ``pyreaction dev``, which starts a fresh
    process on every change."""
    root = Path(os.environ[PROJECT_ENV])
    _, config = find_project(root)
    return load_app(root, config, debug=True)


def _serve(args: argparse.Namespace, reload: bool) -> int:
    import uvicorn

    from ..server.app import DEFAULT_MAX_UPLOAD, ws_max_size

    root, config = find_project(Path.cwd())
    os.environ[PROJECT_ENV] = str(root)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    frame_limit = ws_max_size(config.get("max_upload_size", DEFAULT_MAX_UPLOAD))
    if reload:
        print(f"pyreaction dev: serving {root.name} at http://{args.host}:{args.port} (reloads on save)")
        uvicorn.run(
            f"{__name__}:app_factory",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(root)],
            log_level="warning",
            ws_max_size=frame_limit,
        )
    else:
        uvicorn.run(
            load_app(root, config),
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            ws_max_size=frame_limit,
        )
    return 0


def cmd_dev(args: argparse.Namespace) -> int:
    return _serve(args, reload=True)


def cmd_run(args: argparse.Namespace) -> int:
    return _serve(args, reload=False)
