import tomllib
from pathlib import Path

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


def test_project_settings_and_environment_combine(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from reaction.cli.serve import load_app

    (tmp_path / "app.py").write_text(
        "from reaction import component, html\n\n@component\ndef App():\n    return html.p('hi')\n"
    )
    monkeypatch.setenv("PYREACTION_RECONNECT_MAX", "3000")
    monkeypatch.setenv("PYREACTION_RECONNECT_ATTEMPTS", "12")
    app = load_app(tmp_path, {"app": "app:App", "title": "t", "reconnect_attempts": 5})
    with TestClient(app) as client:
        page = client.get("/").text
    assert 'data-reconnect-max="3000"' in page, "from the environment"
    assert 'data-reconnect-attempts="5"' in page, "[tool.pyreaction] wins over the environment"


def test_host_and_port_come_from_the_environment(monkeypatch):
    from reaction.cli import build_parser

    args = build_parser().parse_args(["run"])
    assert (args.host, args.port) == ("127.0.0.1", 8000)
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("PYREACTION_HOST", "0.0.0.0")
    args = build_parser().parse_args(["run"])
    assert (args.host, args.port) == ("0.0.0.0", 9123)
    monkeypatch.setenv("PYREACTION_PORT", "7000")
    assert build_parser().parse_args(["dev"]).port == 7000, "PYREACTION_PORT wins over PORT"
    assert build_parser().parse_args(["run", "--port", "1"]).port == 1, "the flag wins"


def test_init_points_the_project_where_pyreaction_came_from(tmp_path, monkeypatch):
    import json as _json
    from argparse import Namespace

    from reaction.cli import scaffold

    class Dist:
        def __init__(self, info):
            self.info = info

        def read_text(self, name):
            return _json.dumps(self.info)

    def project(info, name):
        monkeypatch.setattr(scaffold.metadata, "distribution", lambda _: Dist(info))
        scaffold.cmd_init(Namespace(directory=str(tmp_path / name), template="default", no_install=True, no_git=True))
        return (tmp_path / name / "pyproject.toml").read_text()

    from_git = project({"url": "https://github.com/Jeomon/pyreaction", "vcs_info": {"vcs": "git", "commit_id": "abc"}}, "a")
    assert '[tool.uv.sources]\npyreaction = { git = "https://github.com/Jeomon/pyreaction" }' in from_git
    here = Path(__file__).resolve().parents[1]
    from_folder = project({"url": here.as_uri(), "dir_info": {"editable": True}}, "b")
    assert f'pyreaction = {{ path = "{here.as_posix()}", editable = true }}' in from_folder
    from_index = project({"url": "https://files.example/pyreaction-0.1.0.whl", "archive_info": {}}, "c")
    assert "[tool.uv.sources]" not in from_index
