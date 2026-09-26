"""Short spoken answers about what is on screen ("what songs can you see?", "is Butter on this
page?"), written by the chat LLM from the front window's text and the session so far.

Same backend switch as the rest: LiveKit Inference by default, LM Studio with
OREO_LLM_PROVIDER=lmstudio.
"""

from __future__ import annotations

import os

PROVIDER = os.environ.get("OREO_LLM_PROVIDER", "livekit")
LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")

SYSTEM = """You are the voice of a Mac assistant. You get the text currently shown in the app in
front (one line per on-screen element, in reading order: sidebar, then the page, then panels),
the session so far, and the user's question. Answer only from that screen text. Name the items
the user asked about (for songs: title and artist), at most five, in one or two short spoken
sentences with no lists, markdown or emoji. If what they asked about is not on screen, say so
plainly and, if useful, say what is there instead. The "page:" line is the tab's title: don't
quote it or call things "page entries" unless the user asks about the page itself."""


async def about_screen(question: str, app: str, screen: str, conversation: str = "") -> str:
    user = (
        f"App in front: {app}\n\nSession so far:\n{conversation or '-'}\n\nOn screen:\n{screen}\n\nQuestion: {question}"
    )
    if PROVIDER == "livekit":
        from livekit.agents import inference, llm

        model = inference.LLM(
            model=os.environ.get("OREO_CHAT_MODEL", "openai/gpt-5-mini"),
            extra_kwargs={"reasoning_effort": "minimal", "max_completion_tokens": 160},
        )
        try:
            ctx = llm.ChatContext()
            ctx.add_message(role="system", content=SYSTEM)
            ctx.add_message(role="user", content=user)
            async with model.chat(chat_ctx=ctx) as stream:
                return "".join([c async for c in stream.to_str_iterable()]).strip()
        finally:
            await model.aclose()

    import openai

    client = openai.AsyncOpenAI(base_url=LMSTUDIO_BASE_URL, api_key=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"))
    try:
        resp = await client.chat.completions.create(
            model=os.environ.get("OREO_CHAT_MODEL", "qwen/qwen3.5-9b"),
            max_tokens=160,
            temperature=0.2,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            extra_body={"reasoning_effort": os.environ.get("OREO_REASONING_EFFORT", "none")},
        )
        return (resp.choices[0].message.content or "").strip()
    finally:
        await client.close()
