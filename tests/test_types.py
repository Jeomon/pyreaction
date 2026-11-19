"""html.<tag> attributes are typed: wrong values fail type checking, data_*/aria_* still pass."""

import textwrap

import pytest

mypy_api = pytest.importorskip("mypy.api")

GOOD = """
from reaction import EventHandler, event, html, js

html.div("x", id="a", class_name="box", hidden=True, data_id=7, aria_label="ok", style={"color": "red"})
html.input(type="checkbox", checked=False, value=3, on_change=lambda e: None, my_custom="x")
html.a("home", href="/", target="_blank", on_click=event(lambda: None, prevent_default=True))
html.button("go", type="submit", disabled=True, on_click=js("element.blur()"))
html.svg(html.circle(cx=5, cy=5, r=4, fill="teal"), view_box="0 0 10 10")
html.my_widget("custom tags take anything", foo=1)
"""

BAD = """
from reaction import html

html.button("x", on_click="alert(1)")
html.input(checked="yes")
html.td(colspan="two")
html.div(tabindex="first")
"""


def check(code, tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(textwrap.dedent(code))
    out, err, status = mypy_api.run([str(path), "--no-incremental", "--config-file", "/dev/null"])
    return out, status


def test_typed_attributes_accept_valid_code(tmp_path):
    out, status = check(GOOD, tmp_path)
    assert status == 0, out


def test_typed_attributes_reject_wrong_types(tmp_path):
    out, status = check(BAD, tmp_path)
    assert status == 1
    for arg in ('"on_click"', '"checked"', '"colspan"', '"tabindex"'):
        assert f"Argument {arg}" in out, out
    assert "Found 4 errors" in out, out
