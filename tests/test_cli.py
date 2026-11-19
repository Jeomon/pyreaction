import tomllib

import pytest
from starlette.testclient import TestClient

from reaction import cli
from reaction.cli.serve import find_project, load_app


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
    assert config["project"]["scripts"] == {"dev": "reaction.cli:dev", "pro": "reaction.cli:pro"}
    assert config["build-system"]["build-backend"] == "hatchling.build"
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
    with pytest.raises(SystemExit, match="reaction init"):
        cli.main(["run"])


def test_no_command_means_run(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "build_parser", _parser_recording(seen))
    cli.main([])
    cli.main(["--port", "9000", "--host", "0.0.0.0"])
    cli.main(["dev", "--port", "8001"])
    assert seen == [("run", "127.0.0.1", 8000), ("run", "0.0.0.0", 9000), ("dev", "127.0.0.1", 8001)]


def _parser_recording(seen):
    real = cli.build_parser

    def build():
        parser = real()
        for action in parser._subparsers._group_actions:
            for name, sub in action.choices.items():
                if name in ("run", "dev"):
                    sub.set_defaults(func=lambda a, name=name: seen.append((name, a.host, a.port)) or 0)
        return parser

    return build


def test_dev_and_pro_entry_points_pass_their_options_on(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "build_parser", _parser_recording(seen))
    monkeypatch.setattr(cli.sys, "argv", ["pro", "--port", "9100"])
    cli.pro()
    monkeypatch.setattr(cli.sys, "argv", ["dev"])
    cli.dev()
    assert seen == [("run", "127.0.0.1", 9100), ("dev", "127.0.0.1", 8000)]
