"""The front app's on-screen controls, read through macOS accessibility, so Jev can *choose* one.

"Open liked songs" with Spotify in front becomes: snapshot every pressable element of Spotify's
window (and its menu bar) with the path of sections it sits in ("Your Library > Liked Songs"),
then one Jev Choice over those elements, then AXPress on the winner. Nothing is generated and
nothing is guessed: every option is an element that exists on screen right now, so a pick can't
point at a button that isn't there. Menu commands are included because they are stable in every
Mac app.

Reading a window takes ~0.3-1 s, so the agent starts the snapshot in parallel with routing.
Needs the Accessibility permission for the process running macbrow (the same one keystrokes need).
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("macbrow.ui")

MAX_NODES = 8000  # walk budget per window
MAX_DEPTH = 45
MAX_OPTIONS = 230  # Jev Choice allows 255; leave room for "none"
PRESSABLE_ROLES = {
    "AXButton",
    "AXLink",
    "AXMenuItem",
    "AXMenuButton",
    "AXPopUpButton",
    "AXRow",
    "AXCell",
    "AXTab",
    "AXRadioButton",
    "AXCheckBox",
    "AXDisclosureTriangle",
    "AXOutlineRow",
}
SELECTABLE_ROLES = {"AXRow", "AXOutlineRow", "AXTab", "AXRadioButton"}  # can be chosen by selecting
ROLE_NAMES = {
    "AXButton": "button",
    "AXLink": "link",
    "AXMenuItem": "menu command",
    "AXMenuButton": "menu button",
    "AXPopUpButton": "pop-up menu",
    "AXRow": "list item",
    "AXCell": "list item",
    "AXOutlineRow": "sidebar item",
    "AXTab": "tab",
    "AXRadioButton": "tab",
    "AXCheckBox": "checkbox",
    "AXDisclosureTriangle": "expander",
}
# Pressing one of these changes something that can't be taken back: always confirm by voice.
DANGEROUS = re.compile(
    r"\b(delete|remove|erase|trash|send|post|publish|share|pay|buy|purchase|order|checkout|subscribe|"
    r"unsubscribe|log ?out|sign ?out|quit|uninstall|reset|clear|block|report|discard|don'?t save|replace|"
    r"format|unfollow|leave|archive|empty)\b",
    re.IGNORECASE,
)


@dataclass
class Element:
    label: str
    role: str
    path: tuple[str, ...]  # labels of the sections it sits in, outermost first
    ref: Any = field(repr=False, compare=False, default=None)  # AXUIElement
    in_menu: bool = False
    actions: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return ROLE_NAMES.get(self.role, "control")

    @property
    def where(self) -> str:
        """The nearest sections it sits in ("Main > Your Library"), outermost wrappers dropped."""
        return " > ".join([p for p in self.path if p][-3:])

    def describe(self) -> str:
        if self.in_menu:
            return f"menu command '{self.label}' in the {self.path[-1]} menu"
        where = f" in {self.where}" if self.where else ""
        return f"{self.kind} '{self.label}'{where}"

    @property
    def dangerous(self) -> bool:
        return bool(DANGEROUS.search(self.label))


@dataclass
class Snapshot:
    app: str
    window: str
    elements: list[Element]
    elapsed_ms: float


# ------------------------------------------------------------------------------ reading
def _ax():
    import ApplicationServices as AS  # pyobjc; imported lazily so tests run without it

    return AS


def _get(el: Any, attr: str) -> Any:
    err, value = _ax().AXUIElementCopyAttributeValue(el, attr, None)
    return value if err == 0 else None


def _clean(text: Any) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).replace("\n", " ").strip()
    return re.sub(r"\s+", " ", s)[:90]


def _label(el: Any, role: str) -> str:
    for attr in ("AXTitle", "AXDescription", "AXHelp"):
        v = _clean(_get(el, attr))
        if v:
            return v
    if role in ("AXRow", "AXCell", "AXOutlineRow", "AXLink"):  # rows are labelled by their text
        texts: list[str] = []
        for kid in (_get(el, "AXChildren") or [])[:6]:
            for attr in ("AXValue", "AXTitle", "AXDescription"):
                v = _clean(_get(kid, attr))
                if v:
                    texts.append(v)
                    break
        return _clean(" ".join(texts))
    return ""


def _pid(app_name: str) -> int | None:
    from AppKit import NSWorkspace

    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.localizedName() == app_name:
            return int(a.processIdentifier())
    return None


def snapshot(app_name: str, include_menus: bool = True, max_nodes: int = MAX_NODES) -> Snapshot | None:
    """Pressable elements of ``app_name``'s focused window (plus its menu commands)."""
    AS = _ax()
    t0 = time.perf_counter()
    pid = _pid(app_name)
    if pid is None:
        return None
    app = AS.AXUIElementCreateApplication(pid)
    # Chromium/Electron apps build their tree only for an assistive client; harmless elsewhere.
    AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    win = _get(app, "AXFocusedWindow") or next(iter(_get(app, "AXWindows") or []), None)
    elements: list[Element] = []
    budget = [max_nodes]
    noise: set[str] = set()

    def walk(el: Any, path: tuple[str, ...], depth: int) -> None:
        if budget[0] <= 0 or depth > MAX_DEPTH:
            return
        budget[0] -= 1
        role = _get(el, "AXRole") or ""
        label = _label(el, role)
        err, actions = AS.AXUIElementCopyActionNames(el, None)
        actions = tuple(actions or ())
        # Cells without a press action are text holders: pressing them silently does nothing.
        if label and ("AXPress" in actions or role in SELECTABLE_ROLES):
            elements.append(Element(label=label, role=role, path=path, ref=el, actions=actions))
        keep = (
            label and role not in ("AXStaticText", "AXImage", "AXWebArea") and label.lower() not in noise and depth > 1
        )
        child_path = path + (label[:40],) if keep else path
        for kid in _get(el, "AXChildren") or []:
            walk(kid, child_path, depth + 1)

    window_title = _clean(_get(win, "AXTitle")) if win is not None else ""
    # Wrapper labels that every element shares (the window, the app, a web view named after the
    # page) say nothing about where an element is; keep them out of paths.
    noise.update({window_title.lower(), app_name.lower()})
    if win is not None:
        walk(win, (), 0)
    if include_menus:
        bar = _get(app, "AXMenuBar")
        for top in (_get(bar, "AXChildren") or [])[1:]:  # skip the Apple menu
            top_title = _clean(_get(top, "AXTitle"))
            for menu in _get(top, "AXChildren") or []:
                for item in _get(menu, "AXChildren") or []:
                    title = _clean(_get(item, "AXTitle"))
                    if title and _get(item, "AXEnabled") is not False:
                        elements.append(
                            Element(label=title, role="AXMenuItem", path=("menu", top_title), ref=item, in_menu=True)
                        )
    return Snapshot(app_name, window_title, elements, (time.perf_counter() - t0) * 1e3)


