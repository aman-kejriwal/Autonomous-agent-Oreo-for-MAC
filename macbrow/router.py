"""Jev routing: utterance + live Mac state -> tool, arguments, confidence.

One request selects the tool (Choice over the tools currently available) and,
speculatively in the same request, every enum argument for every candidate
tool. Only the selected tool's answers are consumed. Free-text arguments need
the selected tool first, so they are filled in a second, tiny request that
asks Jev to *select* the right span of the utterance (no generation).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul

from .applescript import MacContext
from .registry import MAX_CHOICE_OPTIONS, Tool, ToolRegistry

log = logging.getLogger("macbrow.router")

CHAT = "chat"
NEW_ACTION = "new_action"
STOP = "stop_listening"
LEAVE = "leave_current_app"  # offered when re-routing among in-place tools only

MIN_TOOL_CONFIDENCE = 0.45  # below this we treat the pick as uncertain
NEW_ACTION_MIN_CONFIDENCE = 0.6  # hesitant new_action -> ask about the best existing tool instead
UNCERTAIN_TOOL_MIN_PROB = 0.2
MAX_TEXT_CANDIDATES = 200  # Jev Choice allows 255 options


@dataclass
class Route:
    kind: str  # "tool" | "chat" | "new_action" | "uncertain" | "stop" | "leave"
    tool: Tool | None = None
    args: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    is_confirmation: float = 0.0
    is_denial: float = 0.0
    browser_followup: float = 0.0  # p(utterance continues/corrects the recent browser task)
    web_goal_complete: float = 1.0  # p(request has the concrete details a website form needs)
    web_goal_forbidden: float = 0.0  # p(request requires buying/paying/signing in/changing an account)
    web_goal_missing: str = "nothing"  # what a clarifying question should ask for
    # p(the user explicitly named another app/page/tab/item to go to or act on); 1.0 when not asked
    explicit_elsewhere: float = 1.0
    latency_ms: float = 0.0
    weakest_arg: tuple[str, float] | None = None

    @property
    def summary(self) -> str:
        if self.tool:
            return f"{self.tool.name}({', '.join(f'{k}={v!r}' for k, v in self.args.items())})"
        return self.kind


def _state(utterance: str, ctx: MacContext, recent_browser: dict[str, str] | None = None) -> dict[str, Any]:
    import datetime as _dt

    st: dict[str, Any] = {
        "utterance": utterance,
        "today": _dt.date.today().strftime("%A %d %B %Y"),
        "frontmost_app": ctx.active_app,
        "running_apps": ctx.running_apps,
    }
    if ctx.focus:
        st["open_in_frontmost_app"] = ctx.focus.describe()
    if ctx.recent:
        st["recently_worked_on"] = [f.describe() for f in ctx.recent]
    if recent_browser:
        st["recent_browser_task"] = recent_browser
    return st


def _arg_qid(tool: Tool, arg_name: str) -> str:
    return f"arg::{tool.name}::{arg_name}"


class JevRouter:
    def __init__(self, registry: ToolRegistry, client: AsyncTypeSafeClient | None = None):
        self.registry = registry
        self.client = client or AsyncTypeSafeClient()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ routing
    async def route(
        self,
        utterance: str,
        ctx: MacContext,
        *,
        awaiting_confirmation: bool = False,
        recent_browser: dict[str, str] | None = None,
        exclude: frozenset[str] | set[str] = frozenset(),
        offer_leave: bool = False,
    ) -> Route:
        """``exclude`` drops tools from the Choice; ``offer_leave`` adds an option meaning "nothing
        in-place fits, this needs another app or page" (route.kind "leave")."""
        t0 = time.perf_counter()
        tools = [t for t in self.registry.available(ctx) if t.name not in exclude]
        front = ctx.active_app.lower()
        tools.sort(key=lambda t: (t.scope or "").lower() != front)  # the app in front first; stable otherwise
        criteria: dict[str, Any] = {}
        for t in tools[: MAX_CHOICE_OPTIONS - 4]:
            desc = t.choice_description()
            if t.scope and t.scope.lower() == front:
                desc["app_is_in_front"] = True
            if t.moves or t.runner == "browser":
                desc["takes_user_elsewhere"] = True  # opens/switches to another app, page, tab or item
            criteria[t.name] = desc
        criteria[CHAT] = {
            "what": "The user is chatting, asking a general question, or thinking aloud; "
            "they are not asking the assistant to do something on the Mac.",
            "examples": ["how are you", "what's the capital of France", "thanks"],
        }
        criteria[STOP] = {
            "what": "The user wants the voice assistant itself to stop, quit, go to sleep, or stop listening.",
            "examples": [
                "stop listening",
                "quit macbrow",
                "quit your application wherever you are running",
                "shut yourself down",
                "stop running",
                "goodbye assistant, you can stop now",
            ],
            "not_for": "Quitting a named application such as Slack or Chrome, or cancelling a pending action.",
        }
        criteria[NEW_ACTION] = {
            "what": "The user wants an action performed on the Mac (in an app or the system) "
            "that none of the listed tools can do.",
            "not_for": "Requests a listed tool already covers, even if worded differently.",
        }
        if offer_leave:
            criteria[LEAVE] = {
                "what": "None of the listed tools can do this inside the app or page that is open now; it "
                "needs another app, website or page (e.g. playing a video while on a shopping site).",
            }

        questions: dict[str, Any] = {
            "intent": Choice(
                instructions=(
                    "The user spoke `utterance` to a voice assistant that controls this Mac. "
                    "`frontmost_app` is the app currently in focus and `running_apps` are open. "
                    "`open_in_frontmost_app` is what the user is working on right now (the note, tab, folder, "
                    "document or message open there); `recently_worked_on` is what was open in other apps "
                    "they used a moment ago. The user works with apps continuously and rarely repeats the "
                    "app's name: a request that names no app ('write buy milk', 'add a line', 'read it back', "
                    "'close this') acts on `open_in_frontmost_app`, so pick the tool that works on it; one that "
                    "names something in `recently_worked_on` ('in my note', 'on that page') acts on that. "
                    "Stay in the current app: a command that names no other app, website or item ('pause', "
                    "'next', 'search for X', 'type X', 'write X', 'go back', 'scroll down') is for `frontmost_app` "
                    "and the site or item open there, so pick a tool that works there (search_here, type_here, "
                    "app_action, or a tool with app_is_in_front), never a tool of a background app (pausing "
                    "Spotify while a YouTube video is in front) and never one that opens a new tab, a new site "
                    "or another app when an in-place tool can really do it. If nothing in the current app or "
                    "page can do what was asked (playing a video while on a shopping site, playing music while "
                    "in Notes), pick the tool that can; the assistant asks before leaving. Never swap in a "
                    "weaker in-place action that drops part of the request. "
                    "Which tool best fulfils the request? Prefer a tool scoped to `frontmost_app` "
                    "when the request is ambiguous between apps. A website, URL, or web search "
                    "goes to a browser tool, not to opening an application. Closing, switching or reloading "
                    "tabs and windows is a browser-control tool, never a web task."
                ),
                criteria=criteria,
            )
        }
        # Speculative enum-argument questions for every candidate tool.
        for t in tools:
            for spec in t.args:
                if spec.kind != "enum":
                    continue
                crit = spec.resolve_criteria(ctx)
                if not crit:
                    continue
                questions[_arg_qid(t, spec.name)] = Choice(
                    instructions=[
                        f"Assume the user wants to run the tool '{t.name}' ({t.description}).",
                        spec.instructions,
                        "Base the answer on `utterance`; use `frontmost_app` and `open_in_frontmost_app` "
                        "when the user says 'this app' or leaves the target implicit.",
                    ],
                    criteria=crit,
                )
        if any(t.runner == "browser" for t in tools):
            questions.update(_web_goal_questions())
        questions["explicit_elsewhere"] = Noul(
            instructions=(
                "The user is in `frontmost_app` (on `open_in_frontmost_app`). Does `utterance` EXPLICITLY point "
                "somewhere other than that app, page or tab: either to go there (open, go to, switch to, create "
                "or start a different app, website, page, file, folder, note, document, tab or window; 'new "
                "tab', 'next tab', 'new window') or to act there by naming it ('pause Spotify', 'next song on "
                "Spotify', 'write milk in my note', 'what's playing in Music', 'in Slack say hi')? A command that "
                "only says what to do ('pause', 'next', 'search for X', 'play X', 'type X', 'write X', 'add X', "
                "'scroll down', 'go back') without naming a different app, place or item, or asking for a new "
                "one, is NOT explicit: it is meant for what is in front."
            ),
            criteria={
                "true": "'open Spotify', 'go to github', 'play espresso on YouTube', 'open my shopping note', "
                "'create a note called groceries', 'make a new note with buy milk', 'switch to Slack', "
                "'new tab', 'next tab', 'search Amazon for earbuds' while on YouTube, 'pause Spotify', "
                "'write buy milk in my note' while in Chrome",
                "false": "'pause', 'next', 'search for espresso', 'play espresso', 'type hello', 'write buy "
                "milk', 'find the invoice', 'add milk', 'go back', 'what's this page'",
            },
        )
        if recent_browser:
            questions["browser_followup"] = Noul(
                instructions=(
                    "`recent_browser_task` describes a web task the assistant just performed in Chrome, or a "
                    "clarifying question it asked about one (see `question`). Is `utterance` a follow-up, "
                    "correction, answer, or next step for that same task, rather than a new unrelated request?"
                ),
                criteria={
                    "true": "Continues, corrects, or refines the recent task: 'yes, now set the return date', "
                    "'no, the other one', 'the return should be November 4th', 'add it to the cart'",
                    "false": "A new request unrelated to that page, or a general Mac command",
                },
            )
        if awaiting_confirmation:
            questions["confirm"] = Noul(
                instructions="Does `utterance` confirm or approve going ahead with a pending action?",
                criteria={"true": "yes, go ahead, do it, confirmed, sure", "false": "anything else"},
            )
            questions["deny"] = Noul(
                instructions="Does `utterance` cancel, decline, or say no to a pending action?",
                criteria={"true": "no, cancel, stop, never mind, don't", "false": "anything else"},
            )

        resp = await self.client.system_one(state=_state(utterance, ctx, recent_browser), questions=questions)
        intent = resp.choices["intent"]
        route = Route(
            kind="tool",
            confidence=float(intent.confidence),
            probabilities={k: round(float(v), 3) for k, v in intent.probabilities.items()},
        )
        if "web_goal_complete" in resp.nouls:
            route.web_goal_complete = float(resp.nouls["web_goal_complete"].noul)
            route.web_goal_forbidden = float(resp.nouls["web_goal_forbidden"].noul)
            route.web_goal_missing = resp.choices["web_goal_missing"].choice
        if recent_browser:
            route.browser_followup = float(resp.nouls["browser_followup"].noul)
        route.explicit_elsewhere = float(resp.nouls["explicit_elsewhere"].noul)
        if awaiting_confirmation:
            route.is_confirmation = float(resp.nouls["confirm"].noul)
            route.is_denial = float(resp.nouls["deny"].noul)

        picked = intent.choice
        if picked == CHAT:
            route.kind = "chat"
        elif picked == STOP:
            route.kind = "stop"
        elif picked == LEAVE:
            route.kind = "leave"
        elif picked == NEW_ACTION:
            route.kind = "new_action"
            # A hesitant new_action with a plausible existing tool is a question, not a codegen trigger.
            if intent.confidence < NEW_ACTION_MIN_CONFIDENCE:
                best = (
                    max(
                        ((k, v) for k, v in intent.probabilities.items() if k not in (CHAT, NEW_ACTION, STOP)),
                        key=lambda kv: kv[1],
                        default=None,
                    )
                    if not offer_leave
                    else None
                )  # re-routing in place: a weak pick is not a question
                if best and best[1] >= UNCERTAIN_TOOL_MIN_PROB and (tool := self.registry.get(best[0])):
                    picked = tool.name
                    route.kind = "uncertain"
                    route.tool = tool
        if picked not in (CHAT, NEW_ACTION, STOP, LEAVE):
            tool = self.registry.get(picked)
            if tool is None:
                route.kind = "new_action"
                route.tool = None
            else:
                route.tool = tool
                if route.kind != "uncertain" and intent.confidence < MIN_TOOL_CONFIDENCE:
                    route.kind = "uncertain"
                # Consume only the selected tool's speculative answers.
                weakest: tuple[str, float] | None = None
                for spec in tool.args:
                    if spec.kind != "enum":
                        continue
                    ans = resp.choices.get(_arg_qid(tool, spec.name))
                    if ans is None:
                        continue
                    route.args[spec.name] = ans.choice
                    if weakest is None or ans.confidence < weakest[1]:
                        weakest = (spec.name, float(ans.confidence))
                route.weakest_arg = weakest
                # Second request only when a free-text slot exists.
                text_specs = [s for s in tool.args if s.kind == "text"]
                if text_specs:
                    await self._fill_text_args(utterance, ctx, tool, text_specs, route)

        route.latency_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "route %s conf=%.2f %.0fms probs=%s front=%s focus=%s",
            route.summary,
            route.confidence,
            route.latency_ms,
            _top(route.probabilities),
            ctx.active_app,
            ctx.focus.describe() if ctx.focus else None,
        )
        return route

    async def web_goal_check(self, text: str, ctx: MacContext) -> tuple[float, str]:
        """Completeness of a (merged) web-task goal: (p_complete, most_missing)."""
        resp = await self.client.system_one(state=_state(text, ctx), questions=_web_goal_questions())
        return float(resp.nouls["web_goal_complete"].noul), resp.choices["web_goal_missing"].choice

    # ---------------------------------------------------------- text arguments
    async def _fill_text_args(
        self, utterance: str, ctx: MacContext, tool: Tool, specs: list[Any], route: Route
    ) -> None:
        """Select-not-generate: Jev picks which span of the utterance is the argument."""
        candidates = _span_candidates(utterance, MAX_TEXT_CANDIDATES)
        if not candidates:
            for spec in specs:
                route.args[spec.name] = spec.default or utterance
            return
        crit = {c: None for c in candidates}
        crit["__none__"] = "No part of the utterance is this argument"
        questions = {
            spec.name: Choice(
                instructions=[
                    f"The user is running the tool '{tool.name}' ({tool.description}).",
                    spec.instructions,
                    "Choose the option that is exactly and only that value, copied from `utterance`, "
                    "without leading command words like 'open', 'search for', 'say', or 'that says'.",
                ],
                criteria=crit,
            )
            for spec in specs
        }
        resp = await self.client.system_one(state=_state(utterance, ctx), questions=questions)
        for spec in specs:
            ans = resp.choices[spec.name]
            if ans.choice == "__none__" and spec.optional:
                route.args[spec.name] = spec.default or ""
                continue  # leaving an optional slot empty is never a reason to ask back
            value = ans.choice if ans.choice != "__none__" else (spec.default or utterance)
            route.args[spec.name] = _clean_value(value)
            if route.weakest_arg is None or ans.confidence < route.weakest_arg[1]:
                route.weakest_arg = (spec.name, float(ans.confidence))


