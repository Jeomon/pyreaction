"""A todo list: state, keyed lists, a controlled input, a form, effects and context.

    uv run python examples/todo.py
"""

import asyncio

from reaction import component, create_context, html, run, use_context, use_effect, use_state

Theme = create_context("light")

CSS = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; }
  .dark { background: #1d1d1f; color: #eee; padding: 1rem; border-radius: 8px; }
  li { display: flex; gap: .5rem; align-items: center; padding: .25rem 0; }
  li.done span { text-decoration: line-through; opacity: .6; }
  form { display: flex; gap: .5rem; }
  input[type=text] { flex: 1; }
</style>
"""


@component
def TodoItem(todo, on_toggle, on_remove):
    return html.li(
        html.input(type="checkbox", checked=todo["done"], on_change=lambda: on_toggle(todo["id"])),
        html.span(todo["text"]),
        html.button("x", on_click=lambda: on_remove(todo["id"]), aria_label="remove"),
        class_name="done" if todo["done"] else None,
    )


@component
def Clock():
    seconds, set_seconds = use_state(0)

    async def tick():
        while True:
            await asyncio.sleep(1)
            set_seconds(lambda s: s + 1)

    use_effect(tick, [])
    return html.small(f"open for {seconds}s")


@component
def TodoApp():
    todos, set_todos = use_state([])
    draft, set_draft = use_state("")
    next_id, set_next_id = use_state(1)
    theme = use_context(Theme)

    def add(e):
        text = draft.strip()
        if text:
            set_todos(todos + [{"id": next_id, "text": text, "done": False}])
            set_next_id(next_id + 1)
            set_draft("")

    def toggle(todo_id):
        set_todos([{**t, "done": not t["done"]} if t["id"] == todo_id else t for t in todos])

    def remove(todo_id):
        set_todos([t for t in todos if t["id"] != todo_id])

    left = sum(not t["done"] for t in todos)
    return html.div(
        html.h1("Todos"),
        html.form(
            html.input(type="text", value=draft, placeholder="What needs doing?",
                       on_change=lambda e: set_draft(e.value)),
            html.button("Add", type="submit"),
            on_submit=add,
        ),
        html.ul([TodoItem(t, toggle, remove, key=t["id"]) for t in todos]),
        html.p(f"{left} left", " · ", Clock()),
        class_name=theme,
    )


@component
def App():
    theme, set_theme = use_state("light")
    return html.main(
        html.button(
            "Dark mode" if theme == "light" else "Light mode",
            on_click=lambda: set_theme("dark" if theme == "light" else "light"),
        ),
        Theme(TodoApp(), value=theme),
    )


if __name__ == "__main__":
    run(App, title="pyreaction todos", head=CSS)