def fingerprint(app_name: str) -> frozenset[str]:
    """What the window shows right now (labels of its controls), to tell whether a press did anything."""
    snap = snapshot(app_name, include_menus=False)
    return window_fingerprint(snap) if snap else frozenset()


def window_fingerprint(snap: Snapshot) -> frozenset[str]:
    return frozenset([f"window:{snap.window}", *(f"{e.role}:{e.label}" for e in snap.elements if not e.in_menu)])


def _pressable_inside(el: Any, depth: int = 0) -> list[Any]:
    """Buttons/links inside a row: in web-based apps (Spotify) the row ignores AXPress, its button acts."""
    found: list[Any] = []
    if depth > 4:
        return found
    for kid in _get(el, "AXChildren") or []:
        role = _get(kid, "AXRole") or ""
        err, actions = _ax().AXUIElementCopyActionNames(kid, None)
        if role in ("AXButton", "AXLink") and "AXPress" in (actions or ()):
            found.append(kid)
        found.extend(_pressable_inside(kid, depth + 1))
    return found


def _click_center(ref: Any) -> bool:
    """A real mouse click at the element's centre; the pointer is put back afterwards."""
    import Quartz

    pos, size = _get(ref, "AXPosition"), _get(ref, "AXSize")
    if pos is None or size is None:
        return False
    ok_p, point = _ax().AXValueGetValue(pos, _ax().kAXValueCGPointType, None)
    ok_s, dims = _ax().AXValueGetValue(size, _ax().kAXValueCGSizeType, None)
    if not (ok_p and ok_s) or dims.width <= 0 or dims.height <= 0:
        return False
    target = (point.x + dims.width / 2, point.y + dims.height / 2)
    back = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, kind, target, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.05)
    Quartz.CGWarpMouseCursorPosition(back)
    return True