def _web_goal_questions() -> dict[str, Any]:
    return {
        "web_goal_complete": Noul(
            instructions=(
                "Suppose `utterance` is a task to carry out on a website (shopping, flights, email, calendar, forms). "
                "Could an assistant fill every field the site REQUIRES using only the words in `utterance` plus "
                "`today`, without inventing a value? A generic product ('a vacuum cleaner') is enough for a search "
                "box; a short message is enough content; emails need no dates; a calendar event needs only a "
                "title and a time, attendees are optional. A relative date that resolves "
                "unambiguously from `today` ('Monday', 'tomorrow', 'next Friday at 2pm') IS a usable date. "
                "A travel search needs both cities/airports (a country is not enough) and dates that resolve to "
                "specific days: 'in November' or 'for five days' do not."
            ),
            criteria={
                "true": "Every required field has a usable value in the utterance (given today's date)",
                "false": "A required field has no usable value: vague/missing dates, a country instead of a city, "
                "no recipient, no idea what to search for",
            },
        ),
        "web_goal_forbidden": Noul(
            instructions=(
                "Would carrying out `utterance` on a website require the assistant ITSELF to place an order or pay, "
                "sign in or enter credentials, or change account settings? Browsing, searching, comparing prices, "
                "reading, filtering, opening product pages and adding to a cart do NOT count, even if the user "
                "mentions wanting to buy something eventually."
            ),
            criteria={
                "true": "The task cannot be completed without a purchase/payment, a sign-in, or an account change",
                "false": "It is research, navigation, search, reading, or cart-building; no money or credentials involved",
            },
        ),
        "web_goal_missing": Choice(
            instructions="If `utterance` were a website task, which single required detail is most clearly missing or vague?",
            criteria={
                "exact_dates": "Dates are missing or don't resolve to specific days ('in November', 'for five days'); "
                "'Monday' or 'tomorrow' DO resolve and are not missing",
                "destination": "Where to is missing, or too broad (a country or region instead of a city/airport)",
                "origin": "Where from",
                "product": "What item to search for or which one to pick",
                "recipient": "Who the message/email is for",
                "content": "What the message/note/form should say",
                "nothing": "Nothing important is missing",
            },
        ),
    }


