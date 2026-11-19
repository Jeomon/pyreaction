from pyreaction import component, html, use_state


@component
def Counter(start: int = 0):
    count, set_count = use_state(start)
    return html.div(
        html.button("-", on_click=lambda: set_count(count - 1)),
        html.span(count, class_name="count"),
        html.button("+", on_click=lambda: set_count(count + 1)),
        class_name="counter",
    )
