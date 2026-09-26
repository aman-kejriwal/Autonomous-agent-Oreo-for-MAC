"""Session memory: what was said and done in this session, so every decision can follow the task.

Kept in memory only, for the life of the session (until the user says "stop listening" or the
assistant restarts), and never written to disk. Every Jev question (routing, argument filling,
on-screen picks) and every LLM call (chat replies, screen answers, tool writing) gets the recent
turns, so "play it", "no, the other one" or "do the same in Notes" resolve against the task the
user is on instead of being read as a fresh, context-free command.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

MAX_TURNS = 12  # kept; older turns drop off
WINDOW_S = 30 * 60  # a turn older than this is no longer "the current task"
SHOWN_TURNS = 8  # given to each decision


@dataclass
class Turn:
    said: str
    app: str
    place: str  # the note, tab, window... that was open
    did: str  # the action taken ("ui_press", "search_here(query='butter song')", "chat")
    reply: str  # what the assistant answered
    values: list[str] = field(default_factory=list)  # free-text arguments it used ("butter song")
    at: float = field(default_factory=time.time)


class SessionMemory:
    def __init__(self) -> None:
        self.turns: list[Turn] = []

    def add(self, said: str, app: str, place: str, did: str, reply: str, values: list[str] | None = None) -> None:
        self.turns.append(Turn(said, app, place, did, reply or "", [v for v in (values or []) if v]))
        del self.turns[:-MAX_TURNS]

    def note_reply(self, text: str) -> None:
        """The chat LLM's answer arrives after the turn was recorded."""
        if self.turns and not self.turns[-1].reply and text:
            self.turns[-1].reply = text

    def clear(self) -> None:
        self.turns.clear()

    def _live(self, now: float | None = None) -> list[Turn]:
        now = time.time() if now is None else now
        return [t for t in self.turns if now - t.at <= WINDOW_S]

    def recent(self, n: int = SHOWN_TURNS, now: float | None = None) -> list[dict[str, str]]:
        """Oldest first, for a Jev state or an LLM prompt."""
        out = []
        for t in self._live(now)[-n:]:
            item = {"user": t.said, "app": t.app, "did": t.did, "assistant": t.reply[:200]}
            if t.place:
                item["on"] = t.place
            out.append(item)
        return out

    def values(self, n_turns: int = 4, now: float | None = None) -> list[str]:
        """Things the recent turns were about: the free-text values they used and short phrases
        from what the user said, newest first, so "play it" can pick "butter song"."""
        out: list[str] = []
        for t in reversed(self._live(now)[-n_turns:]):
            out.extend(t.values)
            words = [w.strip(".,!?\"'") for w in t.said.split()]
            for size in (2, 3, 4):
                out.extend(" ".join(words[i : i + size]) for i in range(len(words) - size + 1))
        seen: set[str] = set()
        unique = []
        for v in out:
            key = v.lower().strip()
            if len(key) >= 3 and key not in seen:
                seen.add(key)
                unique.append(v.strip())
        return unique

    def as_text(self, n: int = SHOWN_TURNS) -> str:
        lines = []
        for t in self.recent(n):
            where = f" ({t['app']}{', ' + t['on'] if t.get('on') else ''})"
            lines.append(f"- user{where}: {t['user']}\n  did: {t['did']}; said: {t['assistant'] or '-'}")
        return "\n".join(lines)
