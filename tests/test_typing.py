import asyncio
import os

_env = dict(os.environ)
import agent  # noqa: E402
from oreo.agent import Outcome  # noqa: E402

# agent.py loads .env.local on import; keep real API keys away from the other tests
os.environ.clear()
os.environ.update(_env)


class _Mac:
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        self.handled: list[str] = []

        class _Memory:
            def as_text(self) -> str:
                return ""

        self.memory = _Memory()
        mac = self

        class _Context:
            async def refresh(self):
                mac.handled.append("<refresh>")

        self.context = _Context()

    async def handle(self, text: str) -> Outcome:
        self.handled.append(text)
        return self.outcome


class _Session:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.replies: list[str] = []

    def say(self, text, **kw):
        self.said.append(text)

    def generate_reply(self, user_input=None, **kw):
        self.replies.append(user_input)


def _hud() -> tuple[agent.HUDOverlay, list[str]]:
    hud = agent.HUDOverlay()
    sent: list[str] = []
    hud._send = sent.append
    return hud, sent


def _oreo(outcome: Outcome, monkeypatch):
    hud, sent = _hud()
    mac, session = _Mac(outcome), _Session()
    oreo = agent.OreoAgent(mac, hud)
    monkeypatch.setattr(agent.OreoAgent, "session", property(lambda self: session))

    async def no_instructions(self, text):
        return None

    monkeypatch.setattr(agent.OreoAgent, "update_instructions", no_instructions)
    return oreo, mac, session, sent


def test_typed_command_runs_like_a_spoken_one(monkeypatch):
    oreo, mac, session, sent = _oreo(Outcome(speak="Opened WhatsApp."), monkeypatch)

    async def run():
        oreo.hud.show()
        await oreo.run_typed("open whatsapp")
        oreo.hud._hide_task.cancel()

    asyncio.run(run())
    assert mac.handled == ["<refresh>", "open whatsapp"]  # re-reads the front app first
    assert "TEXT:open whatsapp" in sent and "RESPONSE:Opened WhatsApp." in sent
    assert session.said == ["Opened WhatsApp."] and session.replies == []
    assert sent[-1] == "INPUT"  # the field comes back for a follow-up


def test_typed_chat_goes_to_the_llm(monkeypatch):
    oreo, mac, session, sent = _oreo(Outcome(handoff_to_llm=True), monkeypatch)

    async def run():
        oreo.hud.show()
        await oreo.run_typed("tell me a joke")
        oreo.hud._hide_task.cancel()

    asyncio.run(run())
    assert session.replies == ["tell me a joke"]


def test_wake_opens_the_field_unless_typing_is_off(monkeypatch):
    async def wake(enabled: bool) -> list[str]:
        monkeypatch.setattr(agent, "TYPING_ENABLED", enabled)
        hud, sent = _hud()
        hud.wake()
        hud._hide_task.cancel()
        return sent

    assert asyncio.run(wake(True)) == ["SHOW", "INPUT"]
    assert asyncio.run(wake(False)) == ["SHOW"]


def test_the_hud_is_never_the_app_in_front(monkeypatch):
    from oreo import applescript

    class _Res:
        def __init__(self, out: str) -> None:
            self.ok, self.output = True, out

    async def front_is(out):
        async def fake(script, timeout=5):
            return _Res(out)

        monkeypatch.setattr(applescript, "run_applescript", fake)
        return await applescript.get_active_app("Google Chrome"), await applescript.get_running_apps()

    # while the user types into the pop-up it is briefly active; commands still act on Chrome
    assert asyncio.run(front_is("oreo_ui"))[0] == "Google Chrome"
    assert asyncio.run(front_is("Notes"))[0] == "Notes"
    assert asyncio.run(front_is("Finder, oreo_ui, Notes"))[1] == ["Finder", "Notes"]
