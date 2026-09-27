"""LiveKit Agents entrypoint for oreo.

    uv run python agent.py console      # local mic/speaker, no LiveKit server needed
    uv run python agent.py dev          # connect to LIVEKIT_URL as a worker
    uv run python agent.py download-files

Pipeline: STT -> (Jev router -> AppleScript) | LLM for brief replies -> TTS.
Speech defaults to free on-device Parakeet/Kokoro via mlx-audio; OREO_SPEECH=gradium uses Gradium.
LLM defaults to LiveKit Inference (openai/gpt-5-mini); OREO_LLM_PROVIDER=lmstudio uses a local model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, StopResponse, get_job_context, inference, llm
from livekit.agents.voice.io import AudioInput
from livekit.plugins import gradium, silero
from livekit.plugins import openai as lk_openai

from oreo import policy
from oreo.agent import DynamicMacAgent

load_dotenv(".env.local")
load_dotenv()

log = logging.getLogger("oreo.voice")


# ---------------------------------------------------------------------------
# HUD overlay manager – launches the SwiftUI pop-down and talks to it via stdin.
# ---------------------------------------------------------------------------

# The pop-up stays up for the whole conversation and hides only after this many seconds in which
# nobody spoke and nothing was running (or when the user explicitly ends the session).
HUD_IDLE_S = float(os.environ.get("OREO_HUD_IDLE_S", "5"))
# After the user stops talking, speech-to-text can take seconds to deliver the words (local
# Parakeet: up to ~9 s). That wait is not silence; it gets this much grace before the countdown.
TRANSCRIPT_GRACE_S = float(os.environ.get("OREO_TRANSCRIPT_GRACE_S", "10"))
# The mic is only heard after this global shortcut (the HUD registers it); the conversation stays
# open until the pop-up hides, then the mic goes deaf again. "off" listens all the time.
HOTKEY = os.environ.get("OREO_HOTKEY", "option+space").strip()
HOTKEY_ENABLED = HOTKEY.lower() not in ("", "off")
# After the shortcut, this long to start talking before it goes back to sleep.
WAKE_S = float(os.environ.get("OREO_WAKE_S", "8"))
# The shortcut also opens a text field: type a command instead of saying it (Return sends it).
TYPING_ENABLED = os.environ.get("OREO_TYPING", "1") != "0"


def pretty_hotkey(spec: str) -> str:
    """'option+space' -> '⌥Space'."""
    symbols = {"cmd": "⌘", "command": "⌘", "opt": "⌥", "option": "⌥", "alt": "⌥", "ctrl": "⌃", "control": "⌃"}
    symbols["shift"] = "⇧"
    return "".join(symbols.get(p.strip().lower(), p.strip().capitalize()) for p in spec.split("+"))


class MicGate(AudioInput):
    """The mic while open; silence of the same shape while closed, so VAD and STT keep running but
    hear nothing until the shortcut wakes the assistant."""

    def __init__(self, source: AudioInput) -> None:
        super().__init__(label="oreo-mic-gate", source=source)
        self.open = False

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await super().__anext__()
        if self.open:
            return frame
        return rtc.AudioFrame.create(frame.sample_rate, frame.num_channels, frame.samples_per_channel)


class HUDOverlay:
    """Manages the oreo_ui companion process.

    Presence: visible while the user speaks, while a turn is being handled, and while the agent
    thinks or speaks; ``HUD_IDLE_S`` seconds after all of that stops it hides. Speaking again
    brings it back (the agent keeps listening while it is hidden).
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._hide_task: asyncio.Task | None = None
        self._visible = False
        self._user_speaking = False
        self._agent_active = False  # thinking or speaking
        self._agent_state = "listening"  # LiveKit agent state, drives the HUD's colours
        self._mode = ""  # last STATE sent to the HUD
        self._busy = 0  # turns being handled (routing, AppleScript, browser tasks)
        self._transcript_due = 0.0  # loop time until which the user's last words may still arrive
        self.hotkey_failed = False  # the HUD couldn't register the activation shortcut
        self.on_hotkey: Callable[[], None] | None = None
        self.on_hotkey_failed: Callable[[], None] | None = None
        self.on_hidden: Callable[[], None] | None = None
        self.on_typed: Callable[[str], None] | None = None
        self.on_dismiss: Callable[[], None] | None = None

    async def start(self) -> None:
        bin_path = Path(__file__).resolve().parent / "oreo_ui"
        script = Path(__file__).resolve().parent / "oreo_ui.swift"

        if bin_path.exists() and os.access(bin_path, os.X_OK):
            cmd = [str(bin_path)]
        elif script.exists():
            cmd = ["swift", str(script)]
        else:
            log.warning("HUD overlay executable/script not found – UI disabled")
            return

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            log.info("HUD overlay started via %s (pid %s)", cmd[0], self._proc.pid)

            async def _monitor_err() -> None:
                if self._proc and self._proc.stderr:
                    err = await self._proc.stderr.read()
                    if err:
                        log.warning("HUD overlay stderr: %s", err.decode(errors="replace").strip())

            async def _read_events() -> None:
                while self._proc and self._proc.stdout:
                    line = await self._proc.stdout.readline()
                    if not line:
                        return
                    event = line.decode(errors="replace").strip()
                    if event == "HOTKEY" and self.on_hotkey:
                        self.on_hotkey()
                    elif event.startswith("TYPED:") and self.on_typed:
                        self.on_typed(event.removeprefix("TYPED:"))
                    elif event == "TYPING":
                        self.typing()
                    elif event == "DISMISS" and self.on_dismiss:
                        self.on_dismiss()
                    elif event == "HOTKEY_FAILED":
                        self.hotkey_failed = True
                        if self.on_hotkey_failed:
                            self.on_hotkey_failed()

            asyncio.create_task(_monitor_err())
            asyncio.create_task(_read_events())
        except Exception as e:
            log.warning("Failed to start HUD overlay: %s", e)
            self._proc = None

    def _send(self, cmd: str) -> None:
        if self._proc and self._proc.stdin and self._proc.returncode is None:
            try:
                self._proc.stdin.write((cmd + "\n").encode())
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._proc.stdin.drain())
                except RuntimeError:
                    pass
            except Exception as e:
                log.debug("HUD send error: %s", e)

    def show(self) -> None:
        """Make sure the HUD is up and cancel any pending auto-hide."""
        if self._hide_task and not self._hide_task.done():
            self._hide_task.cancel()
        if not self._visible:  # SHOW also clears the response line, so only send it when hidden
            self._send("SHOW")
            self._visible = True

    def hide(self, delay: float = 2.5) -> None:
        """Hide the HUD after ``delay`` seconds unless something shows it again first."""

        async def _delayed_hide() -> None:
            await asyncio.sleep(delay)
            self._send("HIDE")
            self._visible = False
            if self.on_hidden:
                self.on_hidden()

        if self._hide_task and not self._hide_task.done():
            self._hide_task.cancel()
        self._hide_task = asyncio.ensure_future(_delayed_hide())

    def wake(self) -> None:
        """The shortcut was pressed: pop up and give the user ``WAKE_S`` to start talking."""
        self.show()
        self.ask_for_input()
        if not (self._user_speaking or self._agent_active or self._busy):
            self.hide(delay=max(WAKE_S, HUD_IDLE_S))

    def ask_for_input(self) -> None:
        """Show the text field so the user can type (the HUD takes the keyboard until Return/Escape)."""
        if TYPING_ENABLED and self._visible:
            self._send("INPUT")

    def typing(self) -> None:
        """Keys are going into the field: don't close under the user while they type."""
        self.show()
        self.hide(delay=max(WAKE_S, HUD_IDLE_S) * 3)

    def sleep(self) -> None:
        """The shortcut was pressed again: close now."""
        self.hide(delay=0)

    # -- presence ------------------------------------------------------------------------
    def user_speaking(self, speaking: bool) -> None:
        if self._user_speaking and not speaking:
            self._transcript_due = asyncio.get_running_loop().time() + TRANSCRIPT_GRACE_S
        self._user_speaking = speaking
        self._update()

    def transcript_arrived(self) -> None:
        """The words are in: from here on only the countdown (or the turn itself) matters."""
        self._transcript_due = 0.0
        self._update()

    def agent_active(self, active: bool) -> None:
        self._agent_active = active
        self._update()

    def agent_state(self, state: str) -> None:
        self._agent_state = state
        self.agent_active(state in ("thinking", "speaking"))

    @contextlib.contextmanager
    def busy(self):
        self._busy += 1
        self._transcript_due = 0.0
        self._update()
        try:
            yield
        finally:
            self._busy -= 1
            self._update()

    def _update(self) -> None:
        if self._agent_state == "speaking":
            mode = "speaking"
        elif self._agent_state == "thinking" or self._busy:
            mode = "thinking"
        else:
            mode = "hearing" if self._user_speaking else "listening"
        if mode != self._mode:
            self._mode = mode
            self._send(f"STATE:{mode}")

        if self._user_speaking or self._agent_active or self._busy:
            self.show()
            return
        # Silence countdown, after any wait for the last words; any activity cancels it.
        waiting = max(0.0, self._transcript_due - asyncio.get_running_loop().time())
        self.hide(delay=waiting + HUD_IDLE_S)

    def set_text(self, text: str) -> None:
        self._send(f"TEXT:{text}")

    def set_response(self, text: str) -> None:
        self._send(f"RESPONSE:{text}")

    def loading(self) -> None:
        self._send("LOADING")

    def done(self) -> None:
        self._send("DONE")

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._send("QUIT")
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except (TimeoutError, Exception):
                self._proc.kill()
        self._proc = None