_WORD_RE = re.compile(r"\S+")


def _span_candidates(utterance: str, max_candidates: int = 200) -> list[str]:
    """Word-bounded substrings of the utterance, longest first, so Jev can pick exactly the
    argument ("send Constance a message saying hi" -> "Constance" and "hi") instead of a
    whole trailing clause. Suffixes are always included; inner spans fill the remaining budget.
    """
    text = utterance.strip().rstrip(".!?")
    words = [(m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    n = len(words)
    spans: list[str] = []
    seen: set[str] = set()

    def add(i: int, j: int) -> None:
        span = text[words[i][0] : words[j - 1][1]].strip().rstrip(".,;:!?").strip(",;:")
        key = span.lower()
        if len(span) >= 2 and key not in seen:
            seen.add(key)
            spans.append(span)

    for i in range(n):  # suffixes first: the common case for spoken commands
        add(i, n)
    for length in range(n - 1, 0, -1):  # then every shorter inner span
        for i in range(n - length):
            if len(spans) >= max_candidates:
                return spans
            add(i, i + length)
    return spans


def _clean_value(v: str) -> str:
    v = v.strip().strip("\"'").rstrip(".,;:!?")
    # Spoken URLs: "github dot com" -> "github.com"
    v = re.sub(r"\s+dot\s+", ".", v, flags=re.IGNORECASE)
    v = re.sub(r"\s+slash\s+", "/", v, flags=re.IGNORECASE)
    return v


def _top(probs: dict[str, float], n: int = 3) -> dict[str, float]:
    return dict(sorted(probs.items(), key=lambda kv: -kv[1])[:n])
