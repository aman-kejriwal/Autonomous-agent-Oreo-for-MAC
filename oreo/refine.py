"""Fallback transcript repair: when Jev can't make sense of a command, fix what speech-to-text
likely misheard and try once more.

Only used after the normal path struggled (an unsure route, an argument Jev couldn't pin down,
nothing on screen matched, or no tool fits). The exact words always go first; this never runs on
a turn that already worked. The LLM may only fix mishearings ("in box" -> "inbox", "stared" ->
"starred"), using the app's on-screen labels and app names as hints; it may not change what was
asked for. A repair that rewrites too much is thrown away, and an unchanged one is not retried.

OREO_REFINE=0 turns it off.
"""

from __future__ import annotations

import difflib
import os
import re

from . import answer

ENABLED = os.environ.get("OREO_REFINE", "1") != "0"
SOUND_ALIKE = 0.6  # letter similarity for a swapped word to count as a misheard one fixed
FILLER = {"uh", "um", "umm", "er", "erm", "ah", "hmm", "mm"}
POLITE = {"hey", "ok", "okay", "so", "please", "can", "could", "would", "will", "you"}  # droppable up front
MAX_LABELS = 150

SYSTEM = """You repair speech-to-text mistakes in a command someone spoke to a Mac voice assistant.
The assistant could not understand the command as transcribed. Words may be misheard (sound-alikes),
split or merged ("in box" for "inbox", "you tube" for "YouTube"), or mixed with filler ("uh", "um")
and false starts.

Rules:
- Replace only the words that were likely misheard. Prefer words that appear in the on-screen
  labels or app names. Delete filler (uh, um) and false starts.
- Keep every other word exactly as spoken: the same wording, order, questions stay questions,
  number words stay words. Never add a request or change the action, its target, or any name,
  number or text to be typed, searched or sent.
- If nothing looks misheard, return the command exactly as given.

Examples:
"can you see in box on the screen" -> "can you see inbox on the screen"
"uh open the stared mails" -> "open the starred mails"
"open whats up" -> "open WhatsApp"
"type see you at nine" -> "type see you at nine"

Reply with the repaired command only: one line, no quotes, no explanation."""


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def accept(original: str, repaired: str) -> str | None:
    """The repaired command when every change is a repair; None when nothing changed that matters
    (case, punctuation) or anything was reworded, added or dropped instead of fixed.

    A repair may only swap words for sound-alikes ("in box" -> "inbox", "whats up" -> "WhatsApp",
    "male" -> "mail") and delete filler or a repeated false start."""
    repaired = repaired.strip().strip("\"'").splitlines()[0].strip() if repaired.strip() else ""
    said, new = _norm(original).split(), _norm(repaired).split()
    if not new or said == new:
        return None
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, said, new, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        old_words, new_words = said[i1:i2], new[j1:j2]
        if op == "delete" and all(w in FILLER or w in new or (i1 == 0 and w in POLITE) for w in old_words):
            continue  # "uh", "open the" said twice, or a leading "can you"
        if op == "replace":
            dropped = [w for w in old_words if w not in FILLER]
            if not dropped:
                continue
            if difflib.SequenceMatcher(None, "".join(dropped), "".join(new_words)).ratio() >= SOUND_ALIKE:
                continue
        return None  # reworded, added or dropped: a rewrite, not a repair
    return repaired


def hints(labels: list[str], apps: list[str]) -> str:
    """On-screen labels and app names, short and de-duplicated, for the prompt."""
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        label = " ".join(label.split())
        if 1 < len(label) <= 40 and label.lower() not in seen:
            seen.add(label.lower())
            out.append(label)
        if len(out) >= MAX_LABELS:
            break
    return f"Apps: {', '.join(apps[:40]) or '-'}\nOn-screen labels: {' | '.join(out) or '-'}"


async def repair(utterance: str, app: str, labels: list[str], apps: list[str], conversation: str = "") -> str | None:
    """A repaired command, or None when there is nothing to repair (see ``accept``)."""
    user = (
        f"App in front: {app}\n{hints(labels, apps)}\n\nSession so far:\n{conversation or '-'}\n\n"
        f"Command as transcribed: {utterance}"
    )
    return accept(utterance, await answer.complete(SYSTEM, user, max_tokens=80))
