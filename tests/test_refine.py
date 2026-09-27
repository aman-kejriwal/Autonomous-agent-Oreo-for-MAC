import asyncio

from oreo import refine
from oreo.agent import DynamicMacAgent
from oreo.applescript import MacContext
from oreo.router import Route


def test_accepts_a_misheard_word_fixed():
    assert refine.accept("Can you see in box on the screen?", "Can you see inbox on the screen?") == (
        "Can you see inbox on the screen?"
    )
    assert refine.accept("open the stared mails", '"open the starred mails"') == "open the starred mails"


def test_rejects_no_change_and_rewrites():
    assert refine.accept("Open the inbox.", "open the inbox") is None  # only case/punctuation
    assert refine.accept("open the inbox", "") is None
    # a different request is not a repair
    assert refine.accept("open the inbox", "compose a new email to my manager about the budget") is None
    # the misheard word fixed, but the question turned into a command: rejected
    assert refine.accept("Can you see in box on the screen?", "Show Inbox on the screen?") is None
    # dropping filler is fine
    assert refine.accept("uh um can you open the stared mails", "can you open the starred mails")
    # merged sound-alikes are repairs; an invented word or a changed number is not
    assert refine.accept("open whats up", "open WhatsApp") == "open WhatsApp"
    assert refine.accept("open the sent male", "open the Sent mail") == "open the Sent mail"
    assert refine.accept("Not in the two.", "Not in the inbox.") is None
    assert refine.accept("type see you at nine", "type see you at 9") is None
    assert refine.accept("open the open the inbox", "open the inbox") == "open the inbox"  # false start
    assert refine.accept("Uh can you open the stared mails", "open the starred mails") == "open the starred mails"
    assert refine.accept("send it to you", "send it to") is None  # "you" only goes when it leads


def test_takes_only_the_first_line():
    assert refine.accept("open you tube", "open YouTube\nI fixed 'you tube'.") == "open YouTube"


def test_hints_are_short_and_unique():
    text = refine.hints(["Inbox", "inbox", "x", "A" * 60, "Starred"], ["Notes", "Spotify"])
    assert text == "Apps: Notes, Spotify\nOn-screen labels: Inbox | Starred"


class _Router:
    """Routes by exact utterance; everything else is 'no tool fits'."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.seen: list[str] = []

    async def route(self, utterance, ctx, **kw) -> Route:
        self.seen.append(utterance)
        return self.routes.get(utterance, Route("new_action"))


def _agent(routes: dict[str, Route], repaired: str | None, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")  # the client is built but never called: the router is fake
    agent = DynamicMacAgent(enable_learning=False)
    ctx = MacContext("Google Chrome", ["Google Chrome", "Notes"], [])

    async def latest():
        return ctx

    async def nothing_on_screen(*a, **kw):
        return False

    async def no_snapshot(ctx):
        return None

    calls: list[str] = []

    async def repair(utterance, *a, **kw):
        calls.append(utterance)
        return repaired

    agent.context.latest = latest
    agent._start_ui_snapshot = lambda ctx: None
    agent._ui_snapshot = no_snapshot
    agent._press_on_screen = nothing_on_screen
    agent.router = _Router(routes)
    monkeypatch.setattr(refine, "repair", repair)
    return agent, calls


def test_confused_command_is_repaired_and_retried(monkeypatch):
    agent, calls = _agent({"what is the weather": Route("chat")}, "what is the weather", monkeypatch)
    out = asyncio.run(agent.handle("wat is the whether"))
    assert agent.router.seen == ["wat is the whether", "what is the weather"]
    assert out.handoff_to_llm and out.refined == "what is the weather"
    assert agent.memory.turns[-1].said == "what is the weather"  # later turns see what was meant


def test_unchanged_repair_keeps_the_old_behaviour(monkeypatch):
    agent, calls = _agent({}, None, monkeypatch)
    out = asyncio.run(agent.handle("frobnicate the widget"))
    assert agent.router.seen == ["frobnicate the widget"]  # no retry
    assert out.speak == "I don't have a tool for that yet." and out.refined is None


def test_repairs_only_once(monkeypatch):
    agent, calls = _agent({}, "frobnicate the gadget", monkeypatch)  # the repair is still not understood
    out = asyncio.run(agent.handle("frobnicate the widget"))
    assert calls == ["frobnicate the widget"]
    assert agent.router.seen == ["frobnicate the widget", "frobnicate the gadget"]
    assert out.speak == "I don't have a tool for that yet."


def test_understood_commands_never_call_the_llm(monkeypatch):
    agent, calls = _agent({"hello": Route("chat")}, "anything", monkeypatch)
    asyncio.run(agent.handle("hello"))
    assert calls == []