def press_verified(
    el: Element, app_name: str, before: frozenset[str], allow_click: bool, settle_s: float = 0.7
) -> str | None:
    """Press ``el`` and confirm the window changed; try the next way when it didn't. Returns how it
    worked ("inner button", "press", "select", "click", "menu") or None when nothing visibly happened."""
    AS = _ax()
    if el.in_menu:  # menu commands act reliably and often open another window; no page to compare
        return "menu" if AS.AXUIElementPerformAction(el.ref, "AXPress") == 0 else None

    def changed() -> bool:
        time.sleep(settle_s)
        return fingerprint(app_name) != before

    attempts: list[tuple[str, Any]] = []
    if el.role in ("AXRow", "AXCell", "AXOutlineRow"):
        attempts += [("inner button", ref) for ref in _pressable_inside(el.ref)[:2]]
    if "AXPress" in el.actions:
        attempts.append(("press", el.ref))
    if el.role in SELECTABLE_ROLES:
        attempts.append(("select", el.ref))
    if allow_click:
        attempts.append(("click", el.ref))
    for how, ref in attempts:
        if how == "click":
            done = _click_center(ref)
        elif how == "select":
            done = AS.AXUIElementSetAttributeValue(ref, "AXSelected", True) == 0
        else:
            done = AS.AXUIElementPerformAction(ref, "AXPress") == 0
        if done and changed():
            return how
    return None


# ------------------------------------------------------------------------------ choosing
_WORD = re.compile(r"[a-z0-9]+")
_ACTS = {"AXButton": 3, "AXLink": 3, "AXMenuItem": 3, "AXTab": 2, "AXRadioButton": 2, "AXRow": 1, "AXOutlineRow": 1}


def _words(s: str) -> set[str]:
    return set(_WORD.findall(s.lower()))


def candidates(snap: Snapshot, utterance: str, limit: int = MAX_OPTIONS) -> list[Element]:
    """De-duplicated elements, the ones sharing words with the utterance first, capped for a Choice."""
    seen: dict[tuple[str, bool], Element] = {}
    for el in snap.elements:
        key = (el.label.lower(), el.in_menu)
        # a row and the button inside it usually carry the same label: keep the button, it acts
        if key not in seen or _ACTS.get(el.role, 0) > _ACTS.get(seen[key].role, 0):
            seen[key] = el
    unique = list(seen.values())
    if len(unique) <= limit:
        return unique
    said = _words(utterance)
    order = {id(e): i for i, e in enumerate(unique)}
    unique.sort(key=lambda e: (-len(said & _words(e.label + " " + e.where)), e.in_menu, order[id(e)]))
    return unique[:limit]


def base_key(key: str) -> str:
    """Option key without the " (2)" suffix that told identical labels apart."""
    return re.sub(r" \(\d+\)$", "", key).lower()


def _same_target(a: str, b: str) -> bool:
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def pick_confidence(choice: str, probabilities: dict[str, float]) -> float:
    """Probability of the chosen target, counting every option that names the same thing: "Arijit
    Singh" in the library and "Arijit Singh Artist" on the home page are one intent, not two."""
    base = base_key(choice)
    return sum(p for k, p in probabilities.items() if k != "__none__" and _same_target(base_key(k), base))


def choice_criteria(elements: list[Element]) -> tuple[dict[str, str], dict[str, Element]]:
    """Choice options keyed by the element's own label (unique-ified), described with its place."""
    crit: dict[str, str] = {}
    by_key: dict[str, Element] = {}
    for el in elements:
        key = el.label[:70]
        n = 2
        while key in by_key:
            key = f"{el.label[:64]} ({n})"
            n += 1
        by_key[key] = el
        crit[key] = el.describe()
    return crit, by_key


def press_args(el: Element) -> dict[str, str]:
    """What a staged (confirm-first) press remembers; the element itself may be rebuilt by then."""
    return {"label": el.label, "where": el.where, "menu": "1" if el.in_menu else ""}


def find(snap: Snapshot, args: dict[str, str]) -> Element | None:
    """The element a staged press meant, in a fresh snapshot."""
    menu = bool(args.get("menu"))
    same = [e for e in snap.elements if e.label == args["label"] and e.in_menu == menu]
    return next((e for e in same if e.where == args.get("where", "")), same[0] if same else None)
