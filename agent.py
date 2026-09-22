"""LiveKit Agents entrypoint for macbrow.

    uv run python agent.py console      # local mic/speaker, no LiveKit server needed
    uv run python agent.py dev          # connect to LIVEKIT_URL as a worker
    uv run python agent.py download-files

Pipeline: Gradium STT -> (Jev router -> AppleScript) | LLM for brief replies -> Gradium TTS.
LLM defaults to LiveKit Inference (openai/gpt-5-mini); MACBROW_LLM_PROVIDER=lmstudio uses a local model.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, StopResponse, get_job_context, inference, llm
from livekit.plugins import gradium, silero
from livekit.plugins import openai as lk_openai

from macbrow import policy
from macbrow.agent import DynamicMacAgent

load_dotenv(".env.local")
load_dotenv()

log = logging.getLogger("macbrow.voice")


# ---------------------------------------------------------------------------
# HUD overlay manager – launches the SwiftUI pop-down and talks to it via stdin.
# ---------------------------------------------------------------------------

class HUDOverlay:
    """Manages the macbrow_ui companion process."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._hide_task: asyncio.Task | None = None

    async def start(self) -> None:
        bin_path = Path(__file__).resolve().parent / "macbrow_ui"
        script = Path(__file__).resolve().parent / "macbrow_ui.swift"

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
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            log.info("HUD overlay started via %s (pid %s)", cmd[0], self._proc.pid)

            async def _monitor_err() -> None:
                if self._proc and self._proc.stderr:
                    err = await self._proc.stderr.read()
                    if err:
                        log.warning("HUD overlay stderr: %s", err.decode(errors="replace").strip())

            asyncio.create_task(_monitor_err())
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
        # Cancel any pending auto-hide so the HUD stays visible.
        if self._hide_task and not self._hide_task.done():
            self._hide_task.cancel()
        self._send("SHOW")

    def hide(self, delay: float = 2.5) -> None:
        """Hide the HUD after a short delay so the user can read the response."""
        async def _delayed_hide() -> None:
            await asyncio.sleep(delay)
            self._send("HIDE")
        if self._hide_task and not self._hide_task.done():
            self._hide_task.cancel()
        self._hide_task = asyncio.ensure_future(_delayed_hide())

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

INSTRUCTIONS = """You are macbrow, a terse voice assistant that controls this Mac.
Mac actions are handled by a fast tool router before you see the message, so anything
that reaches you is small talk or a quick question. Answer in one short spoken sentence.
No markdown, no lists, no emoji, no follow-up questions."""

LLM_PROVIDER = os.environ.get("MACBROW_LLM_PROVIDER", "livekit")  # "livekit" | "lmstudio"
LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")


def build_chat_llm() -> llm.LLM:
    if LLM_PROVIDER == "livekit":
        return inference.LLM(
            model=os.environ.get("MACBROW_CHAT_MODEL", "openai/gpt-5-mini"),
            extra_kwargs={
                "reasoning_effort": os.environ.get("MACBROW_CHAT_REASONING", "minimal"),
                "max_completion_tokens": 80,
            },
        )
    return lk_openai.LLM(
        model=os.environ.get("MACBROW_CHAT_MODEL", "qwen/qwen3.5-9b"),
        base_url=LMSTUDIO_BASE_URL,
        api_key=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"),
        temperature=0.3,
        max_completion_tokens=60,
        # Qwen 3.5 thinks by default; LM Studio turns it off with reasoning_effort "none".
        extra_body={"reasoning_effort": os.environ.get("MACBROW_REASONING_EFFORT", "none")},
    )


class MacBrowAgent(Agent):
    def __init__(self, mac: DynamicMacAgent, hud: HUDOverlay) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self.mac = mac
        self.hud = hud

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        if not text.strip():
            raise StopResponse()

        # Show HUD with user text and start loading.
        self.hud.show()
        self.hud.set_text(text)
        self.hud.loading()

        outcome = await self.mac.handle(text)
        r = outcome.route
        log.info(
            "turn %r -> %s timings=%s",
            text,
            "llm" if outcome.handoff_to_llm else (r.summary if r else "?"),
            {k: round(v) for k, v in outcome.timings.items()},
        )
        if outcome.handoff_to_llm:
            self.hud.done()
            self.hud.hide(delay=1.0)
            return  # normal LLM reply

        if outcome.stop:
            self.hud.done()
            self.hud.set_response(outcome.speak or "Goodbye.")
            self.hud.hide(delay=2.0)
            try:
                await self.session.say(outcome.speak or "Goodbye.", add_to_chat_ctx=False)
            except RuntimeError:
                pass
            get_job_context().shutdown(reason="user asked macbrow to stop")
            raise StopResponse()

        self.hud.done()
        if outcome.speak:
            self.hud.set_response(outcome.speak)
            self.hud.hide(delay=3.0)
            # Speak the deterministic result and keep it in history so the LLM has context later.
            try:
                self.session.say(outcome.speak, add_to_chat_ctx=True)
            except RuntimeError as e:  # session closing mid-turn (ctrl-c during a route)
                log.warning("could not speak result: %s", e)
        else:
            self.hud.hide(delay=1.5)
        raise StopResponse()


server = AgentServer()


@server.rtc_session(agent_name=os.environ.get("MACBROW_AGENT_NAME", "macbrow"))
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
        enable_learning=os.environ.get("MACBROW_LEARN", "1") != "0",
        on_learning=_filler,
    )
    await mac.start()

    session = AgentSession(
        stt=gradium.STT(
            model_name=os.environ.get("GRADIUM_STT_MODEL", "default"), language=os.environ.get("MACBROW_LANG", "en")
        ),
        llm=build_chat_llm(),
        tts=gradium.TTS(
            model_name=os.environ.get("GRADIUM_TTS_MODEL", "default"),
            voice_id=os.environ.get("GRADIUM_VOICE_ID") or None,
        ),
        vad=silero.VAD.load(),
        preemptive_generation=False,  # we decide per-turn whether the LLM runs at all
    )

    # Wire up real-time transcript updates to the HUD.
    @session.on("user_input_transcribed")
    def _on_transcript(ev):
        if ev.transcript.strip():
            hud.show()
            hud.set_text(ev.transcript)

    async def _close() -> None:
        await hud.close()
        await mac.aclose()

    ctx.add_shutdown_callback(_close)

    log.info(
        "pipeline: stt=%s tts=%s voice_id=%s llm=%s",
        type(session.stt).__name__ if session.stt else None,
        f"{type(session.tts).__module__}.{type(session.tts).__name__}" if session.tts else None,
        os.environ.get("GRADIUM_VOICE_ID") or "gradium default",
        LLM_PROVIDER,
    )
    await session.start(agent=MacBrowAgent(mac, hud), room=ctx.room)
    greeting = "macbrow ready." if policy.ENABLED else "macbrow ready. Warning: the safety policy is off."
    session.say(greeting, add_to_chat_ctx=False)

    # Show welcome animation so the user immediately sees the HUD is active on start
    hud.show()
    hud.set_text("macbrow ready")
    hud.set_response("Listening for commands…")
    hud.hide(delay=2.8)


if __name__ == "__main__":
    agents.cli.run_app(server)
