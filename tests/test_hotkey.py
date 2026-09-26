import asyncio
import os

from livekit import rtc
from livekit.agents.voice.io import AudioInput

_env = dict(os.environ)
import agent  # noqa: E402

# agent.py loads .env.local on import; keep real API keys away from the other tests
os.environ.clear()
os.environ.update(_env)


class _Mic(AudioInput):
    def __init__(self) -> None:
        super().__init__(label="fake mic")

    async def __anext__(self) -> rtc.AudioFrame:
        return rtc.AudioFrame(b"\x01\x02" * 240, 24000, 1, 240)


def test_gate_is_silent_until_opened():
    async def run():
        gate = agent.MicGate(_Mic())
        closed = await gate.__anext__()
        gate.open = True
        opened = await gate.__anext__()
        return closed, opened

    closed, opened = asyncio.run(run())
    # same shape either way, so VAD and STT keep running; only the samples are muted
    assert (closed.sample_rate, closed.num_channels, closed.samples_per_channel) == (24000, 1, 240)
    assert not any(bytes(closed.data))
    assert bytes(opened.data) == b"\x01\x02" * 240


def test_hiding_the_popup_reports_it():
    async def run():
        hud = agent.HUDOverlay()
        hud._send = lambda cmd: None
        hidden = []
        hud.on_hidden = lambda: hidden.append(True)
        hud.wake()
        assert hud._visible
        hud.sleep()
        await asyncio.sleep(0.01)
        return hud, hidden

    hud, hidden = asyncio.run(run())
    assert hidden == [True] and not hud._visible


def test_pretty_hotkey():
    assert agent.pretty_hotkey("option+space") == "⌥Space"
    assert agent.pretty_hotkey("ctrl+shift+m") == "⌃⇧M"
