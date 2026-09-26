from oreo.applescript import MacContext
from oreo.memory import MAX_TURNS, WINDOW_S, SessionMemory
from oreo.router import _state


def _session() -> SessionMemory:
    m = SessionMemory()
    m.add("open spotify", "Electron", "", "open_app(app='Spotify')", "Spotify is in front")
    m.add("open the liked songs", "Spotify", "Spotify Premium", "ui_press()", "Opened Liked Songs.")
    m.add(
        "can you see a butter song on this page", "Spotify", "Spotify Premium", "read_screen()", "Yes, Butter by BTS."
    )
    return m


def test_recent_turns_oldest_first():
    turns = _session().recent()
    assert [t["user"] for t in turns] == [
        "open spotify",
        "open the liked songs",
        "can you see a butter song on this page",
    ]
    assert turns[1] == {
        "user": "open the liked songs",
        "app": "Spotify",
        "did": "ui_press()",
        "assistant": "Opened Liked Songs.",
        "on": "Spotify Premium",
    }


def test_values_offer_what_it_refers_to():
    m = _session()
    m.add("search for espresso", "Google Chrome", "YouTube", "search_here(query='espresso')", "", ["espresso"])
    values = m.values()
    assert values[0] == "espresso"  # newest turn's argument first
    assert "butter song" in values  # a phrase from an earlier question, so "play it" can pick it


def test_memory_is_bounded_and_expires():
    m = SessionMemory()
    for i in range(MAX_TURNS + 5):
        m.add(f"command {i}", "Notes", "", "chat", "")
    assert len(m.turns) == MAX_TURNS and m.turns[0].said == "command 5"
    later = m.turns[-1].at + WINDOW_S + 1
    assert m.recent(now=later) == [] and m.values(now=later) == []


def test_chat_reply_is_attached_to_the_last_turn_once():
    m = _session()
    m.add("what's the weather like", "Spotify", "", "chat", "")
    m.note_reply("I can't check the weather.")
    m.note_reply("a second item for the same turn is ignored")
    assert m.turns[-1].reply == "I can't check the weather."


def test_router_state_carries_the_conversation():
    ctx = MacContext("Spotify", ["Spotify"], [])
    st = _state("now play it", ctx, conversation=_session().recent())
    assert st["conversation"][-1]["user"] == "can you see a butter song on this page"
    assert "conversation" not in _state("now play it", ctx)