INSTRUCTIONS = """You are Oreo, a terse voice assistant that controls this Mac.
Mac actions are handled by a fast tool router before you see the message, so anything
that reaches you is small talk or a quick question. Answer in one short spoken sentence.
No markdown, no lists, no emoji, no follow-up questions."""

LLM_PROVIDER = os.environ.get("OREO_LLM_PROVIDER", "livekit")  # "livekit" | "lmstudio"
LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")

# "local": free, on-device Parakeet STT + Kokoro TTS served by mlx-audio (./console.sh starts it).
# "gradium": Gradium streaming STT/TTS (needs GRADIUM_API_KEY and credits).
SPEECH_PROVIDER = os.environ.get("OREO_SPEECH", "local")
LOCAL_SPEECH_URL = os.environ.get("OREO_SPEECH_URL", "http://127.0.0.1:8123/v1")
LOCAL_STT_MODEL = os.environ.get("OREO_STT_MODEL", "mlx-community/parakeet-tdt-0.6b-v2")
LOCAL_TTS_MODEL = os.environ.get("OREO_TTS_MODEL", "mlx-community/Kokoro-82M-bf16")
LOCAL_VOICE = os.environ.get("OREO_VOICE", "af_heart")


def build_speech() -> tuple[agents.stt.STT, agents.tts.TTS]:
    if SPEECH_PROVIDER == "gradium":
        return (
            gradium.STT(
                model_name=os.environ.get("GRADIUM_STT_MODEL", "default"), language=os.environ.get("OREO_LANG", "en")
            ),
            gradium.TTS(
                model_name=os.environ.get("GRADIUM_TTS_MODEL", "default"),
                voice_id=os.environ.get("GRADIUM_VOICE_ID") or None,
            ),
        )
    # Non-streaming: AgentSession segments the mic with Silero VAD and sends each utterance.
    return (
        lk_openai.STT(
            model=LOCAL_STT_MODEL,
            language=os.environ.get("OREO_LANG", "en"),
            base_url=LOCAL_SPEECH_URL,
            api_key="local",
            use_realtime=False,
        ),
        lk_openai.TTS(
            model=LOCAL_TTS_MODEL, voice=LOCAL_VOICE, base_url=LOCAL_SPEECH_URL, api_key="local", response_format="pcm"
        ),
    )


