import tomllib

import pytest
from starlette.testclient import TestClient

from pyreaction import cli
from pyreaction.cli.serve import find_project, load_app


def test_init_creates_a_project_that_serves(tmp_path, monkeypatch):
    dest = tmp_path / "My App!"
    assert cli.main(["init", str(dest), "--no-install", "--no-git"]) == 0

    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert files == [
        ".gitignore",
        "README.md",
        "app.py",
        "components/__init__.py",
        "components/counter.py",
        "public/style.css",
        "pyproject.toml",
    ]
    config = tomllib.loads((dest / "pyproject.toml").read_text())
    assert config["project"]["name"] == "My-App"
    assert config["tool"]["pyreaction"]["app"] == "app:App"
    assert "{{" not in (dest / "app.py").read_text()

    root, settings = find_project(dest / "components")
    assert root == dest
    monkeypatch.syspath_prepend(str(dest))
    client = TestClient(load_app(root, settings))
    page = client.get("/").text
    assert "<h1>My-App</h1>" in page
    assert '<link rel="stylesheet" href="/static/style.css">' in page
    assert ".counter" in client.get("/static/style.css").text


def test_init_refuses_a_non_empty_folder(tmp_path, capsys):
    (tmp_path / "keep.txt").write_text("mine")
    assert cli.main(["init", str(tmp_path), "--no-install", "--no-git"]) == 1
    assert "not empty" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_commands_outside_a_project_explain_what_to_do(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="pyreaction init"):
        cli.main(["run"])
