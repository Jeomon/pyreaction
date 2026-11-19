from reaction import component, html

from components.counter import Counter


@component
def App():
    return html.main(
        html.h1("{{name}}"),
        html.p("Edit ", html.code("app.py"), " and save: the page updates on its own."),
        Counter(),
    )