def build_chat_llm() -> llm.LLM:
    if LLM_PROVIDER == "livekit":
        return inference.LLM(
            model=os.environ.get("OREO_CHAT_MODEL", "openai/gpt-5-mini"),
            extra_kwargs={
                "reasoning_effort": os.environ.get("OREO_CHAT_REASONING", "minimal"),
                "max_completion_tokens": 80,
            },
        )
    return lk_openai.LLM(
        model=os.environ.get("OREO_CHAT_MODEL", "qwen/qwen3.5-9b"),
        base_url=LMSTUDIO_BASE_URL,
        api_key=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"),
        temperature=0.3,
        max_completion_tokens=60,
        # Qwen 3.5 thinks by default; LM Studio turns it off with reasoning_effort "none".
        extra_body={"reasoning_effort": os.environ.get("OREO_REASONING_EFFORT", "none")},
    )


class OreoAgent(Agent):
    def __init__(self, mac: DynamicMacAgent, hud: HUDOverlay) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self.mac = mac
        self.hud = hud

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        if not text.strip() or not await self.run_turn(text):
            raise StopResponse()
        # else: the chat LLM answers (normal reply)

    async def run_typed(self, text: str) -> None:
        """A command typed into the HUD: the same turn as a spoken one, then the field again."""
        await self.mac.context.refresh()  # the HUD just handed the front back to the user's app
        if await self.run_turn(text):
            self.session.generate_reply(user_input=text)
        self.hud.ask_for_input()

    async def run_turn(self, text: str) -> bool:
        """Handle one command, spoken or typed. True when the chat LLM should answer it."""
        # Show HUD with user text and start loading. It stays up while the turn runs.
        with self.hud.busy():
            self.hud.set_text(text)
            self.hud.loading()
            outcome = await self.mac.handle(text)
            if outcome.refined:  # show what it understood after repairing a mishearing
                self.hud.set_text(outcome.refined)
        r = outcome.route
        log.info(
            "turn %r -> %s timings=%s",
            text,
            "llm" if outcome.handoff_to_llm else (r.summary if r else "?"),
            {k: round(v) for k, v in outcome.timings.items()},
        )
        if outcome.handoff_to_llm:
            self.hud.done()
            # The chat LLM answers with the whole session in view, not just this sentence.
            history = self.mac.memory.as_text()
            await self.update_instructions(
                INSTRUCTIONS + (f"\n\nThis session so far, oldest first:\n{history}" if history else "")
            )
            return True

        if outcome.stop:  # the user explicitly ended the session: close now, not after the idle wait
            self.hud.done()
            self.hud.set_response(outcome.speak or "Goodbye.")
            self.hud.hide(delay=2.0)
            if HOTKEY_ENABLED and not self.hud.hotkey_failed:
                # Push-to-talk: "stop" ends this conversation, not the assistant; the shortcut wakes it.
                self.mac.memory.clear()
                self.session.say(outcome.speak or "Goodbye.", add_to_chat_ctx=False)
                return False
            try:
                await self.session.say(outcome.speak or "Goodbye.", add_to_chat_ctx=False)
            except RuntimeError:
                pass
            get_job_context().shutdown(reason="user asked oreo to stop")
            return False

        self.hud.done()
        if outcome.speak:
            self.hud.set_response(outcome.speak)
            # Speak the deterministic result and keep it in history so the LLM has context later.
            try:
                self.session.say(outcome.speak, add_to_chat_ctx=True)
            except RuntimeError as e:  # session closing mid-turn (ctrl-c during a route)
                log.warning("could not speak result: %s", e)
        return False


