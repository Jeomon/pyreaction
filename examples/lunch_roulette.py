"""Lunch roulette, the app from "Give Me 1 Hour, I'll Make React Click Forever",
in pyreaction: components and props, state, events and a controlled input, a form,
lists with keys, conditions, lifted state, context, effects with cleanup and
dependencies, a derived value, and a custom hook.

    uv run python examples/lunch_roulette.py
"""

import asyncio

from pyreaction import (
    StrictMode,
    component,
    create_context,
    html,
    run,
    use_context,
    use_effect,
    use_state,
    use_title,
)

CSS = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 34rem; margin: 2rem auto; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: .6rem .9rem; margin: .4rem 0;
          display: flex; align-items: center; gap: .6rem; }
  .card .name { flex: 1; }
  .tag { background: #ffd54f; border-radius: 4px; padding: 0 .3rem; font-size: .8rem; }
  .banner { background: #e8f5e9; padding: .6rem; border-radius: 8px; }
  form { display: flex; gap: .4rem; margin: .6rem 0; }
  form input { flex: 1; }
</style>
"""

START = [
    {"id": "ramen", "name": "Ramen Road", "emoji": "🍜"},
    {"id": "pizza", "name": "Pizza Palace", "emoji": "🍕"},
    {"id": "sushi", "name": "Sushi Go", "emoji": "🍣"},
]

# Context: the votes reach the banner without passing through the components between.
VotesContext = create_context(None, name="VotesContext")


def use_countdown(start, tick=1.0):
    """A custom hook: the timer logic, reusable. Each caller gets its own seconds."""
    seconds, set_seconds = use_state(start)

    async def run_timer():
        # An effect starts the timer after the page is on screen. Being async, its
        # task is cancelled (the cleanup) when the countdown leaves the page.
        while True:
            await asyncio.sleep(tick)
            set_seconds(lambda s: max(s - 1, 0))  # the updater: always the latest seconds

    use_effect(run_timer, [])  # empty dependencies: start once
    return seconds


@component
def Countdown(start, tick):
    seconds = use_countdown(start, tick)
    return html.p(f"Voting closes in {seconds}s", class_name="countdown")


@component
def RestaurantCard(name, emoji, votes, on_vote):
    """Props are read-only inputs; the votes live in App and come down as a prop."""
    return html.li(
        html.span(emoji),
        html.span(name, class_name="name"),
        # Conditions: show a tag only sometimes, and words instead of a bare 0.
        html.span("popular", class_name="tag") if votes >= 3 else None,
        html.span(f"{votes} votes" if votes else "no votes yet", class_name="votes"),
        # Pass the function itself, not on_click=on_vote() (that loops forever).
        html.button("Vote", on_click=on_vote),
        class_name="card",
    )


@component
def LeaderBanner(restaurants):
    votes = use_context(VotesContext)
    # Derived, not stored: computed from the votes on every render.
    leader = max(restaurants, key=lambda r: votes.get(r["id"], 0))
    use_title(f"{leader['name']} leads")  # the tab title follows the leader
    return html.p(f"Leading: {leader['emoji']} {leader['name']}", class_name="banner")


@component
def Sidebar(restaurants):
    return html.aside(LeaderBanner(restaurants))  # no votes prop: context delivers them


@component
def SuggestForm(on_add):
    text, set_text = use_state("")  # a controlled input: state decides what it shows

    def submit(e):  # forms never reload the page: the default is prevented for you
        if text.strip():
            on_add(text.strip())
            set_text("")

    return html.form(
        html.input(value=text, placeholder="Suggest a place", on_change=lambda e: set_text(e.value)),
        html.button("Add", type="submit"),
        html.small(f"Suggesting: {text}") if text else None,
        on_submit=submit,
    )


@component
def App(start=30, tick=1.0):
    restaurants, set_restaurants = use_state(START)
    votes, set_votes = use_state({})  # lifted up: App is the one owner of the votes

    def vote(rid):
        set_votes(lambda v: {**v, rid: v.get(rid, 0) + 1})

    def add(name):
        new = {"id": f"new-{len(restaurants)}", "name": name, "emoji": "🍽️"}
        set_restaurants([new, *restaurants])  # first in the list: keys keep votes on their cards

    return VotesContext(
        html.main(
            html.h1("Lunch Roulette"),
            Sidebar(restaurants),
            Countdown(start, tick, key="countdown"),
            SuggestForm(add),
            html.ul([
                RestaurantCard(r["name"], r["emoji"], votes.get(r["id"], 0), lambda r=r: vote(r["id"]), key=r["id"])
                for r in restaurants
            ]),
        ),
        value=votes,
    )


@component
def Main():
    return StrictMode(App())


if __name__ == "__main__":
    run(Main, title="Lunch Roulette", head=CSS, debug=True)
