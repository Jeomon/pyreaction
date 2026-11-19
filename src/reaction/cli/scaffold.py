"""``reaction init``: copy a project template into a new folder."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

import orjson

from .. import __version__

TEMPLATES = Path(__file__).parent / "templates"


def _install_source() -> tuple[str, str] | None:
    """Where pyreaction was installed from, when not from an index: ("path", folder)
    for a local folder, ("git", url) for a Git repository.

    A new project points at the same place, since that version may not be on PyPI.
    """
    try:
        raw = metadata.distribution("pyreaction").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    info = orjson.loads(raw)
    url = info.get("url", "")
    vcs = info.get("vcs_info")
    if vcs and vcs.get("vcs") == "git" and not url.startswith("file://"):
        return "git", url
    if not url.startswith("file://"):
        return None
    path = Path(unquote(urlparse(url).path))
    return ("path", path.as_posix()) if (path / "pyproject.toml").is_file() else None


def _render_template(src: Path, dest: Path, values: dict[str, str]) -> list[Path]:
    written = []
    for path in sorted(src.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src)
        name = rel.name
        if name == "_gitignore":
            name = ".gitignore"
        elif name.endswith(".tmpl"):
            name = name[: -len(".tmpl")]
        target = dest / rel.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        target.write_text(text, encoding="utf-8")
        written.append(target.relative_to(dest))
    return written


def _inside_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=path, capture_output=True, text=True
    )
    return result.returncode == 0


def template_names() -> list[str]:
    return sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir())


def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.directory).resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"error: {dest} is not empty", file=sys.stderr)
        return 1
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", dest.name).strip("-._") or "app"

    source = _install_source()
    if source is None:
        sources = ""
    elif source[0] == "git":
        sources = f'\n[tool.uv.sources]\npyreaction = {{ git = "{source[1]}" }}\n'
    else:
        sources = f'\n[tool.uv.sources]\npyreaction = {{ path = "{source[1]}", editable = true }}\n'
    values = {"name": name, "dependency": f"pyreaction>={__version__}", "sources": sources}

    dest.mkdir(parents=True, exist_ok=True)
    files = _render_template(TEMPLATES / args.template, dest, values)
    print(f"Created {name} in {dest}")
    for f in files:
        print(f"  {f.as_posix()}")
    if source is not None:
        where = "your local copy at" if source[0] == "path" else "the Git repository"
        print(f"  (pyreaction comes from {where} {source[1]})")

    if not args.no_git and shutil.which("git") and not _inside_git_repo(dest):
        subprocess.run(["git", "init", "-q"], cwd=dest, check=False)
        print("Initialised a git repository")

    uv = shutil.which("uv")
    if not args.no_install and uv:
        print("Installing dependencies with uv...")
        if subprocess.run([uv, "sync", "--quiet"], cwd=dest).returncode != 0:
            print("error: uv sync failed; run it again inside the project", file=sys.stderr)
            return 1

    cd = os.path.relpath(dest)
    run = "uv run dev" if uv else "dev"
    print("\nNext:")
    if cd != ".":
        print(f"  cd {cd}")
    if not uv:
        print("  pip install -e .")
    print(f"  {run}")
    return 0