server = AgentServer()


@server.rtc_session(agent_name=os.environ.get("OREO_AGENT_NAME", "oreo"))
async def entrypoint(ctx: agents.JobContext) -> None:
    session: AgentSession | None = None

    def _filler(text: str) -> None:
        if session is None:
            return
        try:
            session.say(text, add_to_chat_ctx=False)
        except RuntimeError:
            pass

    # Launch the HUD overlay.
    hud = HUDOverlay()
    await hud.start()

    mac = DynamicMacAgent(
        enable_learning=os.environ.get("OREO_LEARN", "1") != "0",
        on_learning=_filler,
    )
    await mac.start()

    stt, tts = build_speech()
    session = AgentSession(
        stt=stt,
        llm=build_chat_llm(),
        tts=tts,
        vad=silero.VAD.load(),
        turn_handling={
            "preemptive_generation": {"enabled": False},  # we decide per-turn whether the LLM runs at all
            # On laptop speakers the mic hears our own TTS and routes it as a command. Make agent speech
            # uninterruptible so the mic feeds silence to STT while we talk (discard_audio_if_uninterruptible).
            "interruption": {"enabled": False},
        },
    )

    # Wire up real-time transcript updates to the HUD.
    @session.on("user_input_transcribed")
    def _on_transcript(ev):
        if ev.transcript.strip():
            hud.show()
            hud.set_text(ev.transcript)
            if ev.is_final:
                hud.transcript_arrived()

    # Chat replies are written by the LLM after the turn was recorded; add them to session memory.
    @session.on("conversation_item_added")
    def _on_item(ev):
        item = ev.item
        if getattr(item, "role", None) == "assistant" and getattr(item, "text_content", None):
            mac.memory.note_reply(item.text_content)

    # Keep the HUD alive while anyone is talking or the agent is working.
    @session.on("user_state_changed")
    def _on_user_state(ev):
        hud.user_speaking(ev.new_state == "speaking")

    @session.on("agent_state_changed")
    def _on_agent_state(ev):
        hud.agent_state(ev.new_state)

    async def _close() -> None:
        await hud.close()
        await mac.aclose()

    ctx.add_shutdown_callback(_close)

    log.info(
        "pipeline: speech=%s stt=%s tts=%s voice=%s llm=%s",
        SPEECH_PROVIDER,
        type(session.stt).__name__ if session.stt else None,
        f"{type(session.tts).__module__}.{type(session.tts).__name__}" if session.tts else None,
        (os.environ.get("GRADIUM_VOICE_ID") or "gradium default") if SPEECH_PROVIDER == "gradium" else LOCAL_VOICE,
        LLM_PROVIDER,
    )
    oreo = OreoAgent(mac, hud)
    await session.start(agent=oreo, room=ctx.room)

    typed_turns: set[asyncio.Task] = set()

    def _on_typed(text: str) -> None:
        log.info("typed: %r", text)
        task = asyncio.create_task(oreo.run_typed(text))
        typed_turns.add(task)
        task.add_done_callback(typed_turns.discard)

    hud.on_typed = _on_typed
    hud.on_dismiss = hud.sleep

    # Push-to-talk: the mic is deaf until the shortcut, and again once the pop-up hides.
    gate: MicGate | None = None
    if HOTKEY_ENABLED and session.input.audio is not None:
        gate = MicGate(session.input.audio)
        session.input.audio = gate

        def _on_hotkey() -> None:
            if gate.open:
                log.info("shortcut: sleeping")
                hud.sleep()
            else:
                log.info("shortcut: listening")
                gate.open = True
                hud.wake()

        def _on_hotkey_failed() -> None:
            log.warning("shortcut %s is unavailable; listening all the time instead", HOTKEY)
            hud.on_hidden = None
            gate.open = True

        def _on_hidden() -> None:
            if gate.open:
                log.info("pop-up closed: mic off until %s", pretty_hotkey(HOTKEY))
            gate.open = False

        hud.on_hotkey, hud.on_hotkey_failed, hud.on_hidden = _on_hotkey, _on_hotkey_failed, _on_hidden
        if hud.hotkey_failed:  # reported before we were listening for it
            _on_hotkey_failed()
    greeting = "Oreo ready." if policy.ENABLED else "Oreo ready. Warning: the safety policy is off."
    session.say(greeting, add_to_chat_ctx=False)

    # Show welcome animation so the user immediately sees the HUD is active on start
    hud.show()
    hud.set_text("Oreo ready")
    listening_all_the_time = gate is None or gate.open
    hud.set_response("Listening for commands…" if listening_all_the_time else f"Press {pretty_hotkey(HOTKEY)} to talk")
    hud.agent_active(False)  # start the silence countdown (the greeting's speech state extends it)


if __name__ == "__main__":
    agents.cli.run_app(server)
