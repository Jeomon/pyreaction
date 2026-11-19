"""The public testing helper."""

import asyncio

import pytest

from reaction import (
    Form,
    Link,
    Router,
    component,
    event,
    html,
    use_async,
    use_effect,
    use_ref,
    use_state,
    use_title,
)
from reaction.testing import render


@component
def Todo():
    items, set_items = use_state([])
    draft, set_draft = use_state("")

    def add():
        set_items(items + [draft])
        set_draft("")

    return html.div(
        html.input(name="item", value=draft, on_change=lambda e: set_draft(e.value)),
        html.button("Add", class_name="primary", on_click=add),
        html.ul([html.li(i, key=i) for i in items], id="list"),
    )


def test_type_click_and_find():
    with render(Todo()) as page:
        page.type("input[name=item]", "milk")
        page.click("button.primary")
        page.type("input", "eggs")
        page.click(page.find(text="Add"))
        assert [li.text for li in page.find_all("#list li")] == ["milk", "eggs"]
        assert page.find("input").value == ""
        with pytest.raises(LookupError):
            page.find("table")


def test_errors_fail_the_test_and_can_be_collected():
    @component
    def Broken():
        def boom():
            raise ValueError("handler failed")

        return html.button("x", on_click=boom)

    with render(Broken()) as page, pytest.raises(ValueError, match="handler failed"):
        page.click("button")
    with render(Broken(), raise_errors=False) as page:
        page.click("button")
        assert [str(e) for e in page.errors] == ["handler failed"]


def test_async_data_effects_title_and_navigation():
    @component
    def User(id):
        use_title(f"User {id}")

        async def load():
            await asyncio.sleep(0.01)
            return f"user #{id}"

        data = use_async(load, [id])
        return html.h1("..." if data.loading else data.value)

    @component
    def App():
        return html.main(Link("Seven", to="/users/7"), Router({"/users/{id}": User}, fallback=html.p("home")))

    with render(App()) as page:
        assert page.find("p").text == "home"
        page.navigate("/users/7")
        assert page.find("h1").text == "user #7"
        assert page.title == "User 7" and page.location == "/users/7"


def test_forms_keys_and_element_calls():
    saved = []

    @component
    def App():
        box = use_ref()
        width, set_width = use_state(None)

        async def measure():
            set_width((await box.current.call("getBoundingClientRect"))["width"])

        return html.div(
            Form(html.input(name="tag"), action=lambda fields: saved.append(fields.getlist("tag"))),
            html.input(ref=box, on_key_down=event(measure, keys=["Enter"])),
            html.p(width, id="w"),
        )

    sizes = {"getBoundingClientRect": {"width": 320}}
    with render(App(), on_call=lambda node, method, args: sizes.get(method)) as page:
        page.submit("form", {"tag": ["a", "b"]})
        assert saved == [["a", "b"]]
        assert [m for _, m, _ in page.calls] == ["reset"]
        page.key(page.find_all("input")[1], "a")  # filtered out by keys=["Enter"]
        assert page.find("#w").text == ""
        page.key(page.find_all("input")[1], "Enter")
        assert page.find("#w").text == "320"


def test_effects_run_and_clean_up_on_close():
    log = []

    @component
    def App():
        use_effect(lambda: (log.append("on"), lambda: log.append("off"))[1], [])
        return html.p("x")

    page = render(App())
    assert log == ["on"]
    page.close()
    assert log == ["on", "off"]
