"""The app from "Give Me 1 Hour, I'll Make React Click Forever", chapter by chapter:
every bug the video shows in plain JavaScript stays fixed here."""

import asyncio
import runpy
from pathlib import Path

from pyreaction.testing import render

EXAMPLE = Path(__file__).parent.parent / "examples" / "lunch_roulette.py"
m = runpy.run_path(str(EXAMPLE))
App = m["App"]


def names(page):
    return [n.text for n in page.find_all("li.card .name")]


def votes_of(page, name):
    card = next(c for c in page.find_all("li.card") if name in c.text)
    return next(n.text for n in card.walk() if n.attrs.get("class") == "votes")


def vote(page, name, times=1):
    index = names(page).index(name)
    for _ in range(times):
        page.click(page.find_all("li.card button")[index])


def test_components_props_and_conditions():
    with render(App(start=30, tick=60)) as page:
        assert page.find("h1").text == "Lunch Roulette"
        assert names(page) == ["Ramen Road", "Pizza Palace", "Sushi Go"]  # one card, three props objects
        assert votes_of(page, "Sushi Go") == "no votes yet"  # a condition instead of a bare 0
        assert page.find_all(".tag") == []


def test_state_events_lifted_state_context_and_derived_leader():
    with render(App(start=30, tick=60)) as page:
        assert "Ramen Road" in page.find(".banner").text  # nobody voted: the first card leads
        vote(page, "Pizza Palace", 3)
        assert votes_of(page, "Pizza Palace") == "3 votes"
        assert [t.text for t in page.find_all(".tag")] == ["popular"]
        # The banner sits under Sidebar, which never receives the votes: context delivers them,
        # and the leader is computed from them rather than stored.
        assert "Pizza Palace" in page.find(".banner").text
        assert page.title == "Pizza Palace leads"  # an effect keeps the tab title in step
        vote(page, "Sushi Go", 4)
        assert "Sushi Go" in page.find(".banner").text and page.title == "Sushi Go leads"


def test_controlled_input_form_and_keys():
    with render(App(start=30, tick=60)) as page:
        vote(page, "Pizza Palace", 2)
        page.type("form input", "Taco Town")
        assert page.find("form small").text == "Suggesting: Taco Town"  # state drives the box
        page.submit("form")  # no page reload: the form's default is prevented
        assert names(page) == ["Taco Town", "Ramen Road", "Pizza Palace", "Sushi Go"]
        assert page.find("form input").value == "", "the box is cleared"
        # Added at the top, yet every vote stays on its own card: keys, not positions.
        assert votes_of(page, "Pizza Palace") == "2 votes"
        assert votes_of(page, "Taco Town") == "no votes yet"


def test_countdown_ticks_once_per_tick_never_faster_and_stops_at_zero():
    with render(App(start=30, tick=0.05)) as page:
        assert page.find(".countdown").text == "Voting closes in 30s"
        vote(page, "Sushi Go")  # re-renders must not start more timers
        page._run(asyncio.sleep(0.5))
        page.settle()
        left = int(page.find(".countdown").text.split()[-1].rstrip("s"))
        assert 18 <= left <= 26, f"one timer: about 10 ticks in 0.5s, not a pile-up ({30 - left})"
        page._run(asyncio.sleep(1.8))
        page.settle()
        assert page.find(".countdown").text == "Voting closes in 0s", "the updater never goes below zero"


def test_the_whole_app_under_strict_mode_in_debug():
    """StrictMode renders twice and sets each effect up twice; the app is pure and its
    effects clean up, so it behaves the same."""
    from pyreaction import Root

    async def main():
        root = Root(m["Main"](), debug=True, threaded=False)
        root.attach(lambda message: None)
        await root.settle()
        await asyncio.sleep(0.05)
        root.close()
        return root

    asyncio.run(main())
    with render(m["Main"]()) as page:
        vote(page, "Ramen Road", 3)
        assert votes_of(page, "Ramen Road") == "3 votes"
